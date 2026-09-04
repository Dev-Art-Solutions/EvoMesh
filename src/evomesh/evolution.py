from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from evomesh.codebase import project_map
from evomesh.git import GitError, GitIdentity, GitRepository, PublishPolicy
from evomesh.models import ModelProvider
from evomesh.processes import run_command
from evomesh.storage import SQLiteRepository

logger = logging.getLogger(__name__)

PIPELINE_STATE_KEY = "evolution.pipeline"

# Where each generation explains itself. Tracked, not ignored: the reasoning
# behind a change has to travel with the change.
BACKLOG_DIR = Path("docs") / "evolution"

# What a generation is asked for, now that the asking goes to an agent that can
# read the project rather than to one prompt which had to carry all of it.
HARNESS_RULES = "\n".join(
    (
        "Rules for this project:",
        "- Change a module that ALREADY RUNS. A brand new file is almost always "
        "the wrong answer: nothing imports it, so none of its code executes, and "
        "the validation suite fails any module nothing imports.",
        "- Wiring one of the DEAD modules above into a load-bearing one is real "
        "work, and is worth more than another new file.",
        "- Read a file before you change it. Use edit, not write, for a file that "
        "already exists, and keep each change as small as the objective allows.",
        "- Everything you touch must stay valid Python: ruff, pyright and pytest "
        "are run against your work as soon as you are finished.",
        "- Stay inside this directory. It is a disposable copy, not the running mesh.",
        "- End your final answer with one sentence starting exactly with "
        "'RATIONALE:' explaining what you changed and why -- it is the only "
        "record of your reasoning that survives into this generation's history.",
    )
)


def harness_objective(objective: str, project: str, context: str = "") -> str:
    """What the Evolver asks the harness for, in the harness's own terms.

    The map still goes first, because the rules refer to it -- "the DEAD modules
    above" needs something above it, or the model is guessing at the codebase
    again. What changed is that the map is now orientation for an agent that can
    go and look, rather than the whole of what it will ever see.
    """
    parts = (context, project, f"OBJECTIVE: {objective}", HARNESS_RULES)
    return "\n\n".join(part for part in parts if part).strip()


def harness_repair_objective(
    failure: dict[str, object], project: str, touched: Iterable[str] = ()
) -> str:
    """Fix what validation reported, with the file it happened in reachable.

    The old repair prompt carried the error text and one whole file, and the
    model had to rewrite that file from whatever it could infer. This one names
    the command, its real output and what this generation has already touched;
    the model reads the rest for itself.
    """
    changed = ", ".join(touched)
    parts = (
        project,
        f"The validation command `{failure.get('command')}` failed with exit "
        f"code {failure.get('exit_code')}.",
        f"OUTPUT:\n{clip(str(failure.get('output', '')), 1500)}",
        f"Files this generation has already changed: {changed}" if changed else "",
        "Read the files involved, then fix the failure and nothing else. If the "
        "output says a module is unreachable, the fix is to edit a module that "
        "already runs so that it imports and uses it -- never to rewrite the "
        "unreachable file again.",
        HARNESS_RULES,
    )
    return "\n".join(part for part in parts if part)


# Where a generation's plan lives, before any of it is code. Inside the
# candidate for the same reason BACKLOG_DIR is: git add -A has to pick it up so
# the reasoning behind the plan lands in the same commit as what it produced.
PLAN_DIR = Path("docs") / "evolution" / "plans"
PLAN_NODES_DIR = PLAN_DIR / "nodes"
PLAN_FILE = "plan.md"
PLAN_EVAL_FILE = "plan.eval.md"

PLAN_DRAFT_RULES = "\n".join(
    (
        "Rules for this stage:",
        f"- Do not touch any source file. Write exactly one file, "
        f"`{(PLAN_DIR / PLAN_FILE).as_posix()}`, inside this candidate.",
        "- State the goal, the approach you intend to take, and the reasoning "
        "behind that approach -- specific enough that someone splitting it "
        "into smaller work items later has something real to split.",
        "- End your final answer with one sentence starting exactly with "
        "'RATIONALE:' summarising the plan in one line.",
    )
)

