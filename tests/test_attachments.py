"""Attaching a file to an agent chat, and an agent handing one back.

Two directions, one seam: ConsoleChannel.attach() is what both the Control
Center's chat panel and Telegram's inbound file handling call once a file
already exists on disk, and cognition.extract_file_references() is what both
the desktop chat panel's link rendering and Telegram's outbound upload use to
find what an agent's own reply wants to hand back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from evomesh.cognition import extract_file_references
from evomesh.config import Settings
from evomesh.console import MAX_ATTACHMENT_BYTES, ConsoleChannel
from evomesh.contracts import AgentDefinition, AgentStatus, TelegramSettings
from evomesh.environment import Environment
from evomesh.models import MockProvider
from evomesh.telegram import TelegramChannel


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        data_path=tmp_path / "data.db",
        generation_path=tmp_path / "generations",
        workspace_path=tmp_path / "workspace",
    )


# -- extract_file_references ---------------------------------------------


def test_extract_file_references_finds_one_line() -> None:
    reply = "Here you go.\nFILE: playground/report.csv\nLet me know if you want more."
    assert extract_file_references(reply) == ["playground/report.csv"]


def test_extract_file_references_finds_several() -> None:
    reply = "FILE: a.txt\nsome prose in between\nFILE: b.txt"
    assert extract_file_references(reply) == ["a.txt", "b.txt"]


def test_extract_file_references_ignores_prose_mentioning_a_file() -> None:
    reply = "I read the file report.csv but did not create a new one."
    assert extract_file_references(reply) == []


def test_extract_file_references_strips_reasoning_first() -> None:
    reply = "<think>FILE: not-a-real-answer.txt</think>\nFILE: real.txt"
    assert extract_file_references(reply) == ["real.txt"]


def test_extract_file_references_accepts_document_write_output_formats() -> None:
    # document_write produces real binary deliverables on request (a PDF
    # report, an xlsx workbook) -- these must reach a human the same way a
    # FILE: report.csv already does, not be silently dropped as "the harness
    # improvising a binary write" the way an image/video/archive still is.
    reply = "Here you go.\nFILE: news.pdf"
    assert extract_file_references(reply) == ["news.pdf"]


def test_extract_file_references_still_rejects_unrelated_binary_formats() -> None:
    reply = "FILE: photo.png"
    assert extract_file_references(reply) == []


# -- ConsoleChannel.attach --------------------------------------------------


async def test_attach_lands_in_the_agents_own_playground(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="Writer", purpose="Write", status=AgentStatus.ACTIVE)
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)
    console = ConsoleChannel(environment)
    await console.route('/chat "Writer"')
    source = tmp_path / "notes.txt"
    source.write_text("hello", encoding="utf-8")

    reply = await console.attach(source)

    assert reply == "Writer> Mock response"
    landed = environment.memory_for(agent).playground_path / "notes.txt"
    assert landed.read_text(encoding="utf-8") == "hello"
    await environment.stop()


async def test_attach_nudges_toward_document_read_for_a_supported_extension(
    tmp_path: Path,
) -> None:
    """document_read only ever reaches the model through
    bdi._respond_through_harness, and even then nothing else ties "a file
    arrived" to "read it" -- left to inference alone, a small local model
    is exactly the kind that answers "thanks, got your file" without
    opening it. The inbox message itself has to say so."""
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="Writer", purpose="Write", status=AgentStatus.ACTIVE)
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)
    console = ConsoleChannel(environment)
    await console.route('/chat "Writer"')
    source = tmp_path / "report.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")

    await console.attach(source)

    runtime = environment.runtimes[agent.id]
    last_inbox = runtime._inbox[-1]
    assert "document_read" in last_inbox.content
    assert '"path": "report.csv"' in last_inbox.content
    await environment.stop()


async def test_attach_does_not_nudge_for_an_unsupported_extension(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="Writer", purpose="Write", status=AgentStatus.ACTIVE)
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)
    console = ConsoleChannel(environment)
    await console.route('/chat "Writer"')
    source = tmp_path / "notes.txt"
    source.write_text("hello", encoding="utf-8")

    await console.attach(source)

    runtime = environment.runtimes[agent.id]
    last_inbox = runtime._inbox[-1]
    assert "document_read" not in last_inbox.content
    await environment.stop()


async def test_a_relative_file_reference_in_a_reply_becomes_absolute(tmp_path: Path) -> None:
    """The desktop chat panel and Telegram both read this reply text with no
    way of their own to know what "the agent's workspace" resolves to --
    the backend is the one place that mapping lives, so it rewrites the
    relative path itself before returning."""
    environment = Environment(
        settings_for(tmp_path), {"ollama": MockProvider(["FILE: report.csv"])}
    )
    await environment.start()
    agent = AgentDefinition(name="Writer", purpose="Write", status=AgentStatus.ACTIVE)
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)
    console = ConsoleChannel(environment)
    await console.route('/chat "Writer"')

    reply = await console.route("send it over")

    expected = environment.memory_for(agent).playground_path / "report.csv"
    assert f"FILE: {expected}" in reply
    await environment.stop()


async def test_attach_lands_in_a_configured_project_path(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    project = tmp_path / "real-project"
    project.mkdir()
    agent = AgentDefinition(
        name="Coder", purpose="Code", status=AgentStatus.ACTIVE, project_path=str(project)
    )
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)
    console = ConsoleChannel(environment)
    await console.route('/chat "Coder"')
    source = tmp_path / "spec.md"
    source.write_text("spec", encoding="utf-8")

    await console.attach(source)

    assert (project / "spec.md").read_text(encoding="utf-8") == "spec"
    # It did not also land in the playground -- project_path replaces it.
    assert not (environment.memory_for(agent).playground_path / "spec.md").exists()
    await environment.stop()


async def test_attach_lands_in_the_mesh_directory_for_a_system_agent(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start(start_agent_loops=True)
    console = ConsoleChannel(environment)
    await console.route("/chat guardian")
    source = tmp_path / "incident.log"
    source.write_text("boom", encoding="utf-8")

    await console.attach(source)

    assert (environment.project_root / "incident.log").read_text(encoding="utf-8") == "boom"
    await environment.stop()


async def test_attach_never_overwrites_a_same_named_file(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="Writer", purpose="Write", status=AgentStatus.ACTIVE)
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)
    console = ConsoleChannel(environment)
    await console.route('/chat "Writer"')
    playground = environment.memory_for(agent).playground_path

    first = tmp_path / "notes.txt"
    first.write_text("first", encoding="utf-8")
    await console.attach(first)
    second = tmp_path / "notes.txt"  # same name, different content, sent again
    second.write_text("second", encoding="utf-8")
    await console.attach(second)

    assert (playground / "notes.txt").read_text(encoding="utf-8") == "first"
    assert (playground / "notes-2.txt").read_text(encoding="utf-8") == "second"
    await environment.stop()


async def test_attach_refuses_a_missing_file(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="Writer", purpose="Write", status=AgentStatus.ACTIVE)
    await environment.register_agent(agent)
    console = ConsoleChannel(environment)
    await console.route('/chat "Writer"')

    reply = await console.attach(tmp_path / "does-not-exist.txt")

    assert "No such file" in reply
    await environment.stop()


async def test_attach_refuses_an_oversized_file(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="Writer", purpose="Write", status=AgentStatus.ACTIVE)
    await environment.register_agent(agent)
    console = ConsoleChannel(environment)
    await console.route('/chat "Writer"')
    huge = tmp_path / "huge.bin"
    with huge.open("wb") as handle:
        handle.seek(MAX_ATTACHMENT_BYTES)
        handle.write(b"\0")

    reply = await console.attach(huge)

    assert "too large" in reply
    await environment.stop()


async def test_attach_requires_an_agent_to_be_selected(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    source = tmp_path / "notes.txt"
    source.write_text("hello", encoding="utf-8")

    reply = await console.attach(source)

    assert "Select an agent" in reply
    await environment.stop()


async def test_the_command_form_matches_the_direct_call(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="Writer", purpose="Write", status=AgentStatus.ACTIVE)
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)
    console = ConsoleChannel(environment)
    await console.route('/chat "Writer"')
    source = tmp_path / "notes.txt"
    source.write_text("hello", encoding="utf-8")

    reply = await console.route(f'/attach "{source}"')

    assert reply == "Writer> Mock response"
    await environment.stop()


# -- Telegram, both directions ---------------------------------------------


class FakeTelegramFiles:
    """Adds getFile/file-download/sendDocument to the shape test_publishing's
    FakeTelegram already exercises for text -- a separate, smaller fake here
    rather than growing that one with attachment-only branches it does not
    otherwise need."""

    def __init__(self, updates: list[dict[str, Any]], file_bytes: bytes = b"") -> None:
        self.updates = updates
        self.sent: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.file_bytes = file_bytes
        self.channel: TelegramChannel | None = None
        self._served = False

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if "/file/" in request.url.path:
            return httpx.Response(200, content=self.file_bytes)
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            return self._ok({"username": "evomesh_test_bot"})
        if method == "sendMessage":
            payload = json.loads(request.content or b"{}")
            self.sent.append(payload)
            return self._ok({"message_id": len(self.sent)})
        if method == "sendDocument":
            self.documents.append({"chat_id": request.url.params.get("chat_id")})
            return self._ok({"message_id": 1})
        if method == "getFile":
            return self._ok(
                {"file_path": "documents/report.csv", "file_size": len(self.file_bytes)}
            )
        if method == "getUpdates":
            if self._served:
                assert self.channel is not None
                self.channel.stop()
                return self._ok([])
            self._served = True
            return self._ok(self.updates)
        raise AssertionError(f"unexpected Telegram method {method}")

    @staticmethod
    def _ok(result: Any) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": result})


def document_update(chat_id: int, file_id: str, name: str, update_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "document": {"file_id": file_id, "file_name": name},
        },
    }


async def telegram_environment(tmp_path: Path) -> Environment:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="Writer", purpose="Write", status=AgentStatus.ACTIVE)
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)
    return environment


async def test_a_document_sent_to_the_bot_lands_in_the_selected_agents_workspace(
    tmp_path: Path,
) -> None:
    environment = await telegram_environment(tmp_path)
    console_agent = environment.registry.get("Writer")
    fake = FakeTelegramFiles(
        [
            {"update_id": 1, "message": {"chat": {"id": 4242}, "text": '/chat "Writer"'}},
            document_update(4242, "abc123", "report.csv", update_id=2),
        ],
        file_bytes=b"a,b,c\n1,2,3\n",
    )
    channel = TelegramChannel(
        environment,
        TelegramSettings(enabled=True, token="1:abc", allowed_chat_ids=[4242]),
        fake.client(),
    )
    fake.channel = channel

    await channel.run()

    landed = environment.memory_for(console_agent).playground_path / "report.csv"
    assert landed.read_bytes() == b"a,b,c\n1,2,3\n"
    assert any("Writer>" in item["text"] for item in fake.sent)


async def test_an_oversized_telegram_document_is_refused_before_downloading(
    tmp_path: Path,
) -> None:
    environment = await telegram_environment(tmp_path)
    fake = FakeTelegramFiles(
        [
            {"update_id": 1, "message": {"chat": {"id": 4242}, "text": '/chat "Writer"'}},
            document_update(4242, "abc123", "huge.bin", update_id=2),
        ],
        file_bytes=b"x" * (MAX_ATTACHMENT_BYTES + 1),
    )
    channel = TelegramChannel(
        environment,
        TelegramSettings(enabled=True, token="1:abc", allowed_chat_ids=[4242]),
        fake.client(),
    )
    fake.channel = channel

    await channel.run()

    assert any("Error" in item["text"] and "too large" in item["text"] for item in fake.sent)


async def test_a_reply_naming_a_file_uploads_it_as_a_telegram_document(tmp_path: Path) -> None:
    # Built directly (not via telegram_environment) -- the agent's runtime
    # captures its provider at start_agent() time, so the FILE:-returning
    # mock has to be in place before that, not swapped in afterwards.
    environment = Environment(
        settings_for(tmp_path), {"ollama": MockProvider(["FILE: summary.txt"])}
    )
    await environment.start()
    writer = AgentDefinition(name="Writer", purpose="Write", status=AgentStatus.ACTIVE)
    await environment.register_agent(writer)
    await environment.start_agent(writer.id, start_delay=3600)
    playground = environment.memory_for(writer).playground_path
    playground.mkdir(parents=True, exist_ok=True)
    (playground / "summary.txt").write_text("done", encoding="utf-8")
    fake = FakeTelegramFiles(
        [
            {"update_id": 1, "message": {"chat": {"id": 4242}, "text": '/chat "Writer"'}},
            {"update_id": 2, "message": {"chat": {"id": 4242}, "text": "send me the file"}},
        ]
    )
    channel = TelegramChannel(
        environment,
        TelegramSettings(enabled=True, token="1:abc", allowed_chat_ids=[4242]),
        fake.client(),
    )
    fake.channel = channel

    await channel.run()

    assert len(fake.documents) == 1
