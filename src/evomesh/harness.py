"""A model that can look at the project before it answers.

Everything else in this package asks a model one question and takes whatever
comes back. That is why a mutation has to be a whole file in one answer: there
is no shape here for a model that comes back and asks something first.

This module is that shape. Send the transcript and the tool schemas, run
whatever the model asked for, append the results, send again -- until it answers
without calling a tool, or until a cap ends the job.

Two front ends drive the same tools. Models that have tool calling in their chat
template use it; the ones that fit on a small card mostly do not, so they get a
one-line JSON protocol in plain text instead. Dropping the second front end
would mean the harness only works on hardware this project was written not to
require.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from evomesh.cognition import strip_reasoning
from evomesh.harness_session import HarnessSession
from evomesh.harness_tools import (
    ALL_TOOLS,
    ASK_TOOLS,
    LEARN_TOOLS,
    READ_ONLY_TOOLS,
    SHELL_TOOLS,
    WEB_TOOLS,
    WRITE_TOOLS,
    Tool,
    ToolContext,
    ToolLimits,
    ToolRegistry,
)
from evomesh.models import (
    ChatMessage,
    ChatTurn,
    ModelProvider,
    ModelUnavailableError,
    ToolCall,
    ToolsUnsupportedError,
)
from evomesh.processes import run_command

logger = logging.getLogger(__name__)

SYSTEM = (
    "You are a careful engineer working inside a real project. Use the tools to "
    "look at the code before you answer -- never guess a file's contents. Prefer "
    "grep to find where something lives, then read only the part you need. When "
    "you know the answer, state it plainly and name the files it came from. Keep "
    "the answer short."
)

WRITE_SYSTEM = (
    "You are a careful engineer working inside a real project, and your changes "
    "are real. Read a file before you change it -- always. Use edit for a file "
    "that exists and write only for one that does not.\n"
    "edit replaces an exact piece of text and REFUSES unless that text appears "
    "exactly once in the file, so include the surrounding lines that make it "
    "unique. If it refuses, do not guess: read the file and widen your anchor.\n"
    "Make the smallest change that does the job, then say what you changed and "
    "why. Never rewrite a whole file to alter one line."
)

# The text front end has to teach the protocol as well as the task, because the
# model has no schema to conform to -- only this paragraph.
TEXT_SYSTEM = (
    "You are a careful engineer working inside a real project. You cannot see "
    "the files; you ask for them with tools.\n"
    "To use a tool, reply with ONE line of JSON and nothing else:\n"
    '{"tool": "read", "args": {"path": "src/evomesh/bdi.py", "offset": 1, "limit": 80}}\n'
    "You will be given the result and may then use another tool.\n"
    "When you can answer, reply with the answer as plain text and no JSON. "
    "Name the files it came from. Never guess a file's contents."
)

# Appended to TEXT_SYSTEM only when HarnessRunner.structured_fallback is on
# (see _ask() below) -- structured_fallback constrains OllamaProvider.
# generate() to TEXT_PROTOCOL_FORMAT, which forces the *entire* response to
# be one JSON object, so the plain-text final answer TEXT_SYSTEM asks for
# above is no longer a shape the model can produce. This line replaces that
# instruction with an equally terminal JSON shape instead, left off by
# default so a mesh that never opts in never sees its fallback prompt change.
TEXT_SYSTEM_STRUCTURED_SUFFIX = (
    "\nYour reply is grammar-constrained to JSON. When you can answer, use "
    'an \'answer\' key instead of \'tool\'/\'args\': {"answer": "..."}.'
)

# The envelope structured_fallback constrains OllamaProvider.generate() to,
# on the text-protocol fallback path -- see HarnessRunner.structured_fallback
# and _ask() below. Both "tool" and "answer" are optional at the schema level
# (required=[]) because Ollama's `format` forces the *entire* response to
# validate against this shape, and the fallback protocol genuinely has two
# distinct terminal cases (call a tool, or give the final answer) -- a schema
# that required one specific key would make the other case impossible to
# express. parse_text_call() below treats "answer" as a fourth, terminal
# spelling alongside its existing three tool-name spellings, unconditionally
# (harmless when structured_fallback is off: a model that never sends an
# "answer" key simply never exercises this branch).
TEXT_PROTOCOL_FORMAT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {"type": "string"},
        "args": {"type": "object"},
        "answer": {"type": "string"},
    },
    "required": [],
}

OUTCOMES = ("answered", "capped", "failed")

# Said once, when a writing job is past halfway and has changed nothing. A 27B
# model spent all twenty steps and thirty-two tool calls reading -- re-reading
# the same two files at different offsets, which the repeat guard cannot see --
# and the job ended having done nothing. A model cannot budget what it cannot
# see, and this is the cheapest way to show it.
BUDGET_NOTE = (
    "You are on step {step} of {limit} and have not changed a file yet. Stop "
    "surveying and make the smallest change that does the job now; you can "
    "read more afterwards if the edit is refused."
)

# Said once, after `edit` has been denied twice in a row for text that does
# not exist in the file at all (not just stale or mis-indented). Two of those
# back to back is not bad luck -- it is the model composing `old` from what it
# thinks the code should say, rather than from what `read` actually showed it.
# Found live (generation 1219/1220, sessions 010969 and 010979 both): a job
# read the real file correctly, then submitted seven straight `edit` calls
# whose `old` text matched none of it, describing a plausible-looking function
# that was never in the file -- and never once responded to the denial's own
# "actual start" excerpt by copying from it. The denial already shows the real
# text; this says, once, to use it verbatim instead of guessing again.
FABRICATION_HINT = (
    "That is the second edit in a row where 'old' matches nothing in the "
    "file -- not stale, not mis-indented, just not there. Stop composing "
    "'old' from what the change should look like. Call `read` on the exact "
    "lines you are about to change, then paste that output's text into "
    "'old' character-for-character, including its indentation."
)

# Said once, to a model whose tool call did not parse. Observed on gemma:2b: it
# opens the object, forgets a brace, and the reply is neither a call nor an
# answer -- accepting it as the answer ends a job that had not finished.
BROKEN_CALL_HINT = (
    "That was not a usable tool call. Reply with EXACTLY one line of JSON, every "
    'brace closed: {"tool": "<name>", "args": {...}}. If you can already answer, '
    "reply with plain text and no JSON at all."
)

# Observed on llama3.1:8B: told that its anchor was ambiguous, it worked out the
# fix, wrote the corrected call as prose, and stopped. Text is not executed, and
# a job that ends holding the answer to its own problem is the worst way to end.
TEXT_CALL_HINT = (
    "You wrote a call to {name} as text, and text is not run. Issue it as a real "
    "tool call. If you are finished instead, answer in plain prose."
)

# Said whenever HarnessSettings.self_check_command is configured and a job
# that changed a file tries to end while that command still fails -- so
# "answered" means the code actually passes the project's own lint/type/test
# gate, not just that the model stopped asking for tools.
SELF_CHECK_HINT = (
    "Before this can be the answer, the project's own self-check command "
    "found problems with what you changed. Fix them, then answer again.\n\n"
    "{output}"
)


@dataclass
class HarnessResult:
    """What a job did, in the terms the caller has to act on.

    ``capped`` is deliberately not ``failed``: the job ran out of room rather
    than going wrong, and the two need different responses -- the same line
    validation draws between a candidate that failed and a run the host blocked.
    """

    outcome: str
    answer: str = ""
    steps: int = 0
    tool_calls: int = 0
    seconds: float = 0.0
    detail: str = ""
    session_path: Path | None = None
    used_tool_protocol: str = "none"
    reads: int = 0
    edits: int = 0
    writes: int = 0
    deletes: int = 0
    # Everything the tools produced, and the largest transcript the model was
    # actually sent. The second is what says whether this job would survive on a
    # smaller model, and it is otherwise invisible.
    tool_chars: int = 0
    prompt_chars: int = 0

    @property
    def changed_files(self) -> int:
        return self.edits + self.writes + self.deletes

    def summary(self) -> str:
        where = f", session: {self.session_path}" if self.session_path else ""
        # Reads before changes, in that order, because the ratio is the number
        # worth seeing: a job that changed three files having read none is the
        # invented-module failure wearing a different hat.
        changes = f", {self.reads} read/{self.changed_files} changed" if self.changed_files else ""
        cost = f", {self.prompt_chars} prompt chars" if self.prompt_chars else ""
        return (
            f"{self.steps} step{'s' if self.steps != 1 else ''}, "
            f"{self.seconds:.1f} s, {self.tool_calls} tool call"
            f"{'s' if self.tool_calls != 1 else ''}{changes}{cost}, "
            f"{self.used_tool_protocol}{where}"
        )


# Assistant turns kept whole however tight the budget: what the model was just
# doing. Older ones keep this much of their narration and of each long argument.
RECENT_TURNS = 3
ELIDED_KEEP = 160
# The newest tool result is never cut below this: it is what the model asked
# for one step ago, and a job that cannot see it cannot do anything with it.
NEWEST_RESULT_FLOOR = 1500
_ELIDED = "chars elided]"


def message_size(message: ChatMessage) -> int:
    """What a message really costs the model: its text and its tool calls'
    arguments, which go back to the server on every turn as well."""
    return len(message.content) + sum(
        len(json.dumps(call.arguments, default=str)) for call in message.tool_calls
    )


def _elide(text: str) -> str:
    if len(text) <= ELIDED_KEEP + 40 or text.endswith(_ELIDED):
        return text
    return f"{text[:ELIDED_KEEP]} [... {len(text) - ELIDED_KEEP} {_ELIDED}"


def _elided_turn(message: ChatMessage) -> ChatMessage:
    """An old assistant turn, its narration and its long arguments cut short.

    A call carrying a thought_signature is left exactly as it was: Gemini checks
    that opaque value against the call it was minted for (see ToolCall).
    """
    calls = [
        call
        if call.thought_signature
        else replace(
            call,
            arguments={
                key: _elide(value) if isinstance(value, str) else value
                for key, value in call.arguments.items()
            },
        )
        for call in message.tool_calls
    ]
    return replace(message, content=_elide(message.content), tool_calls=calls)


def compact(messages: list[ChatMessage], limit: int) -> tuple[list[ChatMessage], int]:
    """Cut the transcript down to ``limit``, oldest first, and say so.

    Rule 3 applied to the loop rather than to one tool. In order, until it fits:
    the oldest tool results (a file can simply be read again); then the
    narration and long arguments of all but the last few assistant turns; then
    the results of the newest turn but its last; and only then the newest
    result itself, never below NEWEST_RESULT_FLOOR. The task is never touched.
    What is dropped leaves a marker naming the tool and the size, so the model
    can tell "I have not read that" from "I read it and it said nothing".

    Found live 2026-09-24: this used to cut tool results only, and count only
    message text. The model's own narration was never cut, so past a point the
    task plus what the model had said about it outgrew the whole budget and
    *every* new result was dropped before it was seen -- 20 of 250 recent jobs,
    6% of all steps, run blind (generation 1375 from step 48 of 60, sure by then
    that its read tool was returning fabricated content). Tool-call arguments
    (a `write`'s whole file, a `python -c` script) were sent every turn and never
    counted at all: 14429 characters of them in that one job.
    """
    total = sum(message_size(message) for message in messages)
    if total <= limit:
        return messages, total
    kept = list(messages)
    assistants = [index for index, message in enumerate(kept) if message.role == "assistant"]
    newest_turn = assistants[-1] if assistants else 0

    def swap(index: int, message: ChatMessage) -> None:
        nonlocal total
        total += message_size(message) - message_size(kept[index])
        kept[index] = message

    def drop_results(indices: Iterable[int]) -> None:
        for index in indices:
            if total <= limit:
                return
            message = kept[index]
            if index == 0 or message.role != "tool" or message.content.startswith("[dropped"):
                continue
            swap(
                index,
                replace(
                    message,
                    content=f"[dropped {len(message.content)} characters of "
                    f"{message.name or 'tool'} output; run it again if you still need it]",
                ),
            )

    drop_results(range(newest_turn))
    for index in assistants[:-RECENT_TURNS]:
        if total <= limit:
            break
        swap(index, _elided_turn(kept[index]))
    drop_results(range(newest_turn + 1, len(kept) - 1))
    last = kept[-1]
    if total > limit and len(kept) > 1 and last.role == "tool":
        room = max(NEWEST_RESULT_FLOOR, len(last.content) - (total - limit))
        if room < len(last.content):
            cut = len(last.content) - room
            swap(
                len(kept) - 1,
                replace(
                    last,
                    content=f"{last.content[:room]}\n[... {cut} more characters cut to fit "
                    "the transcript -- ask for a narrower range ...]",
                ),
            )
    return kept, total


def call_key(call: ToolCall) -> str:
    return f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"


# Safe to answer from cache no matter how many other calls came between the
# two identical ones, because these three (and only these three) are pure:
# same path, same args, same job root untouched by a write since -> same
# bytes back, guaranteed. WRITE_NAMES is the other half of that guarantee --
# the exact tool names that can invalidate the cache below, kept in sync
# with harness_tools.WRITE_TOOLS rather than spelled out separately.
CACHEABLE_NAMES = frozenset(tool.name for tool in READ_ONLY_TOOLS)
WRITE_NAMES = frozenset(tool.name for tool in WRITE_TOOLS)


@dataclass
class HarnessRunner:
    provider: ModelProvider
    context: ToolContext
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    session: HarnessSession = field(default_factory=lambda: HarnessSession(None))
    model: str | None = None
    num_ctx: int | None = None
    max_steps: int = 24
    max_seconds: float = 300.0
    # What the model may be sent in one turn. The tools cap their own output;
    # this caps the pile of it, which is the part that grows without asking.
    transcript_chars: int = 12000
    system: str = SYSTEM
    tool_chars: int = 0
    prompt_chars: int = 0
    # See HarnessSettings.self_check_command -- empty turns this off.
    self_check_command: str = ""
    self_check_max_attempts: int = 2
    # See HarnessSettings.structured_fallback -- off by default.
    structured_fallback: bool = False
    _self_check_attempts: int = field(default=0, init=False, repr=False)
    _self_check_last_output: str = field(default="", init=False, repr=False)
    # A separate flag from the output string above: a check can fail (a
    # non-zero exit) while printing nothing at all, and the answer must
    # still say so rather than silently passing a job that never actually
    # cleared its own self-check.
    _self_check_failed: bool = field(default=False, init=False, repr=False)

    async def run(self, task: str) -> HarnessResult:
        started = time.monotonic()
        messages = [ChatMessage(role="user", content=task)]
        native = True
        calls_made = 0
        corrected = False
        last_call = ""
        repeats = 0
        nudged = False
        fabrications = 0
        fabrication_nudged = False
        seen: dict[str, str] = {}
        self.tool_chars = 0
        self.prompt_chars = 0
        self.session.record("job", task=task, root=str(self.context.root))

        for step in range(1, self.max_steps + 1):
            elapsed = time.monotonic() - started
            if elapsed > self.max_seconds:
                return self._end(
                    "capped", started, step - 1, calls_made, native,
                    detail=f"the {self.max_seconds:.0f}s wall clock ran out",
                )
            # Half the budget spent with nothing changed is the shape of a job
            # that will end having only read. Said once: twice is noise.
            if (
                not nudged
                and self.context.allow_write
                and step > self.max_steps // 2
                and not self.context.tally.edits
                and not self.context.tally.writes
                and not self.context.tally.deletes
            ):
                nudged = True
                self.session.record("budget", step=step, limit=self.max_steps)
                messages.append(
                    ChatMessage(
                        role="user",
                        content=BUDGET_NOTE.format(step=step, limit=self.max_steps),
                    )
                )
            messages, size = compact(messages, self.transcript_chars)
            self.prompt_chars = max(self.prompt_chars, size)
            try:
                turn, native = await self._ask(messages, native)
            except ModelUnavailableError as exc:
                return self._end(
                    "failed", started, step, calls_made, native, detail=str(exc)
                )

            self.session.record(
                "turn",
                step=step,
                text=turn.text,
                tools=[call.name for call in turn.tool_calls],
            )
            if not turn.tool_calls:
                # Said once per attempt, never twice in a row: a model that
                # cannot produce the protocol after being shown it will not
                # produce it on the third telling either, and the step budget is
                # better spent letting the job end with what it has.
                unexecuted = self._unexecuted_call(turn.text) if native else ""
                if (unexecuted or looks_like_broken_call(turn.text)) and not corrected:
                    corrected = True
                    self.session.record("malformed", text=turn.text[:400])
                    messages.append(ChatMessage(role="assistant", content=turn.text))
                    messages.append(
                        ChatMessage(
                            role="user",
                            content=TEXT_CALL_HINT.format(name=unexecuted)
                            if unexecuted
                            else BROKEN_CALL_HINT,
                        )
                    )
                    continue
                hint = await self._self_check_feedback()
                if hint is not None:
                    messages.append(ChatMessage(role="assistant", content=turn.text))
                    messages.append(ChatMessage(role="user", content=hint))
                    continue
                answer = turn.text
                if self._self_check_failed:
                    detail = self._self_check_last_output or "(no output)"
                    answer = (
                        f"{answer}\n\n[self-check still reports problems after "
                        f"{self._self_check_attempts} attempt(s):]\n{detail[:1000]}"
                    )
                return self._end(
                    "answered", started, step, calls_made, native, answer=answer
                )

            corrected = False
            messages.append(
                ChatMessage(role="assistant", content=turn.text, tool_calls=turn.tool_calls)
            )
            for call in turn.tool_calls:
                key = call_key(call)
                if key == last_call:
                    repeats += 1
                    if repeats >= 2:
                        return self._end(
                            "capped", started, step, calls_made, native,
                            detail=f"the same {call.name} call three times in a row",
                        )
                    # Answered from the first result rather than run again: it
                    # would produce the same bytes and cost a step.
                    result = (
                        f"{seen[key]}\n[this is the same {call.name} call as last "
                        "time, and the same answer. Do something else.]"
                    )
                    self.session.record("repeat", name=call.name, args=call.arguments)
                elif call.name in CACHEABLE_NAMES and key in seen:
                    # Not just the immediately previous call -- found live: 116
                    # of 1870 tool calls across 60 recent jobs were an exact
                    # repeat of an earlier call in the same job, and every one
                    # of them was non-adjacent (something else came between),
                    # so the check above never once caught it. A small model
                    # re-reading a file it already has is a step spent
                    # producing nothing new, not a mistake worth re-running.
                    repeats = 0
                    result = (
                        f"{seen[key]}\n[this is the same {call.name} call as "
                        "earlier in this job, and nothing has written to this "
                        "job's files since -- the same answer. Do something else.]"
                    )
                    self.session.record("cached", name=call.name, args=call.arguments)
                else:
                    repeats = 0
                    result = await self._invoke(call)
                    if call.name == "edit" and result.startswith(
                        "DENIED: that text is not in"
                    ):
                        fabrications += 1
                        if fabrications >= 2 and not fabrication_nudged:
                            fabrication_nudged = True
                            self.session.record("fabrication", step=step)
                            result = f"{result}\n\n{FABRICATION_HINT}"
                    elif call.name in WRITE_NAMES and not result.startswith("DENIED"):
                        # Only a landed change earns a clean slate. Found live:
                        # the model re-read the file between two fabricated
                        # edits (checking itself, reasonably) and that alone
                        # reset the count to zero, so the nudge needed a third
                        # fabrication instead of a second -- one that never
                        # came before the job ran out of steps to act on it.
                        fabrications = 0
                    if call.name in WRITE_NAMES:
                        # A cached read answered from before this write would
                        # be stale, possibly hiding the model's own edit from
                        # it. Correctness over cache-hit rate: every prior
                        # read in this job is invalidated, not just the one
                        # whose path this write actually touched.
                        seen.clear()
                    seen[key] = result
                    self.tool_chars += len(result)
                last_call = key
                calls_made += 1
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=result,
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )

        return self._end(
            "capped", started, self.max_steps, calls_made, native,
            detail=f"the {self.max_steps}-step budget ran out",
        )

    async def _ask(self, messages: list[ChatMessage], native: bool) -> tuple[ChatTurn, bool]:
        """One model turn, falling back to the text protocol for good.

        The fallback is permanent within a job: a model that refused tools on
        the first turn will refuse on the seventh, and re-trying each time would
        spend a whole step budget discovering the same thing.
        """
        if native:
            try:
                turn = await self.provider.chat(
                    messages,
                    tools=self.registry.schemas(),
                    system=self.system,
                    model=self.model,
                    num_ctx=self.num_ctx,
                )
                return ChatTurn(text=strip_reasoning(turn.text), tool_calls=turn.tool_calls), True
            except ToolsUnsupportedError as exc:
                logger.info("harness: falling back to the text protocol (%s)", exc)
                self.session.record("fallback", reason=str(exc))
        text_system = TEXT_SYSTEM + (
            TEXT_SYSTEM_STRUCTURED_SUFFIX if self.structured_fallback else ""
        )
        answer = await self.provider.generate(
            self._render(messages),
            system=f"{text_system}\n\nTools:\n{self.registry.describe()}",
            model=self.model,
            num_ctx=self.num_ctx,
            format=TEXT_PROTOCOL_FORMAT if self.structured_fallback else None,
        )
        return parse_text_call(strip_reasoning(answer)), False

    def _render(self, messages: list[ChatMessage]) -> str:
        """The transcript as one prompt, for a model with no message roles.

        Tool results are labelled by name rather than by id: a small model reads
        "read ->" and knows what it is looking at, where a hex correlation id is
        noise it will try to interpret.
        """
        lines: list[str] = []
        for index, message in enumerate(messages):
            if message.role == "user":
                lines.append(f"TASK: {message.content}" if index == 0 else message.content)
            elif message.role == "assistant":
                asked = ", ".join(call.name for call in message.tool_calls)
                lines.append(f"YOU: {message.content or f'(used {asked})'}")
            elif message.role == "tool":
                lines.append(f"{message.name} ->\n{message.content}")
        lines.append("Reply with one tool call as JSON, or with the final answer as text.")
        return "\n\n".join(lines)

    def _unexecuted_call(self, text: str) -> str:
        """The name of a tool the model described instead of calling.

        Only meaningful on the native front end, where an answer with no tool
        calls is normally the end of the job. A model that has tools and still
        writes one out in prose has not finished; it has misused the interface,
        and one reminder is cheaper than losing the work.
        """
        attempted = parse_text_call(text)
        if attempted.tool_calls and attempted.tool_calls[0].name in self.registry.tools:
            return attempted.tool_calls[0].name
        return ""

    async def _invoke(self, call: ToolCall) -> str:
        result = await self.registry.invoke(self.context, call.name, call.arguments)
        self.session.record(
            "tool", name=call.name, args=call.arguments, chars=len(result), result=result
        )
        return result

    async def _self_check_feedback(self) -> str | None:
        """A user-turn message to send back and keep the job going, or None
        to let it end as answered.

        None covers four different reasons, deliberately not distinguished
        to the caller: not configured, nothing was actually changed this job
        (a read-only answer has nothing for a linter to check), the check
        just passed, or the attempt budget is spent -- that last case still
        leaves ``_self_check_last_output`` set, which is what lets the
        answer note the residual failure instead of hiding it.
        """
        if not self.self_check_command:
            return None
        tally = self.context.tally
        if not (tally.edits or tally.writes or tally.deletes):
            return None
        if self._self_check_attempts >= self.self_check_max_attempts:
            return None
        try:
            parts = shlex.split(self.self_check_command, posix=True)
        except ValueError as exc:
            logger.warning("harness self_check_command could not be parsed: %s", exc)
            return None
        if not parts:
            return None
        self._self_check_attempts += 1
        try:
            result = await asyncio.wait_for(
                run_command(parts[0], *parts[1:], cwd=self.context.root),
                timeout=self.context.shell_seconds,
            )
        except TimeoutError:
            logger.warning(
                "harness self_check_command did not finish within %.0fs",
                self.context.shell_seconds,
            )
            return None
        except OSError as exc:
            # A broken self-check must never be the reason a real answer is
            # blocked -- that would make a typo in evomesh.yaml look like
            # every job in the mesh suddenly failing its own code.
            logger.warning("harness self_check_command could not run: %s", exc)
            return None
        self.session.record(
            "self_check", attempt=self._self_check_attempts, exit_code=result.exit_code
        )
        if result.exit_code == 0:
            self._self_check_failed = False
            self._self_check_last_output = ""
            return None
        self._self_check_failed = True
        self._self_check_last_output = result.output.strip()
        if self._self_check_attempts >= self.self_check_max_attempts:
            return None
        return SELF_CHECK_HINT.format(
            output=self._self_check_last_output[:2000] or "(no output)"
        )

    def _end(
        self,
        outcome: str,
        started: float,
        steps: int,
        calls: int,
        native: bool,
        *,
        answer: str = "",
        detail: str = "",
    ) -> HarnessResult:
        tally = self.context.tally
        result = HarnessResult(
            outcome=outcome,
            answer=answer,
            steps=steps,
            tool_calls=calls,
            seconds=time.monotonic() - started,
            detail=detail,
            session_path=self.session.path,
            used_tool_protocol="native tools" if native else "text protocol",
            tool_chars=self.tool_chars,
            prompt_chars=self.prompt_chars,
            reads=tally.reads,
            edits=tally.edits,
            writes=tally.writes,
            deletes=tally.deletes,
        )
        self.session.record(
            "end",
            outcome=outcome,
            steps=steps,
            tool_calls=calls,
            seconds=round(result.seconds, 2),
            detail=detail,
            reads=result.reads,
            edits=result.edits,
            writes=result.writes,
            deletes=result.deletes,
        )
        return result


def looks_like_broken_call(text: str) -> bool:
    """Whether the model was reaching for a tool and dropped it.

    The distinction matters because a parse failure is otherwise treated as the
    answer -- correct for prose that merely contains a brace, and wrong for
    ``{"tool": "grep", "args": {...}`` with the last brace missing.
    """
    stripped = text.strip()
    return stripped.startswith("{") and '"tool"' in stripped


def _objects(text: str) -> Iterator[tuple[int, object]]:
    """Every balanced ``{...}`` in the text that parses, in the order written.

    Scanning for balance rather than taking the span from the first brace to the
    last is what handles the commonest small-model answer: a paragraph of
    explanation with the tool call inside it, sometimes twice. The wide span
    swallows the prose between two objects and parses as nothing at all, so the
    call the model did make was read as an answer and the job ended early --
    observed on mistral:7b the first time it was pointed at this repository.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}" and depth:
            depth -= 1
            if depth == 0:
                try:
                    yield start, json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    continue


def parse_text_call(raw: str) -> ChatTurn:
    """Pull a tool call out of whatever the model actually said.

    The first parseable object naming a tool wins -- it is the model's next
    move, and the loop will come back for the rest. Anything else is the answer.
    That way round is deliberate: a model that finished and wrote a sentence
    containing a brace is answering, and treating a parse failure as an error
    would end jobs that had already succeeded.
    """
    text = raw.strip()
    for start, payload in _objects(text):
        if not isinstance(payload, dict):
            continue
        name = payload.get("tool") or payload.get("name")
        if not isinstance(name, str) or not name:
            # Not a tool call -- but under structured_fallback, a finished
            # turn is *also* one JSON object (Ollama's `format` allows no
            # other shape), naming its answer instead of a tool. Checked
            # here, not as an earlier/separate branch, so an object that
            # has neither key still falls through to the loop's next object
            # or the plain-text return below, exactly as before this key
            # existed.
            answer = payload.get("answer")
            if isinstance(answer, str) and answer:
                return ChatTurn(text=answer)
            continue
        # Three spellings, because three model families use different ones and
        # the arguments are the part a refusal cannot recover from.
        args = payload.get("args") or payload.get("arguments") or payload.get("parameters") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        return ChatTurn(
            text=text[:start].strip(), tool_calls=[ToolCall(name=name, arguments=args)]
        )
    return ChatTurn(text=text)


def build_runner(
    provider: ModelProvider,
    root: Path,
    *,
    session: HarnessSession | None = None,
    limits: ToolLimits | None = None,
    model: str | None = None,
    num_ctx: int | None = None,
    max_steps: int = 24,
    max_seconds: float = 300.0,
    transcript_chars: int = 12000,
    read_only: bool = True,
    allow_write: bool = False,
    write_prefix: str | None = None,
    shell_allow: frozenset[str] = frozenset(),
    shell_seconds: float = 60.0,
    scraping_executable: str = "",
    scraping_timeout: float = 30.0,
    ask_agent: Callable[[str, str], Awaitable[str]] | None = None,
    learn_skill: Callable[[str, str, str], Awaitable[str]] | None = None,
    patch_skill: Callable[[str, str, str], Awaitable[str]] | None = None,
    skills_root: Path | None = None,
    custom_tools: tuple[Tool, ...] = (),
    self_check_command: str = "",
    self_check_max_attempts: int = 2,
    structured_fallback: bool = False,
) -> HarnessRunner:
    """Assemble a job. Read-only unless the caller asks for both halves.

    Two arguments rather than one because they answer different questions:
    ``read_only`` is what this job is for, and ``allow_write`` is whether the
    configuration permits any job to change a file at all. A writing job on a
    mesh that forbids writes gets the tools and a refusal that names the
    setting -- which is a thing the model can report, rather than a capability
    that silently is not there.
    """
    context = ToolContext(
        root=root.resolve(strict=False),
        limits=limits or ToolLimits(),
        allow_write=allow_write,
        write_prefix=write_prefix,
        shell_allow=shell_allow,
        shell_seconds=shell_seconds,
        scraping_executable=scraping_executable,
        scraping_timeout=scraping_timeout,
        ask_agent=ask_agent,
        learn_skill=learn_skill,
        patch_skill=patch_skill,
        skills_root=skills_root,
        session=session,
    )
    # Each optional tool joins the registry only when a human has actually
    # configured it. An unusable tool in the schema is a tool a model will try.
    tools = READ_ONLY_TOOLS if read_only else ALL_TOOLS
    if shell_allow:
        tools = tools + SHELL_TOOLS
    if scraping_executable:
        tools = tools + WEB_TOOLS
    if ask_agent is not None:
        tools = tools + ASK_TOOLS
    if learn_skill is not None:
        tools = tools + LEARN_TOOLS
    # Already filtered by the caller to ones whose command is allow-listed --
    # the same "an unusable tool in the schema is a tool a model will try"
    # reasoning above, applied to a custom tool's own program instead of
    # shell/fetch's.
    tools = tools + custom_tools
    return HarnessRunner(
        provider=provider,
        context=context,
        registry=ToolRegistry(tools),
        session=session or HarnessSession(None),
        model=model,
        num_ctx=num_ctx,
        max_steps=max_steps,
        max_seconds=max_seconds,
        transcript_chars=transcript_chars,
        system=SYSTEM if read_only else WRITE_SYSTEM,
        self_check_command=self_check_command if allow_write else "",
        self_check_max_attempts=self_check_max_attempts,
        structured_fallback=structured_fallback,
    )
