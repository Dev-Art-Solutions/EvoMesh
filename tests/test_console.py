import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

import evomesh.control
from evomesh.config import Settings
from evomesh.console import ConsoleChannel
from evomesh.contracts import AgentDefinition
from evomesh.control import ControlServer
from evomesh.environment import Environment
from evomesh.models import MockProvider


async def test_console_routes_commands(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    skill_dir = tmp_path / "skills" / "research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: research\ndescription: Look things up before answering.\n---\n\n"
        "Do the research before you answer.\n",
        encoding="utf-8",
    )
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    assert "status: READY" in (await console.route("/status"))
    assert "Agent Architect" in (await console.route("/agents"))
    assert "research: Look things up before answering." in (await console.route("/skills"))
    assert "Do the research before you answer." in (await console.route("/skill show research"))
    assert "mock-specialist" in (await console.route("/models ollama"))
    assert await console.route("/chat architect") == "Talking to Agent Architect."
    agent = AgentDefinition(name="Writer", purpose="Write", model_name="mock-model")
    await environment.register_agent(agent)
    await environment.start_agent(agent.id)
    assert "ollama:mock-specialist" in (
        await console.route('/model "Writer" mock-specialist ollama')
    )
    assert "context window of 32768" in (await console.route('/num-ctx "Writer" 32768'))
    assert "no longer overrides" in (await console.route('/num-ctx "Writer" clear'))
    assert await console.route('/chat "Writer"') == "Talking to Writer."
    assert await console.route("hello") == "Writer> Mock response"
    await environment.stop()


async def test_skill_install_adds_a_skill_from_a_local_file(tmp_path: Path) -> None:
    """The installation mechanism this session was actually asked for: a
    SKILL.md a human already has on disk becomes an installed skill in one
    command, no restart needed to see it in /skills."""
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    source = tmp_path / "shared-skill.md"
    source.write_text(
        "---\nname: wire-a-dead-module\ndescription: Wire an unreachable module in.\n---\n\n"
        "Pick one module from the dead list and give it a real caller.\n",
        encoding="utf-8",
    )

    result = await console.route(f'/skill install "{source}"')

    assert "Installed 'wire-a-dead-module'" in result
    assert "wire-a-dead-module: Wire an unreachable module in." in (
        await console.route("/skills")
    )
    assert "is not a file" in (await console.route("/skill install nowhere.md"))
    await environment.stop()


async def test_goal_add_with_an_interval_sets_recurring_automatically(tmp_path: Path) -> None:
    """The command surface for the cadence feature: a human giving an
    interval_seconds should never also have to remember --recurring, because
    a standing check that permanently fails after max_attempts defeats the
    reason it was given an interval in the first place."""
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    agent = AgentDefinition(name="Watcher", purpose="Watch things")
    await environment.register_agent(agent)

    result = await console.route('/goal add "Watcher" "Check example.com" 5 3600')

    assert "Re-checked every 3600s" in result
    assert "cycle_seconds" in result
    goal = next(iter(agent.mind.goals))
    assert goal.interval_seconds == 3600
    assert goal.recurring is True
    await environment.stop()


async def test_control_server_accepts_commands_and_shutdown(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    shutdown = asyncio.Event()
    server = ControlServer(environment, shutdown, port=0)
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    async def request(command: str) -> dict[str, object]:
        writer.write((json.dumps({"command": command}) + "\n").encode())
        await writer.drain()
        return json.loads(await reader.readline())

    assert (await request("/ping"))["output"] == "EvoMesh control ready"
    assert "status: READY" in str((await request("/status"))["output"])
    assert (await request("/exit"))["shutdown"] is True
    assert shutdown.is_set()
    writer.close()
    await writer.wait_closed()
    await server.stop()
    await environment.stop()


class ExplodingChannel(ConsoleChannel):
    """A handler that raises exactly what the old dispatcher tuple did not list."""

    async def _command_boom(self, parts: list[str]) -> str:
        raise sqlite3.OperationalError("no such table: state")


async def test_control_server_reports_a_failed_command_and_keeps_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing command must reach the Control Center as text, not a dropped socket.

    The dispatcher used to catch a hand-listed tuple of exceptions. A wiped
    database raised sqlite3.OperationalError, which is not a RuntimeError, so
    it escaped, closed the writer, and reached a human as "the mesh control
    connection was lost" for every /evolution status they typed.

    The handler raises here rather than the storage layer: a repository heals a
    wiped database on its own now, and this is a claim about the dispatcher,
    which owes the same answer for whatever any handler raises.
    """
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    shutdown = asyncio.Event()
    monkeypatch.setattr(evomesh.control, "ConsoleChannel", ExplodingChannel)
    server = ControlServer(environment, shutdown, port=0)
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    async def request(command: str) -> dict[str, object]:
        writer.write((json.dumps({"command": command}) + "\n").encode())
        await writer.drain()
        return json.loads(await reader.readline())

    response = await request("/boom")
    assert response["error"] is True
    assert response["output"] == "Error: OperationalError: no such table: state"
    # The point of the fix: the same connection still answers afterwards.
    assert (await request("/ping"))["output"] == "EvoMesh control ready"

    writer.close()
    await writer.wait_closed()
    await server.stop()
    await environment.stop()
