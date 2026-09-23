"""A tiny real MCP server, launched by tests via `sys.executable` -- no
node/npx dependency, so stdio tests don't need anything beyond the `mcp`
package already installed for the client side. Exposes two tools: `echo`
(a normal round trip) and `fail` (an is_error result, to exercise
McpConnection.call_tool's error path)."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

server = MCPServer("test-fixture")


@server.tool()
def echo(text: str) -> str:
    return text


@server.tool()
def fail(reason: str) -> str:
    raise ValueError(reason)


if __name__ == "__main__":
    server.run()
