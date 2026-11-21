"""One view of what an agent's jobs can use: tools, skills, MCP servers.

Not a registry. ToolRegistry, SkillRegistry and McpManager stay the only
owners of what is installed and connected; this module reads the three and
answers the questions none of them could answer alone -- is it usable right
now, and if not, why? Before it, a tool whose program was not allow-listed,
a bundle whose script was missing, a TOOL.md that failed to parse and an MCP
server that had never connected all looked the same to a human: absent, or
present and silently failing on first use.

Everything here is a check, never an action: no dependency is installed,
and nothing a capability ships is run. The one exception is opt-in and
narrow -- ``check_python_requirements`` starts the tool's own interpreter to
ask ``importlib.util.find_spec`` whether a declared module is importable,
which locates a module without executing it.
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from evomesh.harness_tools import custom_tool_program
from evomesh.mcp_client import MESH_WIDE, McpServerStatus
from evomesh.tools import ToolDefinition, bundled_files

if TYPE_CHECKING:
    from evomesh.contracts import AgentDefinition
    from evomesh.skills import SkillRegistry
    from evomesh.tools import ToolRegistry


class State(StrEnum):
    AVAILABLE = "available"
    # Installed and valid, but this mesh's config keeps it off the menu.
    INACTIVE = "inactive"
    MISSING_DEPENDENCY = "missing-dependency"
    INVALID = "invalid"
    MISCONFIGURED = "misconfigured"
    # An MCP server that connects on first use and has not been used yet.
    NOT_CONNECTED = "not-connected"
    UNREACHABLE = "unreachable"


KINDS = ("tool", "skill", "mcp")


@dataclass(frozen=True)
class Capability:
    kind: str
    name: str
    state: State
    source: str
    summary: str = ""
    problems: tuple[str, ...] = ()


def _program_parts(definition: ToolDefinition, tool_dir: Path) -> list[str]:
    command = definition.command.replace("{tool_dir}", str(tool_dir.resolve(strict=False)))
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _program_found(program: str) -> bool:
    if any(separator in program for separator in ("/", "\\")):
        return Path(program).is_file()
    return shutil.which(program) is not None


def tool_capability(
    definition: ToolDefinition, tool_dir: Path, allow: frozenset[str]
) -> Capability:
    """A tool's state from what can be checked without running anything:
    its program allow-listed and present, its bundled files on disk, the
    programs and files its `requires:` names."""
    problems: list[str] = []
    missing = False
    parts = _program_parts(definition, tool_dir)
    if parts and not _program_found(parts[0]):
        missing = True
        problems.append(f"program not found: {parts[0]}")
    for relative in bundled_files(definition):
        if not (tool_dir / relative).is_file():
            missing = True
            problems.append(f"bundled file missing: {relative}")
    for item in definition.requires:
        kind, _, value = item.partition(":")
        if kind == "program" and not _program_found(value.strip()):
            missing = True
            problems.append(f"required program not on PATH: {value.strip()}")
    try:
        program = custom_tool_program(definition)
    except ValueError as exc:
        program = ""
        problems.append(str(exc))
    inactive = program not in allow
    if inactive:
        problems.insert(0, f"'{program}' is not in harness.shell_allow, so no job is offered it")
    state = (
        State.MISSING_DEPENDENCY if missing else State.INACTIVE if inactive else State.AVAILABLE
    )
    return Capability(
        kind="tool",
        name=definition.name,
        state=state,
        source=definition.path.parent.as_posix(),
        summary=definition.description,
        problems=tuple(problems),
    )


_FIND_SPEC = (
    "import importlib.util, sys\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        found = importlib.util.find_spec(name) is not None\n"
    "    except (ImportError, ValueError):\n"
    "        found = False\n"
    "    if not found:\n"
    "        print(name)\n"
)


async def check_python_requirements(
    definition: ToolDefinition, tool_dir: Path, *, seconds: float = 30.0
) -> list[str]:
    """Problems with the `python:<module>` entries of ``requires`` -- asked
    of the interpreter the tool's own command runs, since a tool pointing at
    its own venv (the document tools do) has modules the mesh's lacks."""
    modules = sorted(
        {
            item.partition(":")[2].strip().split(".")[0]
            for item in definition.requires
            if item.startswith("python:")
        }
    )
    if not modules:
        return []
    parts = _program_parts(definition, tool_dir)
    program = Path(parts[0]).name.lower().removesuffix(".exe") if parts else ""
    if program not in ("python", "python3", "py"):
        return [f"python requirements not checked: the command runs '{program}', not python"]
    if not _program_found(parts[0]):
        return []  # already reported as a missing program
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            parts[0],
            "-c",
            _FIND_SPEC,
            *modules,
            cwd=str(tool_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), seconds)
    except (OSError, TimeoutError) as exc:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        return [f"python requirements not checked: {str(exc) or type(exc).__name__}"]
    missing = stdout.decode("utf-8", "replace").split()
    return [f"python module not importable by {parts[0]}: {name}" for name in missing]


