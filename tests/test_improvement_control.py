"""The improvement backlog as the control plane of evolution:
evidence -> backlog -> prioritize -> delegate -> implement -> review/validate
-> deploy -> measure."""

from __future__ import annotations

from pathlib import Path

from evomesh.behaviors import EvolverBehavior
from evomesh.cognition import CycleContext
from evomesh.coordination import WorkStatus
from evomesh.events import Event, EventType
from evomesh.evolution import EnvironmentEvolver
from evomesh.improvements import (
    EVIDENCE_HUMAN_BACKLOG,
    Candidate,
    ImprovementBacklog,
    ImprovementControl,
    ImprovementCoordinator,
    ImprovementScout,
    ImprovementStatus,
    ImprovementTriage,
    PriorityFactors,
)
from tests.fakes import ScriptedValidator, StubRepairer, failing, passing
from tests.test_cycles import BUSY_SOURCE, STEPPED_BACKLOG, _seed_package, evolving, git_project


def control(*, require_review: bool = False) -> tuple[ImprovementControl, list[str]]:
    backlog = ImprovementBacklog()
    announced: list[str] = []

    async def save() -> None:
        return None

    async def announce(text: str) -> None:
        announced.append(text)

    return (
        ImprovementControl(
            backlog,
            ImprovementCoordinator(backlog),
            ImprovementTriage(),
            ImprovementScout(),
            save=save,
            announce=announce,
            require_review=require_review,
        ),
        announced,
    )


def candidate(ref: str, impact: float = 1.0) -> Candidate:
    return Candidate(
        ref=ref,
        kind=EVIDENCE_HUMAN_BACKLOG,
        title=ref,
        problem=ref,
        component="evomesh",
        evidence={},
        factors=PriorityFactors(impact=impact),
    )


async def test_the_best_scoring_evidence_is_chosen_not_a_rotation() -> None:
    plane, _ = control()
    await plane.sync([candidate("item:low"), candidate("item:high", impact=3)], set())

    chosen = plane.choose()

    assert chosen is not None and chosen.source_ref == "item:high"
    assert chosen.status is ImprovementStatus.ACTIVE
    assert plane.choose() is chosen, "one improvement in progress at a time"


async def test_evidence_that_disappears_retires_a_waiting_item() -> None:
    plane, _ = control()
    await plane.sync([candidate("item:a")], set())
    await plane.sync([], set())
    item = next(iter(plane.backlog.items.values()))
    assert item.status is ImprovementStatus.REJECTED
    await plane.sync([candidate("item:a")], set())
    assert item.status is ImprovementStatus.READY, "back once it is evidenced again"


async def test_runtime_events_become_work_only_when_they_recur_and_are_not_environmental() -> None:
    plane, _ = control()
    stall = Event(
        EventType.AGENT_STALLED,
        "progress_tracker",
        agent_id="a",
        goal_id="g",
        payload={"reason": "no new step for 4 cycles"},
    )
    first = await plane.propose_from_event(stall)
    assert first is not None and first.status is ImprovementStatus.TRIAGED
    await plane.propose_from_event(stall)
    third = await plane.propose_from_event(stall)
    assert third is first and first.status is ImprovementStatus.READY

    down = await plane.propose_from_event(
        Event(EventType.TASK_FAILED, "x", agent_id="a", payload={"reason": "provider timeout"})
    )
    assert down is not None and down.status is ImprovementStatus.REJECTED


def _control_context(context: CycleContext, plane: ImprovementControl) -> None:
    context.services["improvements"] = plane


async def test_a_backlog_item_is_worked_step_by_step_then_verified(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _seed_package(root, STEPPED_BACKLOG)
    project = await git_project(root)
    doubled = BUSY_SOURCE.replace("return value", "return value * 2")
    documented = doubled.replace("def helper(value):\n", 'def helper(value):\n    """x2"""\n')
    evolver, context, _ = await evolving(
        tmp_path,
        project,
        [[("src/evomesh/busy.py", doubled)], [("src/evomesh/busy.py", documented)]],
        ScriptedValidator([passing(), passing()]),
        StubRepairer(),
    )
    plane, announced = control()
    _control_context(context, plane)
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2, auto_promote=True)

    for _ in range(4):  # plan, propose, validate, decide -- step 1
        await behavior.cycle(context)
    item = next(iter(plane.backlog.items.values()))
    assert item.source_ref == "item:Double the helper"
    first_work = plane.backlog.work_items[item.work_item_ids[0]]
    assert first_work.assigned_agent_id == context.definition.id
    assert isinstance(first_work.inputs["generation"], int)

    for _ in range(4):  # step 2: settling step 1 puts the item back in line
        await behavior.cycle(context)
    assert first_work.status is WorkStatus.COMPLETED
    assert (await evolver.pipeline_state()).get("stage", "plan") == "plan"

    await _open_once(behavior, context, evolver)

    assert item.status is ImprovementStatus.VERIFIED, (item.status, item.rejection_reason)
    assert all(
        plane.backlog.work_items[work_id].status is WorkStatus.COMPLETED
        for work_id in item.work_item_ids
    )
    assert item.validation_passed is True
    assert any("verified" in text for text in announced)


async def _open_once(
    behavior: EvolverBehavior, context: CycleContext, evolver: EnvironmentEvolver
) -> None:
    """One plan-stage cycle: settles and measures, then (nothing left to
    do here) opens nothing substantive."""
    await behavior.cycle(context)


async def test_exhausted_attempts_escalate_to_a_human_once(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _seed_package(root, STEPPED_BACKLOG)
    project = await git_project(root)
    broken = BUSY_SOURCE.replace("return value", "return value +")
    evolver, context, _ = await evolving(
        tmp_path,
        project,
        [[("src/evomesh/busy.py", broken)]] * 3,
        ScriptedValidator([failing("pytest", "boom")] * 3),
        None,
    )
    plane, announced = control()
    _control_context(context, plane)
    behavior = EvolverBehavior(auto_validate=True, max_repairs=0, auto_promote=True)

    for _ in range(3 * 4 + 1):
        await behavior.cycle(context)

    item = next(iter(plane.backlog.items.values()))
    assert item.status is ImprovementStatus.NEEDS_HUMAN
    work = plane.backlog.work_items[item.work_item_ids[0]]
    assert work.status is WorkStatus.FAILED and work.attempts == 3
    assert sum("needs_human" in text for text in announced) == 1
