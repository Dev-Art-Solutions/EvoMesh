"""W3 (closure plan v2 19.3, 18.6; AC-20): a real defect in an isolated
fixture package, from a failing check to VERIFIED -- and the ways it must
not get there.

known failing regression check -> eligible improvement -> work item routed to
the code-capable executor -> candidate edit in its own workspace -> a real
acceptance run (old tree red, candidate green) -> a separate review bound to
the same candidate revision -> promotion into the fixture deployment -> a
post-change observation from the real suite -> VERIFIED.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evomesh.behaviors import EvolverBehavior
from evomesh.coordination import WorkItem, WorkStatus
from evomesh.evolution import BaselineResult, EnvironmentEvolver, Generation, ValidationResult
from evomesh.improvements import (
    EVIDENCE_RUNTIME_FAULT,
    Candidate,
    ImprovementStatus,
    Observation,
    PriorityFactors,
    ReviewVerdict,
)
from evomesh.models import MockProvider
from tests.fakes import StubRepairer
from tests.test_cycles import evolving, git_project
from tests.test_improvement_control import _control_context, control

DEFECT = '"""Prices."""\n\n\ndef total(items):\n    return sum(items) + 1\n'
FIXED = '"""Prices."""\n\n\ndef total(items):\n    return sum(items)\n'
ACCEPTANCE = (
    "from evomesh.pricing import total\n\n\n"
    "def test_total_adds_exactly():\n    assert total([1, 2]) == 3\n"
)


def _fixture(root: Path) -> None:
    package = root / "src" / "evomesh"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        '"""Fixture."""\n\nfrom evomesh.pricing import total\n', encoding="utf-8"
    )
    (package / "pricing.py").write_text(DEFECT, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_pricing.py").write_text(ACCEPTANCE, encoding="utf-8")


def _acceptance(tree: Path) -> tuple[bool, str]:
    """The fixed, protected acceptance check -- run for real, outside the
    candidate's control (its file is part of the fixture, not the edit)."""
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_pricing.py", "-q", "-p", "no:cacheprovider"],
        cwd=tree,
        env={
            "PYTHONPATH": str(tree / "src"),
            # Nothing written into the tree: a dirty fixture is never applied over.
            "PYTHONDONTWRITEBYTECODE": "1",
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "PATH": os.environ.get("PATH", ""),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    return run.returncode == 0, run.stdout[-2000:]


class AcceptanceValidator:
    """Deterministic validation: the real acceptance check on the candidate."""

    def __init__(self) -> None:
        self.runs: list[bool] = []

    async def validate(self, generation: Generation) -> ValidationResult:
        passed, output = await asyncio.to_thread(_acceptance, generation.path)
        self.runs.append(passed)
        result = ValidationResult(
            passed=passed,
            commands=[
                {"command": "pytest tests/test_pricing.py", "exit_code": 0 if passed else 1,
                 "output": output}
            ],
        )
        (generation.path / "validation-result.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )
        return result


def _real_baseline(evolver: EnvironmentEvolver, project: Path, *, observer_on: bool = True):  # type: ignore[no-untyped-def]
    """The live tree's suite, keyed by what it ran on."""

    async def baseline(timeout_seconds: float = 0) -> BaselineResult | None:
        source = (project / "src" / "evomesh" / "pricing.py").read_bytes()
        key = hashlib.sha256(source).hexdigest()[:16]
        if not observer_on:
            return BaselineResult(key=key, passed=False, blocked=True, output="observer down")
        passed, output = await asyncio.to_thread(_acceptance, project)
        failures = () if passed else ("tests/test_pricing.py::test_total_adds_exactly",)
        return BaselineResult(key=key, passed=passed, failures=failures, output=output)

    return baseline


async def _w3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    root = tmp_path / "project"
    _fixture(root)
    project = await git_project(root)
    validator = AcceptanceValidator()
    evolver, context, harness = await evolving(
        tmp_path,
        project,
        [[("src/evomesh/pricing.py", FIXED)], []],
        validator,  # type: ignore[arg-type]
        StubRepairer(),
    )
    harness.answers = [
        "RATIONALE: total() added a stray 1; it now returns the plain sum",
        "Read the diff against the objective.\nVERDICT: COMPLETE",
    ]
    monkeypatch.setattr(evolver, "baseline", _real_baseline(evolver, project))
    plane, announced = control(require_review=True)
    _control_context(context, plane)
    behavior = EvolverBehavior(
        auto_validate=True,
        max_repairs=0,
        auto_promote=True,
        review=True,
        baseline_tests=True,
        test_backlog=False,
        scout_when_idle=False,
    )
    return project, evolver, context, harness, validator, plane, announced, behavior


async def _until_promoted(behavior: EvolverBehavior, context, project: Path) -> None:  # type: ignore[no-untyped-def]
    """Cycle until the fix lands; validation runs in the background, so a
    cycle can simply find it still running."""
    target = project / "src" / "evomesh" / "pricing.py"
    for _ in range(300):
        await behavior.cycle(context)
        if target.read_text(encoding="utf-8") == FIXED:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the fix never landed")


async def test_w3_a_real_defect_goes_from_failing_check_to_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, evolver, context, harness, validator, plane, announced, behavior = await _w3(
        tmp_path, monkeypatch
    )
    assert _acceptance(project)[0] is False, "the check fails on the live tree first"

    await _until_promoted(behavior, context, project)
    assert (project / "src" / "evomesh" / "pricing.py").read_text(encoding="utf-8") == FIXED
    item = next(iter(plane.backlog.items.values()))
    work = plane.backlog.work_items[item.work_item_ids[0]]
    assert item.source == "failing_tests"
    assert work.assigned_agent_id == context.definition.id
    assert work.inputs["handle"]["assignee"] == context.definition.id
    assert validator.runs == [True], "the candidate passed the real acceptance check"
    assert item.review_verdict is ReviewVerdict.COMPLETE
    assert item.review_revision and item.review_revision == item.validation_revision
    assert any("VERDICT" in objective for objective in harness.objectives), "a separate review"

    await behavior.cycle(context)  # settle, then observe the promoted tree

    assert _acceptance(project)[0] is True, "the check passes after promotion"
    assert item.status is ImprovementStatus.VERIFIED, (item.status, item.inconclusive_reason)
    assert item.verification is not None
    assert item.verification.observers == ["baseline_suite"]
    assert len(item.verification.observation_ids) == 1
    assert any("verified" in text for text in announced)

    provider = evolver.provider
    assert isinstance(provider, MockProvider)
    calls = len(provider.calls)
    await behavior.cycle(context)  # nothing eligible is left
    assert evolver.workspace.supervisor.candidates() == []
    assert len(provider.calls) == calls, "idle costs no model call"


async def test_w3_a_disabled_observer_never_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, evolver, context, _, _, plane, _, behavior = await _w3(tmp_path, monkeypatch)
    await _until_promoted(behavior, context, project)
    monkeypatch.setattr(evolver, "baseline", _real_baseline(evolver, project, observer_on=False))

    for _ in range(3):
        await behavior.cycle(context)

    item = next(iter(plane.backlog.items.values()))
    assert item.status is ImprovementStatus.VERIFYING
    assert "unhealthy" in item.inconclusive_reason


# -- the rules W3 rests on, one at a time ------------------------------------------


def _fault() -> Candidate:
    return Candidate(
        ref="fault:m.f:KeyError",
        kind=EVIDENCE_RUNTIME_FAULT,
        title="Fix KeyError",
        problem="KeyError",
        component="m",
        evidence={},
        factors=PriorityFactors(),
        observations=2,
    )


async def _verifying():  # type: ignore[no-untyped-def]
    plane, _ = control()
    await plane.sync([_fault()], {_fault().ref})
    item = plane.choose()
    assert item is not None
    work = plane.coordinator.create_work_item(item, "fix", capabilities=["code.edit"])
    assert work is not None
    work.status = WorkStatus.COMPLETED
    plane.coordinator.record_review(item, ReviewVerdict.COMPLETE, "rev-1")
    plane.coordinator.record_validation(item, passed=True, revision="rev-1")
    assert plane.coordinator.begin_verification(item)
    return plane, item


def _log(name: str, eligible: int = 1, healthy: bool = True) -> Observation:
    return Observation(
        name, "runtime_log", frozenset({EVIDENCE_RUNTIME_FAULT}), eligible=eligible,
        healthy=healthy,
    )


async def test_zero_eligible_probes_never_verify() -> None:
    plane, item = await _verifying()

    for index in range(5):
        await plane.sync([], set(), [_log(f"quiet-{index}", eligible=0)])

    assert item.status is ImprovementStatus.VERIFYING
    assert "no eligible requests" in item.inconclusive_reason


async def test_empty_syncs_are_not_observations() -> None:
    plane, item = await _verifying()

    for _ in range(5):
        await plane.sync([], set())

    assert item.status is ImprovementStatus.VERIFYING
    assert item.verification is not None and item.verification.observation_ids == []


async def test_the_same_reading_counts_once() -> None:
    plane, item = await _verifying()

    for _ in range(3):
        await plane.sync([], set(), [_log("log:100")])
    assert item.status is ImprovementStatus.VERIFYING
    await plane.sync([], set(), [_log("log:200")])

    assert item.status is ImprovementStatus.VERIFIED


async def test_a_candidate_edit_invalidates_the_old_review() -> None:
    plane, _ = control()
    await plane.sync([_fault()], {_fault().ref})
    item = plane.choose()
    assert item is not None
    work = plane.coordinator.create_work_item(item, "fix", capabilities=["code.edit"])
    assert work is not None
    work.status = WorkStatus.COMPLETED
    plane.coordinator.record_review(item, ReviewVerdict.COMPLETE, "rev-1")
    # A repair changed the candidate after the review; validation ran again.
    plane.coordinator.record_validation(item, passed=True, revision="rev-2")

    assert item.review_verdict is None
    assert not plane.coordinator.begin_verification(item)


async def test_a_cancelled_required_task_is_not_success() -> None:
    plane, _ = control()
    await plane.sync([_fault()], {_fault().ref})
    item = plane.choose()
    assert item is not None
    done = plane.coordinator.create_work_item(item, "part 1", capabilities=["code.edit"])
    cancelled = WorkItem(parent_goal_id=item.id, improvement_id=item.id, objective="part 2")
    plane.backlog.work_items[cancelled.id] = cancelled
    item.work_item_ids.append(cancelled.id)
    assert done is not None
    done.status = WorkStatus.COMPLETED
    cancelled.status = WorkStatus.CANCELLED
    plane.coordinator.record_review(item, ReviewVerdict.COMPLETE)
    plane.coordinator.record_validation(item, passed=True)

    assert not plane.coordinator.begin_verification(item)
    cancelled.failure_history.append("waived: an operator removed this requirement")
    assert plane.coordinator.begin_verification(item)


async def test_only_an_operator_verifies_by_hand() -> None:
    plane, item = await _verifying()

    with pytest.raises(ValueError):
        await plane.verify(item.id, actor="agent:evolver", reason="looks fine to me")
    await plane.verify(item.id, actor="operator:iliya", reason="reproduced; fixed")

    assert item.status is ImprovementStatus.VERIFIED
    assert item.verified_by.startswith("operator:iliya")
