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

import asyncio
import difflib
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evomesh.harness_session import HarnessSession
from evomesh.permissions import FilesystemPolicy, PermissionDeniedError
from evomesh.processes import run_command

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
class ToolTally:
    """How the job spent itself.

    Counted because a job that wrote four files having read none is the harness
    equivalent of the invented-module failure ``codebase.py`` exists to stop, and
    the number is what a later phase will weigh before validating a generation.
    """

    reads: int = 0
    edits: int = 0
    writes: int = 0


@dataclass
class ToolContext:
    root: Path
    limits: ToolLimits = field(default_factory=ToolLimits)
    # Set when the job runs on behalf of an agent. None means the caller is the
    # human at the console, who already has the filesystem this process has.
    policy: FilesystemPolicy | None = None
    agent_id: str = ""
    # Two separate gates on purpose. A read-only job simply has no write tools
    # registered; this flag is the configuration saying no even when they are,
    # so a refusal can name the setting a human has to change.
    allow_write: bool = False
    # Programs the shell tool may run, by bare name. Empty refuses everything,
    # which is why the tool is not even registered until a human fills this in.
    shell_allow: frozenset[str] = frozenset()
    shell_seconds: float = 60.0
    session: HarnessSession | None = None
    tally: ToolTally = field(default_factory=ToolTally)


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
    context.tally.reads += 1
    offset = max(1, int(args.get("offset", 1) or 1))
    limit = int(args.get("limit", 0) or 0)
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    window = lines[offset - 1 :]
    if limit > 0:
        window = window[:limit]
    numbered = "\n".join(f"{number:>5}| {line}" for number, line in enumerate(window, offset))
    if not numbered:
        return f"{target} has no lines at offset {offset} ({len(lines)} lines total)"
    # The bar is not decoration. With two spaces, a 27B model copied the number
    # and the indentation into its edit anchor and lost two attempts to a target
    # that was never in the file; a delimiter makes the prefix unmistakable.
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
    context.tally.reads += 1
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
    context.tally.reads += 1
    if target.is_file():
        return f"{target.name} ({target.stat().st_size} bytes)"
    entries: list[str] = []
    for path in sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name)):
        if path.name in SKIP_DIRECTORIES:
            continue
        entries.append(f"{path.name}/" if path.is_dir() else f"{path.name}")
    return "\n".join(entries) if entries else f"{target} is empty"


def _diff(context: ToolContext, target: Path, before: str, after: str) -> str:
    where = "/".join(_inside(context.root, target))
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{where}",
            tofile=f"b/{where}",
            n=2,
        )
    ).rstrip()


def _announce(context: ToolContext, target: Path, before: str, after: str, kind: str) -> str:
    """Write the intention to the session, then let the caller apply it.

    This order is the point. A process killed between the two leaves a record
    saying what it was about to do; the other order leaves a changed file and no
    explanation, which is the state that costs an hour to reconstruct.
    """
    diff = _diff(context, target, before, after)
    if context.session is not None:
        context.session.record(
            kind,
            path="/".join(_inside(context.root, target)),
            diff=diff,
            bytes_before=len(before),
            bytes_after=len(after),
        )
    return diff


def _writable(context: ToolContext) -> None:
    if not context.allow_write:
        raise ToolDenied(
            "DENIED: this job may not change files. Set harness.allow_write: true "
            "in evomesh.yaml to allow it."
        )


def _match_lines(content: str, needle: str) -> list[int]:
    lines: list[int] = []
    start = content.find(needle)
    while start >= 0:
        lines.append(content.count("\n", 0, start) + 1)
        start = content.find(needle, start + 1)
    return lines


