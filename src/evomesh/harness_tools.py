"""What a model in the harness is allowed to do, and how it is stopped.

Three read-only tools -- read, grep, ls -- behind one registry. Every one of
them resolves its path against the job root, verifies containment, and then asks
the same FilesystemPolicy a skill asks, before anything is opened. The check
lives here rather than in the loop so that a fourth tool added later cannot
arrive unguarded by forgetting a line somewhere else.

A refusal is a tool *result*, not an exception. "You may not read that" is
information the model can act on, and a loop that dies on the first denied path
cannot work under least privilege at all.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evomesh.permissions import FilesystemPolicy, PermissionDeniedError

# Directories no answer about this project ever comes out of, and which a
# recursive grep would otherwise spend its whole match budget inside.
SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".pytest-tmp",
        ".ruff_cache",
        "node_modules",
        "dist",
        "obj",
        "bin",
        "generations",
    }
)


@dataclass
class ToolLimits:
    """How much of a file a tool may put into the transcript.

    Rule 3 says the trim is ours. A tool that returns a whole 900-line module
    hands the truncation decision to the model server, which drops the oldest
    end -- the objective. So the tool truncates, and says what it withheld.
    """

    result_chars: int = 4000
    result_lines: int = 200
    grep_matches: int = 40


@dataclass
class ToolContext:
    root: Path
    limits: ToolLimits = field(default_factory=ToolLimits)
    # Set when the job runs on behalf of an agent. None means the caller is the
    # human at the console, who already has the filesystem this process has.
    policy: FilesystemPolicy | None = None
    agent_id: str = ""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[[ToolContext, dict[str, Any]], Awaitable[str]]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolDenied(Exception):
    """A refusal the model is expected to read and work around."""


def _resolve(context: ToolContext, raw: str) -> Path:
    """Resolve against the job root, then prove the result is inside it.

    Resolution comes first because ``root/../../etc`` only becomes visible as an
    escape once it is normalised; comparing the string would pass it through.
    """
    candidate = Path(raw.strip().strip('"').strip("'") or ".")
    target = (candidate if candidate.is_absolute() else context.root / candidate).resolve(
        strict=False
    )
    root = context.root.resolve(strict=False)
    if target != root and root not in target.parents:
        raise ToolDenied(f"DENIED: {raw} is outside the job root {root}")
    return target


def _inside(root: Path, path: Path) -> tuple[str, ...]:
    """The path's parts below the job root, for skip decisions and reporting."""
    try:
        return path.relative_to(root).parts
    except ValueError:
        return path.parts


async def _permit(context: ToolContext, target: Path, operation: str) -> None:
    if context.policy is None or not context.agent_id:
        return
    try:
        await context.policy.require(context.agent_id, target, operation)
    except PermissionDeniedError as exc:
        raise ToolDenied(f"DENIED: {exc}") from exc


def _clip(text: str, limits: ToolLimits, *, unit: str) -> str:
    """Cut to budget and say what was withheld, in terms of the next request.

    The withheld count is what makes the truncation recoverable: a model told
    "240 more lines, use offset=201" can ask for the rest, while one handed a
    silently shortened file believes it has seen the whole thing.
    """
    lines = text.splitlines()
    withheld = 0
    if len(lines) > limits.result_lines:
        withheld = len(lines) - limits.result_lines
        lines = lines[: limits.result_lines]
    body = "\n".join(lines)
    if len(body) > limits.result_chars:
        body = body[: limits.result_chars]
        withheld = max(withheld, 1)
    if withheld:
        hint = f"use offset={limits.result_lines + 1}" if unit == "lines" else "narrow the pattern"
        body += f"\n[... {withheld} more {unit} withheld, {hint} ...]"
    return body


async def tool_read(context: ToolContext, args: dict[str, Any]) -> str:
    target = _resolve(context, str(args.get("path", "")))
    await _permit(context, target, "read")
    if target.is_dir():
        raise ToolDenied(f"DENIED: {target} is a directory, use ls")
    if not target.is_file():
        raise ToolDenied(f"DENIED: {target} does not exist")
    offset = max(1, int(args.get("offset", 1) or 1))
    limit = int(args.get("limit", 0) or 0)
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    window = lines[offset - 1 :]
    if limit > 0:
        window = window[:limit]
    numbered = "\n".join(f"{number:>5}  {line}" for number, line in enumerate(window, offset))
    if not numbered:
        return f"{target} has no lines at offset {offset} ({len(lines)} lines total)"
    return _clip(numbered, context.limits, unit="lines")


