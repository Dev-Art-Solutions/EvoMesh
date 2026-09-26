"""A human can pause a goal and keep it: it never runs while paused, nothing
but a resume reopens it, and it comes back on its own schedule."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from evomesh.config import Settings
from evomesh.console import ConsoleChannel
from evomesh.contracts import AgentDefinition, AgentStatus, GoalStatus, MindState, now_utc
from evomesh.environment import Environment
from evomesh.goal_manager import PAUSED, GoalManager
from evomesh.models import MockProvider


def test_a_paused_goal_stays_held_when_its_schedule_comes_due() -> None:
    mind = MindState()
    goal = mind.add_goal("Crawl a site", recurring=True, interval_seconds=7200)
    manager = GoalManager(mind)

    assert manager.pause(goal)
    later = now_utc() + timedelta(days=1)
    manager.refresh(at=later)

    assert goal.status is GoalStatus.BLOCKED and goal.blocked_reason == PAUSED
    assert manager.runnable_goals(at=later) == [], "a paused goal is never picked"
    assert goal.interval_seconds == 7200, "its schedule is kept"

    assert manager.resume(goal)
    assert goal.blocked_reason != PAUSED
    assert goal in manager.runnable_goals(at=later)


def test_a_finished_goal_cannot_be_paused_and_a_running_one_resumes_only_if_paused() -> None:
    mind = MindState()
    goal = mind.add_goal("One shot")
    manager = GoalManager(mind)
    manager.cancel(goal, "dropped")

    assert not manager.pause(goal)
    assert not manager.resume(mind.add_goal("Not paused"))


async def test_pause_and_resume_from_the_console(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="Watcher", purpose="Watch", status=AgentStatus.ACTIVE)
    await environment.register_agent(agent)
    goal = agent.mind.add_goal("Crawl every 2h", recurring=True, interval_seconds=7200)
    console = ConsoleChannel(environment)

    paused = await console.route(f"/goal pause Watcher {goal.id}")
    listed = await console.route("/goals Watcher")
    stored = next(a for a in await environment.repository.load_agents() if a.id == agent.id)

    assert "paused" in paused
    assert f"{goal.id} [paused]" in listed
    assert stored.mind.goal(goal.id).blocked_reason == PAUSED, "a restart keeps it paused"

    resumed = await console.route(f"/goal resume Watcher {goal.id}")
    assert "back on its schedule" in resumed
    assert goal.blocked_reason != PAUSED
    await environment.stop()