def _neighbourhoods(content: str, at: list[int], *, context_lines: int = 2) -> str:
    """Each match with the lines around it, so the anchor can be widened here."""
    lines = content.splitlines()
    blocks: list[str] = []
    for number in at[:4]:
        start = max(1, number - context_lines)
        end = min(len(lines), number + context_lines)
        body = "\n".join(
            f"{index:>5}{'>' if index == number else ' '} {lines[index - 1]}"
            for index in range(start, end + 1)
        )
        blocks.append(f"-- match at line {number}\n{body}")
    if len(at) > 4:
        blocks.append(f"-- and {len(at) - 4} more")
    return "\n".join(blocks)


async def tool_edit(context: ToolContext, args: dict[str, Any]) -> str:
    """Replace an exact string, and refuse when it is not unique.

    The refusal is the tool's reason for existing. A replacement that silently
    takes the first of three matches produces a candidate that passes ruff,
    pyright and pytest and does the wrong thing -- strictly worse than the
    whole-file rewrite it replaces, because that one fails loudly.
    """
    _writable(context)
    target = _resolve(context, str(args.get("path", "")))
    await _permit(context, target, "write")
    old = str(args.get("old") or args.get("old_string") or "")
    new = str(args.get("new") or args.get("new_string") or "")
    if not old:
        raise ToolDenied("DENIED: edit needs 'old', the exact text to replace")
    if not target.is_file():
        raise ToolDenied(f"DENIED: {target} does not exist. Use write to create a file.")
    if old == new:
        raise ToolDenied("DENIED: 'old' and 'new' are identical, so this edit changes nothing")
    content = target.read_text(encoding="utf-8")
    found = _match_lines(content, old)
    where = "/".join(_inside(context.root, target))
    if not found:
        raise ToolDenied(
            f"DENIED: that text is not in {where}. Read the file again -- it may have "
            "changed since you last saw it, or the indentation may differ."
        )
    if len(found) > 1:
        # The refusal carries the surrounding lines, not just the count. A model
        # told only "3 matches" has to go and read the file again to widen its
        # anchor; one shown the three neighbourhoods can widen it immediately,
        # which is the difference between a refusal that costs a step and one
        # that costs a job. Observed on llama3.1:8B, which understood "add more
        # surrounding lines" and then narrated its intention instead of reading.
        raise ToolDenied(
            f"DENIED: {len(found)} matches in {where}. Extend 'old' with the lines "
            f"around the one you mean until it appears exactly once:\n"
            + _neighbourhoods(content, found)
        )
    updated = content.replace(old, new, 1)
    diff = _announce(context, target, content, updated, kind="edit")
    target.write_text(updated, encoding="utf-8")
    context.tally.edits += 1
    return f"edited {where}\n{diff}" if diff else f"edited {where}"


async def tool_write(context: ToolContext, args: dict[str, Any]) -> str:
    """Write a whole file, refusing to overwrite one that is already there.

    Creating and replacing are different intentions, so they are different
    calls rather than the same call with different luck.
    """
    _writable(context)
    target = _resolve(context, str(args.get("path", "")))
    await _permit(context, target, "write")
    content = str(args.get("content") or "")
    overwrite = bool(args.get("overwrite"))
    where = "/".join(_inside(context.root, target))
    before = ""
    if target.exists():
        if target.is_dir():
            raise ToolDenied(f"DENIED: {where} is a directory")
        if not overwrite:
            raise ToolDenied(
                f"DENIED: {where} already exists. Use edit to change part of it, or "
                'pass "overwrite": true to replace the whole file.'
            )
        before = target.read_text(encoding="utf-8")
    diff = _announce(context, target, before, content, kind="write")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    context.tally.writes += 1
    verb = "replaced" if before else "created"
    return f"{verb} {where} ({len(content)} bytes)\n{diff}" if diff else f"{verb} {where}"