def mcp_capability(status: McpServerStatus) -> Capability:
    problems = list(status.problems)
    if status.last_error:
        problems.append(f"last error: {status.last_error}")
    if status.retry_in is not None:
        problems.append(f"next connect attempt in {status.retry_in:.0f}s")
    # A conflict or a filter typo is worth reading, but the server still works.
    static = [
        problem
        for problem in status.problems
        if not problem.startswith(("conflict:", "tools filter"))
    ]
    if static:
        state = State.MISCONFIGURED
    elif status.connected:
        state = State.AVAILABLE
    elif status.last_error:
        state = State.UNREACHABLE
    else:
        state = State.NOT_CONNECTED
    owners = ", ".join(status.owners) or MESH_WIDE
    tools = "?" if status.tools is None else str(status.tools)
    return Capability(
        kind="mcp",
        name=status.name,
        state=state,
        source=status.transport,
        summary=f"{tools} tools, {status.calls} calls; configured by {owners}",
        problems=tuple(problems),
    )


def inventory(
    tools: ToolRegistry,
    skills: SkillRegistry,
    mcp_rows: Iterable[McpServerStatus],
    allow: frozenset[str],
) -> list[Capability]:
    """Every capability the mesh knows of, valid or not. Reads only what
    the registries already hold plus a few stat()s -- cheap enough to run
    on every `/capabilities`."""
    found: list[Capability] = [
        tool_capability(definition, tools.directory(definition), allow)
        for definition in sorted(tools.discover(), key=lambda item: item.name)
    ]
    found += [
        Capability("tool", name, State.INVALID, f"tools/{name}", problems=(why,))
        for name, why in sorted(tools.errors.items())
    ]
    found += [
        Capability(
            "skill",
            skill.name,
            State.AVAILABLE,
            skill.path.parent.as_posix(),
            summary=skill.description,
        )
        for skill in sorted(skills.discover(), key=lambda item: item.name)
    ]
    found += [
        Capability("skill", name, State.INVALID, f"skills/{name}", problems=(why,))
        for name, why in sorted(skills.errors.items())
    ]
    found += [mcp_capability(row) for row in mcp_rows]
    return found


async def deep_check(tools: ToolRegistry, found: list[Capability]) -> list[Capability]:
    """``found`` with each tool's `python:` requirements actually checked."""
    checked: list[Capability] = []
    for item in found:
        if item.kind != "tool" or item.state == State.INVALID:
            checked.append(item)
            continue
        definition = tools.get(item.name)
        extra = await check_python_requirements(definition, tools.directory(definition))
        if not extra:
            checked.append(item)
            continue
        state = State.MISSING_DEPENDENCY if any(
            "not importable" in problem for problem in extra
        ) else item.state
        checked.append(replace(item, state=state, problems=(*item.problems, *extra)))
    return checked


