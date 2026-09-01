from pathlib import Path

import pytest

from evomesh.agents import AgentRegistry
from evomesh.architect import ArchitectInterview
from evomesh.config import AgentModelSettings, Settings
from evomesh.contracts import AgentDefinition, AgentStatus, FilesystemGrant, Message
from evomesh.environment import Environment, HealthState
from evomesh.evolution import CandidateWorkspace, FileMutation, GenerationSupervisor
from evomesh.messaging import MessageBus
from evomesh.models import MockProvider
from evomesh.permissions import FilesystemPolicy, PermissionDeniedError
from evomesh.storage import SQLiteRepository


@pytest.fixture
async def repository(tmp_path: Path) -> SQLiteRepository:
    item = SQLiteRepository(tmp_path / "evomesh.db")
    await item.initialize()
    return item


async def test_message_bus_direct_and_persisted(repository: SQLiteRepository) -> None:
    bus = MessageBus(repository)
    bus.register("receiver")
    message = Message(sender_id="sender", recipient_id="receiver", content="hello")
    await bus.send(message)
    assert await bus.receive("receiver", wait_seconds=0.1) == message
    assert (await repository.load_messages())[0].content == "hello"


async def test_broadcast_excludes_sender(repository: SQLiteRepository) -> None:
    bus = MessageBus(repository)
    bus.register("sender")
    bus.register("receiver")
    await bus.send(Message(sender_id="sender", recipient_id=None, content="news"))
    assert (await bus.receive("receiver", wait_seconds=0.1)).content == "news"


def test_registry_rejects_duplicate_names() -> None:
    registry = AgentRegistry()
    registry.register(AgentDefinition(name="Scout", purpose="Observe"))
    with pytest.raises(ValueError):
        registry.register(AgentDefinition(name="scout", purpose="Duplicate"))


async def test_permission_matching_and_traversal(
    repository: SQLiteRepository, tmp_path: Path
) -> None:
    policy = FilesystemPolicy(repository)
    root = tmp_path / "allowed"
    await policy.grant(FilesystemGrant(agent_id="a", path=str(root), read=True))
    assert await policy.require("a", root / "child.txt", "read") == (root / "child.txt").resolve()
    with pytest.raises(PermissionDeniedError):
        await policy.require("a", root.parent / "outside.txt", "read")
    with pytest.raises(PermissionDeniedError):
        await policy.require("a", root / "child.txt", "write")


def test_architect_multiturn_candidate() -> None:
    interview = ArchitectInterview()
    assert "called" in interview.begin("Create a research agent")
    answers = ["Researcher", "Research local papers", "Never modify papers", "none"]
    for answer in answers:
        interview.answer(answer)
    interview.answer("Markdown.Read")
    final = interview.answer("ollama:qwen3:8b")
    assert "ready" in final
    candidate = interview.confirm()
    assert candidate.name == "Researcher"
    assert candidate.skills == ["Markdown.Read"]
    assert candidate.provider == "ollama"
    assert candidate.model_name == "qwen3:8b"
    assert candidate.status == "active"


async def test_environment_boot_and_restart(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    first = Environment(settings, {"ollama": MockProvider()})
    await first.start()
    assert first.health_state == HealthState.READY
    assert len(first.registry.all()) == 4
    created = AgentDefinition(
        name="Persistent", purpose="Survive restart", status=AgentStatus.ACTIVE
    )
    await first.register_agent(created)
    await first.stop()

    second = Environment(settings, {"ollama": MockProvider()})
    await second.start()
    assert second.registry.get("Persistent").id == created.id
    assert len(second.registry.all()) == 5
    await second.stop()


async def test_each_agent_uses_its_configured_model(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    provider = MockProvider(["specialist response"])
    environment = Environment(settings, {"ollama": provider})
    await environment.start()
    specialist = AgentDefinition(
        name="Specialist", purpose="Use its own model", model_name="qwen3:14b"
    )
    await environment.register_agent(specialist)
    await environment.start_agent(specialist.id)
    await environment.send_message(
        Message(sender_id="human", recipient_id=specialist.id, content="hello")
    )
    response = await environment.bus.receive("human", wait_seconds=1)
    assert response.content == "specialist response"
    assert provider.calls[-1]["model"] == "qwen3:14b"
    updated = await environment.configure_agent_model(
        specialist.id, "ollama", "qwen3:32b"
    )
    assert updated.model_name == "qwen3:32b"
    assert specialist.id in environment.runtimes
    await environment.stop()


async def test_system_agent_model_settings_override_persisted_values(tmp_path: Path) -> None:
    data_path = tmp_path / "data.db"
    first = Environment(
        Settings(data_path=data_path, generation_path=tmp_path / "generations"),
        {"ollama": MockProvider()},
    )
    await first.start()
    await first.configure_agent_model("guardian", "ollama", "old-model")
    await first.stop()

    configured = Settings(
        data_path=data_path,
        generation_path=tmp_path / "generations",
        system_agents={
            "guardian": AgentModelSettings(provider="ollama", model="guardian-model")
        },
    )
    restarted = Environment(configured, {"ollama": MockProvider()})
    await restarted.start()
    guardian = restarted.registry.get("guardian")
    assert guardian.provider == "ollama"
    assert guardian.model_name == "guardian-model"
    await restarted.stop()


async def test_builtin_file_skill_enforces_grant(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    target = tmp_path / "notes" / "readme.md"
    await environment.grant_access(
        FilesystemGrant(agent_id="architect", path=str(tmp_path / "notes"), write=True)
    )
    await environment.skills.invoke(
        "architect", "Markdown.Write", {"path": str(target), "content": "# EvoMesh"}
    )
    assert await environment.skills.invoke(
        "architect", "Markdown.Read", {"path": str(target)}
    ) == "# EvoMesh"
    await environment.stop()


async def test_candidate_workspace_is_isolated(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("ACTIVE = True", encoding="utf-8")
    workspace = CandidateWorkspace(source, tmp_path / "runtime")
    candidate = await workspace.create("Improve health reporting")
    (candidate.path / "app.py").write_text("ACTIVE = False", encoding="utf-8")
    assert (source / "app.py").read_text(encoding="utf-8") == "ACTIVE = True"
    assert GenerationSupervisor(tmp_path / "runtime").metadata()["active"] == 1


def test_mutation_cannot_escape_candidate(tmp_path: Path) -> None:
    mutation = FileMutation(relative_path=Path("../active.py"), content="unsafe")
    with pytest.raises(ValueError):
        mutation.target(tmp_path / "candidate")
