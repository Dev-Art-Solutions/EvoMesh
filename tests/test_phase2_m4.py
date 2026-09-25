"""Phase 2 M4: stall recovery, evidence-ranked routing, wakeups and restart."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from evomesh.blackboard import Blackboard
from evomesh.config import Settings
from evomesh.contracts import AgentDefinition, AgentStatus, GoalStatus, MindState, now_utc
from evomesh.coordination import (
    DELEGATED_GOAL_KIND,
    CapabilityRegistry,
    ContractNet,
    WorkItem,
    WorkStatus,
)
from evomesh.environment import Environment
from evomesh.events import Event, EventType
from evomesh.goal_manager import STALL_LIMIT, GoalManager
from evomesh.models import MockProvider


def test_a_stalled_goal_pauses_then_runs_again() -> None:
    """Found in review: a stalled goal was never selected again, so an
    agent's standing goal died on its first stall."""
    mind = MindState()
    goal = mind.add_goal("Poll the feed", recurring=True)
    manager = GoalManager(mind)
    now = now_utc()
    manager.refresh(at=now)

    manager.mark_stalled(goal, "no progress", at=now)

    assert goal.status is GoalStatus.STALLED
    assert manager.next_goal(at=now + timedelta(seconds=10)) is None
    manager.refresh(at=now + timedelta(hours=2))
    assert goal.status is GoalStatus.RUNNABLE, "back in line once the pause is over"


def test_a_one_shot_goal_that_keeps_stalling_fails() -> None:
    mind = MindState()
    goal = mind.add_goal("Write the report")
    manager = GoalManager(mind)
    for _ in range(STALL_LIMIT):
        manager.refresh(at=now_utc() + timedelta(days=1))
        manager.mark_stalled(goal, "same failure repeated 3 times")
    assert goal.status is GoalStatus.FAILED


def test_routing_ranks_by_task_specific_history_from_the_blackboard() -> None:
    registry = CapabilityRegistry()
    steady = AgentDefinition(name="Steady", purpose="p", capabilities=["health.verify"])
    flaky = AgentDefinition(name="Flaky", purpose="p", capabilities=["health.verify"])
    for agent in (steady, flaky):
        registry.register(agent)
    board = Blackboard()
    for agent, status in ((flaky, WorkStatus.NEEDS_HUMAN), (steady, WorkStatus.COMPLETED)):
        done = WorkItem(
            parent_goal_id="g",
            type="assistance",
            objective="diagnose",
            required_capabilities=["health.verify"],
            assigned_agent_id=agent.id,
            status=status,
        )
        board.publish_work(done)
    item = WorkItem(
        parent_goal_id="g2",
        type="assistance",
        objective="diagnose",
        required_capabilities=["health.verify"],
    )

    bid = ContractNet(registry).award(item, history=board.work_history())

    assert bid is not None and bid.agent_id == steady.id


async def test_finished_delegated_work_wakes_its_requester(tmp_path: Path) -> None:
    environment = Environment(
        Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations"),
        {"ollama": MockProvider()},
    )
    await environment.start()
    requester = AgentDefinition(name="Asker", purpose="Ask", status=AgentStatus.ACTIVE)
    await environment.register_agent(requester)
    await environment.start_agent(requester.id, start_delay=3600)
    runtime = environment.runtimes[requester.id]
    runtime._wake.clear()  # pyright: ignore[reportPrivateUsage]

    await environment.events.publish(
        Event(EventType.TASK_COMPLETED, "Guardian", agent_id=requester.id, goal_id="g")
    )

    assert runtime._wake.is_set()  # pyright: ignore[reportPrivateUsage]
    await environment.stop()


async def test_restart_closes_delegated_work_nobody_owns_any_more(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    first = Environment(settings, {"ollama": MockProvider()})
    await first.start()
    helper = AgentDefinition(name="Helper", purpose="Help", status=AgentStatus.ACTIVE)
    owned = WorkItem(parent_goal_id="g", objective="owned", status=WorkStatus.ACTIVE)
    orphan = WorkItem(parent_goal_id="g", objective="orphan", status=WorkStatus.ACTIVE)
    helper.mind.add_goal(
        "owned work", kind=DELEGATED_GOAL_KIND, parameters={"work_item_id": owned.id}
    )
    await first.register_agent(helper)
    for work in (owned, orphan):
        first.blackboard.publish_work(work)
    await first._save_blackboard()  # pyright: ignore[reportPrivateUsage]
    await first.stop()

    second = Environment(settings, {"ollama": MockProvider()})
    await second.start()

    assert second.blackboard.work_items[owned.id].status is WorkStatus.ACTIVE
    assert second.blackboard.work_items[orphan.id].status is WorkStatus.CANCELLED
    await second.stop()
