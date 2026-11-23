"""EvoMesh as an MCP server: the running mesh, reachable from any MCP client.

``mcp_client.py`` lets agents *use* MCP servers; this is the other
direction. Claude Code, Claude Desktop or any other MCP client launches

    python -m evomesh.mcp_server [--host 127.0.0.1] [--port 8765] [--token-file PATH]

as a stdio server, and every tool call becomes one request on the mesh's
own control port -- the same line protocol the Control Center speaks, so
there is no second way into the mesh to keep in step with the first, and
this process holds no state of its own. The mesh must already be running.

``/exit`` is refused, for the reason the Telegram bot refuses it (rule 17):
whoever stopped the mesh from here could not start it again from here.

Executed by an external process, not imported by anything in this package,
which is why it is listed in ``codebase.ENTRY_POINTS``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from evomesh.control import CONTROL_HOST, CONTROL_PORT, read_token, token_path_for

BLOCKED = {"/exit"}
# An agent answering through its harness can take minutes on a local model.
ANSWER_SECONDS = 330.0


def default_token_file() -> Path:
    """The file the mesh named for the processes it starts, else the token of a
    mesh started from the current directory, else from the checkout this module
    lives in (src/evomesh/ -> the project root)."""
    named = os.environ.get("EVOMESH_CONTROL_TOKEN_FILE", "")
    if named:
        return Path(named)
    here = token_path_for(Path.cwd() / ".runtime")
    if here.exists():
        return here
    return token_path_for(Path(__file__).resolve().parents[2] / ".runtime")


class ControlPortClient:
    def __init__(
        self,
        host: str = CONTROL_HOST,
        port: int = CONTROL_PORT,
        token_file: Path | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.token_file = token_file or default_token_file()

    async def run(self, *commands: str, wait_seconds: float = 60.0) -> str:
        """Send ``commands`` on one connection (so ``/chat`` selection holds
        for the next line) and return the last answer."""
        for command in commands:
            first = command.strip().split(maxsplit=1)[0].lower() if command.strip() else ""
            if first in BLOCKED:
                return f"{first} is not available over MCP; stop the mesh from the Control Center."
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), 10
            )
        except (OSError, TimeoutError) as exc:
            return (
                f"EvoMesh is not answering on {self.host}:{self.port} ({exc or 'timeout'}). "
                "Start it first (evomesh-service start, or start-evomesh.bat)."
            )
        # Read on every call: a restarted mesh has a new token.
        token = read_token(self.token_file)
        try:
            output = ""
            for command in commands:
                request = {"command": command, "token": token}
                payload = json.dumps(request, ensure_ascii=False) + "\n"
                writer.write(payload.encode())
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), wait_seconds)
                if not line:
                    return "EvoMesh closed the connection."
                response: dict[str, Any] = json.loads(line)
                output = str(response.get("output") or response.get("error") or "")
            return output
        except TimeoutError:
            return f"EvoMesh did not answer within {int(wait_seconds)}s."
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


def _quoted(value: str) -> str:
    return '"' + value.replace('"', "'") + '"'


def build_server(client: ControlPortClient) -> MCPServer:
    server = MCPServer("evomesh")

    @server.tool()
    async def mesh_status() -> str:
        """EvoMesh health: environment state, provider health, harness queue."""
        return await client.run("/status")

    @server.tool()
    async def list_agents() -> str:
        """Every agent with its status, live phase, model, current goal and last outcome."""
        return await client.run("/agents")

    @server.tool()
    async def ask_agent(agent: str, message: str) -> str:
        """Send one message to an agent by name and wait for its reply (may take minutes)."""
        return await client.run(f"/chat {_quoted(agent)}", message, wait_seconds=ANSWER_SECONDS)

    @server.tool()
    async def agent_reports(agent: str, limit: int = 10) -> str:
        """The agent's last reports (e.g. NewsAnalyzer's analyses) exactly as announced."""
        return await client.run(f"/reports {_quoted(agent)} {max(1, min(50, limit))}")

    @server.tool()
    async def agent_knowledge(agent: str, page: str = "") -> str:
        """An agent's knowledge wiki: the index, or one page when ``page`` is given."""
        suffix = f" {_quoted(page)}" if page else ""
        return await client.run(f"/wiki {_quoted(agent)}{suffix}")

    @server.tool()
    async def notifications(since_id: int = 0) -> str:
        """What the mesh announced on its own since an announcement id."""
        return await client.run(f"/notifications {since_id}")

    @server.tool()
    async def run_command(command: str) -> str:
        """Any EvoMesh console command, e.g. "/goals NewsAnalyzer" or "/evolution status".
        "/help" lists them. /exit is refused."""
        text = command.strip()
        if not text.startswith("/"):
            return "A command starts with '/'. Use ask_agent to talk to an agent."
        return await client.run(text, wait_seconds=ANSWER_SECONDS)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="EvoMesh as a stdio MCP server")
    parser.add_argument("--host", default=CONTROL_HOST)
    parser.add_argument("--port", type=int, default=CONTROL_PORT)
    parser.add_argument("--token-file", type=Path, default=None)
    args = parser.parse_args()
    build_server(ControlPortClient(args.host, args.port, args.token_file)).run()


if __name__ == "__main__":
    main()
