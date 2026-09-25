from __future__ import annotations

from evomesh.cognition import CycleOutcome
from evomesh.contracts import Goal, GoalStatus, LearnedProcedure, MemoryEpisode, MindState
from evomesh.events import Event, EventBus, EventType
from evomesh.progress import ProgressTracker


async def test_event_bus_dispatches_in_order_and_bounds_history() -> None:
    bus = EventBus(max_history=2)
    handled: list[str] = []
    bus.subscribe(EventType.GOAL_UNBLOCKED, lambda event: handled.append(event.goal_id))

    for goal_id in ("a", "b", "c"):
        await bus.publish(Event(EventType.GOAL_UNBLOCKED, "test", goal_id=goal_id))

    assert handled == ["a", "b", "c"]
    assert [event.goal_id for event in bus.history] == ["b", "c"]


def test_progress_tracker_marks_repeated_failure_stalled() -> None:
    goal = Goal(description="Recover provider")
    tracker = ProgressTracker(failure_threshold=3)
    outcome = CycleOutcome.failed("connection refused")

    assert not tracker.observe(goal, outcome).stalled
    assert not tracker.observe(goal, outcome).stalled
    signal = tracker.observe(goal, outcome)

    assert signal.stalled
    assert signal.repeats == 3
    assert "same failure" in signal.reason


def test_progress_tracker_does_not_confuse_repeated_real_work_with_a_stall() -> None:
    goal = Goal(description="Index files")
    tracker = ProgressTracker(no_progress_threshold=2)
    outcome = CycleOutcome(summary="indexed batch", step="index next batch", worked=True)

    tracker.observe(goal, outcome)
    assert not tracker.observe(goal, outcome).stalled


def test_memory_forms_are_structurally_separate_and_bounded() -> None:
    mind = MindState()
    mind.remember("provider is ready")
    for number in range(3):
        mind.record_episode(
            MemoryEpisode(kind="cycle", summary=f"cycle {number}"), keep=2
        )
    mind.remember_procedure(
        LearnedProcedure(name="health-check", trigger="provider unhealthy", steps=["ping"])
    )

    assert [episode.summary for episode in mind.episodes] == ["cycle 1", "cycle 2"]
    assert mind.beliefs[0].statement == "provider is ready"
    assert mind.procedures["health-check"].steps == ["ping"]
    assert GoalStatus.STALLED.value == "stalled"
