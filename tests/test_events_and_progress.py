from __future__ import annotations

from pathlib import Path

from evomesh.cognition import CycleOutcome
from evomesh.config import Settings
from evomesh.contracts import (
    AgentDefinition,
    Goal,
    GoalStatus,
    LearnedProcedure,
    MemoryEpisode,
    MindState,
)
from evomesh.coordination import DELEGATED_GOAL_KIND, WorkItem, WorkStatus
from evomesh.environment import Environment
from evomesh.events import Event, EventBus, EventType
from evomesh.models import MockProvider
from evomesh.progress import ProgressTracker


async def test_event_bus_dispatches_in_order_and_bounds_history() -> None:
    bus = EventBus(max_history=2)
    handled: list[str] = []
    bus.subscribe(EventType.GOAL_UNBLOCKED, lambda event: handled.append(event.goal_id))

    for goal_id in ("a", "b", "c"):
        await bus.publish(Event(EventType.GOAL_UNBLOCKED, "test", goal_id=goal_id))

    assert handled == ["a", "b", "c"]
    assert [event.goal_id for event in bus.history] == ["b", "c"]


async def test_event_bus_filters_irrelevant_events_before_dispatch() -> None:
    bus = EventBus()
    handled: list[str] = []
    bus.subscribe(
        EventType.MESSAGE_RECEIVED,
        lambda event: handled.append(event.agent_id),
        predicate=lambda event: event.agent_id == "wanted",
    )

    await bus.publish(Event(EventType.MESSAGE_RECEIVED, "test", agent_id="other"))
    await bus.publish(Event(EventType.MESSAGE_RECEIVED, "test", agent_id="wanted"))

    assert handled == ["wanted"]


async def test_event_bus_coalesces_an_immediate_duplicate() -> None:
    bus = EventBus(dedup_window_seconds=1.0)
    handled: list[str] = []
    bus.subscribe(EventType.GOAL_UNBLOCKED, lambda event: handled.append(event.goal_id))
    event = Event(EventType.GOAL_UNBLOCKED, "test", goal_id="goal-1")

    await bus.publish(event)
    await bus.publish(event)

    assert handled == ["goal-1"]
    assert len(bus.history) == 1


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
        mind.record_episode(MemoryEpisode(kind="cycle", summary=f"cycle {number}"), keep=2)
    mind.remember_procedure(
        LearnedProcedure(name="health-check", trigger="provider unhealthy", steps=["ping"])
    )

    assert [episode.summary for episode in mind.episodes] == ["cycle 1", "cycle 2"]
    assert mind.beliefs[0].statement == "provider is ready"
    assert mind.procedures["health-check"].steps == ["ping"]
    assert GoalStatus.STALLED.value == "stalled"


def test_progress_tracker_signals_a_stall_once_not_every_cycle_after() -> None:
    goal = Goal(description="Poll a feed", interval_seconds=60)
    tracker = ProgressTracker(failure_threshold=3)
    outcome = CycleOutcome.failed("connection refused")

    signals = [tracker.observe(goal, outcome).stalled for _ in range(6)]

    assert signals == [False, False, True, False, False, False]


async def test_a_repeated_stall_does_not_delegate_the_same_help_twice(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    stalled = AgentDefinition(name="Stalled", purpose="Poll a feed")
    await environment.register_agent(stalled)
    event = Event(
        EventType.AGENT_STALLED,
        "progress_tracker",
        agent_id=stalled.id,
        goal_id="goal-1",
        payload={"reason": "same failure repeated 3 times"},
    )

    await environment.events.publish(event)
    await environment.events.publish(event)

    assistance = [
        item for item in environment.blackboard.work_items.values() if item.type == "assistance"
    ]
    assert len(assistance) == 1
    await environment.stop()


async def test_assistance_causation_loop_escalates_without_new_work(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    stalled = AgentDefinition(name="Looping", purpose="Help", capabilities=["health.verify"])
    origin = WorkItem(
        parent_goal_id="parent",
        objective="diagnose",
        assigned_agent_id=stalled.id,
        causation_chain=[stalled.id],
        delegation_depth=1,
    )
    origin.status = WorkStatus.ACTIVE
    goal = stalled.mind.add_goal(
        "delegated diagnosis",
        kind=DELEGATED_GOAL_KIND,
        parameters={"work_item_id": origin.id},
    )
    await environment.register_agent(stalled)
    environment.blackboard.publish_work(origin)

    await environment.events.publish(
        Event(
            EventType.AGENT_STALLED,
            "progress_tracker",
            agent_id=stalled.id,
            goal_id=goal.id,
            payload={"reason": "reciprocal help"},
        )
    )

    assert origin.status is WorkStatus.NEEDS_HUMAN
    assert list(environment.blackboard.work_items) == [origin.id]
    await environment.stop()


async def test_a_goal_dropped_during_a_cycle_stays_dropped(tmp_path: Path) -> None:
    """Found live: /goal drop during a minute-long model call was undone when
    the cycle finished and set the goal it had started on back to ACTIVE."""
    from evomesh.cognition import CycleContext, CycleOutcome
    from evomesh.contracts import AgentStatus

    environment = Environment(
        Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations"),
        {"ollama": MockProvider()},
    )
    await environment.start()
    agent = AgentDefinition(name="Busy", purpose="Work", status=AgentStatus.ACTIVE)
    goal = agent.mind.add_goal("Do the thing")
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)
    runtime = environment.runtimes[agent.id]

    class DropsMidCycle:
        name = "drops"

        async def cycle(self, context: CycleContext) -> CycleOutcome:
            goal.status = GoalStatus.FAILED  # the human's /goal drop
            return CycleOutcome(summary="did a step", step="a step", worked=True)

    runtime.behavior = DropsMidCycle()  # type: ignore[assignment]
    await runtime.run_cycle()

    assert goal.status is GoalStatus.FAILED
    await environment.stop()


def test_a_mind_forgets_its_oldest_finished_goals_but_not_referenced_ones() -> None:
    from evomesh.contracts import KEEP_CLOSED_GOALS

    mind = MindState()
    anchor = mind.add_goal("depended on")
    anchor.status = GoalStatus.DONE
    mind.add_goal("waits", dependency_goal_ids=[anchor.id])
    for number in range(KEEP_CLOSED_GOALS + 10):
        mind.add_goal(f"finished {number}").status = GoalStatus.DONE
    mind.add_goal("one more")

    closed = [goal for goal in mind.goals if goal.status is GoalStatus.DONE]
    assert len(closed) == KEEP_CLOSED_GOALS + 1, "the referenced one is kept on top"
    assert anchor in mind.goals
    assert "finished 0" not in {goal.description for goal in mind.goals}
