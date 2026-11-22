"""Console commands for an agent's reports, knowledge wiki, email and MCP.

A mixin of ConsoleChannel (console.py resolves ``/name`` to
``_command_name`` with getattr), kept in its own module so console.py does
not grow past what one read can hold. Every command here is reachable from
the terminal, the control port and Telegram alike -- on an agent's private
bot the agent argument may be left out, and means that agent.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from evomesh.browser import BrowserError
from evomesh.capabilities import (
    Capability,
    deep_check,
    grant_report,
    inventory,
    render_grants,
    render_inventory,
)
from evomesh.contracts import AgentDefinition, BrowserGrant, McpServerConfig
from evomesh.knowledge import render_reports
from evomesh.mailer import EmailRefused
from evomesh.manager import coordinates
from evomesh.mcp_client import MESH_WIDE, McpServerStatus, config_fingerprint

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
  /mcp servers                  MCP servers: state, owners, last error, retry timer, config problems
  /mcp check [server]           Connect now (ignoring the retry delay) and report what it offers
  /mcp tools <server>           Connect and list a server's tools
  /mcp grant|revoke <agent> <server>  Mesh-wide servers an agent may use
  /mcp add <agent> <name> <command> [args...]  |  /mcp add <agent> <name> <http-url>
                                A server only this agent uses
  /mcp remove <agent> <name>    Remove that agent's own server
  /mcp reload [server]          Reconnect (after a server was upgraded or restarted)
  /capabilities                 Tools, skills and MCP servers: usable or not, and why
  /capabilities check           Same, also asking each tool's Python for its `requires:` modules
  /capabilities refresh         Rescan tools/ and skills/, drop MCP connections -- no restart
  /capabilities grants          Each agent's effective tools/MCP access, legacy defaults flagged
  /capabilities pin <agent>     Write its current (legacy) access out as an explicit list
  /browser status               Chrome, the automation profile, the extension, the bridge
  /browser start [url] | stop   EvoMesh's own Chrome (dedicated profile); never yours
  /browser grant <agent> read|interact [user]  The browser tool; 'user' = your own Chrome
  /browser revoke <agent>       Take the browser tool away
  /browser pending              Sensitive browser actions waiting for you
  /browser approve|deny <n>     Let one through once, or refuse it
  /coordination <agent> on|off|default  Give any agent the manager's tools for handing
                                out and following work, repeats, stop and messages
                                (not its commands); default: on for managers only
  /manager set|unset <agent>    Make it your point of contact: it routes work to the
                                agent that owns it, follows it, and runs commands
  /manager tasks                What a manager runs for you, numbered, and what finished
  /manager pending              Commands a manager wants to run that change the mesh
  /manager approve|deny <n>     Run one once, or refuse it
  /manager trust on|off         On: what you ask it for runs without a question,
                                grants included (off by default)
  /manager stop                 Stop all it runs for you: repeats and handed-out
                                tasks (saying "stop" to it does the same)
  /manager stop <n>|<task-id>   Stop just one: number n of /manager tasks, or a task id
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
            "/mcp remove <agent> <name>  |  /mcp reload [server]  |  /mcp check [server]"
        )
        action = parts[1].lower() if len(parts) > 1 else "servers"
        environment = self.environment
        agents = environment.registry.all()
        own = [config for agent in agents for config in agent.mcp_servers]
        owned = [(agent.name, config) for agent in agents for config in agent.mcp_servers]
        if action == "servers":
            rows = []
            for status in environment.mcp.diagnose(owned):
                users = [
                    agent.name
                    for agent in agents
                    if agent.name in status.owners
                    or (
                        MESH_WIDE in status.owners
                        and not any(c.name == status.name for c in agent.mcp_servers)
                        and (
                            (allowed := environment.mcp_allowed(agent)) is None
                            or status.name in allowed
                        )
                    )
                ]
                rows.append(_render_server(status, users))
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
        if action == "check":
            targets = [
                config
                for config in [*environment.mcp.mesh_wide, *own]
                if len(parts) < 3 or config.name == parts[2]
            ]
            if not targets:
                return f"No MCP server '{parts[2]}'." if len(parts) > 2 else "No MCP servers."
            seen: set[tuple[str, str]] = set()
            blocks: list[str] = []
            for config in targets:
                key = (config.name, config_fingerprint(config))
                if key in seen:
                    continue
                seen.add(key)
                result = await environment.mcp.check(config, owned)
                verdict = (
                    f"connected in {result.seconds:.1f}s, {len(result.tools)} tools"
                    if not result.error
                    else f"failed after {result.seconds:.1f}s: {result.error}"
                )
                lines = [f"{config.name}: {verdict}", f"    {result.status.transport}"]
                if result.tools:
                    lines.append(f"    tools: {', '.join(result.tools)}")
                lines += [f"    ! {problem}" for problem in result.status.problems]
                blocks.append("\n".join(lines))
            return "\n".join(blocks)
        return usage

    # -- capabilities -----------------------------------------------------

    async def _command_capabilities(self, parts: list[str]) -> str:
        usage = (
            "Usage: /capabilities [tools|skills|mcp]  |  /capabilities check  |  "
            "/capabilities refresh  |  /capabilities grants  |  /capabilities pin <agent>"
        )
        environment = self.environment
        action = parts[1].lower() if len(parts) > 1 else ""
        agents = environment.registry.all()
        owned = [(agent.name, config) for agent in agents for config in agent.mcp_servers]

        def current() -> list[Capability]:
            return inventory(
                environment.tools,
                environment.skills,
                environment.mcp.diagnose(owned),
                environment.settings.harness.shell_programs(),
            )

        if action in ("", "tools", "skills", "mcp", "tool", "skill"):
            return render_inventory(current(), action.rstrip("s") if action != "mcp" else "mcp")
        if action == "check":
            return render_inventory(await deep_check(environment.tools, current()))
        if action == "refresh":
            tools = await environment.tools.refresh()
            skills = await environment.skills.refresh()
            await environment.mcp.reload()
            return "\n".join(
                [
                    tools.render("tools"),
                    skills.render("skills"),
                    "mcp: connections dropped; each reconnects and re-lists its tools "
                    "on next use (/mcp check to do it now)",
                ]
            )
        installed = [definition.name for definition in environment.tools.discover()]
        mesh = [config.name for config in environment.mcp.mesh_wide]
        if action == "grants":
            return render_grants(grant_report(agents, installed, mesh)) or "No agents."
        if action == "pin" and len(parts) == 3:
            agent = environment.registry.get(parts[2])
            pinned: list[str] = []
            for row in grant_report([agent], installed, mesh):
                if not row.legacy:
                    continue
                if row.kind == "tools":
                    agent.tools = list(row.effective)
                else:
                    agent.mcp = list(row.effective)
                pinned.append(f"{row.kind}: {', '.join(row.effective) or 'none'}")
            if not pinned:
                return f"{agent.name} already has explicit tool and MCP lists."
            await environment.repository.save_agent(agent)
            return (
                f"Pinned {agent.name}'s current access, unchanged -- "
                + "; ".join(pinned)
                + ". New tools and servers now need a grant."
            )
        return usage


    # -- browser ------------------------------------------------------------

    async def _command_browser(self, parts: list[str]) -> str:
        usage = (
            "Usage: /browser status  |  /browser start [url]  |  /browser stop  |  "
            "/browser grant <agent> read|interact [user]  |  /browser revoke <agent>  |  "
            "/browser pending  |  /browser approve|deny <n>"
        )
        environment = self.environment
        browser = environment.browser
        action = parts[1].lower() if len(parts) > 1 else "status"
        if action == "status":
            return _render_browser_status(await browser.status())
        if action == "start":
            try:
                result = await browser.chrome.start(parts[2] if len(parts) > 2 else "about:blank")
            except BrowserError as exc:
                return f"Could not start Chrome: {exc.message}"
            if not result.get("started"):
                return f"The automation Chrome is already running (pid {result['pid']})."
            return (
                f"Started the automation Chrome (pid {result['pid']}) on {result['profile']}. "
                "First time on this profile: load browser-extension/ unpacked there "
                "(chrome://extensions, Developer mode, Load unpacked)."
            )
        if action == "stop":
            result = await browser.chrome.stop()
            if not result.get("stopped"):
                return "The automation Chrome is not running."
            forced = " (it had to be forced)" if result.get("forced") else ""
            return (
                f"Stopped the automation Chrome, pid {result['pid']}{forced}. "
                "Your own Chrome was not touched."
            )
        if action == "grant" and len(parts) in (4, 5) and parts[3] in ("read", "interact"):
            if len(parts) == 5 and parts[4] != "user":
                return usage
            agent = environment.registry.get(parts[2])
            session = "user" if len(parts) == 5 else "automation"
            agent.browser = BrowserGrant(level=parts[3], session=session)  # type: ignore[arg-type]
            await environment.repository.save_agent(agent)
            where = (
                "YOUR OWN Chrome, with whatever you are signed into"
                if session == "user"
                else "EvoMesh's own automation Chrome"
            )
            return f"{agent.name} may now {parts[3]} in {where} (the browser tool)."
        if action == "revoke" and len(parts) == 3:
            agent = environment.registry.get(parts[2])
            agent.browser = None
            await environment.repository.save_agent(agent)
            return f"{agent.name} no longer has the browser tool."
        if action == "pending":
            if not browser.pending:
                return "No browser action is waiting for you."
            return "\n".join(
                f"#{item.number} {item.agent}: {item.summary} ({item.reason})"
                + (" -- approved, not yet used" if item.approved else "")
                for item in sorted(browser.pending.values(), key=lambda item: item.number)
            )
        if action in ("approve", "deny") and len(parts) == 3 and parts[2].isdigit():
            try:
                item = (browser.approve if action == "approve" else browser.deny)(int(parts[2]))
            except KeyError:
                return f"No pending browser action #{parts[2]} (it may have expired)."
            if action == "approve":
                return (
                    f"Approved #{item.number} once: {item.agent} may now {item.summary}. "
                    "It has to repeat the exact request to use it."
                )
            return f"Refused #{item.number}: {item.agent} will be told not to retry."
        return usage

    # -- coordination -----------------------------------------------------

    async def _command_coordination(self, parts: list[str]) -> str:
        if len(parts) != 3 or parts[2].lower() not in ("on", "off", "default"):
            return "Usage: /coordination <agent> on|off|default"
        agent = self.environment.registry.get(parts[1])
        if agent.type == "system":
            return f"{agent.name} is a system agent; coordination is for ordinary agents."
        choice = parts[2].lower()
        agent.coordination = None if choice == "default" else choice == "on"
        if coordinates(agent) and not agent.harness_root:
            # Its tools only reach the model through the harness.
            agent.harness_root = str(self.environment.default_harness_root(agent))
        agent.touch()
        await self.environment.repository.save_agent(agent)
        if coordinates(agent):
            return (
                f"{agent.name} now has the coordination tools: the roster, assign_task and "
                "task_status, repeat and stop, message_human. \"stop\" and \"stop 2\" work on "
                "its line. Running commands stays a manager's alone."
            )
        return f"{agent.name} has no coordination tools."

    # -- the manager --------------------------------------------------------

    async def _command_manager(self, parts: list[str]) -> str:
        usage = (
            "Usage: /manager set|unset <agent>  |  /manager tasks  |  /manager pending  |  "
            "/manager approve|deny <n>  |  /manager stop  |  /manager trust on|off"
        )
        environment = self.environment
        desk = environment.manager
        action = parts[1].lower() if len(parts) > 1 else "tasks"
        if action in ("set", "unset") and len(parts) == 3:
            agent = environment.registry.get(parts[2])
            if action == "set" and agent.type == "system":
                return f"{agent.name} is a system agent; a manager is an ordinary agent."
            agent.manager = action == "set"
            if agent.manager and not agent.harness_root:
                # Its tools only reach the model through the harness.
                agent.harness_root = str(environment.default_harness_root(agent))
            agent.touch()
            await environment.repository.save_agent(agent)
            if agent.manager:
                return (
                    f"{agent.name} is now a manager: it can read the roster, hand tasks to "
                    "other agents and follow them, and run read-only commands. Anything that "
                    "changes the mesh waits for /manager approve <n>."
                )
            return f"{agent.name} is an ordinary agent again."
        if action == "tasks":
            managers = [
                agent
                for agent in environment.registry.all()
                if agent.manager or coordinates(agent)
            ]
            if not managers:
                return "No agent coordinates work. /manager set <agent>"
            blocks = [
                (f"{agent.name}:\n" if len(managers) > 1 else "")
                + "\n".join(desk.listing(agent.id))
                for agent in managers
            ]
            return "\n\n".join(blocks) + "\n\n/manager stop <number> stops one."
        if action == "pending":
            live = [item for item in desk.pending.values() if not item.expired()]
            if not live:
                return "No manager command is waiting for you."
            return "\n".join(
                f"#{item.number} {item.command}" for item in sorted(live, key=lambda i: i.number)
            )
        if action in ("approve", "deny") and len(parts) == 3 and parts[2].isdigit():
            number = int(parts[2])
            return await desk.approve(number) if action == "approve" else desk.deny(number)
        if action == "trust":
            if len(parts) == 3 and parts[2].lower() in ("on", "off"):
                await desk.set_trusted(parts[2].lower() == "on")
            if desk.trusted:
                return (
                    "Trust is ON: what you ask a manager for runs without a question, grants "
                    "and installs included. Something it asked for on its own (a repeat, a "
                    "report, another agent's answer) still waits for you. /manager trust off"
                )
            return (
                "Trust is OFF: what you ask for runs, but anything that changes what agents "
                "may do (grants, installs, restart) waits for your yes. /manager trust on"
            )
        if action == "stop":
            if len(parts) == 3:
                # One thing: a repeat by its number, or a task by its id.
                which = parts[2].lstrip("#")
                if which.isdigit():
                    # The number from /manager tasks (its first manager's list).
                    manager = next(
                        (
                            agent
                            for agent in environment.registry.all()
                            if agent.manager or coordinates(agent)
                        ),
                        None,
                    )
                    if manager is None:
                        return "No agent is a manager."
                    return await desk.stop_listed(manager.id, int(which))
                for task in desk.tasks:
                    if task.id == which and task.status in ("running", "stalled"):
                        await desk.stop_task(task)
                        return f"Stopped task {task.id} on {task.agent_name}."
                return f"No running task {which}. /manager tasks lists them."
            managers = [
                agent
                for agent in environment.registry.all()
                if agent.manager or coordinates(agent)
            ]
            said = [await desk.stop_everything(agent.id) for agent in managers]
            return "\n".join(line for line in said if line) or "Nothing is running."
        return usage


def _render_browser_status(status: dict[str, object]) -> str:
    automation = status["automation"]
    assert isinstance(automation, dict)
    host = status["native_host"]
    assert isinstance(host, dict)
    lines = [
        f"chrome: {status['chrome'] or 'NOT FOUND (install Chrome or set browser.chrome_path)'}",
        "automation Chrome: "
        + (f"running, pid {automation['pid']}" if automation["running"] else "not running")
        + f" -- profile {automation['profile']}",
    ]
    installed = automation["extension_installed"]
    lines.append(
        "  extension in that profile: "
        + {True: "installed", False: "NOT installed (load browser-extension/ unpacked there)",
           None: "unknown (the profile has not been started yet)"}[installed]  # type: ignore[index]
    )
    lines.append(f"  extension connected: {'yes' if automation['extension_connected'] else 'no'}")
    lines.append(f"your own Chrome sessions with the extension: {status['user_sessions']}")
    registered = host.get("registered")
    lines.append(
        "native host: "
        + ("registered" if registered else "NOT registered" if registered is False else "unknown")
        + (f" -- {host['detail']}" if host.get("detail") else "")
    )
    return "\n".join(lines)


def _render_server(status: McpServerStatus, users: list[str]) -> str:
    state = "connected" if status.connected else "idle"
    if status.last_error and not status.connected:
        state = "failing"
    tools = "?" if status.tools is None else str(status.tools)
    if status.offered is not None and status.tools is not None and status.offered > status.tools:
        tools += f" of {status.offered}"
    lines = [
        f"{status.name} [{state}, {tools} tools, {status.calls} calls] {status.transport}",
        f"    agents: {', '.join(users) or 'none'}",
    ]
    if len(status.owners) > 1 or status.owners != (MESH_WIDE,):
        lines.append(f"    configured by: {', '.join(status.owners)}")
    if status.connected_at is not None:
        lines.append(f"    connected since {_clock(status.connected_at)}")
    if status.last_error:
        when = f" at {_clock(status.last_error_at)}" if status.last_error_at else ""
        lines.append(f"    last error{when}: {status.last_error}")
    if status.failures > 1:
        lines.append(f"    failed {status.failures} times in a row")
    if status.retry_in is not None:
        lines.append(f"    next connect attempt in {status.retry_in:.0f}s (/mcp check: now)")
    lines += [f"    ! {problem}" for problem in status.problems]
    return "\n".join(lines)


def _clock(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