PLAN_EVAL_RULES = "\n".join(
    (
        "Rules for this stage:",
        "- You are reviewing a plan someone else proposed. Do not touch any "
        "source file.",
        f"- Write exactly one file, `{(PLAN_DIR / PLAN_EVAL_FILE).as_posix()}`, "
        "inside this candidate, holding your review.",
        "- That file's last non-empty line must be exactly 'VERDICT: approve' "
        "or 'VERDICT: reject: <one sentence reason>'.",
        "- End your final answer with one sentence starting exactly with "
        "'RATIONALE:' summarising your verdict.",
    )
)

PLAN_DECOMPOSE_RULES = "\n".join(
    (
        "Rules for this stage:",
        "- Do not touch any source file. Write exactly one file at the NODE "
        "PATH given below, inside this candidate.",
        "- Decide whether this item is already minimal -- one small change to "
        "one module that already runs -- or whether it is still big enough to "
        "split into several independent-ish smaller items.",
        "- If it is minimal, the file's last non-empty line must be exactly "
        "'LEAF'.",
        "- If it should split, write one line per child, each in the exact "
        "form '- <title> :: <reasoning>', optionally followed by "
        "' :: depends on: <n>' naming an earlier child in this same list by "
        "its 1-based position, when it can only be done after that one. Do "
        "not force a binary split -- write as many or as few children as the "
        "item actually needs.",
        "- End your final answer with one sentence starting exactly with "
        "'RATIONALE:' explaining your decision.",
    )
)


def draft_plan_objective(objective: str, project: str, context: str = "") -> str:
    """The harness job that drafts a plan for the objective, before any code."""
    parts = (context, project, f"OBJECTIVE: {objective}", PLAN_DRAFT_RULES)
    return "\n\n".join(part for part in parts if part).strip()


def evaluate_plan_objective(plan_text: str, project: str) -> str:
    """The harness job that reviews a drafted plan and writes a verdict."""
    parts = (
        project,
        f"PLAN TO REVIEW:\n{plan_text}",
        "Judge whether this is a real, groundable change to a module that "
        "already runs, scoped small enough to be split into minimal work "
        "items later. Reject it if it proposes a new file nothing will "
        "import, or if it is too vague to split.",
        PLAN_EVAL_RULES,
    )
    return "\n\n".join(part for part in parts if part).strip()


def decompose_objective(node: PlanNode, project: str) -> str:
    """The harness job that splits one plan item, or declares it minimal."""
    parts = (
        project,
        f"NODE PATH: {(PLAN_NODES_DIR / f'{node.id}.md').as_posix()}",
        f"ITEM TITLE: {node.title}",
        f"ITEM REASONING SO FAR:\n{node.reasoning}",
        PLAN_DECOMPOSE_RULES,
    )
    return "\n\n".join(part for part in parts if part).strip()


def parse_plan_verdict(text: str) -> tuple[bool, str]:
    """Pull the 'VERDICT: approve|reject: ...' line an evaluation ends with.

    Defaults to reject when the line is missing, the same fail-closed choice
    ``ENVIRONMENT_MARKERS`` makes elsewhere: a plan that cannot even be read
    back as approved has not earned the work of decomposing it.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("VERDICT:"):
            body = stripped.split(":", 1)[1].strip()
            if body.lower().startswith("approve"):
                return True, body
            return False, body.split(":", 1)[1].strip() if ":" in body else body
    return False, "no VERDICT line was found in the review"


def parse_plan_children(text: str) -> list[dict[str, Any]] | None:
    """Parse a decompose node's file: ``None`` for a declared leaf, else its
    children as ``{"title", "reasoning", "depends_on"}`` dicts (``depends_on``
    holding 1-based positions into this same list, resolved by the caller).
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(line.upper() == "LEAF" for line in lines):
        return None
    children: list[dict[str, Any]] = []
    for line in lines:
        if not line.startswith("-"):
            continue
        segments = [segment.strip() for segment in line[1:].split("::")]
        if len(segments) < 2:
            continue
        depends_on: list[int] = []
        for extra in segments[2:]:
            if extra.lower().startswith("depends on:"):
                for token in extra.split(":", 1)[1].split(","):
                    token = token.strip()
                    if token.isdigit():
                        depends_on.append(int(token))
        children.append(
            {"title": segments[0], "reasoning": segments[1], "depends_on": depends_on}
        )
    return children


