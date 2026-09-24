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
import json
import re
import shlex
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evomesh._agent_ids import AgentIdValidator
from evomesh.harness_session import HarnessSession
from evomesh.humanize import humanize_size
from evomesh.permissions import FilesystemPolicy, PermissionDeniedError
from evomesh.processes import run_command
from evomesh.tools import ToolDefinition

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
    deletes: int = 0


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
    # Narrower than the job root: set when a stage's whole job is to write one
    # known file (the plan draft/eval/decompose stages, each of which only
    # ever needs docs/evolution/plans/**), so a model that ignores the prose
    # instruction not to touch a source file gets a named refusal instead of
    # a stray file landing in the candidate. None means the job root itself
    # is the only boundary, same as before this existed.
    write_prefix: str | None = None
    # Programs the shell tool may run, by bare name. Empty refuses everything,
    # which is why the tool is not even registered until a human fills this in.
    shell_allow: frozenset[str] = frozenset()
    shell_seconds: float = 60.0
    # Absolute path to the Scrapling executable in its own environment (see
    # scripts/install-scrapling.ps1). Empty is why the fetch tool is not even
    # registered, same reasoning as shell_allow above.
    scraping_executable: str = ""
    scraping_timeout: float = 30.0
    # Bound to this job's own agent_id by the caller (environment.py's
    # submit_harness_job), so the tool itself never needs the mesh's message
    # bus or agent registry directly -- it asks by name, waits for the
    # reply, and gets back a string like every other tool. None is why
    # ask_agent is not even registered (see build_runner): a harness job run
    # for the human at the console, or with no live mesh behind it at all
    # (a test), has no other agent to ask.
    ask_agent: Callable[[str, str], Awaitable[str]] | None = None
    # Bound to this job's own agent_id, the same way ask_agent is -- writes a
    # new skills/<name>/SKILL.md (or overwrites one this same agent already
    # authored) and returns what the registry says about it. None is why
    # learn_skill is not even registered (see build_runner): a human's own
    # harness job, one with no live mesh behind it (a test), or an agent
    # never granted this capability (AgentDefinition.can_learn_skills) has
    # no business writing into the mesh-wide skills/ directory.
    learn_skill: Callable[[str, str, str], Awaitable[str]] | None = None
    # The same grant as learn_skill, targeted instead: a unique-match text
    # replacement within a skill this same agent already wrote, the way the
    # harness's own edit tool works on an ordinary file. Wired alongside
    # learn_skill (see build_runner) -- one capability, two tools.
    patch_skill: Callable[[str, str, str], Awaitable[str]] | None = None
    # The mesh's own project root, so a job whose own root is not that
    # tree (a non-system agent's own playground, most of the time) can
    # still *read* the mesh-wide skills/ directory every job's own catalog
    # line points at (see environment.py's _run_harness_job). Read-only,
    # by design: only tool_read/tool_grep/tool_ls fall back to it (via
    # _resolve_readable) when a path is not inside the job's own root --
    # tool_edit/tool_write/tool_delete never do, so learn_skill/patch_skill
    # stay the only way anything actually changes a skill. None (a human's
    # own harness job, or a test with no live mesh) means no fallback.
    skills_root: Path | None = None
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


def _resolve_readable(context: ToolContext, raw: str) -> Path:
    """Like ``_resolve``, but a path that does not exist inside the job's
    own root gets one more chance: the mesh-wide ``skills/`` directory,
    since every job's own task text (environment.py's catalog line) names a
    path there -- ``skills/<name>/SKILL.md`` -- regardless of what this
    particular job's own root is. A non-system agent's root is its own
    playground, not the mesh's project tree, so that path was syntactically
    inside the job root (``_resolve`` never raises for it) and simply never
    existed there: found live, a NewsAnalyzer whose actual per-headline
    judgment never left the job because ``read`` on its own
    news-impact-analysis skill came back "does not exist", so it improvised
    a narrated report instead of the one-line format ``report_pattern``
    actually requires -- and the improvised report matched nothing, so
    nothing was ever announced despite real headlines to report on.

    A genuinely outside-root path (``../etc/passwd``) still gets the
    fallback offered too -- it is the *destination*, not the shape of the
    original refusal, that decides whether this is a legitimate skill read.

    Read-only tools call this; ``tool_edit``/``tool_write``/``tool_delete``
    call ``_resolve`` directly and never get the fallback, so this is not a
    second way to change a skill -- only ``learn_skill``/``patch_skill``
    still are.
    """
    try:
        in_root = _resolve(context, raw)
    except ToolDenied:
        in_root = None
    if in_root is not None and in_root.exists():
        return in_root
    if context.skills_root is not None:
        candidate = Path(raw.strip().strip('"').strip("'") or ".")
        if not candidate.is_absolute():
            base = context.skills_root.resolve(strict=False)
            fallback = (base / candidate).resolve(strict=False)
            skills_dir = (base / "skills").resolve(strict=False)
            in_skills = fallback == skills_dir or skills_dir in fallback.parents
            if in_skills and fallback.exists():
                return fallback
    if in_root is None:
        root = context.root.resolve(strict=False)
        raise ToolDenied(f"DENIED: {raw} is outside the job root {root}")
    return in_root


def valid_id(agent_id: str) -> bool:
    """Whether ``agent_id`` is one the harness will answer about.

    The harness only ever touches a fixed, well-formed id space. Anything that
    breaks the ``<namespace>:<name>`` shape -- or, for namespaces, the
    ``<namespace>/<name>`` shape -- is rejected out of hand, so a malformed id
    from a model never reaches the file policy. The shape check itself lives in
    :class:`evomesh._agent_ids.AgentIdValidator`; this function is the harness's
    view of that single source of truth.
    """
    return AgentIdValidator.is_valid(agent_id)


def _inside(root: Path, path: Path) -> tuple[str, ...]:
    """The path's parts below the job root, for skip decisions and reporting."""
    try:
        return path.relative_to(root).parts
    except ValueError:
        return path.parts


