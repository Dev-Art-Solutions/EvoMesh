from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from evomesh.git import GitRepository
from evomesh.models import ModelProvider
from evomesh.storage import SQLiteRepository


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

    def rollback(self) -> None:
        metadata = self.metadata()
        metadata["active"] = metadata["last_known_good"]
        self._write(metadata)

    def _write(self, data: dict[str, Any]) -> None:
        temporary = self.metadata_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(self.metadata_path)


class CandidateWorkspace:
    def __init__(self, repository_root: Path, generations_root: Path) -> None:
        self.repository_root = repository_root.resolve()
        self.supervisor = GenerationSupervisor(generations_root.resolve())

    async def create(self, objective: str) -> Generation:
        metadata = self.supervisor.metadata()
        existing = [int(item) for item in dict(metadata.get("candidates", {}))]
        number = max([int(metadata["active"]), *existing], default=1) + 1
        destination = self.supervisor.root / f"{number:06d}-candidate"
        if destination.exists():
            raise FileExistsError(destination)
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
                shutil.copytree,
                self.repository_root,
                destination,
                ignore=shutil.ignore_patterns(
                    ".git", ".venv", "data", "generations", "__pycache__"
                ),
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
                    "output": output.decode(),
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
    def __init__(
        self,
        workspace: CandidateWorkspace,
        repository: SQLiteRepository,
        provider: ModelProvider | None = None,
    ) -> None:
        self.workspace = workspace
        self.repository = repository
        self.provider = provider

    async def create_candidate(self, objective: str) -> Generation:
        generation = await self.workspace.create(objective)
        await self.repository.record_mutation(
            {"generation": generation.number, "objective": objective, "status": "candidate"}
        )
        return generation

    async def propose_mutation(self, objective: str) -> FileMutation:
        if self.provider is None:
            raise RuntimeError("A local model provider is required to propose a mutation")
        prompt = (
            "Propose one controlled EvoMesh file mutation for this objective. "
            "Return only JSON with relative_path, content, and rationale.\n\n"
            f"Objective: {objective}"
        )
        raw = await self.provider.generate(
            prompt,
            system=(
                "You are Environment Evolver. Never target absolute paths "
                "or parent directories."
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

    async def commit_candidate(self, generation: Generation, objective: str) -> str:
        git = GitRepository(generation.path)
        commit = await git.commit_mutation(generation.number, objective)
        generation.git_commit = commit
        self.workspace.supervisor.record_candidate(generation)
        return commit
