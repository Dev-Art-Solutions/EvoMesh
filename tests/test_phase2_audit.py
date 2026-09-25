"""Phase 2 bypass-audit migrations B-006, B-007, B-008, B-009 and B-017."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evomesh.behaviors import EvolverBehavior
from evomesh.cognition import CycleOutcome
from evomesh.config import Settings
from evomesh.contracts import AgentDefinition, AgentStatus, GoalStatus, MindState
from evomesh.coordination import DELEGATED_GOAL_KIND
from evomesh.environment import Environment
from evomesh.events import Event, EventType
from evomesh.models import MockProvider
from evomesh.progress import ProgressTracker
from tests.fakes import ScriptedValidator, passing
from tests.test_cycles import MUTATION, evolving
from tests.test_improvement_control import control


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("ACTIVE = True\n", encoding="utf-8")
    return root


def settings(tmp_path: Path) -> Settings:
    return Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")


def test_b006_no_creation_path_accepts_a_dependency_on_nothing() -> None:
    mind = MindState()
    with pytest.raises(ValueError, match="unknown goal"):
        mind.add_goal("waits", dependency_goal_ids=["missing"])
    assert not mind.goals


async def test_b007_every_dependant_of_a_finished_goal_is_unblocked_once(
    tmp_path: Path,
) -> None:
    environment = Environment(settings(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="Planner", purpose="Plan", status=AgentStatus.ACTIVE)
    base = agent.mind.add_goal("gather")
    first = agent.mind.add_goal("draft", dependency_goal_ids=[base.id])
    second = agent.mind.add_goal("review", dependency_goal_ids=[base.id])
    await environment.register_agent(agent)
    agent.mind.open_goals()  # both dependants are BLOCKED now
    base.status = GoalStatus.DONE

    completed = Event(EventType.GOAL_COMPLETED, "bdi", agent_id=agent.id, goal_id=base.id)
    await environment.events.publish(completed)

    unblocked = [
        event.goal_id
        for event in environment.events.history
        if event.type is EventType.GOAL_UNBLOCKED
    ]
    assert sorted(unblocked) == sorted([first.id, second.id])
    await environment.stop()


async def test_b008_a_task_is_delegated_as_a_routed_work_item(tmp_path: Path) -> None:
    environment = Environment(settings(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    sender = AgentDefinition(name="Sender", purpose="Send", status=AgentStatus.ACTIVE)
    helper = AgentDefinition(
        name="Fetcher", purpose="Fetch", status=AgentStatus.ACTIVE, capabilities=["web.fetch"]
    )
    for agent in (sender, helper):
        await environment.register_agent(agent)
        await environment.start_agent(agent.id, start_delay=3600)
    delegate = environment._make_delegate_work(sender.id)  # pyright: ignore[reportPrivateUsage]

    answer = await delegate("web.fetch", "Fetch the pricing page")

    work = next(iter(environment.blackboard.work_items.values()))
    assert work.requester_agent_id == sender.id
    assert work.assigned_agent_id == helper.id
    assert f"work.{work.id}.result" in answer
    for _ in range(50):
        if any(goal.kind == DELEGATED_GOAL_KIND for goal in helper.mind.goals):
            break
        await asyncio.sleep(0.02)
    assert any(goal.kind == DELEGATED_GOAL_KIND for goal in helper.mind.goals)
    with pytest.raises(LookupError, match="capability"):
        await delegate("mt5.execute", "Buy gold")
    await environment.stop()


async def test_b009_a_human_objective_opens_a_tracked_improvement(
    tmp_path: Path, project: Path
) -> None:
    evolver, context, _ = await evolving(
        tmp_path, project, [MUTATION], ScriptedValidator([passing()])
    )
    context.definition.mind.goals.clear()
    context.definition.mind.add_goal("make the console faster", priority=1)
    plane, _ = control()
    context.services["improvements"] = plane

    await EvolverBehavior(auto_validate=True).cycle(context)

    state = await evolver.pipeline_state()
    item = plane.backlog.items[state["improvement_id"]]
    assert item.source == "human_request"
    assert state["work_item_id"] in item.work_item_ids


def test_b017_structural_progress_is_progress_even_with_repeated_text() -> None:
    tracker = ProgressTracker(failure_threshold=3)
    from evomesh.contracts import Goal

    goal = Goal(description="Index files")
    failure = CycleOutcome.failed("timeout reading batch")
    signals = [tracker.observe(goal, failure, (steps,)).stalled for steps in range(6)]
    assert not any(signals), "each cycle completed one more step"
    same = [tracker.observe(goal, failure, (9,)).stalled for _ in range(3)]
    assert same == [False, False, True]
