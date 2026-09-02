import asyncio
import logging
import sqlite3
from pathlib import Path

import httpx
import pytest

from evomesh.agents import AgentRegistry
from evomesh.architect import ArchitectInterview
from evomesh.config import AgentModelSettings, ModelSettings, ProviderSettings, Settings
from evomesh.contracts import AgentDefinition, AgentStatus, FilesystemGrant, Message
from evomesh.environment import Environment, HealthState
from evomesh.evolution import (
    CandidateValidator,
    CandidateWorkspace,
    Generation,
    GenerationStatus,
    GenerationSupervisor,
    uv_executable,
)
from evomesh.harness_tools import ALL_TOOLS, ToolContext, ToolRegistry
from evomesh.messaging import MessageBus
from evomesh.models import MockProvider, OllamaProvider, describe
from evomesh.permissions import FilesystemPolicy, PermissionDeniedError
from evomesh.storage import SQLiteRepository
from tests.fakes import wipe_database


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


def test_architect_drafts_a_candidate_without_asking_questions() -> None:
    interview = ArchitectInterview()
    draft = interview.begin("Create an agent called Researcher that reads markdown papers")
    assert "?" not in draft
    assert interview.candidate is not None
    assert interview.candidate.name == "Researcher"
    assert "Markdown.Read" in interview.candidate.skills
    # The draft already carries the goal its cycle loop will pick up.
    assert interview.candidate.mind.goals[0].description


def test_architect_refines_by_instruction_not_by_questionnaire() -> None:
    interview = ArchitectInterview()
    interview.begin("summarize local research papers")
    interview.refine("name: Researcher")
    interview.refine("ollama:qwen3:8b")
    candidate = interview.confirm()
    assert candidate.name == "Researcher"
    assert (candidate.provider, candidate.model_name) == ("ollama", "qwen3:8b")
    assert candidate.status == "active"


def test_architect_reads_an_explicit_model_from_the_first_sentence() -> None:
    interview = ArchitectInterview()
    interview.begin("watch the git repo, use ollama:qwen3:4b")
    assert interview.candidate is not None
    assert interview.candidate.model_name == "qwen3:4b"
    assert "Git.Status" in interview.candidate.skills


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


async def test_a_generation_cannot_be_written_outside_its_candidate(tmp_path: Path) -> None:
    """Containment moved to the tool that writes, and is still enforced.

    The old FileMutation checked its own path on the way in. Now the harness
    resolves every path against the job root and refuses before opening a file,
    which is the same guarantee closer to the disk.
    """
    root = tmp_path / "candidate"
    (root / "src").mkdir(parents=True)

    result = await ToolRegistry(ALL_TOOLS).invoke(
        ToolContext(root=root, allow_write=True),
        "write",
        {"path": "../active.py", "content": "unsafe"},
    )

    assert result.startswith("DENIED:")
    assert not (tmp_path / "active.py").exists()


async def test_architect_ignores_a_small_model_that_returns_junk() -> None:
    """A 0.5B model answers {"name": "D:/notes", "purpose": "read"}. Reject it."""
    interview = ArchitectInterview()

    async def junk(prompt: str, system: str) -> str:
        return '{"name": "D:/notes", "purpose": "read", "constraints": "no"}'

    await interview.draft(
        "read my markdown notes in D:/notes and write a weekly summary", infer=junk
    )

    assert interview.candidate is not None
    assert interview.candidate.name == "Read Markdown Agent"
    assert "weekly summary" in interview.candidate.purpose
    assert len(interview.candidate.mind.beliefs[0].statement.split()) > 4


async def test_architect_accepts_a_model_answer_that_is_actually_better() -> None:
    interview = ArchitectInterview()

    async def good(prompt: str, system: str) -> str:
        return (
            '{"name": "Notes Summarizer", "purpose": "read the markdown notes in '
            'D:/notes each week and write a summary", "constraints": "never edit the '
            'source notes, only append summaries"}'
        )

    await interview.draft("read my notes in D:/notes and summarize", infer=good)

    assert interview.candidate is not None
    assert interview.candidate.name == "Notes Summarizer"
    assert "each week" in interview.candidate.purpose


async def test_architect_survives_a_model_that_is_down() -> None:
    interview = ArchitectInterview()

    async def broken(prompt: str, system: str) -> str:
        raise RuntimeError("provider not ready")

    summary = await interview.draft("watch the git repository for changes", infer=broken)

    assert "Draft ready" in summary
    assert interview.candidate is not None
    assert "Git.Status" in interview.candidate.skills


