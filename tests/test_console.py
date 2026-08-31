import asyncio
import json
from pathlib import Path

from evomesh.config import Settings
from evomesh.console import ConsoleChannel
from evomesh.contracts import AgentDefinition
from evomesh.control import ControlServer
from evomesh.environment import Environment
from evomesh.models import MockProvider


async def test_console_routes_commands(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    assert "status: READY" in (await console.route("/status"))
    assert "Agent Architect" in (await console.route("/agents"))
    assert "Filesystem.Read" in (await console.route("/skills"))
    assert "mock-specialist" in (await console.route("/models ollama"))
    assert await console.route("/chat architect") == "Talking to Agent Architect."
    agent = AgentDefinition(name="Writer", purpose="Write", model_name="mock-model")
    await environment.register_agent(agent)
    await environment.start_agent(agent.id)
    assert "ollama:mock-specialist" in (
        await console.route('/model "Writer" mock-specialist ollama')
    )
    assert await console.route('/chat "Writer"') == "Talking to Writer."
    assert await console.route("hello") == "Writer> Mock response"
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