def _shown(context: ToolContext, target: Path) -> str:
    """``target`` the way the job names it: relative to its root, ``/``-joined.

    Found live 2026-09-24, twice in one day: a "no match" naming
    `D:\\...\\generations\\001382-candidate\\tests\\test_evolution.py` read to a
    small model as "the tool resolved to some nested generation", and it spent
    its next steps on pwd/ls working out where it was -- the candidate is the
    root, but its absolute path ends in a directory called `generations`.
    """
    if target.is_relative_to(context.root):
        return "/".join(target.relative_to(context.root).parts) or "."
    return str(target)


async def _permit(context: ToolContext, target: Path, operation: str) -> None:
    if context.policy is None or not context.agent_id:
        return
    # The mesh-wide skills/ directory is readable by every agent's own job
    # by design -- render_catalog() is spliced into every job's task
    # unconditionally, not offered only to agents someone remembered to
    # grant it to -- so a per-agent FilesystemGrant is not the right gate
    # for it, the same reasoning _resolve_readable already applies to reach
    # the path at all.
    if operation == "read" and context.skills_root is not None:
        skills_dir = (context.skills_root / "skills").resolve(strict=False)
        if target == skills_dir or skills_dir in target.parents:
            return
    try:
        await context.policy.require(context.agent_id, target, operation)
    except PermissionDeniedError as exc:
        raise ToolDenied(f"DENIED: {exc}") from exc


def _clip(text: str, limits: ToolLimits, *, unit: str, offset: int | None = None) -> str:
    """Cut to budget and say what was withheld, in terms of the next request.

    The withheld count is what makes the truncation recoverable: a model told
    "240 more lines, use offset=201" can ask for the rest, while one handed a
    silently shortened file believes it has seen the whole thing.

    The count and the offset have to be the real ones. Found live 2026-09-24:
    a read cut by the character budget said "1 more lines withheld, use
    offset=201" whatever it had actually shown -- a read at offset=481 that
    showed 60 lines sent the model back to line 201, and after a few of those
    it decided the read tool was returning fabricated content. ``offset`` is
    the file line ``text`` starts at, for a read; the cut is on a line
    boundary whenever there is one, so the next read picks up exactly there.
    """
    lines = text.splitlines()
    shown = lines[: limits.result_lines]
    body = "\n".join(shown)
    partial = False
    if len(body) > limits.result_chars:
        cut = body.rfind("\n", 0, limits.result_chars)
        if cut > 0:
            body = body[:cut]
            shown = shown[: body.count("\n") + 1]
        else:
            body = body[: limits.result_chars]
            partial = True
    withheld = max(len(lines) - len(shown), 1 if partial else 0)
    if withheld:
        if unit != "lines":
            hint = "narrow the pattern"
        elif offset is None:
            hint = f"use offset={len(shown) + 1}"
        else:
            first, last = offset, offset + len(shown) - 1
            hint = f"showing lines {first}-{last}, use offset={last + 1 - partial}"
        body += f"\n[... {withheld} more {unit} withheld, {hint} ...]"
    return body


async def tool_read(context: ToolContext, args: dict[str, Any]) -> str:
    target = _resolve_readable(context, str(args.get("path", "")))
    await _permit(context, target, "read")
    if target.is_dir():
        raise ToolDenied(f"DENIED: {_shown(context, target)} is a directory, use ls")
    if not target.is_file():
        raise _missing(context, target)
    context.tally.reads += 1
    offset = max(1, int(args.get("offset", 1) or 1))
    limit = int(args.get("limit", 0) or 0)
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    window = lines[offset - 1 :]
    if limit > 0:
        window = window[:limit]
    numbered = "\n".join(f"{number:>5}| {line}" for number, line in enumerate(window, offset))
    if not numbered:
        shown = _shown(context, target)
        return f"{shown} has no lines at offset {offset} ({len(lines)} lines total)"
    # The bar is not decoration. With two spaces, a 27B model copied the number
    # and the indentation into its edit anchor and lost two attempts to a target
    # that was never in the file; a delimiter makes the prefix unmistakable.
    return _clip(numbered, context.limits, unit="lines", offset=offset)


async def tool_grep(context: ToolContext, args: dict[str, Any]) -> str:
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ToolDenied("DENIED: grep needs a pattern")
    note = ""
    try:
        expression = re.compile(pattern)
    except re.error as exc:
        # Found live: 29 of 971 refused calls were a pattern like `def foo(` --
        # meant as text, invalid as a regex. Searching for the text is what was
        # meant; refusing only cost a step.
        expression = re.compile(re.escape(pattern))
        note = (
            f"[{pattern} is not a valid regular expression ({exc}); "
            "searched for it as plain text]\n"
        )
    target = _resolve_readable(context, str(args.get("path", ".")))
    await _permit(context, target, "read")
    if not target.exists():
        raise _missing(context, target)
    context.tally.reads += 1
    glob = str(args.get("glob", "*.py") or "*.py")
    files = [target] if target.is_file() else sorted(target.rglob(glob))
    # _resolve_readable can hand back a path entirely outside context.root --
    # the mesh-wide skills/ fallback it documents on itself -- in which case
    # _inside(context.root, ...) can never find path.relative_to(context.root)
    # and falls back to path's full *absolute* parts instead. Found live,
    # 2026-09-23: this checkout (like every candidate generation, which lives
    # under generations/NNNNNN-candidate/) has "generations" as a literal path
    # component, which SKIP_DIRECTORIES also lists to avoid descending into --
    # so every single skills-fallback match was silently discarded as if it
    # sat inside a generations/ directory, for a reason with nothing to do
    # with the skill itself. The exact bug the comment below already guards
    # against for the normal case, just missed for this one. Comparing inside
    # target instead of context.root when target itself is not under
    # context.root keeps every match's reported path relative and short,
    # the same guarantee the normal case already has.
    report_root = context.root if target.is_relative_to(context.root) else target
    matches: list[str] = []
    for path in files:
        # Compared inside the root, never against the absolute path: a checkout
        # that happens to live under a directory called bin or dist would
        # otherwise have every one of its files skipped, and the tool would
        # report "no match" for code that is plainly there.
        if not path.is_file() or SKIP_DIRECTORIES & set(_inside(report_root, path)):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # A file the host will not open is the host's problem, not a result
            # the model can act on -- skip it rather than ending the job.
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if expression.search(line):
                where = "/".join(_inside(report_root, path))
                matches.append(f"{where}:{number}: {line.strip()}")
            if len(matches) >= context.limits.grep_matches:
                found = "\n".join(matches)
                return f"{note}{found}\n[... more matches withheld, narrow the pattern ...]"
    if not matches:
        return f"{note}no match for {pattern} in {_shown(context, target)} ({glob})"
    return note + _clip("\n".join(matches), context.limits, unit="matches")