async def test_architect_rejects_a_generic_name_from_the_model() -> None:
    """qwen2.5:0.5b answers {"name": "agent"}. The derived name is always better."""
    interview = ArchitectInterview()

    async def lazy(prompt: str, system: str) -> str:
        return (
            '{"name": "agent", "purpose": "read markdown notes and write a weekly '
            'summary", "constraints": "do not modify the source notes at all"}'
        )

    await interview.draft(
        "read my markdown notes in D:/notes and write a weekly summary", infer=lazy
    )

    assert interview.candidate is not None
    assert interview.candidate.name == "Read Markdown Agent"
    # The purpose was a genuine improvement, so that half is kept.
    assert "weekly summary" in interview.candidate.purpose


def test_uv_is_found_in_a_tools_directory_above_the_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The launcher starts EvoMesh with a uv that never reaches PATH.
    monkeypatch.setattr("evomesh.evolution.shutil.which", lambda name: None)
    bundled = tmp_path / ".tools" / "uv" / "bin" / "uv.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("", encoding="utf-8")
    candidate = tmp_path / "EvoMesh" / "generations" / "3"
    candidate.mkdir(parents=True)

    assert uv_executable(candidate) == str(bundled)


def test_uv_on_path_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("evomesh.evolution.shutil.which", lambda name: "C:/uv/uv.exe")

    assert uv_executable(tmp_path) == "C:/uv/uv.exe"


async def test_validation_fails_with_a_readable_reason_when_uv_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing toolchain must read as a failed validation, not a broken mutation."""

    # Stub the lookup rather than the filesystem it walks. pytest's temp tree now
    # lives inside the checkout, so "no .tools/uv above this path" is no longer
    # something a temp directory can promise. Discovery itself is covered above.
    def missing(start: Path) -> str:
        raise FileNotFoundError(f"uv is not on PATH and no bundled copy was found above {start}")

    monkeypatch.setattr("evomesh.evolution.uv_executable", missing)
    candidate = tmp_path / "generations" / "1"
    candidate.mkdir(parents=True)
    generation = Generation(number=1, status=GenerationStatus.CANDIDATE, path=candidate)

    result = await CandidateValidator().validate(generation)

    assert result.passed is False
    assert "uv is not on PATH" in str(result.commands[0]["output"])
    assert (candidate / "validation-result.json").exists()


def test_a_model_error_never_reaches_a_human_as_an_empty_message() -> None:
    """httpx raises timeouts with an empty str(), which read as a broken agent."""
    assert describe(httpx.ReadTimeout("")) == "ReadTimeout"
    assert describe(httpx.ConnectError("connection refused")) == "ConnectError: connection refused"


def test_provider_timeout_comes_from_configuration(tmp_path: Path) -> None:
    settings = Settings(
        data_path=tmp_path / "data.db",
        generation_path=tmp_path / "generations",
        models=ModelSettings(
            default_provider="ollama",
            providers={
                "ollama": ProviderSettings(
                    base_url="http://127.0.0.1:11434", model="qwen3", timeout_seconds=42
                )
            },
        ),
    )

    provider = Environment(settings).providers["ollama"]

    assert isinstance(provider, OllamaProvider)
    assert provider.timeout_seconds == 42


async def test_a_wiped_database_heals_on_the_next_write(
    repository: SQLiteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """A clean sweep of data/ must cost the rows, not the running mesh."""
    agent = AgentDefinition(name="Trader", purpose="Trade", model_name="mock-model")
    await repository.save_agent(agent)
    await asyncio.to_thread(wipe_database, repository.path)

    with caplog.at_level(logging.WARNING, logger="evomesh.storage"):
        await repository.save_agent(agent)

    assert [item.name for item in await repository.load_agents()] == ["Trader"]
    assert "has no schema" in caplog.text


async def test_a_wiped_database_heals_on_the_next_read(repository: SQLiteRepository) -> None:
    await asyncio.to_thread(wipe_database, repository.path)
    # The rows are gone, but the query answers instead of raising.
    assert await repository.load_agents() == []
    assert await repository.load_state("stage") is None


async def test_one_wipe_rebuilds_once_however_many_agents_notice(
    repository: SQLiteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Every agent loop has its own connection, so they all fail together."""
    await asyncio.to_thread(wipe_database, repository.path)
    with caplog.at_level(logging.WARNING, logger="evomesh.storage"):
        await asyncio.gather(*(repository.load_agents() for _ in range(8)))
    assert caplog.text.count("has no schema") == 1


async def test_a_failure_that_is_not_a_wipe_still_raises(repository: SQLiteRepository) -> None:
    """Rebuilding fixes a missing schema and nothing else, so nothing else is swallowed."""
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        await repository._all("SELECT nope FROM agents")
