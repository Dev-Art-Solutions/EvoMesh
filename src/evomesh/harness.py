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

import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from evomesh.cognition import strip_reasoning
from evomesh.harness_session import HarnessSession
from evomesh.harness_tools import ToolContext, ToolLimits, ToolRegistry
from evomesh.models import (
    ChatMessage,
    ChatTurn,
    ModelProvider,
    ModelUnavailableError,
    ToolCall,
    ToolsUnsupportedError,
)

logger = logging.getLogger(__name__)

SYSTEM = (
    "You are a careful engineer working inside a real project. Use the tools to "
    "look at the code before you answer -- never guess a file's contents. Prefer "
    "grep to find where something lives, then read only the part you need. When "
    "you know the answer, state it plainly and name the files it came from. Keep "
    "the answer short."
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

OUTCOMES = ("answered", "capped", "failed")

# Said once, to a model whose tool call did not parse. Observed on gemma:2b: it
# opens the object, forgets a brace, and the reply is neither a call nor an
# answer -- accepting it as the answer ends a job that had not finished.
BROKEN_CALL_HINT = (
    "That was not a usable tool call. Reply with EXACTLY one line of JSON, every "
    'brace closed: {"tool": "<name>", "args": {...}}. If you can already answer, '
    "reply with plain text and no JSON at all."
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

    def summary(self) -> str:
        where = f", session: {self.session_path}" if self.session_path else ""
        return (
            f"{self.steps} step{'s' if self.steps != 1 else ''}, "
            f"{self.seconds:.1f} s, {self.tool_calls} tool call"
            f"{'s' if self.tool_calls != 1 else ''}, {self.used_tool_protocol}{where}"
        )


@dataclass
class HarnessRunner:
    provider: ModelProvider
    context: ToolContext
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    session: HarnessSession = field(default_factory=lambda: HarnessSession(None))
    model: str | None = None
    max_steps: int = 24
    max_seconds: float = 300.0
    system: str = SYSTEM

    async def run(self, task: str) -> HarnessResult:
        started = time.monotonic()
        messages = [ChatMessage(role="user", content=task)]
        native = True
        calls_made = 0
        corrected = False
        self.session.record("job", task=task, root=str(self.context.root))

        for step in range(1, self.max_steps + 1):
            elapsed = time.monotonic() - started
            if elapsed > self.max_seconds:
                return self._end(
                    "capped", started, step - 1, calls_made, native,
                    detail=f"the {self.max_seconds:.0f}s wall clock ran out",
                )
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
                if looks_like_broken_call(turn.text) and not corrected:
                    corrected = True
                    self.session.record("malformed", text=turn.text[:400])
                    messages.append(ChatMessage(role="assistant", content=turn.text))
                    messages.append(ChatMessage(role="user", content=BROKEN_CALL_HINT))
                    continue
                return self._end(
                    "answered", started, step, calls_made, native, answer=turn.text
                )

            corrected = False
            messages.append(
                ChatMessage(role="assistant", content=turn.text, tool_calls=turn.tool_calls)
            )
            for call in turn.tool_calls:
                result = await self._invoke(call)
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
                )
                return ChatTurn(text=strip_reasoning(turn.text), tool_calls=turn.tool_calls), True
            except ToolsUnsupportedError as exc:
                logger.info("harness: falling back to the text protocol (%s)", exc)
                self.session.record("fallback", reason=str(exc))
        answer = await self.provider.generate(
            self._render(messages),
            system=f"{TEXT_SYSTEM}\n\nTools:\n{self.registry.describe()}",
            model=self.model,
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

    async def _invoke(self, call: ToolCall) -> str:
        result = await self.registry.invoke(self.context, call.name, call.arguments)
        self.session.record(
            "tool", name=call.name, args=call.arguments, chars=len(result), result=result
        )
        return result

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
        result = HarnessResult(
            outcome=outcome,
            answer=answer,
            steps=steps,
            tool_calls=calls,
            seconds=time.monotonic() - started,
            detail=detail,
            session_path=self.session.path,
            used_tool_protocol="native tools" if native else "text protocol",
        )
        self.session.record(
            "end",
            outcome=outcome,
            steps=steps,
            tool_calls=calls,
            seconds=round(result.seconds, 2),
            detail=detail,
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
            continue
        args = payload.get("args") or payload.get("arguments") or {}
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
    max_steps: int = 24,
    max_seconds: float = 300.0,
) -> HarnessRunner:
    context = ToolContext(root=root.resolve(strict=False), limits=limits or ToolLimits())
    return HarnessRunner(
        provider=provider,
        context=context,
        session=session or HarnessSession(None),
        model=model,
        max_steps=max_steps,
        max_seconds=max_seconds,
    )
