from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from evomesh.cognition import strip_reasoning
from evomesh.git import GitError, GitIdentity, GitRepository, PublishPolicy
from evomesh.models import ModelProvider
from evomesh.storage import SQLiteRepository

logger = logging.getLogger(__name__)

PIPELINE_STATE_KEY = "evolution.pipeline"

MUTATION_INSTRUCTION = (
    "Propose ONE small, safe file change to EvoMesh that advances the objective.\n"
    "Return only a JSON object, no prose and no code fences, with exactly these keys:\n"
    '{"relative_path": "src/evomesh/<file>.py", "content": "<the complete new file>", '
    '"rationale": "<one sentence>"}\n'
    "The path must be relative and must stay inside the project.\n"
    "Escape every newline in content as \\n and every quote as \\\", so the object "
    "stays on one line and parses as JSON."
)
MUTATION_SYSTEM = (
    "You are Environment Evolver. Never target absolute paths "
    "or parent directories. Output JSON only."
)

REPAIR_INSTRUCTION = (
    "The candidate generation failed validation. Repair it with ONE file change.\n"
    "Return only a JSON object, no prose and no code fences, with exactly these keys:\n"
    '{"relative_path": "src/evomesh/<file>.py", "content": "<the complete repaired file>", '
    '"rationale": "<one sentence>"}\n'
    "Return the whole file, not a patch, and change only what the failure demands.\n"
    "The path must be relative and must stay inside the project.\n"
    "Escape every newline in content as \\n and every quote as \\\", so the object "
    "stays on one line and parses as JSON."
)
REPAIR_SYSTEM = (
    "You are Environment Evolver repairing your own candidate. Fix the reported "
    "failure and nothing else. Never target absolute paths or parent directories. "
    "Output JSON only."
)


class GenerationStatus(StrEnum):
    ACTIVE = "active"
    CANDIDATE = "candidate"
    LAST_KNOWN_GOOD = "last-known-good"
    FAILED = "failed"


class Generation(BaseModel):
    number: int
    status: GenerationStatus
    path: Path
    parent: int | None = None
    git_commit: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Proof in the output that the machine, not the mutation, broke the run. A
# candidate is a copy of a tree that already validated, so when the toolchain
# cannot open a directory or reach the network, no rewrite of one source file
# will help -- and asking a model to "fix" it wastes the attempt budget on a
# failure the candidate never caused.
ENVIRONMENT_MARKERS = (
    "PermissionError",
    "[WinError 5]",
    "Access is denied",
    "No space left on device",
    "OSError: [Errno 28]",
    "Connection refused",
    "Temporary failure in name resolution",
)


class ValidationResult(BaseModel):
    passed: bool
    commands: list[dict[str, object]]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def environment_blocker(self) -> str | None:
        """The marker proving the host broke this run, or None if it did not."""
        failure = self.failure()
        if failure is None:
            return None
        output = str(failure.get("output", ""))
        return next((marker for marker in ENVIRONMENT_MARKERS if marker in output), None)

    def failure(self) -> dict[str, object] | None:
        """The command that broke the candidate, or None when nothing did."""
        for command in self.commands:
            if command.get("exit_code") != 0:
                return command
        return None

    def digest(self) -> str:
        """Fingerprint the failure, so a repair that changed nothing is visible.

        Without this the pipeline cannot tell "the model rewrote the file and it
        still fails the same way" from "the model made progress", and it would
        spend every remaining attempt on a repair that provably does nothing.
        """
        failure = self.failure()
        if failure is None:
            return ""
        raw = f"{failure.get('command')}\n{failure.get('output')}"
        return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


class FileMutation(BaseModel):
    relative_path: Path
    content: str
    rationale: str = ""

    def target(self, candidate_root: Path) -> Path:
        if self.relative_path.is_absolute():
            raise ValueError("Mutation path must be relative")
        target = (candidate_root / self.relative_path).resolve(strict=False)
        root = candidate_root.resolve(strict=False)
        if target != root and root not in target.parents:
            raise ValueError("Mutation path escapes the candidate workspace")
        return target