async def tool_shell(context: ToolContext, args: dict[str, Any]) -> str:
    """Run one allowed program in the job root. The only tool that can do harm.

    Sixth of six, and off unless a human lists the programs it may run. No shell
    interpreter is involved: the command is split with shlex and executed
    directly, so ``&&``, ``|`` and ``$(...)`` are arguments rather than
    operators -- every allow-list that has been defeated was defeated through a
    pipe. The first argument is matched *after* parsing, because matching the
    raw string would let `python;curl` through wherever it is re-split later.
    """
    raw = str(args.get("command") or "").strip()
    if not raw:
        raise ToolDenied("DENIED: shell needs a command")
    if not context.shell_allow:
        raise ToolDenied(
            "DENIED: no command may be run. List the programs you trust in "
            "harness.shell_allow in evomesh.yaml."
        )
    try:
        # POSIX rules even on Windows, deliberately. In non-POSIX mode shlex
        # keeps the quotes, so `python -c "print(1)"` reaches python as a string
        # literal: it runs, prints nothing, and exits 0 -- a command that looks
        # like it worked and did nothing, which is the worst possible result.
        # The cost is that an unquoted Windows path loses its backslashes, so
        # the tool description tells the model to quote paths or use slashes.
        parts = shlex.split(raw, posix=True)
    except ValueError as exc:
        raise ToolDenied(f"DENIED: {raw} could not be parsed as a command: {exc}") from exc
    if not parts:
        raise ToolDenied("DENIED: shell needs a command")
    program = Path(parts[0]).name.lower()
    program = program[:-4] if program.endswith(".exe") else program
    if program not in context.shell_allow:
        allowed = ", ".join(sorted(context.shell_allow))
        raise ToolDenied(
            f"DENIED: {program} is not in harness.shell_allow (allowed: {allowed})"
        )
    try:
        result = await asyncio.wait_for(
            run_command(parts[0], *parts[1:], cwd=context.root),
            timeout=context.shell_seconds,
        )
    except TimeoutError:
        # A result, not an exception: a command that ran too long is something
        # the model can work around, and a tool that can hang is a worker that
        # never comes back and a queue that never drains.
        raise ToolDenied(
            f"DENIED: {program} did not finish within {context.shell_seconds:.0f}s"
        ) from None
    except OSError as exc:
        raise ToolDenied(f"DENIED: {program} could not be started: {exc}") from exc
    context.tally.reads += 1
    body = _clip(result.output.rstrip(), context.limits, unit="lines")
    return f"exit {result.exit_code}\n{body}" if body else f"exit {result.exit_code}"


READ_ONLY_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="read",
        description=(
            "Read a text file. Each line is shown with its number as a display "
            "prefix that is NOT part of the file -- never copy those numbers into "
            "an edit anchor. Long files are truncated; the reply says how many "
            "lines were withheld and which offset asks for them."
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

WRITE_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="edit",
        description=(
            "Replace an exact piece of text in a file. Fails unless 'old' appears "
            "exactly once, so include enough surrounding lines to be unambiguous."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the job root."},
                "old": {
                    "type": "string",
                    "description": "The exact text to replace, unique within the file.",
                },
                "new": {"type": "string", "description": "What to put in its place."},
            },
            "required": ["path", "old", "new"],
        },
        run=tool_edit,
    ),
    Tool(
        name="write",
        description=(
            "Write a whole file. Refuses to replace an existing file unless "
            "overwrite is true; prefer edit for a file that already exists."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the job root."},
                "content": {"type": "string", "description": "The complete file."},
                "overwrite": {
                    "type": "boolean",
                    "description": "Replace the file if it already exists.",
                },
            },
            "required": ["path", "content"],
        },
        run=tool_write,
    ),
)

SHELL_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="shell",
        description=(
            "Run one allowed program in the job root and return its exit code "
            "and output. No shell interpreter: pipes, redirects and && are "
            "arguments, not operators, and only allowed programs run. Quote any "
            "path containing backslashes, or write it with forward slashes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The program and its arguments, e.g. python -c 'import x'.",
                }
            },
            "required": ["command"],
        },
        run=tool_shell,
    ),
)

ALL_TOOLS: tuple[Tool, ...] = READ_ONLY_TOOLS + WRITE_TOOLS


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
