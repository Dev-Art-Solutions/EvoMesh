"""Console commands for an agent's reports, knowledge wiki, email and MCP.

A mixin of ConsoleChannel (console.py resolves ``/name`` to
``_command_name`` with getattr), kept in its own module so console.py does
not grow past what one read can hold. Every command here is reachable from
the terminal, the control port and Telegram alike -- on an agent's private
bot the agent argument may be left out, and means that agent.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from evomesh.contracts import AgentDefinition, McpServerConfig
from evomesh.knowledge import render_reports
from evomesh.mailer import EmailRefused

if TYPE_CHECKING:
    from evomesh.environment import Environment

OPS_HELP = """  /reports [agent] [n]          Its last n reports as sent to you (default 10)
  /wiki [agent]                 Its knowledge index (pages it compiled from what it learned)
  /wiki <agent> <page>          One page
  /wiki search <agent> <text>   Search its pages
  /wiki lint <agent>            Health check: index drift (repaired), broken links, orphans
  /wiki log <agent>             What was ingested/written/linted, newest last
  /email accounts               Configured SMTP accounts and which agents may use each
  /email grant|revoke <agent> <account>  Let it send from that account (send_email tool)
  /email test <account> <to>    Send a test message through the account
  /email log [n]                Recent sends and refusals
  /mcp servers                  MCP servers, connection state, tool counts, last error
  /mcp tools <server>           Connect and list a server's tools
  /mcp grant|revoke <agent> <server>  Mesh-wide servers an agent may use
  /mcp add <agent> <name> <command> [args...]  |  /mcp add <agent> <name> <http-url>
                                A server only this agent uses
  /mcp remove <agent> <name>    Remove that agent's own server
  /mcp reload [server]          Reconnect (after a server was upgraded or restarted)
