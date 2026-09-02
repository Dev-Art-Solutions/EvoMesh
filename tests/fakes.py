"""Test doubles shared by more than one module.

Here rather than imported from a sibling test file, which is what phase 4
did and what every candidate generation then failed on: `from
tests.test_cycles import ...` resolves in the checkout and not inside a
candidate, where pytest's rootdir is the candidate and `tests` is not a
package. Validation *is* this suite, so the candidates were being failed
for our import.
"""

from __future__ import annotations

from pathlib import Path

from evomesh.evolution import (
    CandidateRepairer,
    Generation,
    ValidationResult,
)
from evomesh.harness import HarnessResult
from evomesh.harness_queue import HarnessGateway, HarnessJob, HarnessQueue


class FakeHarness(HarnessGateway):
    """A harness whose jobs finish the moment they are asked.

    The pipeline is what these tests are about, not the tool loop, and a
    synchronous job keeps one stage to one cycle. The behavior falls through
    when a job it just submitted is already done, so this shortcut exercises the
    same code path a real worker reaches one cycle later.
    """

    def __init__(
        self, batches: list[list[tuple[str, str]]], answer: str = "flip"
    ) -> None:
        super().__init__(HarnessQueue(), {})
        self.batches = batches
        self.answer = answer
        self.objectives: list[str] = []
        self.labels: list[str] = []

    def submit(
        self, objective: str, *, agent_id: str, root: Path, label: str = ""
    ) -> HarnessJob:
        self.objectives.append(objective)
        self.labels.append(label)
        job = self.queue.submit(
            objective, root, agent_id=agent_id, allow_write=True, label=label
        )
        batch = self.batches[min(len(self.objectives) - 1, len(self.batches) - 1)]
        entries: list[dict[str, object]] = []
        for path, content in batch:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            entries.append({"kind": "edit", "path": path, "diff": f"+{content.strip()}"})
        self.sessions[job.number] = entries
        self.queue.finish(
            job,
            HarnessResult(
                outcome="answered", answer=self.answer, steps=3, edits=len(entries)
            ),
        )
        return job


def failing(command: str, output: str) -> ValidationResult:
    return ValidationResult(
        passed=False,
        commands=[
            {"command": "uv sync", "exit_code": 0, "output": ""},
            {"command": command, "exit_code": 1, "output": output},
        ],
    )


def passing() -> ValidationResult:
    return ValidationResult(
        passed=True, commands=[{"command": "uv run pytest", "exit_code": 0, "output": "ok"}]
    )


class ScriptedValidator:
    """Writes a real validation-result.json: the repair stage reads it back."""

    def __init__(self, results: list[ValidationResult]) -> None:
        self.results = results
        self.calls = 0

    async def validate(self, generation: Generation) -> ValidationResult:
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        path = generation.path / "validation-result.json"
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result


class StubRepairer(CandidateRepairer):
    """The real predicate, a stubbed subprocess."""

    def __init__(self) -> None:
        self.calls = 0

    async def autofix(self, generation: Generation) -> dict[str, object]:
        self.calls += 1
        return {
            "command": " ".join(self.AUTOFIX),
            "exit_code": 0,
            "output": "Found 1 error (1 fixed, 0 remaining).\n",
        }