async def tool_grep(context: ToolContext, args: dict[str, Any]) -> str:
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ToolDenied("DENIED: grep needs a pattern")
    try:
        expression = re.compile(pattern)
    except re.error as exc:
        raise ToolDenied(f"DENIED: {pattern} is not a valid regular expression: {exc}") from exc
    target = _resolve(context, str(args.get("path", ".")))
    await _permit(context, target, "read")
    glob = str(args.get("glob", "*.py") or "*.py")
    files = [target] if target.is_file() else sorted(target.rglob(glob))
    matches: list[str] = []
    for path in files:
        # Compared inside the root, never against the absolute path: a checkout
        # that happens to live under a directory called bin or dist would
        # otherwise have every one of its files skipped, and the tool would
        # report "no match" for code that is plainly there.
        if not path.is_file() or SKIP_DIRECTORIES & set(_inside(context.root, path)):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # A file the host will not open is the host's problem, not a result
            # the model can act on -- skip it rather than ending the job.
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if expression.search(line):
                where = "/".join(_inside(context.root, path))
                matches.append(f"{where}:{number}: {line.strip()}")
            if len(matches) >= context.limits.grep_matches:
                found = "\n".join(matches)
                return f"{found}\n[... more matches withheld, narrow the pattern ...]"
    if not matches:
        return f"no match for {pattern} in {target} ({glob})"
    return _clip("\n".join(matches), context.limits, unit="matches")


async def tool_ls(context: ToolContext, args: dict[str, Any]) -> str:
    target = _resolve(context, str(args.get("path", ".")))
    await _permit(context, target, "read")
    if not target.exists():
        raise ToolDenied(f"DENIED: {target} does not exist")
    if target.is_file():
        return f"{target.name} ({target.stat().st_size} bytes)"
    entries: list[str] = []
    for path in sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name)):
        if path.name in SKIP_DIRECTORIES:
            continue
        entries.append(f"{path.name}/" if path.is_dir() else f"{path.name}")
    return "\n".join(entries) if entries else f"{target} is empty"


READ_ONLY_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="read",
        description=(
            "Read a text file, with line numbers. Long files are truncated; the "
            "reply says how many lines were withheld and which offset asks for them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the job root."},
                "offset": {"type": "integer", "description": "First line to return, 1-based."},
                "limit": {"type": "integer", "description": "How many lines to return."},
            },
            "required": ["path"],
        },
        run=tool_read,
    ),
    Tool(
        name="grep",
        description=(
            "Search files for a Python regular expression. Returns path:line: text "
            "for each match, capped."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression."},
                "path": {"type": "string", "description": "Directory or file to search."},
                "glob": {"type": "string", "description": "Filename glob, default *.py."},
            },
            "required": ["pattern"],
        },
        run=tool_grep,
    ),
    Tool(
        name="ls",
        description="List a directory. Directories end with a slash.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory relative to the job root."}
            },
        },
        run=tool_ls,
    ),
)


class ToolRegistry:
    def __init__(self, tools: tuple[Tool, ...] = READ_ONLY_TOOLS) -> None:
        self.tools = {tool.name: tool for tool in tools}

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools.values()]

    def describe(self) -> str:
        """The tool list as prompt text, for a model that cannot call tools."""
        lines: list[str] = []
        for tool in self.tools.values():
            names = ", ".join(tool.parameters.get("properties", {}))
            lines.append(f'- {tool.name}({names}): {tool.description.split(".")[0]}.')
        return "\n".join(lines)

    async def invoke(self, context: ToolContext, name: str, args: dict[str, Any]) -> str:
        tool = self.tools.get(name)
        if tool is None:
            known = ", ".join(self.tools)
            return f"DENIED: there is no tool called {name}. Available tools: {known}"
        try:
            return await tool.run(context, args)
        except ToolDenied as exc:
            return str(exc)
        except (ValueError, TypeError) as exc:
            # A malformed argument is the model's to fix, so it comes back as a
            # result. An OSError is the host's and is left to end the job.
            return f"DENIED: {name} could not use those arguments: {exc}"
