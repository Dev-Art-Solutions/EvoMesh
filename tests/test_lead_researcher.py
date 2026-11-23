"""Lead Researcher's template, grant isolation and real LeadCollector MCP contract."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from evomesh.agent_templates import parse_agent_template
from evomesh.contracts import McpServerConfig
from evomesh.mcp_client import McpConnection, McpManager

TEMPLATE = Path(__file__).resolve().parents[1] / "agent-templates" / "lead-researcher"
APPROVED_TOOLS = {
    "get_collector_health",
    "start_collection",
    "get_collection_status",
    "get_collection_results",
    "cancel_collection",
    "search_leads",
    "export_leads",
}


def test_template_only_resolves_its_own_server() -> None:
    file = TEMPLATE / "AGENT.md"
    template = parse_agent_template(file, file.read_text(encoding="utf-8"))
    assert template.harness
    assert template.mcp == []
    assert not template.tools and not template.email and not template.knowledge
    assert template.mcp_servers == []

    settings = (TEMPLATE / "mcp.windows.yaml.example").read_text(encoding="utf-8")
    configured = file.read_text(encoding="utf-8").replace("\n---\n", f"\n{settings}---\n", 1)
    local = parse_agent_template(file, configured)
    manager = McpManager([McpServerConfig(name="unrelated")])
    assert set(manager.resolve(local.mcp_servers, local.mcp)) == {"leadcollector"}
    assert set(manager.resolve([])) == {"unrelated"}
    assert set(local.mcp_servers[0].tools) == APPROVED_TOOLS


async def test_evomesh_mcp_client_reads_real_collector_preflight() -> None:
    checkout = os.environ.get("EVOMESH_LEADCOLLECTOR_TEST_REPO")
    node = shutil.which("node")
    if not checkout or not node:
        pytest.skip("set EVOMESH_LEADCOLLECTOR_TEST_REPO to enable the Node contract")
    root = await asyncio.to_thread(Path(checkout).resolve)
    script = root / "apps" / "api" / "mcp-server.mjs"
    token = "fixture-agent-" * 4

    class Backend(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/v1/health" or self.headers.get("Authorization") != f"Bearer {token}":
                self.send_error(401)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"database":"ready","worker":{"status":"not_seen_recently","last_seen_at":null}}'
            )

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Backend) as backend:
        loop = Thread(target=backend.serve_forever, daemon=True)
        loop.start()
        config = McpServerConfig(
            name="leadcollector",
            command=node,
            args=[str(script)],
            env={
                "BACKEND_URL": f"http://127.0.0.1:{backend.server_port}",
                "LEADCOLLECTOR_AGENT_TOKEN": token,
            },
            tools=sorted(APPROVED_TOOLS),
        )
        connection = McpConnection(config)
        try:
            assert {tool.name for tool in await connection.list_tools()} == APPROVED_TOOLS
            status = json.loads(await connection.call_tool("get_collector_health", {}))
            assert status["mcp"] == status["api"] == status["database"] == "ready"
            assert status["worker"] == {"status": "not_seen_recently", "last_seen_at": None}
        finally:
            await connection.close()
            await asyncio.to_thread(backend.shutdown)
            loop.join()
