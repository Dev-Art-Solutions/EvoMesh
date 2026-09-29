"""evomesh.mcp_server -- the mesh reachable from an MCP client, through the
control port. The end-to-end test launches it the way Claude Code would: a
real stdio subprocess, spoken to by the real MCP client."""

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import TextContent

from evomesh.config import Settings
from evomesh.contracts import AgentDefinition
from evomesh.control import ControlServer
from evomesh.environment import Environment
from evomesh.mcp_server import ControlPortClient
from evomesh.models import MockProvider

SRC = str(Path(__file__).resolve().parents[1] / "src")


@pytest.fixture
async def mesh(tmp_path: Path) -> AsyncIterator[tuple[Environment, int]]:
    settings = Settings(
        data_path=tmp_path / "data.db",
        generation_path=tmp_path / "generations",
        workspace_path=tmp_path / "workspace",
    )
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    server = ControlServer(environment, asyncio.Event(), port=0)
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    try:
        yield environment, port
    finally:
        await server.stop()
        await environment.stop()


async def test_client_runs_commands_and_refuses_exit(mesh: tuple[Environment, int]) -> None:
    environment, port = mesh
    client = ControlPortClient(port=port)
    assert "status: READY" in await client.run("/status")
    assert "not available over MCP" in await client.run("/exit")
    agent = AgentDefinition(name="Analyst", purpose="p", model_name="mock-model")
    await environment.register_agent(agent)
    environment.memory_for(agent).reports.append("XAUUSD bullish (high): x -- y")
    assert "XAUUSD bullish" in await client.run('/reports "Analyst" 5')


async def test_client_reports_a_mesh_that_is_down() -> None:
    client = ControlPortClient(port=1)
    assert "not answering" in await client.run("/status")


async def test_stdio_round_trip_through_a_real_mcp_client(mesh: tuple[Environment, int]) -> None:
    _, port = mesh
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "evomesh.mcp_server", "--port", str(port)],
        env={"PYTHONPATH": SRC},
    )
    async with Client(params) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        assert {"mesh_status", "list_agents", "ask_agent", "agent_reports"} <= names
        result = await client.call_tool("mesh_status", {})
        text = "".join(block.text for block in result.content if isinstance(block, TextContent))
        assert "status: READY" in text
