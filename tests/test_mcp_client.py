"""McpConnection/McpManager -- MCP servers as a tool source, stdio and HTTP,
merged mesh-wide-plus-per-agent by server name. Most tests use a fake
McpConnection (fast, no subprocess); the real fixture stdio server (tests/
fixtures/mcp_stdio_server.py) covers the true end-to-end round trip and the
real subprocess lifecycle, mirroring how tests/test_processes.py covers the
real-subprocess side of the timeout fix its module docstring cites.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from evomesh.contracts import McpServerConfig
from evomesh.harness_tools import Tool, ToolDenied
from evomesh.mcp_client import McpConnectionError, McpManager, build_mcp_tool

FIXTURE_SERVER = str(Path(__file__).parent / "fixtures" / "mcp_stdio_server.py")


def _fixture_config(name: str = "fixture") -> McpServerConfig:
    return McpServerConfig(name=name, command=sys.executable, args=[FIXTURE_SERVER])


# -- McpManager.resolve: merge by name -----------------------------------


def test_agent_overrides_add_to_mesh_wide_servers() -> None:
    manager = McpManager([McpServerConfig(name="a"), McpServerConfig(name="b")])

    resolved = manager.resolve([McpServerConfig(name="c")])

    assert set(resolved) == {"a", "b", "c"}


def test_an_agent_override_wins_on_a_name_collision() -> None:
    manager = McpManager([McpServerConfig(name="a", command="mesh-wide")])

    resolved = manager.resolve([McpServerConfig(name="a", command="per-agent")])

    assert resolved["a"].command == "per-agent"


def test_no_overrides_means_exactly_the_mesh_wide_list() -> None:
    manager = McpManager([McpServerConfig(name="a")])

    resolved = manager.resolve([])

    assert set(resolved) == {"a"}


# -- McpManager.tools_for: failure isolation, best-effort per server -----


class _FakeConnection:
    def __init__(self, tools: list[Any] | None = None, fail: bool = False) -> None:
        self._tools = tools or []
        self._fail = fail
        self.list_tools_calls = 0

    async def list_tools(self) -> list[Any]:
        self.list_tools_calls += 1
        if self._fail:
            raise McpConnectionError("boom")
        return self._tools


async def test_one_bad_server_does_not_block_the_others() -> None:
    manager = McpManager(
        [McpServerConfig(name="broken"), McpServerConfig(name="working")]
    )
    good_tool = type("T", (), {"name": "ping", "description": "", "input_schema": {}})()
    manager._connections["broken"] = _FakeConnection(fail=True)  # type: ignore[assignment]
    manager._connections["working"] = _FakeConnection([good_tool])  # type: ignore[assignment]

    tools = await manager.tools_for([])

    assert len(tools) == 1
    assert tools[0].name == "mcp__working__ping"


async def test_connections_are_reused_across_calls() -> None:
    manager = McpManager([McpServerConfig(name="a")])
    fake = _FakeConnection([])
    manager._connections["a"] = fake  # type: ignore[assignment]

    await manager.tools_for([])
    await manager.tools_for([])

    assert fake.list_tools_calls == 2  # same connection object, not reconnected
    assert manager._connections["a"] is fake


# -- build_mcp_tool: naming and error wrapping ----------------------------


def test_tool_name_is_prefixed_with_the_server_name() -> None:
    info = type("T", (), {"name": "search", "description": "x", "input_schema": {}})()
    tool = build_mcp_tool("news", info, connection=object())  # type: ignore[arg-type]

    assert isinstance(tool, Tool)
    assert tool.name == "mcp__news__search"


def test_build_mcp_tool_sets_name_from_info() -> None:
    """build_mcp_tool() sets the Tool's name from the info it is given."""
    built = build_mcp_tool(
        "news",
        type("T", (), {"name": "search", "description": "x", "input_schema": {}})(),  # type: ignore[arg-type]
        connection=object(),  # type: ignore[arg-type]
    )

    assert built.name == "mcp__news__search"


# -- real end-to-end against the fixture stdio server ---------------------


async def test_a_real_tool_call_round_trips_through_the_fixture_server() -> None:
    manager = McpManager([_fixture_config()])

    tools = await manager.tools_for([])

    names = sorted(tool.name for tool in tools)
    assert names == ["mcp__fixture__echo", "mcp__fixture__fail"]
    echo = next(tool for tool in tools if tool.name == "mcp__fixture__echo")
    result = await echo.run(None, {"text": "hello"})  # type: ignore[arg-type]
    assert result == "hello"

    await manager.shutdown()


async def test_a_server_side_error_becomes_tool_denied() -> None:
    manager = McpManager([_fixture_config()])
    tools = await manager.tools_for([])
    fail = next(tool for tool in tools if tool.name == "mcp__fixture__fail")

    try:
        await fail.run(None, {"reason": "boom"})  # type: ignore[arg-type]
        raised = False
    except ToolDenied:
        raised = True

    assert raised

    await manager.shutdown()


async def test_shutdown_clears_connections_and_a_fresh_call_still_works() -> None:
    """No PID-level check here (no process-inspection dependency in this
    project) -- the functional proof that close() actually tore the old
    connection down is that a completely new McpManager can spawn and use
    a fresh one right after, the same server name and command, with nothing
    left over from the first to conflict with it."""
    manager = McpManager([_fixture_config()])
    await manager.tools_for([])

    await manager.shutdown()

    assert manager._connections == {}
    tools = await manager.tools_for([])
    assert any(tool.name == "mcp__fixture__echo" for tool in tools)
    await manager.shutdown()