class GenerationStatus(StrEnum):
    ACTIVE = "active"
    CANDIDATE = "candidate"
    LAST_KNOWN_GOOD = "last-known-good"
    FAILED = "failed"


class GenerationChange(BaseModel):
    """One file the generation wrote, and the reason it gave for writing it."""

    path: str
    rationale: str
    kind: str = "mutation"
    # The unified diff the harness recorded before it touched the file, so the
    # backlog entry can show what happened rather than only where.
    diff: str = ""
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PlanNode(BaseModel):
    """One node of a generation's plan tree: a draft, a split, or a leaf.

    A flat list rather than a nested structure, so it round-trips through
    ``model_dump(mode="json")`` the same way ``Generation.changes`` already
    does -- ``parent_id`` alone encodes the tree, and ``None`` marks a root (a
    plan draft; a redrafted plan appends a new root rather than overwriting
    the old one, so a rejected draft is never lost, only superseded).
    """

    id: str
    parent_id: str | None = None
    title: str
    reasoning: str
    kind: str = "root"  # "root" | "split" | "leaf"
    status: str = "open"  # "open" | "superseded" | "leaf" | "done"
    depends_on: list[str] = Field(default_factory=list)
    doc_path: str = ""
    # Root nodes only: the Evaluator's verdict on this draft.
    approved: bool | None = None
    eval_reasoning: str = ""
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Generation(BaseModel):
    number: int
    status: GenerationStatus
    path: Path
    parent: int | None = None
    git_commit: str | None = None
    objective: str = ""
    # Kept on the generation rather than only in the mutation log, because the
    # rationale has to survive into the commit that lands: a generation whose
    # reasoning exists only in a database nobody opens is a change nobody can
    # review a month later.
    changes: list[GenerationChange] = Field(default_factory=list)
    # The plan tree behind this generation's changes, when planning is on
    # (``EvolverBehavior(auto_plan=True)``). Empty for a generation authored
    # the old, flat way -- nothing here assumes it is populated.
    plan: list[PlanNode] = Field(default_factory=list)
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
    # Windows, and seen on this machine: uv could not replace a file in the
    # candidate's venv because something else had it open. Nothing about the
    # candidate's source caused it and no rewrite of it would help.
    "os error 32",
    "being used by another process",
    "Connection refused",
    "Temporary failure in name resolution",
)


class ValidationResult(BaseModel):
    passed: bool
    commands: list[dict[str, object]]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def environment_blocker(self) -> str | None:
        """The proof that the host broke this run, or None if it did not.

        A flag first, markers second. Matching strings in output is a guess that
        happens to be right often; a command that *knows* it could not run says
        so, and the missing-toolchain case is the one where guessing was wrong
        and cost the candidate a verdict it never earned.
        """
        failure = self.failure()
        if failure is None:
            return None
        if failure.get("blocked"):
            return str(failure.get("output", "")).strip().splitlines()[0][:120]
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


