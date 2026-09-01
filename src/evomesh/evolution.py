from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from evomesh.git import GitRepository
from evomesh.models import ModelProvider
from evomesh.storage import SQLiteRepository

PIPELINE_STATE_KEY = "evolution.pipeline"

MUTATION_INSTRUCTION = (
    "Propose ONE small, safe file change to EvoMesh that advances the objective.\n"
    "Return only a JSON object, no prose and no code fences, with exactly these keys:\n"
    '{"relative_path": "src/evomesh/<file>.py", "content": "<the complete new file>", '
    '"rationale": "<one sentence>"}\n'
    "The path must be relative and must stay inside the project."
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


class ValidationResult(BaseModel):
    passed: bool
    commands: list[dict[str, object]]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


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

    def _write(self, data: dict[str, Any]) -> None:
        temporary = self.metadata_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(self.metadata_path)


IGNORED_NAMES = (".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".runtime", "dist")


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


class CandidateValidator:
    COMMANDS = (
        ("uv", "sync"),
        ("uv", "run", "ruff", "check", "."),
        ("uv", "run", "pyright"),
        ("uv", "run", "pytest"),
        ("uv", "run", "python", "-m", "evomesh.smoke"),
    )

    async def validate(self, generation: Generation) -> ValidationResult:
        outcomes: list[dict[str, object]] = []
        for command in self.COMMANDS:
            process = await asyncio.create_subprocess_exec(
                *command,
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
        result = ValidationResult(
            passed=len(outcomes) == len(self.COMMANDS)
            and all(x["exit_code"] == 0 for x in outcomes),
            commands=outcomes,
        )
        (generation.path / "validation-result.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )
        return result


class EnvironmentEvolver:
    """Owns the candidate lifecycle. Driven one stage per cycle by EvolverBehavior."""

    def __init__(
        self,
        workspace: CandidateWorkspace,
        repository: SQLiteRepository,
        provider: ModelProvider | None = None,
        validator: CandidateValidator | None = None,
    ) -> None:
        self.workspace = workspace
        self.repository = repository
        self.provider = provider
        self.validator = validator or CandidateValidator()

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

    async def propose_mutation(self, objective: str, context: str = "") -> FileMutation:
        if self.provider is None:
            raise RuntimeError("A local model provider is required to propose a mutation")
        prompt = f"{context}\n\nOBJECTIVE: {objective}\n\n{MUTATION_INSTRUCTION}".strip()
        raw = await self.provider.generate(
            prompt,
            system=(
                "You are Environment Evolver. Never target absolute paths "
                "or parent directories. Output JSON only."
            ),
        )
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < start:
            raise ValueError("Model response did not contain a JSON mutation")
        return FileMutation.model_validate_json(raw[start : end + 1])

    async def apply_mutation(
        self, generation: Generation, mutation: FileMutation, objective: str
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
                "status": "applied",
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

    async def commit_candidate(self, generation: Generation, objective: str) -> str:
        git = GitRepository(generation.path)
        commit = await git.commit_mutation(generation.number, objective)
        generation.git_commit = commit
        self.workspace.supervisor.record_candidate(generation)
        return commit
