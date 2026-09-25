from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from evomesh.agents import AgentRuntime
from evomesh.blackboard import ArtifactRecord, Blackboard, WorldFact
from evomesh.contracts import AgentDefinition, AgentPhase, AgentRuntimeState, now_utc
from evomesh.coordination import (
    CapabilityRegistry,
    ContractNet,
    Performative,
    WorkBudget,
    WorkItem,
    WorkStatus,
    semantic_message,
)
from evomesh.memory import AgentMemory
from evomesh.messaging import MessageBus
from evomesh.models import MockProvider
from evomesh.storage import SQLiteRepository


def agent(name: str, capabilities: list[str]) -> AgentDefinition:
    return AgentDefinition(name=name, purpose="test", capabilities=capabilities)


def test_capability_registry_requires_every_capability() -> None:
    registry = CapabilityRegistry()
    coder = agent("Coder", ["code.read", "code.edit", "tests.run"])
    reader = agent("Reader", ["code.read"])
    registry.register(reader)
    registry.register(coder)

    assert registry.candidates(["code.read", "code.edit"]) == [coder]


def test_contract_net_prefers_lower_load_then_assigns() -> None:
    registry = CapabilityRegistry()
    busy = agent("Busy", ["code.edit"])
    idle = agent("Idle", ["code.edit"])
    registry.register(busy)
    registry.register(idle)
    existing = WorkItem(parent_goal_id="g", objective="other", assigned_agent_id=busy.id)
    existing.status = WorkStatus.ACTIVE
    item = WorkItem(
        parent_goal_id="g", objective="fix bug", required_capabilities=["code.edit"]
    )
    states = {
        busy.id: AgentRuntimeState(agent_id=busy.id, phase=AgentPhase.ACTING),
        idle.id: AgentRuntimeState(agent_id=idle.id, phase=AgentPhase.IDLE),
    }

    bid = ContractNet(registry).award(item, states=states, active_work=[existing])

    assert bid is not None and bid.agent_id == idle.id
    assert item.assigned_agent_id == idle.id
    assert item.status is WorkStatus.ASSIGNED


def test_contract_net_can_exclude_the_stalled_agent_during_reassignment() -> None:
    registry = CapabilityRegistry()
    stalled = agent("Stalled", ["research"])
    helper = agent("Helper", ["research"])
    registry.register(stalled)
    registry.register(helper)
    item = WorkItem(
        parent_goal_id="g", objective="diagnose", required_capabilities=["research"]
    )

    bid = ContractNet(registry).award(item, exclude_agent_ids={stalled.id})

    assert bid is not None and bid.agent_id == helper.id


def test_work_item_retries_are_bounded_and_reassignable() -> None:
    item = WorkItem(
        parent_goal_id="g", objective="repair", budget=WorkBudget(max_attempts=2)
    )
    item.assign("first")
    item.fail("timeout")
    assert item.status is WorkStatus.PENDING
    assert item.assigned_agent_id is None
    item.assign("second")
    item.fail("invalid output")
    assert item.status is WorkStatus.NEEDS_HUMAN
    assert item.failure_history == ["timeout", "invalid output"]


def test_contract_net_history_is_capability_and_task_specific() -> None:
    registry = CapabilityRegistry()
    first = agent("First", ["code.edit", "research"])
    second = agent("Second", ["code.edit", "research"])
    registry.register(first)
    registry.register(second)
    item = WorkItem(
        parent_goal_id="g",
        type="implementation",
        objective="fix",
        required_capabilities=["code.edit"],
    )
    history: dict[object, tuple[int, int]] = {
        (first.id, "implementation", "code.edit"): (0, 5),
        (second.id, "implementation", "code.edit"): (5, 0),
        # Unrelated research success must not rescue the first bidder.
        (first.id, "research", "research"): (100, 0),
    }

    bids = ContractNet(registry).bids(item, history=history)

    assert bids[0].agent_id == second.id


def test_semantic_message_has_machine_readable_envelope() -> None:
    message = semantic_message(
        Performative.DELEGATE,
        sender_id="coordinator",
        recipient_id="coder",
        task_id="task-1",
        goal_id="goal-1",
        payload={"objective": "fix"},
    )
    assert message.type == "acl"
    assert message.performative == "delegate"
    assert message.payload == {"objective": "fix"}


def test_blackboard_tracks_provenance_and_omits_expired_facts() -> None:
    board = Blackboard()
    board.publish_fact(WorldFact(key="provider.ready", value=True, source="guardian"))
    board.publish_fact(
        WorldFact(
            key="stale",
            value=True,
            source="test",
            expires_at=now_utc() - timedelta(seconds=1),
        )
    )
    board.publish_artifact(ArtifactRecord(key="report", path="report.md", source="researcher"))

    projection = board.projection()
    assert "provider.ready" in projection["Facts"]
    assert "stale" not in projection["Facts"]
    assert "report.md" in projection["Artifacts"]


def test_blackboard_preserves_conflicting_fact_versions_and_restart() -> None:
    board = Blackboard()
    board.publish_fact(WorldFact(key="provider.ready", value=True, source="a"))
    board.publish_fact(WorldFact(key="provider.ready", value=False, source="b"))

    versions = board.fact_versions("provider.ready")
    assert [(item.source, item.value) for item in versions] == [
        ("a", True),
        ("b", False),
    ]
    current = board.fact("provider.ready")
    assert current is not None and current.source == "b"

    restored = Blackboard()
    restored.load(board.dump())
    assert [(item.source, item.value) for item in restored.fact_versions("provider.ready")] == [
        ("a", True),
        ("b", False),
    ]


async def test_delegation_envelope_creates_goal_without_a_model_call(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    definition = agent("Coder", ["code.edit"])
    bus = MessageBus(repository)
    bus.register("coordinator")
    provider = MockProvider()
    runtime = AgentRuntime(
        definition=definition,
        provider=provider,
        bus=bus,
        repository=repository,
        memory=AgentMemory(tmp_path / "workspace", definition),
    )
    item = WorkItem(
        parent_goal_id="parent", objective="edit module", required_capabilities=["code.edit"]
    )
    message = semantic_message(
        Performative.DELEGATE,
        sender_id="coordinator",
        recipient_id=definition.id,
        task_id=item.id,
        goal_id="parent",
        payload=item.model_dump(mode="json"),
    )

    assert await runtime._handle_acl(message)
    reply = await bus.receive("coordinator", wait_seconds=0.2)

    assert reply.performative == Performative.ACCEPT.value
    assert definition.mind.goals[0].parameters["work_item_id"] == item.id
    assert provider.calls == []