def _touched_paths(entries: Iterable[dict[str, Any]]) -> list[str]:
    """The paths a harness job actually wrote, from its recorded entries."""
    return [
        str(entry["path"])
        for entry in entries
        if entry.get("kind") in ("edit", "write") and entry.get("path")
    ]


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

    async def _repository_root_is_a_real_repository(self) -> bool:
        """Whether `repository_root` is actually the top of its own git
        repository, rather than a plain directory `git -C` would silently
        walk up and out of.

        `git -C <dir> worktree add` does not require `<dir>` to be a
        repository at all -- it behaves exactly as running the command from
        inside `<dir>` would, which means it finds the *nearest ancestor*
        `.git` when `<dir>` has none of its own. On a repository_root that is
        a plain directory nested inside an unrelated repository -- found
        live, from a test fixture sitting under this project's own
        `.pytest-tmp` -- that silently creates a real worktree and branch in
        the *ancestor's* repository instead of failing over to the copytree
        fallback below, which is what actually happened the one time this
        went unchecked. A mismatch here means "not this one, don't ask git
        to try" rather than a real failure to report.
        """
        try:
            result = await run_command(
                "git", "-C", str(self.repository_root), "rev-parse", "--show-toplevel"
            )
        except OSError:
            return False
        if result.exit_code != 0:
            return False
        top_level = await asyncio.to_thread(lambda: Path(result.output.strip()).resolve())
        resolved_root = await asyncio.to_thread(self.repository_root.resolve)
        return top_level == resolved_root

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
        if await self._repository_root_is_a_real_repository():
            result = await run_command(
                "git",
                "-C",
                str(self.repository_root),
                "worktree",
                "add",
                "-b",
                f"evomesh/candidate-{number:06d}",
                str(destination),
                "HEAD",
            )
        else:
            result = None
        if result is None or result.exit_code != 0:
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
            # Marked blocked, not failed. A candidate cannot be blamed for a
            # toolchain that is not installed, and without this flag the
            # pipeline reads "no uv" as a verdict and spends the repair budget
            # asking a model to fix somebody's PATH.
            return self._write(
                generation,
                ValidationResult(
                    passed=False,
                    commands=[
                        {
                            "command": "uv",
                            "exit_code": -1,
                            "output": str(exc),
                            "blocked": True,
                        }
                    ],
                ),
            )
        for command in self.COMMANDS:
            result = await run_command(uv, *command[1:], cwd=generation.path)
            outcomes.append(
                {
                    "command": " ".join(command),
                    "exit_code": result.exit_code,
                    "output": result.output,
                }
            )
            if result.exit_code != 0:
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
        result = await run_command(uv, *self.AUTOFIX[1:], cwd=generation.path)
        return {
            "command": " ".join(self.AUTOFIX),
            "exit_code": result.exit_code,
            "output": result.output,
        }