def parse_mutation(raw: str) -> FileMutation:
    """Pull the mutation object out of whatever the model actually said."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"no JSON object was found in the answer: {excerpt(raw)}")
    try:
        return FileMutation.model_validate_json(raw[start : end + 1])
    except ValidationError as exc:
        problem = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in problem.get("loc", ())) or "the object"
        raise ValueError(
            f"the JSON object was unusable at {location}: "
            f"{problem.get('msg', 'invalid JSON')}"
        ) from exc


def excerpt(text: str, limit: int = 200) -> str:
    flattened = " ".join(text.split())
    return flattened[:limit] + ("..." if len(flattened) > limit else "")


def clip(text: str, limit: int, *, keep_end: bool = True) -> str:
    """Truncate without flattening, unlike ``excerpt``.

    A repair prompt carries tool output and source code, and both are unreadable
    once their line structure is collapsed. Tool output fails at the bottom, a
    file has to start at the top, so which end survives is the caller's choice.
    """
    if len(text) <= limit:
        return text
    return "...\n" + text[-limit:] if keep_end else text[:limit] + "\n..."


@dataclass
class GenerationSupervisor:
    root: Path

    @property
    def metadata_path(self) -> Path:
        return self.root / "supervisor.json"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.metadata_path.exists():
            self._write({"active": 1, "last_known_good": 1, "candidates": {}})

    def metadata(self) -> dict[str, Any]:
        self.initialize()
        return json.loads(self.metadata_path.read_text(encoding="utf-8"))

    def candidates(self) -> list[Generation]:
        raw = dict(self.metadata().get("candidates", {}))
        items = [Generation.model_validate(value) for value in raw.values()]
        return sorted(items, key=lambda item: item.number)

    def candidate(self, number: int) -> Generation:
        raw = dict(self.metadata().get("candidates", {})).get(str(number))
        if raw is None:
            raise KeyError(f"Generation {number} is not a known candidate")
        return Generation.model_validate(raw)

    def record_candidate(self, generation: Generation) -> None:
        metadata = self.metadata()
        candidates = dict(metadata.get("candidates", {}))
        candidates[str(generation.number)] = generation.model_dump(mode="json")
        metadata["candidates"] = candidates
        self._write(metadata)

    def promote(self, number: int) -> None:
        metadata = self.metadata()
        candidates = dict(metadata.get("candidates", {}))
        candidate = candidates.get(str(number))
        if not candidate or candidate.get("status") == GenerationStatus.FAILED:
            raise ValueError(f"Generation {number} is not promotable")
        metadata["last_known_good"] = metadata["active"]
        metadata["active"] = number
        self._write(metadata)

    def discard(self, number: int) -> None:
        metadata = self.metadata()
        candidates = dict(metadata.get("candidates", {}))
        if candidates.pop(str(number), None) is None:
            raise ValueError(f"Generation {number} is not a known candidate")
        metadata["candidates"] = candidates
        self._write(metadata)

    def rollback(self) -> None:
        metadata = self.metadata()
        metadata["active"] = metadata["last_known_good"]
        self._write(metadata)

    def record_commits(self, *, active: str, last_known_good: str) -> None:
        """Remember what the tree held before and after a generation landed.

        The running process still executes the code it started with, so this
        also raises the flag that says a restart is owed. Nothing here restarts
        anything: the rollback path has to outlive a process that may not come
        back up, which means it cannot live inside that process.
        """
        metadata = self.metadata()
        metadata["last_known_good_commit"] = last_known_good
        metadata["active_commit"] = active
        metadata["restart_required"] = True
        self._write(metadata)

    def clear_restart_flag(self) -> None:
        metadata = self.metadata()
        metadata["restart_required"] = False
        self._write(metadata)

    def record_publish(self, *, commit: str, published: bool, detail: str) -> None:
        """Remember whether the landed commit reached the remote, and why not.

        A push that failed leaves a tree that is a commit ahead of the remote,
        which is invisible until someone runs git themselves. Writing the reason
        next to the commit means /evolution status can say it out loud.
        """
        metadata = self.metadata()
        metadata["published_commit"] = commit if published else metadata.get("published_commit")
        metadata["publish_ok"] = published
        metadata["publish_detail"] = detail
        self._write(metadata)

    def _write(self, data: dict[str, Any]) -> None:
        temporary = self.metadata_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(self.metadata_path)


PYTEST_TEMP_DIR = ".pytest-tmp"

IGNORED_NAMES = (
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    PYTEST_TEMP_DIR,
    ".ruff_cache",
    ".runtime",
    "dist",
)


class CandidateWorkspace:
    """Copies the code into an isolated generation, and nothing else.

    Runtime state is deliberately excluded. Copying the live SQLite database and
    the agents' memory into every candidate leaks state into a tree that may
    later be promoted, bloats each generation, and races the running mesh for
    the database file.
    """

    def __init__(
        self,
        repository_root: Path,
        generations_root: Path,
        exclude: Iterable[Path] = (),
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.supervisor = GenerationSupervisor(generations_root.resolve())
        self.exclude = {Path(item).resolve() for item in exclude}
        self.exclude.add(self.supervisor.root)

    def _ignore(self, directory: str, names: list[str]) -> set[str]:
        ignored = shutil.ignore_patterns(*IGNORED_NAMES)(directory, names)
        here = Path(directory).resolve()
        for name in names:
            target = (here / name).resolve()
            if target in self.exclude or any(root in target.parents for root in self.exclude):
                ignored.add(name)
        return set(ignored)

    async def create(self, objective: str) -> Generation:
        metadata = self.supervisor.metadata()
        existing = [int(item) for item in dict(metadata.get("candidates", {}))]
        number = max([int(metadata["active"]), *existing], default=1) + 1
        # A discarded candidate keeps its directory so a human can still look at
        # it, and its metadata entry is gone. Skip past anything already on disk
        # rather than colliding with the leftovers of a previous pass.
        destination = self.supervisor.root / f"{number:06d}-candidate"
        while destination.exists():
            number += 1
            destination = self.supervisor.root / f"{number:06d}-candidate"
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.repository_root),
            "worktree",
            "add",
            "-b",
            f"evomesh/candidate-{number:06d}",
            str(destination),
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await process.communicate()
        if process.returncode != 0:
            await asyncio.to_thread(
                shutil.copytree, self.repository_root, destination, ignore=self._ignore
            )
        generation = Generation(
            number=number,
            status=GenerationStatus.CANDIDATE,
            path=destination,
            parent=int(metadata["active"]),
        )
        self.supervisor.record_candidate(generation)
        (destination / "MUTATION_OBJECTIVE.md").write_text(objective + "\n", encoding="utf-8")
        return generation


def uv_executable(start: Path) -> str:
    """Find uv the way the Windows launcher does: PATH first, then `.tools`.

    The launcher runs EvoMesh through a uv that is often not on PATH at all, so
    a validator that only knows the bare name fails every candidate with a
    FileNotFoundError that reads like a broken mutation. A candidate lives a few
    directories below the checkout, so walk up instead of guessing the depth.
    """
    if found := shutil.which("uv"):
        return found
    for directory in (start, *start.parents):
        candidate = directory / ".tools" / "uv" / "bin" / "uv.exe"
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        "uv is not on PATH and no .tools/uv/bin/uv.exe was found above "
        f"{start}; a candidate generation cannot be validated without it"
    )


class CandidateValidator:
    """Runs the same five commands a human would, inside the candidate only.

    pytest gets an explicit ``--basetemp`` under the candidate rather than the
    machine's shared temp root. On a host where that root is not writable by
    this user, every ``tmp_path`` test errors at fixture setup and the candidate
    is reported as failed for something it did not do. Keeping the temp tree
    inside the generation also means a discarded candidate takes its scratch
    files with it. The directory is relative because every command already runs
    with the generation as its working directory.
    """

    COMMANDS = (
        ("uv", "sync"),
        ("uv", "run", "ruff", "check", "."),
        ("uv", "run", "pyright"),
        ("uv", "run", "pytest", "--basetemp", PYTEST_TEMP_DIR),
        ("uv", "run", "python", "-m", "evomesh.smoke"),
    )

    async def validate(self, generation: Generation) -> ValidationResult:
        outcomes: list[dict[str, object]] = []
        try:
            uv = uv_executable(generation.path)
        except FileNotFoundError as exc:
            return self._write(
                generation,
                ValidationResult(
                    passed=False,
                    commands=[{"command": "uv", "exit_code": -1, "output": str(exc)}],
                ),
            )
        for command in self.COMMANDS:
            process = await asyncio.create_subprocess_exec(
                uv,
                *command[1:],
                cwd=generation.path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await process.communicate()
            outcomes.append(
                {
                    "command": " ".join(command),
                    "exit_code": process.returncode,
                    "output": output.decode(errors="replace"),
                }
            )
            if process.returncode != 0:
                break
        return self._write(
            generation,
            ValidationResult(
                passed=len(outcomes) == len(self.COMMANDS)
                and all(x["exit_code"] == 0 for x in outcomes),
                commands=outcomes,
            ),
        )

    @staticmethod
    def _write(generation: Generation, result: ValidationResult) -> ValidationResult:
        (generation.path / "validation-result.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )
        return result


class CandidateRepairer:
    """Mechanical repair: let the linter fix whatever the linter can fix.

    Most of what a small local model gets wrong in a generated file is a style
    rule that ships with a documented autofix. Burning a model call -- and a
    whole generation -- on ``UP017`` is waste, so the deterministic fixer runs
    first and the model is only asked about what survives it.

    The fixer runs over the whole candidate tree rather than the mutated file
    alone. That is safe because a candidate starts as a copy of a tree that
    already passes ``ruff check``, so the only fixable findings in it are the
    ones the mutation just introduced.
    """

    AUTOFIX = ("uv", "run", "ruff", "check", "--fix", ".")

    def can_repair(self, failure: dict[str, object] | None) -> bool:
        """Whether the mechanical fixer has any chance against this failure."""
        if failure is None:
            return False
        if "ruff" not in str(failure.get("command", "")):
            return False
        # Ruff itself says which findings it can fix; anything else is the
        # model's problem, and asking the fixer to try would waste a cycle.
        return "[*]" in str(failure.get("output", ""))

    async def autofix(self, generation: Generation) -> dict[str, object]:
        uv = uv_executable(generation.path)
        process = await asyncio.create_subprocess_exec(
            uv,
            *self.AUTOFIX[1:],
            cwd=generation.path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        return {
            "command": " ".join(self.AUTOFIX),
            "exit_code": process.returncode,
            "output": output.decode(errors="replace"),
        }


class EnvironmentEvolver:
    """Owns the candidate lifecycle. Driven one stage per cycle by EvolverBehavior."""

    def __init__(
        self,
        workspace: CandidateWorkspace,
        repository: SQLiteRepository,
        provider: ModelProvider | None = None,
        validator: CandidateValidator | None = None,
        repairer: CandidateRepairer | None = None,
        identity: GitIdentity | None = None,
        publish: PublishPolicy | None = None,
    ) -> None:
        self.workspace = workspace
        self.repository = repository
        self.provider = provider
        self.validator = validator or CandidateValidator()
        self.repairer = repairer or CandidateRepairer()
        self.identity = identity or GitIdentity()
        self.publish_policy = publish or PublishPolicy()
        # Set by the Environment so a landed generation can ask the process to
        # restart into it. The evolver never restarts anything itself: it does
        # not own the process, and it must stay usable from a test and a script.
        self.on_generation_landed: Callable[[int, str], None] | None = None
        # What the last publish attempt did, so the cycle that applied the
        # generation can put it in the sentence a human actually reads.
        self.last_publish: str = ""

    # -- pipeline state -------------------------------------------------

    async def pipeline_state(self) -> dict[str, Any]:
        raw = await self.repository.load_state(PIPELINE_STATE_KEY)
        return dict(raw) if isinstance(raw, dict) else {"stage": "plan"}

    async def set_pipeline_state(self, state: dict[str, Any]) -> None:
        await self.repository.save_state(PIPELINE_STATE_KEY, state)

    async def reset_pipeline(self) -> None:
        await self.set_pipeline_state({"stage": "plan"})

    # -- candidates -----------------------------------------------------

    def candidate(self, number: int) -> Generation:
        return self.workspace.supervisor.candidate(number)

    def latest_candidate(self) -> Generation | None:
        candidates = self.workspace.supervisor.candidates()
        return candidates[-1] if candidates else None

    def read_validation(self, generation: Generation) -> ValidationResult | None:
        path = generation.path / "validation-result.json"
        if not path.exists():
            return None
        return ValidationResult.model_validate_json(path.read_text(encoding="utf-8"))

    async def create_candidate(self, objective: str) -> Generation:
        generation = await self.workspace.create(objective)
        await self.repository.record_mutation(
            {"generation": generation.number, "objective": objective, "status": "candidate"}
        )
        return generation

    async def _author(
        self, prompt: str, instruction: str, system: str, model: str | None
    ) -> FileMutation:
        if self.provider is None:
            raise RuntimeError("A local model provider is required to propose a mutation")
        failure = ""
        for _ in range(2):
            raw = strip_reasoning(await self.provider.generate(prompt, system=system, model=model))
            try:
                return parse_mutation(raw)
            except ValueError as exc:
                failure = str(exc)
                # One repair pass. A local model that buried the object in prose
                # or truncated a string usually returns it clean once it is told
                # exactly what could not be read.
                prompt = (
                    f"{instruction}\n\nYour previous answer could not be used: "
                    f"{failure}\nReturn only the JSON object, nothing else."
                )
        raise ValueError(f"The model did not return a usable JSON mutation. {failure}")

    async def propose_mutation(
        self, objective: str, context: str = "", model: str | None = None
    ) -> FileMutation:
        prompt = f"{context}\n\nOBJECTIVE: {objective}\n\n{MUTATION_INSTRUCTION}".strip()
        return await self._author(prompt, MUTATION_INSTRUCTION, MUTATION_SYSTEM, model)

    async def propose_repair(
        self,
        generation: Generation,
        failure: dict[str, object],
        focus: Path | None = None,
        model: str | None = None,
    ) -> FileMutation:
        """Ask the model to fix the command that failed, shown the real output."""
        prompt = "\n".join(
            part
            for part in (
                f"COMMAND: {failure.get('command')}",
                f"EXIT CODE: {failure.get('exit_code')}",
                f"OUTPUT:\n{clip(str(failure.get('output', '')), 1500)}",
                self._focus(generation, focus),
                REPAIR_INSTRUCTION,
            )
            if part
        )
        return await self._author(prompt, REPAIR_INSTRUCTION, REPAIR_SYSTEM, model)

    @staticmethod
    def _focus(generation: Generation, focus: Path | None) -> str:
        """Show the model the file it last wrote, so it can return a whole one."""
        if focus is None:
            return ""
        path = generation.path / focus
        if not path.is_file():
            return ""
        body = clip(path.read_text(encoding="utf-8", errors="replace"), 2000, keep_end=False)
        return f"The last change touched {focus.as_posix()}, which currently reads:\n{body}"

    async def autofix(self, generation: Generation) -> dict[str, object]:
        outcome = await self.repairer.autofix(generation)
        await self.repository.record_mutation(
            {
                "generation": generation.number,
                "status": "repaired",
                "how": "ruff --fix",
                "exit_code": outcome.get("exit_code"),
            }
        )
        return outcome

    async def apply_mutation(
        self,
        generation: Generation,
        mutation: FileMutation,
        objective: str,
        status: str = "applied",
    ) -> Path:
        if generation.status != GenerationStatus.CANDIDATE:
            raise ValueError("Mutations may only be applied to candidates")
        target = mutation.target(generation.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(mutation.content, encoding="utf-8")
        await self.repository.record_mutation(
            {
                "generation": generation.number,
                "objective": objective,
                "path": str(mutation.relative_path),
                "rationale": mutation.rationale,
                "status": status,
            }
        )
        return target

    async def validate(self, generation: Generation) -> ValidationResult:
        result = await self.validator.validate(generation)
        await self.repository.record_mutation(
            {
                "generation": generation.number,
                "status": "validated",
                "passed": result.passed,
            }
        )
        return result

    async def finish_candidate(self, number: int, *, passed: bool) -> Generation:
        generation = self.candidate(number)
        if not passed:
            generation.status = GenerationStatus.FAILED
            self.workspace.supervisor.record_candidate(generation)
        await self.repository.record_mutation(
            {"generation": number, "status": "reviewed", "passed": passed}
        )
        return generation

    async def apply_generation(self, number: int, objective: str = "") -> str:
        """Land a candidate's change on the tree the mesh is checked out from.

        Until this ran, promotion moved a number in a metadata file and the mesh
        went on executing exactly the code it always had. Git is the lineage, so
        the candidate's commit is cherry-picked onto the checkout rather than the
        directory being swapped: one canonical tree, an ordinary history, and a
        commit to reset to when the generation turns out to be a mistake.
        """
        generation = self.candidate(number)
        checkout = self.checkout()
        if not await checkout.is_clean():
            raise GitError(
                "the working tree has uncommitted changes; a generation is never "
                "applied over work a human has not committed"
            )
        commit = generation.git_commit
        if commit is None:
            candidate = GitRepository(generation.path, self.identity)
            if not (await candidate.status()).strip():
                raise GitError(f"generation {number} changed nothing to apply")
            commit = await candidate.commit_mutation(number, objective)
            generation.git_commit = commit
            self.workspace.supervisor.record_candidate(generation)
        previous = await checkout.current_commit()
        applied = await checkout.cherry_pick(commit)
        self.workspace.supervisor.record_commits(active=applied, last_known_good=previous)
        await self.repository.record_mutation(
            {
                "generation": number,
                "status": "applied-to-tree",
                "commit": applied,
                "previous": previous,
            }
        )
        # Publish before the restart is asked for: this process may not be here
        # a moment from now, and an unpublished generation would then sit in a
        # local tree with nothing left running to notice.
        self.last_publish = await self.publish(applied)
        if self.on_generation_landed is not None:
            self.on_generation_landed(number, applied)
        return applied

    def checkout(self) -> GitRepository:
        return GitRepository(self.workspace.repository_root, self.identity)

    async def publish(self, commit: str) -> str:
        """Push the landed commit, and report the outcome as one plain sentence.

        A push is the last step, never a gate: the generation is already in the
        tree, so a remote that refuses it is news to report, not a reason to
        unwind work that validated.
        """
        if not self.publish_policy.enabled:
            self.workspace.supervisor.record_publish(
                commit=commit, published=False, detail="auto_push is off"
            )
            return "not published (auto_push is off)"
        checkout = self.checkout()
        try:
            await checkout.push(self.publish_policy.remote, self.publish_policy.branch)
        except GitError as exc:
            detail = excerpt(str(exc), 300)
            logger.warning("Could not publish %s: %s", commit[:8], detail)
            self.workspace.supervisor.record_publish(
                commit=commit, published=False, detail=detail
            )
            await self.repository.record_mutation(
                {"status": "publish-failed", "commit": commit, "detail": detail}
            )
            return f"not published: {detail}"
        branch = self.publish_policy.branch or await checkout.current_branch()
        where = f"{self.publish_policy.remote}/{branch}"
        self.workspace.supervisor.record_publish(commit=commit, published=True, detail=where)
        await self.repository.record_mutation(
            {"status": "published", "commit": commit, "remote": where}
        )
        return f"published to {where}"

    async def revert_tree(self) -> str | None:
        """Put the checkout back on the commit the last promotion replaced."""
        metadata = self.workspace.supervisor.metadata()
        target = metadata.get("last_known_good_commit")
        if not target:
            return None
        checkout = self.checkout()
        if not await checkout.is_clean():
            raise GitError(
                "the working tree has uncommitted changes; refusing to reset over them"
            )
        restored = await checkout.reset_to(str(target))
        self.workspace.supervisor.record_commits(active=restored, last_known_good=str(target))
        await self.repository.record_mutation({"status": "reverted", "commit": restored})
        return restored

    async def promote_candidate(self, number: int, objective: str = "") -> str:
        """Apply the generation first; only a landed change earns the promotion."""
        applied = await self.apply_generation(number, objective)
        self.workspace.supervisor.promote(number)
        await self.repository.record_mutation(
            {"generation": number, "status": "promoted", "commit": applied}
        )
        return applied

    async def decide_candidate(self, number: int, *, promote: bool, objective: str = "") -> str:
        """Promote or discard without a human, and leave a record that it happened."""
        if promote:
            applied = await self.promote_candidate(number, objective)
            return applied
        self.workspace.supervisor.discard(number)
        await self.repository.record_mutation(
            {"generation": number, "status": "discarded", "decided_by": "policy"}
        )
        return ""

    async def commit_candidate(self, generation: Generation, objective: str) -> str:
        git = GitRepository(generation.path, self.identity)
        commit = await git.commit_mutation(generation.number, objective)
        generation.git_commit = commit
        self.workspace.supervisor.record_candidate(generation)
        return commit
