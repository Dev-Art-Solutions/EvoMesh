"""The improvement backlog as the control plane of evolution:
evidence -> backlog -> prioritize -> delegate -> implement -> review/validate
-> deploy -> measure."""

from __future__ import annotations

from pathlib import Path

import pytest

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
    assert work.status is WorkStatus.NEEDS_HUMAN and work.attempts == 3
    assert sum("needs_human" in text for text in announced) == 1


async def test_scope_creep_becomes_a_proposal_ready_on_a_second_report() -> None:
    plane, _ = control()
    first = await plane.propose_discovery(
        "retry loop in src/evomesh/models.py never backs off", generation=5, job=1
    )
    assert first is not None and first.status is ImprovementStatus.TRIAGED
    assert first.component == "src/evomesh/models.py"
    again = await plane.propose_discovery(
        "Retry loop in src/evomesh/models.py never  backs off", generation=9, job=4
    )
    assert again is first and first.status is ImprovementStatus.READY
    await plane.sync([], set())
    assert first.status is ImprovementStatus.READY, "a discovery is not retired by sync"


async def test_a_dependency_holds_an_improvement_until_the_other_is_verified() -> None:
    plane, _ = control()
    await plane.sync([candidate("item:base"), candidate("item:top", impact=5)], set())
    items = {item.source_ref: item for item in plane.backlog.items.values()}
    base, top = items["item:base"], items["item:top"]
    plane.set_dependency(top.id, base.id)
    with pytest.raises(ValueError, match="cycle"):
        plane.set_dependency(base.id, top.id)
    top.epic = "reliability"

    assert plane.choose() is base, "the higher score waits for its dependency"
    assert "epic reliability: 0/1 verified" in plane.summary()


async def test_a_model_endpoint_error_is_environmental_even_inside_a_stall() -> None:
    """Found live: a 404 for a model Ollama does not have became a READY
    improvement, and a stall caused by it hid the error behind its reason."""
    plane, _ = control()
    error = (
        "step 1/1 failed: HTTPStatusError: Client error '404 Not Found' for url "
        "'http://127.0.0.1:11434/api/generate'"
    )
    failed = await plane.propose_from_event(
        Event(EventType.TASK_FAILED, "bdi", agent_id="a", payload={"reason": error})
    )
    stalled = await plane.propose_from_event(
        Event(
            EventType.AGENT_STALLED,
            "progress_tracker",
            agent_id="a",
            payload={"reason": "same failure repeated 3 times", "error": error},
        )
    )
    assert failed is not None and failed.status is ImprovementStatus.REJECTED
    assert stalled is not None and stalled.status is ImprovementStatus.REJECTED


async def test_sync_judges_waiting_runtime_proposals_again() -> None:
    plane, _ = control()
    stale = await plane.propose_from_event(
        Event(EventType.TASK_FAILED, "bdi", agent_id="a", payload={"reason": "odd failure"})
    )
    assert stale is not None
    stale.status = ImprovementStatus.READY  # judged under an older policy
    await plane.sync([], set())
    assert stale.status is ImprovementStatus.TRIAGED


ONE_STEP_BACKLOG = (
    "# Backlog\n\n"
    "- [ ] Double the helper\n"
    "    The helper should double what it gets.\n"
    "    1. [ ] src/evomesh/busy.py `helper` -- return value * 2\n"
)


async def test_the_improvement_lifecycle_survives_a_restart(tmp_path: Path) -> None:
    """Promoted in one process, verified in the next: nothing the control
    plane needs is held only in memory (Phase 2 M5 gate)."""
    root = tmp_path / "project"
    _seed_package(root, ONE_STEP_BACKLOG)
    project = await git_project(root)
    doubled = BUSY_SOURCE.replace("return value", "return value * 2")
    evolver, context, _ = await evolving(
        tmp_path,
        project,
        [[("src/evomesh/busy.py", doubled)]],
        ScriptedValidator([passing()]),
        StubRepairer(),
    )
    store: dict[str, object] = {}

    def persisted(backlog: ImprovementBacklog) -> ImprovementControl:
        async def save() -> None:
            store["backlog"] = backlog.dump()

        return ImprovementControl(
            backlog,
            ImprovementCoordinator(backlog),
            ImprovementTriage(),
            ImprovementScout(),
            save=save,
            require_review=False,
        )

    context.services["improvements"] = persisted(ImprovementBacklog())
    first = EvolverBehavior(auto_validate=True, max_repairs=2, auto_promote=True)
    for _ in range(4):  # plan, propose, validate, decide -- promoted
        await first.cycle(context)
    assert (project / "src" / "evomesh" / "busy.py").read_text(encoding="utf-8") == doubled

    restored = ImprovementBacklog.load(store["backlog"])  # the process restarts here
    context.services["improvements"] = persisted(restored)
    await EvolverBehavior(auto_validate=True, auto_promote=True).cycle(context)

    item = next(iter(restored.items.values()))
    assert item.status is ImprovementStatus.VERIFIED, (item.status, item.rejection_reason)
    work = restored.work_items[item.work_item_ids[0]]
    assert work.status is WorkStatus.COMPLETED