@dataclass
class ValidationRun:
    """One suite running against one candidate, off the agent's cycle.

    The README has claimed since the pipeline was written that one stage per
    cycle means a tick never becomes a ten-minute validation run. It did: the
    stage awaited the suite inline, and an agent's mailbox and cycle share one
    lock, so the Evolver stopped answering for the whole of it and looked
    exactly like an agent that had hung.
    """

    generation: int
    task: asyncio.Task[ValidationResult]
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def running(self) -> bool:
        return not self.task.done()

    @property
    def seconds(self) -> float:
        return (datetime.now(UTC) - self.started_at).total_seconds()

    def describe(self) -> str:
        state = "running" if self.running else "finished"
        return f"validating generation {self.generation} ({state}, {self.seconds:.0f}s)"


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
        # The suite running against the open candidate, if one is. At most one:
        # rule 7 means at most one candidate is open, so a second lane would be
        # a lane with nothing in it.
        self.validation: ValidationRun | None = None

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

    def project_map(self) -> str:
        """What the package looks like right now, for the model to aim at."""
        return project_map(self.workspace.repository_root)

    def mutation_objective(self, objective: str, context: str = "") -> str:
        """The harness job that authors this generation."""
        return harness_objective(objective, self.project_map(), context)

    def repair_objective(
        self, failure: dict[str, object], touched: Iterable[str] = ()
    ) -> str:
        """The harness job that fixes what validation reported."""
        return harness_repair_objective(failure, self.project_map(), touched)

    def leaf_objective(self, node: PlanNode, context: str = "") -> str:
        """The harness job that authors one minimal item from the plan tree."""
        objective = f"{node.title}\n\n{node.reasoning}".strip()
        return harness_objective(objective, self.project_map(), context)

    def draft_plan_objective_text(self, objective: str, context: str = "") -> str:
        return draft_plan_objective(objective, self.project_map(), context)

    def evaluate_plan_objective_text(self, plan_text: str) -> str:
        return evaluate_plan_objective(plan_text, self.project_map())

    def decompose_plan_objective_text(self, node: PlanNode) -> str:
        return decompose_objective(node, self.project_map())

    # -- the plan tree ----------------------------------------------------

    @staticmethod
    def plan_node(generation: Generation, node_id: str) -> PlanNode | None:
        return next((node for node in generation.plan if node.id == node_id), None)

    @staticmethod
    def current_plan_root(generation: Generation) -> PlanNode | None:
        """The plan draft in force -- the newest one not superseded by a
        revision -- or ``None`` when nothing has been drafted yet."""
        roots = [
            node
            for node in generation.plan
            if node.parent_id is None and node.status != "superseded"
        ]
        return roots[-1] if roots else None

    async def record_plan_draft(
        self,
        generation: Generation,
        entries: Iterable[dict[str, Any]],
        objective: str,
        rationale: str,
        status: str = "planned",
    ) -> list[str]:
        """Record a drafted (or redrafted) plan as a new root ``PlanNode``.

        A redraft appends rather than overwrites: ``record_plan_eval`` already
        marked the rejected root superseded the moment it was rejected (not
        here, or ``current_plan_root`` would keep answering with a plan a
        human could see was already turned down, for the whole cycle it takes
        the harness to redraft), so this only ever adds a new one.
        """
        touched = _touched_paths(entries)
        plan_path = generation.path / PLAN_DIR / PLAN_FILE
        if not plan_path.exists():
            return touched
        revision = sum(1 for node in generation.plan if node.parent_id is None) + 1
        generation.plan.append(
            PlanNode(
                id=f"root-{revision}",
                title=objective,
                reasoning=plan_path.read_text(encoding="utf-8", errors="replace"),
                kind="root",
                status="open",
                doc_path=(PLAN_DIR / PLAN_FILE).as_posix(),
            )
        )
        self.workspace.supervisor.record_candidate(generation)
        await self.repository.record_mutation(
            {"generation": generation.number, "status": status, "rationale": rationale}
        )
        return touched

    async def record_plan_eval(
        self,
        generation: Generation,
        entries: Iterable[dict[str, Any]],
        objective: str,
        rationale: str,
        status: str = "evaluated",
    ) -> list[str]:
        touched = _touched_paths(entries)
        eval_path = generation.path / PLAN_DIR / PLAN_EVAL_FILE
        if not eval_path.exists():
            return touched
        approved, reason = parse_plan_verdict(
            eval_path.read_text(encoding="utf-8", errors="replace")
        )
        root = self.current_plan_root(generation)
        if root is not None:
            root.approved = approved
            root.eval_reasoning = reason
            if not approved:
                # Superseded the moment it is turned down, not on the next
                # draft: otherwise `current_plan_root` keeps answering with a
                # plan already rejected for as long as the redraft takes.
                root.status = "superseded"
            self.workspace.supervisor.record_candidate(generation)
        await self.repository.record_mutation(
            {
                "generation": generation.number,
                "status": status,
                "approved": approved,
                "rationale": rationale,
            }
        )
        return touched

    async def record_plan_decompose(
        self,
        generation: Generation,
        entries: Iterable[dict[str, Any]],
        objective: str,
        rationale: str,
        status: str = "decomposed",
    ) -> list[str]:
        """Record a decomposition. ``objective`` is the id of the node being
        split, not the generation's standing objective -- ``EvolverBehavior.
        _decompose`` passes it through ``_through_harness``'s ``record_key``
        override, since a decompose job's target varies node to node while
        the recorder signature it shares with ``record_harness_changes``
        does not carry the pipeline ``state`` to read it from otherwise.
        """
        node_id = objective
        touched = _touched_paths(entries)
        node = self.plan_node(generation, node_id)
        if node is None:
            return touched
        doc_path = generation.path / PLAN_NODES_DIR / f"{node_id}.md"
        if not doc_path.exists():
            return touched
        children = parse_plan_children(doc_path.read_text(encoding="utf-8", errors="replace"))
        node.doc_path = (PLAN_NODES_DIR / f"{node_id}.md").as_posix()
        if children is None:
            node.kind = "leaf"
            node.status = "leaf"
        else:
            node.kind = "split"
            node.status = "done"
            created: list[PlanNode] = [
                PlanNode(
                    id=f"{node_id}.{index}",
                    parent_id=node_id,
                    title=child["title"],
                    reasoning=child["reasoning"],
                    kind="split",
                    status="open",
                )
                for index, child in enumerate(children, start=1)
            ]
            for child_node, child in zip(created, children, strict=True):
                child_node.depends_on = [
                    created[position - 1].id
                    for position in child["depends_on"]
                    if 1 <= position <= len(created) and created[position - 1] is not child_node
                ]
            generation.plan.extend(created)
        self.workspace.supervisor.record_candidate(generation)
        await self.repository.record_mutation(
            {
                "generation": generation.number,
                "status": status,
                "node": node_id,
                "rationale": rationale,
            }
        )
        return touched

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

    async def candidate_changed_nothing(self, generation: Generation) -> bool:
        """Whether the candidate's working tree is, right now, identical to its
        parent commit -- the ground truth `apply_generation` checks before
        committing, asked earlier so a generation the free repair fixed back
        into nothing does not spend a validation run first.

        `ruff --fix` runs outside `record_harness_changes` (it is a subprocess,
        not a harness edit), so `generation.changes` still lists the propose
        stage's edit even after the fix undoes it byte-for-byte. The list is
        a record of what was written, not of what is still there -- only git
        status answers "is there still a diff" honestly.

        A candidate is ordinarily a `git worktree`, sharing the checkout's own
        history; a host where `git worktree add` itself failed falls back to a
        plain directory copy with no `.git` of its own (see `CandidateWorkspace.
        create`). `git -C` does not refuse there -- it walks up looking for a
        `.git` the way it always does, and a candidate created under this
        project's own generations/ (or, in a test, a pytest tmp_path nested
        inside this checkout) sits right below one: the checkout's. Trusting
        `status` there answers "is the checkout clean", a different generation
        entirely, and it can say yes while the candidate itself is full of
        uncommitted work. `rev-parse --show-toplevel` catches this before
        `status` ever gets asked: a real worktree's top level is the candidate
        itself, and anything else -- a foreign repository, or none at all --
        is an environment limit, not evidence of a clean tree, so it reads as
        "no" rather than short-circuiting a repair that may have real work
        left to validate.
        """
        candidate = GitRepository(generation.path, self.identity)
        try:
            top_level = (await candidate.run("rev-parse", "--show-toplevel")).strip()
        except GitError:
            return False
        resolved_top_level = await asyncio.to_thread(lambda: Path(top_level).resolve())
        resolved_candidate = await asyncio.to_thread(generation.path.resolve)
        if resolved_top_level != resolved_candidate:
            return False
        try:
            return await candidate.is_clean()
        except GitError:
            return False

    async def record_harness_changes(
        self,
        generation: Generation,
        entries: Iterable[dict[str, Any]],
        objective: str,
        rationale: str,
        status: str = "applied",
    ) -> list[str]:
        """Record what the harness actually wrote, not what the model said.

        The old contract took the model's word for the path it had changed. A
        session records every applied edit and write with its diff, so the
        generation's history now comes from what reached the disk, and the
        model's prose is only the reason attached to it.
        """
        if generation.status != GenerationStatus.CANDIDATE:
            raise ValueError("Mutations may only be applied to candidates")
        generation.objective = generation.objective or objective
        touched: list[str] = []
        for entry in entries:
            if entry.get("kind") not in ("edit", "write"):
                continue
            path = str(entry.get("path") or "")
            if not path:
                continue
            touched.append(path)
            generation.changes.append(
                GenerationChange(
                    path=path,
                    rationale=rationale,
                    kind="repair" if status == "repaired" else "mutation",
                    diff=str(entry.get("diff") or ""),
                )
            )
            await self.repository.record_mutation(
                {
                    "generation": generation.number,
                    "objective": objective,
                    "path": path,
                    "rationale": rationale,
                    "status": status,
                }
            )
        self.workspace.supervisor.record_candidate(generation)
        return touched

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

    def begin_validation(self, generation: Generation, timeout: float = 1800.0) -> ValidationRun:
        """Start the suite off the caller's cycle, and hand back a handle.

        A separate lane from the harness worker on purpose: a tool loop is the
        GPU and a validation run is CPU and disk, so making one wait for the
        other would be a queue whose only effect is to slow the machine down.
        """
        if self.validation is not None and self.validation.generation == generation.number:
            return self.validation

        async def run() -> ValidationResult:
            return await asyncio.wait_for(self.validate(generation), timeout=timeout)

        self.validation = ValidationRun(
            generation=generation.number,
            task=asyncio.create_task(run(), name=f"evomesh-validate-{generation.number}"),
        )
        return self.validation

    def validation_run(self, number: int) -> ValidationRun | None:
        run = self.validation
        return run if run is not None and run.generation == number else None

    async def take_validation(self, run: ValidationRun) -> ValidationResult:
        """The verdict, and the lane is free again.

        A timeout is reported as blocked rather than failed: the candidate never
        got a verdict, and a suite the machine could not finish is not something
        the candidate did wrong.
        """
        self.validation = None
        try:
            return run.task.result()
        except TimeoutError:
            return ValidationResult(
                passed=False,
                commands=[
                    {
                        "command": "uv run pytest",
                        "exit_code": -1,
                        "output": (
                            f"validation of generation {run.generation} did not finish "
                            f"in {run.seconds:.0f}s and was stopped"
                        ),
                        "blocked": True,
                    }
                ],
            )

    async def cancel_validation(self) -> None:
        """Stop a run the mesh is not going to wait for.

        Nothing is resumed: a candidate is a copy on disk, and re-running the
        suite on it costs time and nothing else -- which is cheaper than a
        pipeline waiting on a task that no longer exists.
        """
        run, self.validation = self.validation, None
        if run is None:
            return
        run.task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await run.task

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
            # Written before the status check, so a generation always carries its
            # own explanation into the commit that lands it.
            generation.objective = generation.objective or objective
            state = await self.pipeline_state()
            self.write_backlog(generation, int(state.get("repairs", 0)))
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

    # -- the backlog ----------------------------------------------------

    def write_backlog(self, generation: Generation, repairs: int = 0) -> Path:
        """Write why this generation exists, into the generation itself.

        It goes inside the candidate so ``git add -A`` picks it up and the
        reasoning lands in the same commit as the code. A month from now the
        question about any of these commits is "why did it do that", and the
        answer has to be in the repository, not in a SQLite file on one machine.
        """
        directory = generation.path / BACKLOG_DIR
        directory.mkdir(parents=True, exist_ok=True)
        entry = directory / f"{generation.number:06d}.md"
        entry.write_text(self.render_backlog(generation, repairs), encoding="utf-8")
        self._reindex_backlog(directory)
        return entry

    @staticmethod
    def _generation_summary(generation: Generation) -> str:
        """One line on what *this* generation is actually trying to do.

        ``generation.objective`` is the Evolver's standing goal -- the same
        sentence on every single generation, because it is the goal, not the
        plan. Printing it under "Why this change" answered a question nobody
        asked and left the one that matters ("why did it touch *this* file")
        unanswered. The model's own rationale for its edit is what actually
        varies generation to generation, so that is the headline now; the
        standing goal moves to a quiet note underneath for context.
        """
        mutations = [change for change in generation.changes if change.kind != "repair"]
        for change in mutations:
            reason = change.rationale.strip()
            if reason:
                return f"**What it set out to do.** {reason}"
        if mutations:
            files = ", ".join(sorted({change.path for change in mutations}))
            return (
                f"**What it set out to do.** The model changed `{files}` but gave "
                "no rationale for it -- see the diff below for what actually moved."
            )
        return "**What it set out to do.** No file changes were recorded."

    @staticmethod
    def _plan_children(nodes: list[PlanNode], parent_id: str | None) -> list[PlanNode]:
        return [node for node in nodes if node.parent_id == parent_id]

    def _render_plan_node(
        self, nodes: list[PlanNode], node: PlanNode, depth: int, lines: list[str]
    ) -> None:
        indent = "  " * depth
        tag = " (leaf)" if node.kind == "leaf" else ""
        headline = node.reasoning.strip().splitlines()[0] if node.reasoning.strip() else ""
        summary = f" — {excerpt(headline, 160)}" if headline else ""
        lines.append(f"{indent}- **{node.title}**{tag}{summary}")
        for child in self._plan_children(nodes, node.id):
            self._render_plan_node(nodes, child, depth + 1, lines)

    def _render_plan_tree(self, generation: Generation) -> list[str]:
        """The plan behind this generation's changes, when it was planned.

        Nothing here assumes ``generation.plan`` is populated -- a generation
        authored the old, flat way (``auto_plan=False``) renders no section
        at all, same as before this existed.
        """
        if not generation.plan:
            return []
        lines = ["## How it was planned", ""]
        for root in self._plan_children(generation.plan, None):
            state = " (superseded)" if root.status == "superseded" else ""
            if root.approved is True:
                verdict = " — approved"
            elif root.approved is False:
                reason = root.eval_reasoning.strip()
                verdict = f" — rejected: {reason}" if reason else " — rejected"
            else:
                verdict = ""
            lines.append(f"- **Plan draft**{state}{verdict}")
            for child in self._plan_children(generation.plan, root.id):
                self._render_plan_node(generation.plan, child, 1, lines)
        lines.append("")
        return lines

    def render_backlog(self, generation: Generation, repairs: int = 0) -> str:
        validation = self.read_validation(generation)
        lines = [
            f"# Generation {generation.number}",
            "",
            f"- **Opened:** {generation.created_at:%Y-%m-%d %H:%M UTC}",
            f"- **Parent generation:** {generation.parent if generation.parent else '-'}",
            f"- **Author:** {self.identity}",
            "",
            *self._render_plan_tree(generation),
            "## Why this change",
            "",
            self._generation_summary(generation),
            "",
            f"*Standing goal: {generation.objective or '(none recorded)'}*",
            "",
        ]
        if generation.changes:
            lines += ["### What it changed, and the reason it gave", ""]
            for index, change in enumerate(generation.changes, start=1):
                label = "Repair" if change.kind == "repair" else "Change"
                reason = change.rationale.strip() or "(the model gave no rationale)"
                lines += [f"{index}. **{label} to `{change.path}`** — {reason}"]
                # The diff travels with the reason. "Why did it do that" is asked
                # about a commit a month later, and the answer belongs in the
                # repository rather than in a session file on one machine.
                if change.diff.strip():
                    lines += [
                        "",
                        "   ```diff",
                        *(
                            f"   {line}"
                            for line in clip(change.diff, 1200, keep_end=False).splitlines()
                        ),
                        "   ```",
                        "",
                    ]
            lines.append("")
        else:
            lines += ["No file changes were recorded for this generation.", ""]

        lines += ["## How it was checked", ""]
        if validation is None:
            lines += [
                "Validation did not run, so this generation carries no verdict.",
                "",
            ]
        else:
            verdict = "passed" if validation.passed else "failed"
            lines.append(f"The suite **{verdict}**:")
            lines.append("")
            lines.append("| Command | Exit |")
            lines.append("| --- | --- |")
            for command in validation.commands:
                lines.append(f"| `{command.get('command')}` | {command.get('exit_code')} |")
            lines.append("")
            if failure := validation.failure():
                lines += [
                    "The failing command reported:",
                    "",
                    "```",
                    clip(str(failure.get("output", "")), 1200).strip(),
                    "```",
                    "",
                ]
        if repairs:
            lines += [
                f"It repaired itself **{repairs} time{'s' if repairs != 1 else ''}** "
                "before reaching that verdict.",
                "",
            ]
        return "\n".join(lines)

    @staticmethod
    def _reindex_backlog(directory: Path) -> None:
        """Rebuild the index from the entries on disk, newest first."""
        entries = sorted(
            (path for path in directory.glob("[0-9]*.md")),
            key=lambda path: path.stem,
            reverse=True,
        )
        rows = []
        for path in entries:
            heading = ""
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("**What it set out to do.**"):
                    heading = line.removeprefix("**What it set out to do.**").strip()
                    break
            rows.append(f"- [Generation {int(path.stem)}]({path.name}) — {heading or '-'}")
        (directory / "README.md").write_text(
            "\n".join(
                [
                    "# Evolution backlog",
                    "",
                    "One entry per generation the Environment Evolver produced: what it",
                    "changed, the reason it gave, and how the change was checked. Written",
                    "by the mesh itself, into the same commit as the code.",
                    "",
                    *rows,
                    "",
                ]
            ),
            encoding="utf-8",
        )

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