def _missing(context: ToolContext, target: Path) -> ToolDenied:
    """"Does not exist", and the path most likely meant, when there is one.

    Found in the last 250 harness sessions: 136 refused calls were a path that
    was not there -- `src/evomesh/behaviors` without its `.py`, a
    `generations/001218-candidate/...` prefix copied out of an absolute path
    the model had been shown, a test file under the wrong name.
    """
    shown = _shown(context, target)
    guesses: list[str] = []

    def offer(path: Path) -> None:
        if path.exists() and path.is_relative_to(context.root):
            relative = "/".join(path.relative_to(context.root).parts)
            if relative not in guesses:
                guesses.append(relative)

    if not target.suffix:
        offer(target.with_suffix(".py"))
    parts = shown.split("/")
    for index, part in enumerate(parts):
        if re.fullmatch(r"\d{6}-candidate", part):
            offer(context.root.joinpath(*parts[index + 1 :]))
    if target.name and len(guesses) < 3:
        for path in sorted(context.root.rglob(target.name))[:20]:
            if not SKIP_DIRECTORIES & set(_inside(context.root, path)):
                offer(path)
    hint = f" Did you mean: {', '.join(guesses[:3])}?" if guesses else ""
    return ToolDenied(f"DENIED: {shown} does not exist.{hint}")


async def tool_ls(context: ToolContext, args: dict[str, Any]) -> str:
    target = _resolve_readable(context, str(args.get("path", ".")))
    await _permit(context, target, "read")
    if not target.exists():
        raise _missing(context, target)
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


def _writable(context: ToolContext, target: Path) -> None:
    if not context.allow_write:
        raise ToolDenied(
            "DENIED: this job may not change files. Set harness.allow_write: true "
            "in evomesh.yaml to allow it."
        )
    if context.write_prefix is not None:
        inside = _inside(context.root, target)
        prefix = Path(context.write_prefix).parts
        if inside[: len(prefix)] != prefix:
            raise ToolDenied(
                f"DENIED: this job may only write inside {context.write_prefix}/, "
                f"not {'/'.join(inside)}"
            )


def _match_lines(content: str, needle: str) -> list[int]:
    lines: list[int] = []
    start = content.find(needle)
    while start >= 0:
        lines.append(content.count("\n", 0, start) + 1)
        start = content.find(needle, start + 1)
    return lines


def _distinctive_lines(text: str) -> list[str]:
    """Lines of `old` worth anchoring on: long enough not to match by luck.

    A short generic line (`continue`, `return None`) matches unrelated code
    by coincidence and points the model at the wrong place -- found live,
    right after the first version of this hint shipped. 12+ characters only.
    """
    return [line.strip() for line in text.splitlines() if len(line.strip()) >= 12]


def _find_elsewhere(context: ToolContext, target: Path, old: str) -> str | None:
    """Which other tracked file, if any, actually contains this text.

    Found live: a job correctly `read` agent_strategies.py, then submitted an
    `edit` for that exact content against harness_tools.py -- the wrong path
    entirely. `old` was not fabricated at all, just aimed at the wrong file,
    so the in-file "does this one line appear" check below has nothing to
    show, and without this a job that confuses two files gets the same "no
    line of old appears anywhere" fallback whether it fabricated the text or
    just named the wrong path -- two different mistakes that need two
    different corrections.
    """
    candidates = _distinctive_lines(old)
    if not candidates:
        return None
    needle = max(candidates, key=len)
    for path in sorted(context.root.rglob("*.py")):
        if path == target or not path.is_file():
            continue
        if SKIP_DIRECTORIES & set(_inside(context.root, path)):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if needle in text:
            return "/".join(_inside(context.root, path))
    return None


def _whitespace_miss(content: str, old: str) -> str | None:
    """Say so outright when every line of `old` is in the file, in order, and
    only its leading or trailing whitespace is wrong -- with the exact text.

    Found live 2026-09-24: a plan job copied a line from a numbered read as
    `     src/...` -- five spaces, the one after `NNNNN|` kept -- and the hint
    below answered "this line of 'old' does appear, but not the rest of it"
    about a one-line `old`. The model could not see what was wrong with it,
    lost two edits and fell back to printing repr() through the shell.
    """
    wanted = old.splitlines()
    if not any(line.strip() for line in wanted):
        return None
    lines = content.splitlines()
    for start in range(len(lines) - len(wanted) + 1):
        block = lines[start : start + len(wanted)]
        if any(have.strip() != want.strip() for have, want in zip(block, wanted, strict=True)):
            continue
        differs = next(
            (
                (index, have, want)
                for index, (have, want) in enumerate(zip(block, wanted, strict=True))
                if have != want
            ),
            None,
        )
        if differs is None:
            # Identical line by line: whatever missed was not whitespace.
            return None
        offset, have, want = differs
        spaces = len(have) - len(have.lstrip())
        given = len(want) - len(want.lstrip())
        where = f"line {start + offset + 1}"
        detail = (
            f"{where} starts with {spaces} spaces and yours with {given}"
            if spaces != given
            else f"{where} differs only in trailing whitespace"
        )
        return (
            f"Every line of 'old' is in the file, but spaced differently: {detail}. "
            "A read's `NNNNN| ` prefix is the number, the bar and exactly ONE space -- "
            "copy only what comes after it. The exact text to use as 'old':\n"
            + "\n".join(block)
        )
    return None


