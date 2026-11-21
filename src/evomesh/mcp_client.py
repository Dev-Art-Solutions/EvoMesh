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

import asyncio
import hashlib
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
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


def _target(config: McpServerConfig) -> tuple[Any, Any]:
    """What ``Client`` connects to, and the HTTP client this module owns
    (None when there is none to close). A bare URL string is the SDK's own
    default client; ``headers`` (an API key, a bearer token) need a client
    of ours -- before, they were accepted in the config and silently never
    sent, so an authenticated HTTP server just refused every connect."""
    if config.command:
        params = StdioServerParameters(
            command=config.command,
            args=list(config.args),
            env=dict(config.env) or None,
        )
        return params, None
    if not config.headers:
        return config.url, None
    http = create_mcp_http_client(headers=dict(config.headers))
    return streamable_http_client(config.url, http_client=http), http


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


def config_fingerprint(config: McpServerConfig) -> str:
    """What makes two configs the same server: everything but the name.

    Two configs under one name with different fingerprints are two different
    servers (another program, another URL, another key, another tool filter),
    and must never share a connection -- see McpManager._connection_for."""
    payload = config.model_dump_json(exclude={"name"})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def config_problems(config: McpServerConfig) -> list[str]:
    """What is wrong with a config before anything connects. Static: no
    process is started and no URL is requested."""
    problems: list[str] = []
    if config.command and config.url:
        problems.append("both command and url are set; command wins (stdio), url is ignored")
    if not config.command and not config.url:
        problems.append("neither command nor url is set; there is nothing to connect to")
    if config.url and not config.command and not config.url.startswith(("http://", "https://")):
        problems.append(f"url {config.url!r} is not http:// or https://")
    if config.command:
        on_disk = Path(config.command)
        if shutil.which(config.command) is None and not on_disk.is_file():
            problems.append(f"command {config.command!r} is not on PATH")
    empty = sorted(key for key, value in config.env.items() if not value)
    if empty:
        problems.append(f"env {', '.join(empty)} empty (a secret that did not resolve?)")
    empty_headers = sorted(key for key, value in config.headers.items() if not value)
    if empty_headers:
        problems.append(f"header {', '.join(empty_headers)} empty")
    if config.timeout_seconds <= 0:
        problems.append("timeout_seconds must be positive")
    return problems


RETRY_AFTER_SECONDS = 60.0


