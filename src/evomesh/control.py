from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

from evomesh.console import ConsoleChannel
from evomesh.environment import Environment
from evomesh.models import describe

CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 8765
TOKEN_FILENAME = "control.token"
# Answers whether the port is EvoMesh's and whether agents are stuck, and
# changes nothing: the supervisor's hang watchdog and the Control Center's
# "is this port ours" check ask it before they can have read a token.
UNAUTHENTICATED = frozenset({"/ping"})

logger = logging.getLogger(__name__)


def token_path_for(runtime_dir: Path) -> Path:
    return runtime_dir / TOKEN_FILENAME


def read_token(path: Path) -> str:
    """The running mesh's control token, or "" when there is none to read."""
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` so only the current user can read it.

    POSIX: created 0600 before a byte is written. Windows: inheritance is cut
    and only the current user is granted, because a project directory on a
    data drive is commonly readable by every local user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(text)
    if sys.platform == "win32":
        user = os.environ.get("USERNAME", "")
        if user:
            result = subprocess.run(  # noqa: S603 - fixed program, our own file
                ["icacls", str(temporary), "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                logger.warning("control token: icacls could not restrict %s", temporary)
    os.replace(temporary, path)


class ControlServer:
    def __init__(
        self,
        environment: Environment,
        shutdown: asyncio.Event,
        host: str = CONTROL_HOST,
        port: int = CONTROL_PORT,
        token_path: Path | None = None,
    ) -> None:
        self.environment = environment
        self.shutdown = shutdown
        self.host = host
        self.port = port
        # A new token every start, so one read from an earlier run is worth
        # nothing. Clients read it from token_path, written before the port
        # opens so nothing can connect before the token is there to read.
        self.token = secrets.token_urlsafe(32)
        self.token_path = token_path
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        if self.token_path is not None:
            await asyncio.to_thread(_write_private, self.token_path, self.token)
            # The path, never the token: every process the mesh starts (a
            # custom tool such as crawl_schedule) inherits where to look.
            os.environ["EVOMESH_CONTROL_TOKEN_FILE"] = str(self.token_path)
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self.token_path is not None and read_token(self.token_path) == self.token:
            with contextlib.suppress(OSError):
                self.token_path.unlink()
            if os.environ.get("EVOMESH_CONTROL_TOKEN_FILE") == str(self.token_path):
                del os.environ["EVOMESH_CONTROL_TOKEN_FILE"]

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        channel = ConsoleChannel(self.environment)
        try:
            while not self.shutdown.is_set() and (line := await reader.readline()):
                response = await self._dispatch(channel, line)
                writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                if response.get("shutdown"):
                    self.shutdown.set()
                    break
                if response.pop("closed", False):
                    break
        except (ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            # A client that closes its own process instead of sending /exit
            # (the Desktop Control Center killed rather than quit, most
            # often) drops the socket mid-read or mid-write. Expected, not a
            # bug -- but left uncaught, asyncio's own default handler logs
            # whatever escapes client_connected_cb as an ERROR with a full
            # traceback, which is what "the mesh crashed" looks like at a
            # glance in mesh.log even though nothing here needed fixing.
            logger.info("control client disconnected: %s", exc)
        finally:
            with contextlib.suppress(ConnectionResetError, ConnectionAbortedError, OSError):
                writer.close()
                await writer.wait_closed()

    async def _dispatch(self, channel: ConsoleChannel, raw: bytes) -> dict[str, Any]:
        # A line that is not a JSON object ends the connection rather than
        # being skipped. Skipping is what let a browser drive this port: a
        # page's fetch() to 127.0.0.1:8765 sends HTTP headers (each one a bad
        # line, answered and passed over) and then a body that is a perfectly
        # good command. The request line is now the last thing read.
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return {"output": "Error: not a control request", "error": True, "closed": True}
        command = str(payload.get("command", "")).strip()
        if command not in UNAUTHENTICATED:
            supplied = str(payload.get("token") or "")
            if not hmac.compare_digest(supplied.encode(), self.token.encode()):
                return {
                    "output": "Error: unauthorized -- send the token from .runtime/control.token",
                    "error": True,
                    "unauthorized": True,
                    "closed": True,
                }
        try:
            if command == "/ping":
                stuck = self.environment.stuck_agents()
                return {"output": "EvoMesh control ready", "running": True, "stuck": stuck}
            if command == "/agents.json":
                # Structured, for a UI to render a live list/cards from --
                # every other command answers in the one text shape shared
                # with the console and Telegram, but a human-readable roster
                # like /agents's own is not something a client should have to
                # parse back apart to draw a button next to each agent.
                return {"output": "", "running": True, "agents": self._agents_json()}
            response = await channel.route(command)
            should_stop = command.lower() == "/exit"
            return {"output": response, "running": not should_stop, "shutdown": should_stop}
        except Exception as exc:  # noqa: BLE001 - a failed command never drops the connection
            return {"output": f"Error: {describe(exc)}", "running": True, "error": True}

    def _agents_json(self) -> list[dict[str, Any]]:
        states = self.environment.runtime_states()
        rows: list[dict[str, Any]] = []
        for definition in self.environment.registry.all():
            state = states.get(definition.id)
            rows.append(
                {
                    "id": definition.id,
                    "name": definition.name,
                    "type": definition.type,
                    "status": str(definition.status),
                    "phase": str(state.phase) if state else "offline",
                    "provider": definition.provider,
                    "model": definition.model_name,
                    "num_ctx": definition.num_ctx,
                    "cycle_seconds": definition.cycle_seconds,
                    "muted": definition.muted,
                    "has_telegram": bool(definition.telegram and definition.telegram.enabled),
                    "goal": state.goal if state else None,
                    "last_outcome": state.last_outcome if state else "",
                    "last_error": state.last_error if state else None,
                    "cycles": state.cycles if state else 0,
                }
            )
        return rows


async def wait_for_console_or_shutdown(
    console: ConsoleChannel,
    shutdown: asyncio.Event,
) -> None:
    console_task = asyncio.create_task(console.run())
    shutdown_task = asyncio.create_task(shutdown.wait())
    done, pending = await asyncio.wait(
        {console_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if console_task in done:
        shutdown.set()
    else:
        console.running = False
    for task in pending:
        task.cancel()
    for task in pending:
        try:
            await task
        except asyncio.CancelledError:
            pass