def render_inventory(found: list[Capability], kind: str = "") -> str:
    """Compact on purpose: usable capabilities by name on one line per kind,
    and a line (plus its reasons) only for what is not usable."""
    blocks: list[str] = []
    for each in KINDS:
        if kind and each != kind:
            continue
        items = [item for item in found if item.kind == each]
        label = {"tool": "tools", "skill": "skills", "mcp": "mcp servers"}[each]
        if not items:
            blocks.append(f"{label}: none")
            continue
        counts: dict[str, int] = {}
        for item in items:
            counts[item.state] = counts.get(item.state, 0) + 1
        summary = ", ".join(f"{count} {state}" for state, count in sorted(counts.items()))
        rows = [f"{label}: {summary}"]
        usable = [item.name for item in items if item.state == State.AVAILABLE]
        if usable and each != "mcp":
            rows.append(f"  available: {', '.join(usable)}")
        for item in items:
            if each != "mcp" and item.state == State.AVAILABLE:
                continue
            detail = f" -- {item.summary}" if each == "mcp" else ""
            rows.append(f"  {item.name} [{item.state}] {item.source}{detail}")
            rows += [f"      {problem}" for problem in item.problems]
        blocks.append("\n".join(rows))
    return "\n".join(blocks)


# -- grants -------------------------------------------------------------


@dataclass(frozen=True)
class Grant:
    """One agent's access to one kind of capability, as it stands."""

    agent: str
    kind: str  # "tools" | "mcp"
    # True: the field is None, the legacy default -- every installed tool or
    # mesh-wide server for an ordinary agent, none for a system agent.
    legacy: bool
    effective: tuple[str, ...]
    # Named in an explicit list, but no longer installed or configured.
    stale: tuple[str, ...] = ()


def grant_report(
    agents: Iterable[AgentDefinition], installed_tools: list[str], mesh_mcp: list[str]
) -> list[Grant]:
    rows: list[Grant] = []
    for agent in sorted(agents, key=lambda item: item.name.lower()):
        system = agent.type == "system"
        for kind, granted, available in (
            ("tools", agent.tools, installed_tools),
            ("mcp", agent.mcp, mesh_mcp),
        ):
            if granted is None:
                effective = () if system else tuple(sorted(available))
                rows.append(Grant(agent.name, kind, True, effective))
            else:
                stale = tuple(name for name in granted if name not in available)
                rows.append(Grant(agent.name, kind, False, tuple(granted), stale))
    return rows


def render_grants(rows: list[Grant]) -> str:
    lines: list[str] = []
    broad: list[str] = []
    quiet: list[str] = []
    for row in rows:
        if row.legacy and not row.effective:
            # A system agent on the default: nothing, which is the point.
            if row.agent not in quiet:
                quiet.append(row.agent)
            continue
        what = ", ".join(row.effective) or "none"
        grows = "every install" if row.kind == "tools" else "every mesh-wide server"
        tag = f" (legacy default: grows with {grows})" if row.legacy else ""
        stale = f"; not installed: {', '.join(row.stale)}" if row.stale else ""
        lines.append(f"{row.agent} {row.kind}: {what}{tag}{stale}")
        if row.legacy and row.agent not in broad:
            broad.append(row.agent)
    if quiet:
        lines.append(f"On a default that grants nothing: {', '.join(quiet)}")
    if broad:
        lines += [
            "",
            f"Broad legacy defaults: {', '.join(broad)}. A tool installed tomorrow, or a "
            "server added to mcp_servers, reaches them without anyone granting it.",
            "Nothing changes on its own. To migrate without changing today's access:",
            "  /capabilities pin <agent>   write today's effective tools and MCP servers as an "
            "explicit list",
            "  then /tool grant|revoke and /mcp grant|revoke edit that list.",
        ]
    return "\n".join(lines)