def _not_found_hint(context: ToolContext, target: Path, content: str, old: str) -> str:
    """Give a not-found refusal something real to anchor a retry on.

    A blind "read the file again" assumes the model's `old` was a faithful
    copy that just went stale. Found live: a model can instead fabricate
    `old` wholesale -- plausible code matching a plan's *description* of a
    function rather than the function's actual body -- and a bare re-read
    doesn't correct that, it just gets re-fabricated the same way on the next
    attempt. Anchoring on whichever line of `old` does appear verbatim points
    straight at the real text to copy from; when no line of it appears at
    all, checking every other file before giving up separates "this text is
    invented" from "this text is real, but for a different path" -- and only
    the first of those is actually fabrication.
    """
    if (respaced := _whitespace_miss(content, old)) is not None:
        return respaced
    candidates: list[tuple[str, list[int]]] = []
    for stripped in _distinctive_lines(old):
        at = _match_lines(content, stripped)
        if at:
            candidates.append((stripped, at))
    if candidates:
        # Fewest coincidental hits first, then longest (most distinctive) line.
        stripped, at = min(candidates, key=lambda c: (len(c[1]), -len(c[0])))
        return "This line of 'old' does appear, but not the rest of it:\n" + _neighbourhoods(
            content, at
        )
    elsewhere = _find_elsewhere(context, target, old)
    if elsewhere is not None:
        return (
            f"That text is not in this file, but it does appear in {elsewhere} -- "
            f'you may be editing the wrong path. Pass "path": "{elsewhere}" instead '
            "if that is where this change belongs."
        )
    lines = content.splitlines()
    shown = lines[:20]
    head = "\n".join(f"{index:>5} {line}" for index, line in enumerate(shown, start=1))
    more = f"\n  ... {len(lines) - 20} more lines" if len(lines) > 20 else ""
    return f"No line of 'old' appears anywhere in the file. Its actual start:\n{head}{more}"


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
    target = _resolve(context, str(args.get("path", "")))
    _writable(context, target)
    await _permit(context, target, "write")
    old = str(args.get("old") or args.get("old_string") or "")
    new = str(args.get("new") or args.get("new_string") or "")
    if not old:
        raise ToolDenied("DENIED: edit needs 'old', the exact text to replace")
    if not target.is_file():
        missing = _missing(context, target)
        raise ToolDenied(f"{missing} To create a new file, use write.")
    if old == new:
        raise ToolDenied("DENIED: 'old' and 'new' are identical, so this edit changes nothing")
    content = target.read_text(encoding="utf-8")
    found = _match_lines(content, old)
    where = "/".join(_inside(context.root, target))
    if not found:
        raise ToolDenied(
            f"DENIED: that text is not in {where}. It may have changed since you last "
            "saw it, or the indentation may differ.\n"
            + _not_found_hint(context, target, content, old)
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
    target = _resolve(context, str(args.get("path", "")))
    _writable(context, target)
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
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # exist_ok=True only forgives a parent that is already a directory. A
        # generation once wrote a *file* at what should have been a directory
        # component of this same tree (docs/evolution/plans, committed as a
        # file holding the literal text "plan.md" -- the model meant to write
        # docs/evolution/plans/plan.md and wrote the path itself instead), and
        # every candidate since has inherited it from the checkout. Left
        # uncaught this OSError crashes the whole harness job -- silently, from
        # the model's side, since nothing here was its mistake to fix -- which
        # is how every plan-drafting stage since kept losing its plan. Turned
        # into a result instead, the model can see the conflict and route
        # around it.
        raise ToolDenied(
            f"DENIED: cannot create the directory for {where}: {exc}. A path "
            "component already exists as a plain file, not a directory -- "
            "edit or delete that file, or write somewhere else."
        ) from exc
    target.write_text(content, encoding="utf-8")
    context.tally.writes += 1
    verb = "replaced" if before else "created"
    return f"{verb} {where} ({humanize_size(len(content))})\n{diff}" if diff else f"{verb} {where}"


async def tool_delete(context: ToolContext, args: dict[str, Any]) -> str:
    """Remove one file. There was no way to do this before.

    Found live: a generation caught by the codebase hygiene check -- a stray
    script at the repository root, or a new module nothing imports -- goes to
    repair with instructions to "wire it in, or delete the file", and every
    repair job that chose delete had no tool that could. `edit` replaces text
    inside a file that already exists; `write` refuses to touch one. Neither
    removes anything, so every hygiene-triggered repair was structurally
    unwinnable, no matter how clearly the model understood the fix.

    Deliberately narrow: one existing file, never a directory -- there is no
    reason for a job authoring one generation to remove a whole tree, and
    refusing it outright is cheaper than reasoning about what it might take
    with it.
    """
    target = _resolve(context, str(args.get("path", "")))
    _writable(context, target)
    await _permit(context, target, "write")
    where = "/".join(_inside(context.root, target))
    if not target.exists():
        raise ToolDenied(f"DENIED: {where} does not exist")
    if target.is_dir():
        raise ToolDenied(f"DENIED: {where} is a directory, delete only removes one file")
    before = target.read_text(encoding="utf-8", errors="replace")
    diff = _announce(context, target, before, "", kind="delete")
    target.unlink()
    context.tally.deletes += 1
    return f"deleted {where}\n{diff}" if diff else f"deleted {where}"



# `python` is the one program most `shell_allow` lists grant, on the
# assumption it only runs a trivial, side-effect-free snippet -- but the
# interpreter itself has no sandbox, and `subprocess`/`os.system` let it run
# any other program on PATH regardless of what harness.shell_allow says.
# Found live (generation 1219, harness job that produced session 010969): a
# job wrote a real file with `write`, got nervous, then ran `python -c
# "import subprocess; subprocess.run(['git', 'checkout', path])"` through
# this same tool and reverted its own edit -- the harness's own change
# tracking (edit/write/delete) never saw the revert, so `_through_harness`
# read back a clean git tree, decided the job had changed nothing, and
# discarded the whole generation as a no-op. Denylisted here rather than
# trying to sandbox the interpreter itself: this only has to stop a model
# reaching for the obvious escape, not a determined attacker.
PYTHON_ESCAPE_HINT = (
    "DENIED: this python command can run another program (subprocess/os."
    "system/shutil/...) or write/delete a file directly (open(..., 'w'), "
    ".write(, write_text(, os.remove(, .unlink(, ...), either of which "
    "defeats harness.shell_allow and this tool's own edit/write/delete "
    "tracking the same way a pipe would. Found live, both ways: a job "
    "reverted its own edit by shelling out to `git checkout`, and a "
    "separate job rewrote a file with a raw `open(path, 'w').write(...)` "
    "heredoc -- the change (or non-change) never showed up in this job's "
    "recorded diff either time. Use edit/write/delete for every file change; "
    "there is no git status/diff/checkout available here at all, and no "
    "need for one -- trust what edit/write already told you."
)
_PYTHON_ESCAPE_NEEDLES = (
    "subprocess",
    "os.system",
    "os.popen",
    "os.spawn",
    "os.exec",
    "os.fork",
    'importlib.import_module("os")',
    "importlib.import_module('os')",
    '__import__("os")',
    "__import__('os')",
    "shutil.",
    "pty.spawn",
    # Direct file mutation: the same escape as subprocess, just without a
    # second process. `open(...).read()` is fine (and common, for a quick
    # check); it is a *write*-mode open or the write call itself that lets a
    # job change a file with none of edit/write/delete's tracking or
    # fabrication guardrails.
    ".write(",
    ".write_text(",
    ".write_bytes(",
    "os.remove(",
    "os.rename(",
    "os.replace(",
    "os.rmdir(",
    "os.truncate(",
    "os.unlink(",
    ".unlink(",
)

# There is no shell interpreter here (see the docstring below), so these
# never act as operators -- they land as literal arguments to whatever ran
# first, which almost always fails in a way that means nothing to the model.
# Found live: `python -m py_compile environment.py && echo "OK"` handed
# py_compile a nonexistent file named literally `&&` to compile next, and
# came back as `[Errno 2] No such file or directory: '&&'` -- a step spent on
# a self-check the model had no way to interpret, right after it had finally
# started landing closer edits post-fabrication-nudge. `<<'EOF'` (piping a
# heredoc script into `python -`) is the same class of mistake but worse: it
# doesn't fail fast, it hangs for the full shell_seconds timeout, because
# nothing here reads stdin for it -- found live burning 60 of a job's ~240
# spent seconds on exactly that. Checked as a prefix, not exact membership:
# shlex glues `<<'EOF'` into one token, `<<EOF`, not two.
_SHELL_OPERATOR_TOKENS = frozenset({"&&", "||", ";", "|", "&"})
_SHELL_REDIRECT_PREFIXES = ("<<", ">>", "<", ">")


def _flag_value(parts: list[str], flag: str) -> str | None:
    """The value after ``flag`` (``-n 20``) or glued to it (``-n20``), if any."""
    for index, part in enumerate(parts):
        if part == flag and index + 1 < len(parts):
            return parts[index + 1]
        if part.startswith(flag) and part != flag:
            return part[len(flag) :]
    return None


def _operands(parts: list[str], *, valued: tuple[str, ...] = ()) -> list[str]:
    """The non-flag arguments, skipping the value of each flag in ``valued``."""
    operands: list[str] = []
    skip = False
    for part in parts:
        if skip:
            skip = False
        elif part in valued:
            skip = True
        elif not part.startswith("-") or part == "-":
            operands.append(part)
    return operands


async def _shell_as_tool(context: ToolContext, parts: list[str]) -> tuple[str, str] | None:
    """A read-only unix command, done with the tool that already does it.

    Found in the last 250 harness sessions: 380 of 971 refused calls were a
    small model reaching for `ls`, `cd`, `grep`, `git`, `wc`, `cat`, `find`
    through `shell`, each one a step spent learning that `shell` only runs
    python. The ones that only look at files are answered instead, through
    read/ls/grep (the same permission checks, the same clipping), with a note
    naming the tool to call directly next time. ``None`` for anything else.
    """
    program, rest = Path(parts[0]).name.lower().removesuffix(".exe"), parts[1:]
    if program == "pwd":
        return "pwd", (
            ". -- the job root. There is no working directory to change: every tool "
            "takes a path relative to this root."
        )
    if program == "ls":
        target = (_operands(rest) or ["."])[0]
        return f'ls {{"path": "{target}"}}', await tool_ls(context, {"path": target})
    if program == "cat" and len(files := _operands(rest)) == 1:
        return f'read {{"path": "{files[0]}"}}', await tool_read(context, {"path": files[0]})
    if program in ("head", "tail") and len(files := _operands(rest, valued=("-n",))) == 1:
        count = _flag_value(rest, "-n") or next(
            (part[1:] for part in rest if part[1:].isdigit() and part.startswith("-")), "10"
        )
        limit = int(count) if count.lstrip("+").isdigit() else 10
        offset = 1
        if program == "tail":
            target = _resolve_readable(context, files[0])
            if target.is_file():
                total = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
                offset = max(1, total - limit + 1)
        call = {"path": files[0], "offset": offset, "limit": limit}
        return f"read {json.dumps(call)}", await tool_read(context, call)
    if program == "sed" and rest[:1] == ["-n"] and len(rest) == 3:
        span = re.fullmatch(r"(\d+),(\d+)p", rest[1])
        if span is not None:
            first, last = int(span.group(1)), int(span.group(2))
            call = {"path": rest[2], "offset": first, "limit": max(1, last - first + 1)}
            return f"read {json.dumps(call)}", await tool_read(context, call)
    if program == "grep" and (found := _operands(rest, valued=("-e", "-m", "-A", "-B", "-C"))):
        pattern = _flag_value(rest, "-e") or found.pop(0)
        if "-i" in rest or any(part.startswith("-") and "i" in part[1:3] for part in rest):
            pattern = f"(?i){pattern}"
        call = {"pattern": pattern, "path": found[0] if found else "."}
        include = next(
            (part.split("=", 1)[1] for part in rest if part.startswith("--include=")), ""
        )
        if include:
            call["glob"] = include
        return f"grep {json.dumps(call)}", await tool_grep(context, call)
    if program == "wc" and "-l" in rest and len(files := _operands(rest)) == 1:
        target = _resolve_readable(context, files[0])
        if not target.is_file():
            raise ToolDenied(f"DENIED: {_shown(context, target)} does not exist")
        count = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
        return "read (line count)", f"{count} {_shown(context, target)}"
    if program == "find":
        name = _flag_value(rest, "-name") or _flag_value(rest, "-iname")
        roots = [part for part in _operands(rest, valued=("-name", "-iname", "-type")) if part]
        if name:
            base = _resolve_readable(context, roots[0] if roots else ".")
            hits = sorted(
                "/".join(_inside(context.root, path))
                for path in base.rglob(name)
                if not SKIP_DIRECTORIES & set(_inside(context.root, path))
            )
            listing = "\n".join(hits[:40]) or f"no file named {name}"
            if len(hits) > 40:
                listing += f"\n[... {len(hits) - 40} more, narrow the name ...]"
            return f"ls/grep (find -name {name})", listing
    # Only where the root is its own repository (a candidate's worktree): git
    # walks up to the nearest ancestor .git otherwise (rule 11), and an agent's
    # playground under workspace/ would be shown the whole mesh's checkout.
    if program == "git" and rest[:1] in (["status"], ["diff"]) and (context.root / ".git").exists():
        paths = [str(_resolve(context, part)) for part in _operands(rest[1:])]
        argv = (
            ["git", "status", "--short"] if rest[0] == "status" else ["git", "diff", "--", *paths]
        )
        result = await run_command(*argv, cwd=context.root, timeout_seconds=context.shell_seconds)
        body = _clip(result.output.rstrip() or "(nothing)", context.limits, unit="lines")
        return " ".join(argv[:3]), body
    return None


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
    if program in ("python3", "py") and "python" in context.shell_allow:
        # The same interpreter under the name a model trained on Linux reaches for.
        program, parts = "python", ["python", *parts[1:]]
    operators = _SHELL_OPERATOR_TOKENS & set(parts[1:]) or any(
        token.startswith(_SHELL_REDIRECT_PREFIXES) for token in parts[1:]
    )
    if program not in context.shell_allow:
        translated = None if operators else await _shell_as_tool(context, parts)
        if translated is not None:
            tool, body = translated
            return (
                f"[there is no unix shell here: `{raw}` was answered as {tool} -- "
                f"call that tool directly next time]\n{body}"
            )
        allowed = ", ".join(sorted(context.shell_allow))
        where = (
            " There is no working directory to change: every tool takes a path "
            "relative to the job root."
            if program == "cd"
            else " Files are read with read, grep and ls, which are tools of their own."
        )
        raise ToolDenied(
            f"DENIED: {program} is not in harness.shell_allow (allowed: {allowed}).{where}"
        )
    if program == "python" and any(needle in raw for needle in _PYTHON_ESCAPE_NEEDLES):
        raise ToolDenied(PYTHON_ESCAPE_HINT)
    if operators:
        raise ToolDenied(
            "DENIED: there is no shell interpreter here, so `&&`, `||`, `;`, "
            "`|`, `&`, `<`, `<<`, `>` and `>>` are not operators -- they "
            "would reach the program as literal arguments (or, for `<`/`<<`, "
            "just hang until the timeout, since nothing reads that input). "
            "Run one plain command per `shell` call, with no redirection."
        )
    try:
        result = await run_command(
            parts[0], *parts[1:], cwd=context.root, timeout_seconds=context.shell_seconds
        )
    except OSError as exc:
        raise ToolDenied(f"DENIED: {program} could not be started: {exc}") from exc
    if result.timed_out:
        # A result, not an exception: a command that ran too long is something
        # the model can work around, and a tool that can hang is a worker that
        # never comes back and a queue that never drains. The process itself
        # is already dead -- run_command's own timeout killed it -- so this
        # is just reporting that, not still waiting on anything.
        raise ToolDenied(
            f"DENIED: {program} did not finish within {context.shell_seconds:.0f}s"
        )
    context.tally.reads += 1
    body = _clip(result.output.rstrip(), context.limits, unit="lines")
    return f"exit {result.exit_code}\n{body}" if body else f"exit {result.exit_code}"


def custom_tool_program(definition: ToolDefinition) -> str:
    """The allow-list name a custom tool's command answers to.

    Exposed separately from build_custom_tool() so a caller can decide
    whether to offer the tool's schema at all -- the same "an unusable tool
    in the schema is a tool a model will try" reasoning build_runner already
    applies to the shell and fetch tools -- without needing a ToolContext yet.
    """
    try:
        parts = shlex.split(definition.command, posix=True)
    except ValueError as exc:
        raise ValueError(f"{definition.name}: command could not be parsed: {exc}") from exc
    if not parts:
        raise ValueError(f"{definition.name}: command is empty")
    program = Path(parts[0]).name.lower()
    return program[:-4] if program.endswith(".exe") else program


def build_custom_tool(definition: ToolDefinition, *, tool_dir: Path | None = None) -> Tool:
    """Turn a declarative TOOL.md into a real, named, described tool.

    Backed by the exact allow-listed subprocess path tool_shell uses: a
    parameter's value is appended as one more argv entry after the command's
    own fixed parts, never interpolated into a string that gets re-parsed, so
    a custom tool can never run anything beyond what its own command already
    names -- and never anything at all unless that program is allow-listed.

    A harness job's root is whatever the *job* is about -- an agent's own
    playground, not necessarily this project's tree -- so a bundled script
    referenced by a plain relative path would resolve against the wrong
    directory the moment a job runs anywhere else. ``{tool_dir}`` in the
    command is substituted with the tool's own absolute directory before
    parsing, so `"{tool_dir}/scripts/check.py"` finds the script regardless
    of where the calling job is rooted.
    """
    command = definition.command
    if tool_dir is not None:
        # Resolved here, not trusted from the caller: a relative tool_dir
        # would defeat the entire reason this exists, silently resolving
        # against whatever job happens to be running instead of the tool's
        # own directory -- exactly the bug this placeholder replaces.
        command = command.replace("{tool_dir}", str(tool_dir.resolve(strict=False)))
    base = shlex.split(command, posix=True)
    program = custom_tool_program(definition)

    async def run(context: ToolContext, args: dict[str, Any]) -> str:
        if program not in context.shell_allow:
            allowed = ", ".join(sorted(context.shell_allow))
            raise ToolDenied(
                f"DENIED: {definition.name} runs '{program}', which is not in "
                f"harness.shell_allow (allowed: {allowed})"
            )
        missing = [
            param.name
            for param in definition.parameters
            if param.required and not args.get(param.name)
        ]
        if missing:
            raise ToolDenied(f"DENIED: {definition.name} needs: {', '.join(missing)}")
        values = [str(args[param.name]) for param in definition.parameters if param.name in args]
        try:
            result = await run_command(
                base[0], *base[1:], *values, cwd=context.root, timeout_seconds=context.shell_seconds
            )
        except OSError as exc:
            raise ToolDenied(f"DENIED: {definition.name} could not be started: {exc}") from exc
        if result.timed_out:
            raise ToolDenied(
                f"DENIED: {definition.name} did not finish within {context.shell_seconds:.0f}s"
            )
        context.tally.reads += 1
        body = _clip(result.output.rstrip(), context.limits, unit="lines")
        return f"exit {result.exit_code}\n{body}" if body else f"exit {result.exit_code}"

    return Tool(
        name=definition.name,
        description=definition.description,
        parameters=definition.parameters_schema(),
        run=run,
    )


async def tool_fetch(context: ToolContext, args: dict[str, Any]) -> str:
    """Fetch one URL and return its main content as Markdown, via Scrapling.

    Seventh, and off unless a human has provisioned Scrapling into its own
    environment (scripts/install-scrapling.ps1) and pointed scraping.executable
    at it -- it is not a runtime dependency of this project (CLAUDE.md rule
    16), so there is nothing to fall back to.

    Static by default: `extract get`, no browser, fast. ``dynamic: true``
    switches to `extract fetch`, a real headless Chromium -- needed for a page
    whose content is rendered by client-side JavaScript, and only present if
    `scripts/install-scrapling.ps1 -WithBrowser` (or `scrapling install` run
    by hand from that venv) already pulled the browser down; a plain install
    refuses this branch by naming what is missing, same as everything else
    that is off until configured.
    """
    url = str(args.get("url") or "").strip()
    if not url:
        raise ToolDenied("DENIED: fetch needs a url")
    if not context.scraping_executable:
        raise ToolDenied(
            "DENIED: no fetcher is configured. Run scripts/install-scrapling.ps1 "
            "and set scraping.enabled and scraping.executable in evomesh.yaml."
        )
    dynamic = bool(args.get("dynamic"))
    css_selector = str(args.get("css_selector") or "").strip()
    with tempfile.TemporaryDirectory(prefix="evomesh-fetch-") as scratch:
        output_path = Path(scratch) / "page.md"
        if dynamic:
            # Browser commands take the timeout in milliseconds, not seconds --
            # a different unit on the same CLI, not a typo.
            arguments = [
                "extract",
                "fetch",
                url,
                str(output_path),
                "--ai-targeted",
                "--timeout",
                str(int(context.scraping_timeout * 1000)),
            ]
        else:
            arguments = [
                "extract",
                "get",
                url,
                str(output_path),
                "--ai-targeted",
                "--timeout",
                str(int(context.scraping_timeout)),
            ]
        if css_selector:
            arguments += ["--css-selector", css_selector]
        try:
            result = await run_command(
                context.scraping_executable,
                *arguments,
                # A browser launch is real overhead on top of the page's own
                # timeout, not covered by --timeout above; the static path
                # gets the same margin rather than a second code path.
                timeout_seconds=context.scraping_timeout + 30,
            )
        except OSError as exc:
            raise ToolDenied(f"DENIED: the fetcher could not be started: {exc}") from exc
        if result.timed_out:
            raise ToolDenied(f"DENIED: fetching {url} did not finish in time")
        if result.exit_code != 0 or not output_path.exists():
            hint = (
                " (the browser may not be installed -- see "
                "scripts/install-scrapling.ps1 -WithBrowser)"
                if dynamic
                else ""
            )
            detail = (result.output.strip() or "no output") + hint
            raise ToolDenied(f"DENIED: could not fetch {url}: {detail}")
        content = await asyncio.to_thread(output_path.read_text, encoding="utf-8")
    context.tally.reads += 1
    return _clip(content, context.limits, unit="lines")


async def tool_ask_agent(context: ToolContext, args: dict[str, Any]) -> str:
    """Ask another live agent a question and return its answer.

    The mesh already lets one agent's own goal cycle send another a message
    (NewsWatcher -> Trader, say), but that lands in an inbox some unknown
    number of cycles later -- fine for "here is something you should know",
    wrong for "what is your current position" answered mid-plan. This
    reaches the exact same reactive path a human's own `/chat <agent>`
    console command uses (AgentRuntime._handle, via a private one-shot
    mailbox so this call's own reply can never be raced by that agent's
    ordinary background message loop) and waits for a real answer.
    """
    if context.ask_agent is None:
        raise ToolDenied("DENIED: no other agent is reachable from this job.")
    agent = str(args.get("agent") or "").strip()
    question = str(args.get("question") or "").strip()
    if not agent:
        raise ToolDenied("DENIED: ask_agent needs an agent name or id.")
    if not question:
        raise ToolDenied("DENIED: ask_agent needs a question.")
    try:
        answer = await context.ask_agent(agent, question)
    except TimeoutError:
        raise ToolDenied(f"DENIED: {agent} did not answer in time.") from None
    except (KeyError, LookupError):
        raise ToolDenied(f"DENIED: no agent named {agent!r} is running.") from None
    except ValueError as exc:
        raise ToolDenied(f"DENIED: {exc}") from None
    context.tally.reads += 1
    return _clip(answer, context.limits, unit="lines")


async def tool_learn_skill(context: ToolContext, args: dict[str, Any]) -> str:
    """Write a new skill for this agent's own future use, or update one it
    already wrote.

    Only for a procedure actually worked out and used in this job (or a
    recent one) -- never one only planned, and never a restatement of what a
    tool's own description already says. Use it after combining more than
    one tool call in a way that is not already covered by an existing skill
    (check the skill catalog at the top of this job's prompt first) and that
    is likely to come up again -- the same judgment a human would use before
    writing one by hand. A skill this agent overwrites has to be one it
    authored itself; one already curated by a human is refused, the same as
    a human's own `/skill install` never letting a stray file clobber a
    deliberately written one by accident.
    """
    if context.learn_skill is None:
        raise ToolDenied("DENIED: this agent has not been granted skill-authoring access.")
    name = str(args.get("name") or "").strip()
    description = str(args.get("description") or "").strip()
    body = str(args.get("body") or "").strip()
    if not name:
        raise ToolDenied("DENIED: learn_skill needs a name.")
    if not description:
        raise ToolDenied("DENIED: learn_skill needs a one-line description.")
    if not body:
        raise ToolDenied("DENIED: learn_skill needs a body -- the procedure itself.")
    try:
        result = await context.learn_skill(name, description, body)
    except ValueError as exc:
        raise ToolDenied(f"DENIED: {exc}") from None
    context.tally.writes += 1
    return result


async def tool_patch_skill(context: ToolContext, args: dict[str, Any]) -> str:
    """A small, targeted fix to a skill this same agent already wrote --
    replace exactly one occurrence of old_text with new_text, the same
    unique-match contract the harness's own edit tool uses on a real file.
    Prefer this over learn_skill for a small correction: cheaper to write,
    and the refusal on a non-unique match is what keeps a fix from landing
    somewhere it was not meant to.
    """
    if context.patch_skill is None:
        raise ToolDenied("DENIED: this agent has not been granted skill-authoring access.")
    name = str(args.get("name") or "").strip()
    old_text = str(args.get("old_text") or "")
    new_text = str(args.get("new_text") or "")
    if not name:
        raise ToolDenied("DENIED: patch_skill needs a name.")
    if not old_text:
        raise ToolDenied("DENIED: patch_skill needs 'old_text', the exact text to replace.")
    try:
        result = await context.patch_skill(name, old_text, new_text)
    except ValueError as exc:
        raise ToolDenied(f"DENIED: {exc}") from None
    context.tally.edits += 1
    return result


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
    Tool(
        name="delete",
        description=(
            "Remove one file. Refuses a directory or a path that does not "
            "exist. The only way to undo a write or fix a hygiene check that "
            "flagged something you created."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the job root."},
            },
            "required": ["path"],
        },
        run=tool_delete,
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

WEB_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="fetch",
        description=(
            "Fetch a URL and return its main content as Markdown -- navigation, "
            "ads and scripts stripped. Static by default: content that JavaScript "
            "renders client-side will not appear. Set dynamic=true for that -- "
            "slower, and only works if a browser is installed for it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The page to fetch."},
                "css_selector": {
                    "type": "string",
                    "description": "Optional CSS selector to return only matching content.",
                },
                "dynamic": {
                    "type": "boolean",
                    "description": (
                        "Render with a real browser instead of a plain HTTP request. "
                        "Try false first; only set true if the content is missing."
                    ),
                },
            },
            "required": ["url"],
        },
        run=tool_fetch,
    ),
)