"""


def _first_line(text: str | None) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0][:120] if lines else ""


class OperationsCommands:
    environment: Environment
    locked_agent_id: str | None
    selected_agent: str

    def _agent_or_default(self, parts: list[str], index: int) -> tuple[AgentDefinition, int]:
        """``parts[index]`` as an agent, or -- when it is missing or not an
        agent -- the agent this conversation is locked to or has selected.
        Returns the agent and the index of the next unconsumed argument."""
        registry = self.environment.registry
        if len(parts) > index:
            try:
                return registry.get(parts[index]), index + 1
            except KeyError:
                pass
        fallback = self.locked_agent_id or (
            self.selected_agent if self.selected_agent != "architect" else ""
        )
        if not fallback:
            raise KeyError(parts[index] if len(parts) > index else "an agent name")
        return registry.get(fallback), index

    # -- reports ----------------------------------------------------------

    async def _command_reports(self, parts: list[str]) -> str:
        try:
            agent, rest = self._agent_or_default(parts, 1)
        except KeyError:
            return "Usage: /reports <agent> [n]"
        limit = 10
        if len(parts) > rest:
            if not parts[rest].isdigit():
                return "Usage: /reports <agent> [n]"
            limit = max(1, min(50, int(parts[rest])))
        journal = self.environment.memory_for(agent).reports
        reports = await asyncio.to_thread(journal.recent, limit)
        header = f"{agent.name}: last {len(reports)} report(s), newest first\n\n"
        return header + render_reports(reports) if reports else f"{agent.name} has no reports yet."

    # -- wiki -------------------------------------------------------------

    async def _command_wiki(self, parts: list[str]) -> str:
        action = parts[1].lower() if len(parts) > 1 else ""
        if action in {"search", "lint", "log"}:
            try:
                agent, rest = self._agent_or_default(parts, 2)
            except KeyError:
                return f"Usage: /wiki {action} <agent>" + (" <text>" if action == "search" else "")
            wiki = self.environment.memory_for(agent).wiki
            if action == "search":
                query = " ".join(parts[rest:])
                if not query:
                    return "Usage: /wiki search <agent> <text>"
                hits = await asyncio.to_thread(wiki.search, query, 10)
                if not hits:
                    return f"Nothing in {agent.name}'s wiki matches '{query}'."
                return "\n".join(f"[[{hit.slug}]] {hit.summary}\n    {hit.snippet}" for hit in hits)
            if action == "log":
                lines = await asyncio.to_thread(wiki.recent_log, 30)
                return "\n".join(lines) or f"{agent.name}'s wiki log is empty."
            issues = await asyncio.to_thread(wiki.lint)
            if not issues:
                return f"{agent.name}'s wiki is healthy."
            return f"{agent.name}'s wiki: {len(issues)} issue(s)\n" + "\n".join(
                f"- {issue}" for issue in issues
            )
        try:
            agent, rest = self._agent_or_default(parts, 1)
        except KeyError:
            return "Usage: /wiki <agent> [page]  (or /wiki search|lint|log <agent>)"
        wiki = self.environment.memory_for(agent).wiki
        if len(parts) > rest:
            try:
                return await asyncio.to_thread(wiki.read_page, " ".join(parts[rest:]))
            except ValueError as exc:
                return str(exc)
        index = await asyncio.to_thread(wiki.index_text)
        return (
            f"{agent.name}'s knowledge ({wiki.directory})\n{index}"
            if index
            else f"{agent.name}'s wiki has no pages yet ({wiki.directory})."
        )

    # -- email ------------------------------------------------------------

    async def _command_email(self, parts: list[str]) -> str:
        usage = (
            "Usage: /email accounts  |  /email grant|revoke <agent> <account>  |  "
            "/email test <account> <to>  |  /email log [n]"
        )
        action = parts[1].lower() if len(parts) > 1 else "accounts"
        mailer = self.environment.mailer
        if action == "accounts":
            if not mailer.accounts:
                return (
                    "No email accounts. Add them under email.accounts in evomesh.yaml "
                    "(see evomesh.yaml.example) and restart."
                )
            rows = []
            for name in mailer.names():
                account = mailer.accounts[name]
                users = [
                    agent.name
                    for agent in self.environment.registry.all()
                    if name in agent.email_accounts
                ]
                limit = (
                    f", only {', '.join(account.allowed_recipients)}"
                    if account.allowed_recipients
                    else ""
                )
                rows.append(
                    f"{name}: {account.from_address} via {account.host}:{account.port} "
                    f"({account.security}, {account.max_per_hour}/h{limit})\n"
                    f"    agents: {', '.join(users) or 'none'}"
                )
            return "\n".join(rows)
        if action in {"grant", "revoke"}:
            if len(parts) != 4:
                return usage
            agent = self.environment.registry.get(parts[2])
            account_name = parts[3]
            if action == "grant":
                if account_name not in mailer.accounts:
                    known = ", ".join(mailer.names()) or "none"
                    return f"No email account '{account_name}'. Known: {known}."
                if account_name not in agent.email_accounts:
                    agent.email_accounts.append(account_name)
                await self.environment.repository.save_agent(agent)
                return f"{agent.name} may now send email from '{account_name}'."
            if account_name in agent.email_accounts:
                agent.email_accounts.remove(account_name)
                await self.environment.repository.save_agent(agent)
                return f"{agent.name} may no longer send from '{account_name}'."
            return f"{agent.name} did not have '{account_name}'."
        if action == "test":
            if len(parts) != 4:
                return usage
            try:
                record = await mailer.send(
                    parts[2],
                    parts[3],
                    "EvoMesh test message",
                    "This is a test message from EvoMesh. If you can read it, the account works.",
                    agent="console",
                )
            except (EmailRefused, RuntimeError) as exc:
                return f"Not sent: {exc}"
            return f"Sent a test message to {', '.join(record.to)} through '{parts[2]}'."
        if action == "log":
            limit = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 20
            records = mailer.recent(limit)
            if not records:
                return "Nothing sent since the mesh started (full history: data/email-audit.jsonl)."
            return "\n".join(
                f"{record.at} {record.status} {record.agent} via {record.account} -> "
                f"{', '.join(record.to)}: {record.subject}"
                + (f" ({record.detail})" if record.detail else "")
                for record in records
            )
        return usage

    # -- mcp --------------------------------------------------------------

    async def _command_mcp(self, parts: list[str]) -> str:
        usage = (
            "Usage: /mcp servers  |  /mcp tools <server>  |  /mcp grant|revoke <agent> <server>"
            "  |  /mcp add <agent> <name> <command-or-url> [args...]  |  "
            "/mcp remove <agent> <name>  |  /mcp reload [server]"
        )
        action = parts[1].lower() if len(parts) > 1 else "servers"
        environment = self.environment
        agents = environment.registry.all()
        own = [config for agent in agents for config in agent.mcp_servers]
        if action == "servers":
            rows = []
            for status in environment.mcp.status(own):
                users = [
                    agent.name
                    for agent in agents
                    if any(config.name == status.name for config in agent.mcp_servers)
                    or (
                        any(config.name == status.name for config in environment.mcp.mesh_wide)
                        and (
                            (allowed := environment.mcp_allowed(agent)) is None
                            or status.name in allowed
                        )
                    )
                ]
                state = "connected" if status.connected else "idle"
                tools = "?" if status.tools is None else str(status.tools)
                error = f"\n    last error: {status.last_error}" if status.last_error else ""
                rows.append(
                    f"{status.name} [{state}, {tools} tools, {status.calls} calls] "
                    f"{status.transport}\n    agents: {', '.join(users) or 'none'}{error}"
                )
            return "\n".join(rows) or (
                "No MCP servers. Add mesh-wide ones under mcp_servers in evomesh.yaml, "
                "or one agent's own with /mcp add."
            )
        if action == "tools" and len(parts) == 3:
            config = next(
                (item for item in [*environment.mcp.mesh_wide, *own] if item.name == parts[2]),
                None,
            )
            if config is None:
                return f"No MCP server '{parts[2]}'."
            try:
                infos = await environment.mcp.describe(config)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                return f"{parts[2]}: {exc}"
            return "\n".join(
                f"mcp__{config.name}__{info.name}: {_first_line(info.description)}"
                for info in infos
            ) or f"{parts[2]} offers no tools."
        if action in {"grant", "revoke"} and len(parts) == 4:
            agent = environment.registry.get(parts[2])
            server = parts[3]
            known = {config.name for config in environment.mcp.mesh_wide}
            if action == "revoke" and agent.mcp is not None and server in agent.mcp:
                # Also how a grant for a server since removed from the config
                # is dropped (Environment.stale_grants).
                agent.mcp = [name for name in agent.mcp if name != server]
                await environment.repository.save_agent(agent)
                return f"{agent.name} may use MCP servers: {', '.join(agent.mcp) or 'none'}."
            if server not in known:
                return f"No mesh-wide MCP server '{server}' (an agent's own server needs no grant)."
            current = environment.mcp_allowed(agent)
            names = (
                [config.name for config in environment.mcp.mesh_wide]
                if current is None
                else list(current)
            )
            if action == "grant" and server not in names:
                names.append(server)
            if action == "revoke" and server in names:
                names.remove(server)
            agent.mcp = names
            await environment.repository.save_agent(agent)
            return f"{agent.name} may use MCP servers: {', '.join(names) or 'none'}."
        if action == "add" and len(parts) >= 5:
            agent = environment.registry.get(parts[2])
            name, target = parts[3], parts[4]
            if not name.replace("-", "").replace("_", "").isalnum():
                return "An MCP server name is letters, digits, - and _ only."
            config = (
                McpServerConfig(name=name, url=target)
                if target.startswith(("http://", "https://"))
                else McpServerConfig(name=name, command=target, args=parts[5:])
            )
            agent.mcp_servers = [item for item in agent.mcp_servers if item.name != name]
            agent.mcp_servers.append(config)
            await environment.mcp.reload(name)
            await environment.repository.save_agent(agent)
            return f"{agent.name} now has its own MCP server '{name}' (/mcp tools {name})."
        if action == "remove" and len(parts) == 4:
            agent = environment.registry.get(parts[2])
            before = len(agent.mcp_servers)
            agent.mcp_servers = [item for item in agent.mcp_servers if item.name != parts[3]]
            if len(agent.mcp_servers) == before:
                return f"{agent.name} has no MCP server '{parts[3]}' of its own."
            await environment.mcp.reload(parts[3])
            await environment.repository.save_agent(agent)
            return f"Removed {agent.name}'s MCP server '{parts[3]}'."
        if action == "reload":
            await environment.mcp.reload(parts[2] if len(parts) > 2 else None)
            return "MCP connections dropped; the next job reconnects."
        return usage
