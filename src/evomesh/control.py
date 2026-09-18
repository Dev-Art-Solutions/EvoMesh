from __future__ import annotations

import asyncio
import json
from typing import Any

from evomesh.console import ConsoleChannel
from evomesh.environment import Environment
from evomesh.models import describe

CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 8765


class ControlServer:
    def __init__(
        self,
        environment: Environment,
        shutdown: asyncio.Event,
        host: str = CONTROL_HOST,
        port: int = CONTROL_PORT,
    ) -> None:
        self.environment = environment
        self.shutdown = shutdown
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

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
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(self, channel: ConsoleChannel, raw: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
            command = str(payload.get("command", "")).strip()
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