ASK_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="ask_agent",
        description=(
            "Ask another live agent in this mesh a question and wait for its "
            "real answer -- not a message that sits in its inbox until its "
            "next cycle. Use the agent's name or id (see the mesh roster, "
            "or /agents on the console)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "The other agent's name or id.",
                },
                "question": {"type": "string", "description": "What to ask it."},
            },
            "required": ["agent", "question"],
        },
        run=tool_ask_agent,
    ),
)

LEARN_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="learn_skill",
        description=(
            "Save a procedure you just worked out (which tools, in what order, "
            "producing what) as a skill for your own future use -- only when it "
            "is not already covered by a skill in your catalog, and only after "
            "actually using it, not merely planning to. A skill is prose, read "
            "later by your own future self the same way you read any other "
            "file; it runs nothing on its own."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "kebab-case, e.g. 'news-report-export'.",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "One line: when this applies. This is the only part "
                        "your future self sees before deciding whether to "
                        "read the rest -- be specific, not generic."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "The procedure itself, in Markdown: which tools, in "
                        "what order, what to check first, what never to do. "
                        "Not a restatement of what a tool's own description "
                        "already says."
                    ),
                },
            },
            "required": ["name", "description", "body"],
        },
        run=tool_learn_skill,
    ),
    Tool(
        name="patch_skill",
        description=(
            "Fix or extend one exact piece of a skill you already wrote, "
            "instead of resending the whole thing through learn_skill -- the "
            "same unique-match contract the edit tool uses on a real file: "
            "old_text must appear exactly once."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill's name, e.g. 'news-report-export'.",
                },
                "old_text": {
                    "type": "string",
                    "description": (
                        "The exact text to replace, with enough surrounding "
                        "context to appear exactly once in the skill's file."
                    ),
                },
                "new_text": {
                    "type": "string",
                    "description": "What to replace it with.",
                },
            },
            "required": ["name", "old_text", "new_text"],
        },
        run=tool_patch_skill,
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
