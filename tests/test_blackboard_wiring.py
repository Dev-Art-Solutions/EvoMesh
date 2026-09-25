"""The blackboard as live, persisted shared state, and delegated work that
actually comes back as a structured result."""

from __future__ import annotations

import asyncio
from pathlib import Path

from evomesh.blackboard import ArtifactRecord, Blackboard, WorldFact
from evomesh.contracts import AgentDefinition, AgentStatus, Belief
from evomesh.coordination import DELEGATED_GOAL_KIND, Performative, WorkItem, WorkStatus
from evomesh.environment import Environment
from evomesh.events import Event, EventType
from evomesh.harness_queue import HarnessJob
from evomesh.models import MockProvider
from tests.test_bdi import settings_for


def test_blackboard_round_trips_and_stays_bounded() -> None:
    board = Blackboard(max_facts=2, max_work=2)
    for index in range(3):
        board.publish_fact(WorldFact(key=f"k{index}", value=index, source="t"))
    done = WorkItem(parent_goal_id="g", objective="old", status=WorkStatus.COMPLETED)
    board.publish_work(done)
    board.publish_work(WorkItem(parent_goal_id="g", objective="a"))
    board.publish_work(WorkItem(parent_goal_id="g", objective="b"))
    board.publish_artifact(ArtifactRecord(key="a", path="/x", source="t"))

    restored = Blackboard()
    restored.load(board.dump())

    assert list(restored.facts) == ["k1", "k2"]
    assert done.id not in restored.work_items, "finished work is dropped before open work"
    assert len(restored.work_items) == 2
    assert restored.projection()["Artifacts"] == "- a: /x (source: t)"


async def test_a_revised_belief_becomes_a_persisted_shared_fact(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="Watcher", purpose="Watch", status=AgentStatus.ACTIVE)
    await environment.register_agent(agent)
    agent.mind.revise([Belief(key="market.open", statement="yes")])
    agent.mind.revise([Belief(key="inbox.human", statement="hi")])

    for key in ("market.open", "inbox.human"):
        await environment.events.publish(
            Event(EventType.BELIEF_CHANGED, "bdi", agent.id, payload={"key": key})
        )

    assert environment.blackboard.facts["Watcher.market.open"].value == "yes"
    assert "Watcher.inbox.human" not in environment.blackboard.facts
    assert "Watcher.market.open = yes" in environment._world_snapshot()  # pyright: ignore[reportPrivateUsage]
    await environment.stop()

    again = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await again.start()
    assert again.blackboard.facts["Watcher.market.open"].value == "yes"
    await again.stop()


async def test_a_harness_job_publishes_what_it_wrote(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    job = HarnessJob(number=7, objective="write", root=tmp_path, agent_id="", notify=False)
    environment.harness_sessions[7] = [{"kind": "write", "path": "report.md"}]

    await environment._deliver_harness(job)  # pyright: ignore[reportPrivateUsage]

    artifact = environment.blackboard.artifacts["console:report.md"]
    assert artifact.path == str(tmp_path / "report.md")
    assert artifact.metadata["job"] == 7
    await environment.stop()


async def _until(condition, within: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_a_stall_is_diagnosed_by_the_guardian_and_the_result_comes_back(
    tmp_path: Path,
) -> None:
    provider = MockProvider()
    environment = Environment(settings_for(tmp_path), {"ollama": provider})
    await environment.start()
    stuck = AgentDefinition(name="Stuck", purpose="Poll", status=AgentStatus.ACTIVE)
    goal = stuck.mind.add_goal("Poll the feed")
    await environment.register_agent(stuck)
    await environment.start_agent(stuck.id, start_delay=3600)
    guardian = environment.registry.get("guardian")
    await environment.start_agent(guardian.id, start_delay=3600)
    calls_before = len(provider.calls)

    await environment.events.publish(
        Event(
            EventType.AGENT_STALLED,
            "progress_tracker",
            agent_id=stuck.id,
            goal_id=goal.id,
            payload={"reason": "same failure repeated 3 times"},
        )
    )
    work = next(iter(environment.blackboard.work_items.values()))
    assert work.assigned_agent_id == guardian.id
    assert await _until(lambda: any(g.kind == DELEGATED_GOAL_KIND for g in guardian.mind.goals)), (
        "the guardian accepted the delegated diagnosis"
    )
    assert await _until(lambda: work.status is WorkStatus.ACTIVE)

    for _ in range(3):
        await environment.cycle_agent("Guardian")

    assert work.status is WorkStatus.COMPLETED
    assert "same failure repeated 3 times" in str(
        environment.blackboard.facts[f"work.{work.id}.result"].value
    )
    completed = [e for e in environment.events.history if e.type is EventType.TASK_COMPLETED]
    assert completed and completed[-1].agent_id == stuck.id
    runtime = environment.runtimes[stuck.id]
    assert await _until(
        lambda: any(
            m.performative == Performative.RESULT.value
            for m in runtime._inbox  # pyright: ignore[reportPrivateUsage]
        )
    ), "the stalled agent received the diagnosis as a RESULT"
    assert len(provider.calls) == calls_before, "diagnosis needed no model call"
    await environment.stop()