class McpConnection:
    """One live ``Client`` to one server. Connects on first use; ``list_tools``
    is cached after the first successful call. A failed connect is not
    retried for RETRY_AFTER_SECONDS, so a dead server costs one timeout a
    minute rather than one per job; a failed call drops the connection, so a
    server that restarted is reconnected on the next use instead of every
    later call failing against a dead session."""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._client: Client | None = None
        self._http: Any = None
        self._tools: list[McpToolInfo] | None = None
        self.last_error = ""
        self._failed_at: float | None = None
        self.calls = 0
        self.failures = 0
        self.connected_at: float | None = None
        self.last_error_at: float | None = None
        # How many tools the server offered before config.tools filtered
        # them, and which filter names it does not offer at all -- a typo in
        # `tools:` otherwise just looks like a server with fewer tools.
        self.offered: int | None = None
        self.unknown_filter: list[str] = []

    def _failed(self, message: str) -> None:
        self.last_error = message
        self.last_error_at = time.time()
        self.failures += 1

    def retry_in(self) -> float | None:
        """Seconds until a failed connect is tried again; None when it is not
        waiting to retry."""
        if self._client is not None or self._failed_at is None:
            return None
        left = RETRY_AFTER_SECONDS - (time.monotonic() - self._failed_at)
        return left if left > 0 else None

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def tool_count(self) -> int | None:
        return len(self._tools) if self._tools is not None else None

    async def _ensure_connected(self) -> Client:
        if self._client is None:
            if (
                self._failed_at is not None
                and time.monotonic() - self._failed_at < RETRY_AFTER_SECONDS
            ):
                raise McpConnectionError(
                    f"{self.config.name}: unavailable ({self.last_error}); retrying later"
                )
            http = None
            try:
                target, http = _target(self.config)
                client = Client(target)
                await asyncio.wait_for(client.__aenter__(), self.config.timeout_seconds)
            except Exception as exc:  # noqa: BLE001 - any transport/spawn failure
                if http is not None:
                    await http.aclose()
                self._failed_at = time.monotonic()
                self._failed(f"could not connect: {_why(exc)}")
                raise McpConnectionError(f"{self.config.name}: {self.last_error}") from exc
            self._client = client
            self._http = http
            self._failed_at = None
            self.connected_at = time.time()
            self.failures = 0
            self.last_error = ""
        return self._client

    async def list_tools(self) -> list[McpToolInfo]:
        if self._tools is None:
            client = await self._ensure_connected()
            try:
                result = await asyncio.wait_for(client.list_tools(), self.config.timeout_seconds)
            except Exception as exc:  # noqa: BLE001 - any protocol failure
                await self.close()
                self._failed_at = time.monotonic()
                self._failed(f"list_tools failed: {_why(exc)}")
                raise McpConnectionError(f"{self.config.name}: {self.last_error}") from exc
            wanted = set(self.config.tools)
            offered = {tool.name for tool in result.tools}
            self.offered = len(offered)
            self.unknown_filter = sorted(wanted - offered)
            self._tools = [tool for tool in result.tools if not wanted or tool.name in wanted]
        return self._tools

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        """Run one tool. Raises ToolDenied on any failure -- connect,
        transport, timeout, or the server's own reported error -- never a raw
        exception: ToolRegistry.invoke only catches ToolDenied/ValueError/
        TypeError, and an MCP server's own network hiccups are exactly the
        kind of "model can work around it, job should not die" failure every
        other tool in harness_tools.py already converts itself."""
        try:
            client = await self._ensure_connected()
        except McpConnectionError as exc:
            raise ToolDenied(f"DENIED: mcp {self.config.name}.{name}: {exc}") from exc
        self.calls += 1
        try:
            result = await asyncio.wait_for(
                client.call_tool(name, args), self.config.timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - any transport/protocol failure
            # The session may be dead (server restarted, pipe closed); the
            # next use reconnects rather than failing against it forever.
            await self.close()
            self._failed(f"{name} failed: {_why(exc)}")
            raise ToolDenied(
                f"DENIED: mcp {self.config.name}.{name} failed: {_why(exc)}"
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
        http, self._http = self._http, None
        self._tools = None
        if client is not None:
            try:
                await asyncio.wait_for(client.__aexit__(None, None, None), 10)
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.warning("mcp: %s did not close cleanly", self.config.name, exc_info=True)
        if http is not None:
            try:
                await http.aclose()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.warning("mcp: %s http client did not close", self.config.name)


def _why(exc: BaseException) -> str:
    # The SDK's transports run in a TaskGroup, so a refused connection
    # arrives as "unhandled errors in a TaskGroup (1 sub-exception)" -- the
    # one line a human reads to fix the config. The leaf is the reason.
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def build_mcp_tool(server_name: str, info: McpToolInfo, connection: McpConnection) -> Tool:
    """One harness_tools.Tool per MCP-server tool, prefixed ``mcp__{server}__
    {tool}`` (double underscores, matching how a Claude Code session itself
    already names its own MCP tools) so two servers can never collide."""

    async def run(context: Any, args: dict[str, Any]) -> str:
        del context  # unused: an MCP tool's sandbox is the server itself
        return await connection.call_tool(info.name, args)

    return Tool(
        name=f"mcp__{server_name}__{info.name}",
        description=(info.description or f"({server_name} MCP tool)")[:600],
        parameters=info.input_schema or {"type": "object", "properties": {}},
        run=run,
    )


MESH_WIDE = "mesh-wide"


def _transport(config: McpServerConfig) -> str:
    if config.command:
        return f"stdio: {config.command} {' '.join(config.args)}".strip()
    return f"http: {config.url}"


@dataclass(frozen=True)
class McpServerStatus:
    name: str
    transport: str
    connected: bool
    tools: int | None
    calls: int
    last_error: str
    # The fields below are filled by McpManager.diagnose; status() leaves
    # them at their defaults.
    fingerprint: str = ""
    # Who configures this exact server: "mesh-wide" and/or agent names.
    owners: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    retry_in: float | None = None
    failures: int = 0
    connected_at: float | None = None
    last_error_at: float | None = None
    offered: int | None = None


@dataclass(frozen=True)
class McpCheck:
    """One explicit connect-and-list, for `/mcp check`."""

    status: McpServerStatus
    tools: list[str] = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0


@dataclass
class McpManager:
    """Mesh-wide, owned by Environment.mcp. One McpConnection per distinct
    server, reused across every agent and job.

    A *name* should mean one server everywhere in a mesh run, but nothing
    enforced it: an agent's own `/mcp add` under a mesh-wide name, or two
    agents each adding a different "files" server, used to share whichever
    connection was made first -- one agent's jobs silently calling another
    program, with another key, under the tool names it expected. Connections
    are now matched on the whole config (config_fingerprint), so a config
    that differs gets a connection of its own, and diagnose() names the
    conflict instead of hiding it."""

    mesh_wide: list[McpServerConfig]

    def __post_init__(self) -> None:
        # Keyed by name for the first config seen under it; a different
        # config under the same name is keyed "name@fingerprint".
        self._connections: dict[str, McpConnection] = {}

    def resolve(
        self,
        agent_overrides: list[McpServerConfig],
        allowed: list[str] | None = None,
    ) -> dict[str, McpServerConfig]:
        """Mesh-wide defaults (only the ``allowed`` names, when given), then
        the agent's own servers layered on top by name."""
        resolved = {
            config.name: config
            for config in self.mesh_wide
            if allowed is None or config.name in allowed
        }
        for config in agent_overrides:
            resolved[config.name] = config
        return resolved

    def _find(self, config: McpServerConfig) -> tuple[str, McpConnection | None]:
        """The key this config's connection lives (or would live) under."""
        existing = self._connections.get(config.name)
        if existing is None:
            return config.name, None
        existing_config = getattr(existing, "config", None)
        fingerprint = config_fingerprint(config)
        if existing_config is None or config_fingerprint(existing_config) == fingerprint:
            return config.name, existing
        key = f"{config.name}@{fingerprint}"
        return key, self._connections.get(key)

    def _connection_for(self, config: McpServerConfig) -> McpConnection:
        key, connection = self._find(config)
        if connection is None:
            if key != config.name:
                logger.warning(
                    "mcp: server '%s' is configured differently in two places; "
                    "this one gets its own connection (see /mcp servers)",
                    config.name,
                )
            connection = McpConnection(config)
            self._connections[key] = connection
        return connection

    async def tools_for(
        self,
        agent_overrides: list[McpServerConfig],
        allowed: list[str] | None = None,
    ) -> tuple[Tool, ...]:
        """Best-effort per server: one that fails to connect or list its
        tools logs a warning and contributes nothing, never raises."""
        tools: list[Tool] = []
        for name, config in self.resolve(agent_overrides, allowed).items():
            connection = self._connection_for(config)
            try:
                infos = await connection.list_tools()
            except McpConnectionError as exc:
                logger.warning("mcp: %s", exc)
                continue
            tools.extend(build_mcp_tool(name, info, connection) for info in infos)
        return tuple(tools)

    async def describe(self, config: McpServerConfig) -> list[McpToolInfo]:
        """Connect if needed and list one server's tools -- `/mcp tools`."""
        return await self._connection_for(config).list_tools()

    def status(self, extra: list[McpServerConfig] | None = None) -> list[McpServerStatus]:
        configs = {config.name: config for config in [*self.mesh_wide, *(extra or [])]}
        rows: list[McpServerStatus] = []
        for name, config in sorted(configs.items()):
            _, connection = self._find(config)
            rows.append(
                McpServerStatus(
                    name=name,
                    transport=_transport(config),
                    connected=bool(connection and connection.connected),
                    tools=connection.tool_count if connection else None,
                    calls=connection.calls if connection else 0,
                    last_error=connection.last_error if connection else "",
                )
            )
        return rows

    def diagnose(
        self, owned: list[tuple[str, McpServerConfig]] | None = None
    ) -> list[McpServerStatus]:
        """One row per distinct server -- mesh-wide plus every ``(owner,
        config)`` in ``owned`` -- with its static config problems, its
        connection's history, and a note when one name means two servers.
        Never connects."""
        entries = [(MESH_WIDE, config) for config in self.mesh_wide] + list(owned or [])
        groups: dict[tuple[str, str], tuple[McpServerConfig, list[str]]] = {}
        for owner, config in entries:
            key = (config.name, config_fingerprint(config))
            groups.setdefault(key, (config, []))[1].append(owner)
        variants: dict[str, int] = {}
        for name, _ in groups:
            variants[name] = variants.get(name, 0) + 1
        rows: list[McpServerStatus] = []
        for (name, fingerprint), (config, owners) in sorted(groups.items()):
            _, connection = self._find(config)
            problems = config_problems(config)
            if variants[name] > 1:
                problems.append(
                    f"conflict: '{name}' is configured {variants[name]} different ways; "
                    "each gets its own connection, but the tool names "
                    f"(mcp__{name}__*) are the same -- rename one"
                )
            if connection is not None and connection.unknown_filter:
                problems.append(
                    "tools filter names what the server does not offer: "
                    + ", ".join(connection.unknown_filter)
                )
            rows.append(
                McpServerStatus(
                    name=name,
                    transport=_transport(config),
                    connected=bool(connection and connection.connected),
                    tools=connection.tool_count if connection else None,
                    calls=connection.calls if connection else 0,
                    last_error=connection.last_error if connection else "",
                    fingerprint=fingerprint,
                    owners=tuple(dict.fromkeys(owners)),
                    problems=tuple(problems),
                    retry_in=connection.retry_in() if connection else None,
                    failures=connection.failures if connection else 0,
                    connected_at=connection.connected_at if connection else None,
                    last_error_at=connection.last_error_at if connection else None,
                    offered=connection.offered if connection else None,
                )
            )
        return rows

    async def check(
        self, config: McpServerConfig, owned: list[tuple[str, McpServerConfig]] | None = None
    ) -> McpCheck:
        """Drop this server's connection and connect again now, ignoring the
        retry delay, then list its tools. Starts the configured program (or
        calls the URL) -- only ever on an explicit human request."""
        key, connection = self._find(config)
        if connection is not None:
            await connection.close()
            self._connections.pop(key, None)
        started = time.monotonic()
        tools: list[str] = []
        error = ""
        try:
            infos = await self._connection_for(config).list_tools()
            tools = [info.name for info in infos]
        except McpConnectionError as exc:
            error = str(exc)
        seconds = time.monotonic() - started
        fingerprint = config_fingerprint(config)
        row = next(
            (
                item
                for item in self.diagnose(owned)
                if item.name == config.name and item.fingerprint == fingerprint
            ),
            None,
        )
        if row is None:
            row = self.diagnose([("check", config)])[-1]
        return McpCheck(status=row, tools=tools, error=error, seconds=seconds)

    async def reload(self, name: str | None = None) -> None:
        """Drop one server's connections (every config under that name) or
        all of them; the next use reconnects and re-lists tools. For a server
        that was upgraded or restarted."""
        keys = [
            key
            for key in self._connections
            if name is None or key == name or key.startswith(f"{name}@")
        ]
        for key in keys:
            connection = self._connections.pop(key, None)
            if connection is not None:
                await connection.close()

    async def shutdown(self) -> None:
        for connection in self._connections.values():
            await connection.close()
        self._connections.clear()
