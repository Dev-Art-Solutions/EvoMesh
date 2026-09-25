from __future__ import annotations

import pytest

from evomesh.coordination import WorkStatus
from evomesh.events import Event, EventType
from evomesh.improvements import (
    Evidence,
    Improvement,
    ImprovementBacklog,
    ImprovementCoordinator,
    ImprovementScout,
    ImprovementStatus,
    ImprovementTriage,
    PriorityFactors,
    ReviewVerdict,
    VerificationPlan,
)


def improvement(**overrides: object) -> Improvement:
    fields: dict[str, object] = {
        "title": "Stop retry loop",
        "problem": "same provider failure repeats",
        "evidence": [
            Evidence(kind="runtime", reference="event-1", value=4, source="guardian")
        ],
        "component": "runtime",
        "source": "metrics",
        "created_by": "scout",
        "success_criteria": ["no more than two identical retries"],
    }
    fields.update(overrides)
    return Improvement.model_validate(fields)


def test_improvement_requires_evidence() -> None:
    with pytest.raises(ValueError, match="requires evidence"):
        improvement(evidence=[])


def test_backlog_deduplicates_and_merges_evidence() -> None:
    backlog = ImprovementBacklog()
    first = backlog.add(improvement())
    second = backlog.add(
        improvement(
            evidence=[
                Evidence(kind="runtime", reference="event-2", value=5, source="guardian")
            ]
        )
    )

    assert first is second
    assert len(first.evidence) == 2
    assert len(backlog.items) == 1


def test_triage_and_priority_are_explicit() -> None:
    high = improvement(
        title="High",
        problem="high impact failure",
        factors=PriorityFactors(impact=5, recurrence=5, estimated_effort=1, risk=1),
    )
    low = improvement(
        title="Low",
        problem="cosmetic issue",
        factors=PriorityFactors(impact=1, recurrence=1, estimated_effort=2, risk=2),
    )
    backlog = ImprovementBacklog()
    for item in (low, high):
        backlog.add(item)
        assert ImprovementTriage().triage(item) is ImprovementStatus.READY

    assert backlog.ready() == [high, low]
    assert high.factors.score > low.factors.score


def test_scout_only_proposes_from_supported_runtime_events() -> None:
    scout = ImprovementScout()
    assert scout.from_event(Event(EventType.MESSAGE_RECEIVED, "human")) is None

    proposal = scout.from_event(
        Event(
            EventType.AGENT_STALLED,
            "progress_tracker",
            goal_id="goal-1",
            payload={"reason": "same failure", "repeats": 3},
        )
    )
    assert proposal is not None
    assert proposal.evidence[0].reference == "agent_stalled:goal-1"


def test_coordinator_enforces_wip_and_closes_verification_loop() -> None:
    backlog = ImprovementBacklog()
    item = backlog.add(
        improvement(
            verification=VerificationPlan(
                metric="retry_count", baseline=5, target=2, minimum_observations=2
            )
        )
    )
    ImprovementTriage().triage(item)
    coordinator = ImprovementCoordinator(backlog, max_active_work_items=1)
    assert coordinator.activate_next() is item
    work = coordinator.create_work_item(item, "implement fix", capabilities=["code.edit"])
    assert work is not None
    work.status = WorkStatus.COMPLETED
    assert not coordinator.begin_verification(item)
    coordinator.record_review(item, ReviewVerdict.COMPLETE)
    coordinator.record_validation(item, passed=True)
    assert coordinator.begin_verification(item)
    assert item.status is ImprovementStatus.VERIFYING
    assert coordinator.observe(item, 2) is ImprovementStatus.VERIFYING
    assert coordinator.observe(item, 1) is ImprovementStatus.VERIFIED


def test_backlog_round_trip_preserves_entities() -> None:
    backlog = ImprovementBacklog()
    original = backlog.add(improvement())
    ImprovementTriage().triage(original)

    restored = ImprovementBacklog.load(backlog.dump())

    assert restored.items[original.id].status is ImprovementStatus.READY
    assert restored.items[original.id].evidence[0].reference == "event-1"


def test_exhausted_work_budget_escalates_instead_of_looping() -> None:
    backlog = ImprovementBacklog()
    item = backlog.add(improvement())
    ImprovementTriage().triage(item)
    coordinator = ImprovementCoordinator(backlog)
    coordinator.activate_next()
    work = coordinator.create_work_item(item, "repair", capabilities=["code.edit"])
    assert work is not None
    for number in range(work.budget.max_attempts):
        work.fail(f"failure {number}")

    assert coordinator.refresh(item) is ImprovementStatus.NEEDS_HUMAN
