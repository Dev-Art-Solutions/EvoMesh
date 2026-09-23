"""Persistent connections to Model Context Protocol servers, stdio and HTTP.

One ``McpConnection`` per configured server, owned by the mesh-wide
``McpManager`` (see Environment.mcp), connected lazily on first use and kept
alive across every harness job that wants it -- a stdio server is a real
subprocess (``node``/``npx``/``python``...) and an HTTP server is a real
network round trip either way, so reconnecting per job (jobs run ~40-90s)
would make every job pay a cold-start tax for nothing.

The MCP SDK's own ``Client`` accepts a ``StdioServerParameters`` (local
subprocess, spoken to over stdin/stdout) or a bare URL string (HTTP) as its
single constructor argument, handling both the process/connection lifecycle
and the tool-call JSON-RPC framing -- confirmed directly against the
installed ``mcp`` package (v2), not just its docs, since doc sites drift
from releases. This module never hand-rolls that framing.

Windows subprocess lifecycle note, same class of bug as processes.py's
``run_command`` timeout fix: ``Client``'s own async context manager owns the
stdio child's lifetime, and ``McpConnection.close()``/``McpManager.
shutdown()`` must actually run it (``Environment.stop()`` does, in a
``finally`` reached on every shutdown path -- see __main__.py) rather than
just dropping the reference, or a server's node/python process outlives the
mesh the same way a hung shell command's did before that fix.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mcp import Client, StdioServerParameters
from mcp.types import TextContent

from evomesh.contracts import McpServerConfig
from evomesh.harness_tools import Tool, ToolDenied

if TYPE_CHECKING:
    from mcp.types import Tool as McpToolInfo

logger = logging.getLogger(__name__)


class McpConnectionError(Exception):
    """Could not connect to, or list tools from, one MCP server.

    Caught per server by McpManager.tools_for -- one bad server (a typo'd
    command, a refused connection, a dead URL) logs a warning and
    contributes zero tools, the same "a sweep failing here is nothing to
    block a new generation over" philosophy already used for filesystem
    grant pruning in behaviors.py's _open().
    """


def _target(config: McpServerConfig) -> StdioServerParameters | str:
    if config.command:
        return StdioServerParameters(
            command=config.command,
            args=list(config.args),
            env=dict(config.env) or None,
        )
    return config.url


def _content_text(blocks: list[Any]) -> str:
    """Every TextContent block's text, joined -- other content kinds (image,
    audio, embedded resource) are real MCP shapes this harness has no way
    to hand a text-only tool result, so they are named rather than dropped
    silently."""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, TextContent):
            parts.append(block.text)
        else:
            parts.append(f"[{type(block).__name__} content omitted]")
    return "\n".join(parts)


class McpConnection:
    """One live ``Client`` to one server. Connects on first use; ``list_tools``
    is cached after the first successful call (a server's tool set does not
    change mid-job, and jobs are short enough that a refresh mid-mesh-run is
    not worth the extra round trip -- see McpManager.shutdown for the only
    way this cache is ever cleared)."""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._client: Client | None = None
        self._tools: list[McpToolInfo] | None = None

    async def _ensure_connected(self) -> Client:
        if self._client is None:
            try:
                client = Client(_target(self.config))
                await client.__aenter__()
            except Exception as exc:  # noqa: BLE001 - any transport/spawn failure
                raise McpConnectionError(
                    f"{self.config.name}: could not connect: {exc}"
                ) from exc
            self._client = client
        return self._client

    async def list_tools(self) -> list[McpToolInfo]:
        if self._tools is None:
            client = await self._ensure_connected()
            try:
                result = await client.list_tools()
            except Exception as exc:  # noqa: BLE001 - any protocol failure
                raise McpConnectionError(
                    f"{self.config.name}: list_tools failed: {exc}"
                ) from exc
            self._tools = list(result.tools)
        return self._tools

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        """Run one tool. Raises ToolDenied on any failure -- connect,
        transport, or the server's own reported error -- never a raw
        exception: ToolRegistry.invoke only catches ToolDenied/ValueError/
        TypeError, and an MCP server's own network hiccups are exactly the
        kind of "model can work around it, job should not die" failure
        every other tool in harness_tools.py already converts itself
        (mirroring tool_shell's own OSError-to-ToolDenied conversion)."""
        try:
            client = await self._ensure_connected()
        except McpConnectionError as exc:
            raise ToolDenied(f"DENIED: mcp {self.config.name}.{name}: {exc}") from exc
        try:
            result = await client.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001 - any transport/protocol failure
            raise ToolDenied(
                f"DENIED: mcp {self.config.name}.{name} failed: {exc}"
            ) from exc
        text = _content_text(list(result.content))
        if result.is_error:
            raise ToolDenied(
                f"DENIED: mcp {self.config.name}.{name} reported an error: "
                f"{text or 'no detail given'}"
            )
        return text

    async def close(self) -> None:
        client, self._client = self._client, None
        self._tools = None
        if client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.warning("mcp: %s did not close cleanly", self.config.name, exc_info=True)


def build_mcp_tool(server_name: str, info: McpToolInfo, connection: McpConnection) -> Tool:
    """One harness_tools.Tool per MCP-server tool, prefixed ``mcp__{server}__
    {tool}`` (double underscores, matching how a Claude Code session itself
    already names its own MCP tools) so two servers can never collide."""

    async def run(context: Any, args: dict[str, Any]) -> str:
        del context  # unused: an MCP tool's sandbox is the server itself
        return await connection.call_tool(info.name, args)

    return Tool(
        name=f"mcp__{server_name}__{info.name}",
        description=info.description or f"({server_name} MCP tool)",
        parameters=info.input_schema or {"type": "object", "properties": {}},
        run=run,
    )


@dataclass
class McpManager:
    """Mesh-wide, owned by Environment.mcp. One McpConnection per resolved
    server *name*, reused across every agent and job -- if two agents ever
    configure genuinely different servers under the same name (not the
    intended use: a name should mean one server everywhere in a mesh run),
    whichever connects first wins for the lifetime of the connection rather
    than being silently torn down and reconnected out from under a job that
    may still be using it."""

    mesh_wide: list[McpServerConfig]

    def __post_init__(self) -> None:
        self._connections: dict[str, McpConnection] = {}

    def resolve(self, agent_overrides: list[McpServerConfig]) -> dict[str, McpServerConfig]:
        """Mesh-wide defaults, then agent overrides layered on top by name."""
        resolved = {config.name: config for config in self.mesh_wide}
        for config in agent_overrides:
            resolved[config.name] = config
        return resolved

    def _connection_for(self, config: McpServerConfig) -> McpConnection:
        connection = self._connections.get(config.name)
        if connection is None:
            connection = McpConnection(config)
            self._connections[config.name] = connection
        return connection

    async def tools_for(self, agent_overrides: list[McpServerConfig]) -> tuple[Tool, ...]:
        """Best-effort per server: one that fails to connect or list its
        tools logs a warning and contributes nothing, never raises."""
        tools: list[Tool] = []
        for name, config in self.resolve(agent_overrides).items():
            connection = self._connection_for(config)
            try:
                infos = await connection.list_tools()
            except McpConnectionError as exc:
                logger.warning("mcp: %s", exc)
                continue
            tools.extend(build_mcp_tool(name, info, connection) for info in infos)
        return tuple(tools)

    async def shutdown(self) -> None:
        for connection in self._connections.values():
            await connection.close()
        self._connections.clear()
