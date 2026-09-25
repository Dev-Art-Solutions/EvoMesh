"""The deliberation cycle every agent runs.

An agent is not a chat endpoint. It owns goals, and a cycle is one turn of
perceive -> deliberate -> act -> reflect against the highest-priority open goal.
The reactive path (a human talking to the agent) and the proactive path (the
cycle) share the same budgeted prompt so an agent never answers a chat message
having forgotten what it was working on.

Prompts are assembled under a hard character budget. Small local models silently
drop whatever does not fit, and what they drop first is the oldest part of the
prompt -- which is exactly where memory lives. Budgeting here, rather than
hoping the server copes, is what keeps memory from evaporating mid-goal.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from evomesh.cognitive_services import (
    CognitiveModelService,
    CognitiveServiceType,
    ContextAssembler,
    ContextSelectionRecord,
    ModelInvocationReason,
    TaskPacket,
)
from evomesh.contracts import AgentDefinition, AgentPhase, Goal, Message
from evomesh.memory import AgentMemory, MemoryBudget, clip
from evomesh.models import ModelProvider
from evomesh.rules import RuntimeEvent

logger = logging.getLogger(__name__)

REASONING_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
REASONING_START = re.compile(r"<(think|thinking|reasoning)>", re.IGNORECASE)
REASONING_END = re.compile(r"</(think|thinking|reasoning)>", re.IGNORECASE)
FIELD_PATTERN = re.compile(
    r"^[*_#>\s-]*(STEP|RESULT|FACT|DONE|STATUS)[*_\s]*[:\-]\s*(.*)$", re.IGNORECASE
)
MARKDOWN_EDGE = " *_`"
# Matches a true line start, or right after "Name> " -- console.py's _talk()
# and attach() both prefix an agent's raw reply with "<agent name>> " before
# anything downstream (Telegram, the desktop chat panel) ever sees it, so a
# FILE: line the model wrote at the very start of its own reply is no longer
# at the start of *this* string by the time either of them looks for it.
FILE_LINE = re.compile(r"(?:^|>\s)FILE:\s*(.+?)\s*$", re.MULTILINE)

CYCLE_FORMAT = (
    "Reply with exactly these four lines and nothing else:\n"
    "STEP: <the single next action you take now>\n"
    "RESULT: <what you achieved or concluded, at most two sentences>\n"
    "FACT: <one durable fact worth remembering, or NONE>\n"
    "DONE: <yes if the goal is fully met, otherwise no>"
)

AUTONOMY_RULES = (
    "Work autonomously. Never ask the human a question: decide with what you have "
    "and state the assumption you made. Be brief and concrete."
)


def strip_reasoning(text: str) -> str:
    """Remove chain-of-thought blocks that reasoning models emit.

    Left in place these dominate the prompt on the next cycle and get written
    into memory as if they were conclusions.

    Only the tidiest models return a matched pair. Most chat templates already
    contain the opening tag, so Ollama returns the reasoning itself and closes
    it with a bare ``</think>``; a truncated answer does the opposite and opens
    a block it never closes. Both halves are reasoning, not an answer.
    """
    text = REASONING_BLOCK.sub("", text)
    if closes := list(REASONING_END.finditer(text)):
        text = text[closes[-1].end() :]
    if (opened := REASONING_START.search(text)) is not None:
        text = text[: opened.start()]
    return text.strip()


def extract_file_references(text: str) -> list[str]:
    """Paths an agent named as files it wants to hand back.

    A free-form "just writes a path somewhere in prose" is exactly the
    format small models are unreliable at producing consistently -- the same
    reason STEP:/RESULT:/FACT: exist instead of asking for a paragraph. One
    more explicit field, in the same shape: a line starting with ``FILE:``
    and nothing else on it. Relative to the replying agent's own
    ``default_harness_root`` -- callers resolve that themselves, since this
    module has no notion of which agent it belongs to.
    """
    stripped = strip_reasoning(text)
    return [
        match.group(1)
        for match in FILE_LINE.finditer(stripped)
        if _path_is_a_known_deliverable(match.group(1))
    ]


# Text files the harness `write`/`edit` tools could plausibly have produced,
# plus the binary formats document_write actually generates (.docx/.pdf/
# .xlsx/.xlsm) -- a real, requested deliverable, not the harness improvising
# a binary write. Anything else (image, audio, video, archive) stays out:
# never something a model should be asked to write back on its own.
def _path_is_a_known_deliverable(path: str) -> bool:
    return Path(path).suffix.lower() in (
        ".txt", ".md", ".json", ".yaml", ".yml",
        ".py", ".csv", ".html", ".htm", ".xml",
        ".docx", ".pdf", ".xlsx", ".xlsm",
    )


@dataclass
class CycleReply:
    step: str = ""
    result: str = ""
    fact: str = ""
    done: bool = False
    blocked: bool = False


def parse_cycle_reply(raw: str) -> CycleReply:
    """Parse the four-line contract, tolerating everything small models do to it."""
    text = strip_reasoning(raw)
    reply = CycleReply()
    matched = False
    current: str | None = None
    for line in text.splitlines():
        match = FIELD_PATTERN.match(line)
        if match:
            matched = True
            current = match.group(1).upper()
            value = match.group(2).strip().strip(MARKDOWN_EDGE).strip()
            if current == "STEP":
                reply.step = value
            elif current == "RESULT":
                reply.result = value
            elif current == "FACT":
                reply.fact = value
            elif current == "DONE":
                reply.done = value.strip().lower().startswith(("y", "true", "done"))
            else:
                lowered = value.strip().lower()
                reply.blocked = lowered.startswith(("block", "cannot", "can't", "fail"))
                reply.done = reply.done or lowered.startswith(("done", "complete", "ok"))
        elif current in {"STEP", "RESULT", "FACT"} and line.strip():
            extra = line.strip().strip(MARKDOWN_EDGE).strip()
            if current == "STEP":
                reply.step = f"{reply.step} {extra}".strip()
            elif current == "RESULT":
                reply.result = f"{reply.result} {extra}".strip()
            else:
                reply.fact = f"{reply.fact} {extra}".strip()
    if not matched:
        # An unformatted answer is still work: keep it rather than discarding the cycle.
        reply.result = clip(text, 600)
    return reply


@dataclass
class CycleOutcome:
    summary: str = ""
    step: str = ""
    fact: str = ""
    goal_done: bool = False
    phase: AgentPhase = AgentPhase.IDLE
    error: str | None = None
    worked: bool = False
    # The next cycle has work that waits on nothing -- run it now instead of a
    # whole cycle_seconds later (see AgentRuntime.wake).
    again: bool = False

    @classmethod
    def idle(cls, summary: str) -> CycleOutcome:
        return cls(summary=summary, phase=AgentPhase.IDLE)

    @classmethod
    def failed(cls, error: str) -> CycleOutcome:
        return cls(summary=error, error=error, phase=AgentPhase.ERROR, worked=True)


@dataclass
class AgentCycleTrace:
    agent_id: str = ""
    turn: int = 0
    cognition: str = ""
    outcome: str = ""


@dataclass
class CycleContext:
    """Everything a behavior may touch during one cycle."""

    definition: AgentDefinition
    provider: ModelProvider
    memory: AgentMemory
    budget: MemoryBudget
    world: str = ""
    inbox: list[Message] = field(default_factory=list)
    services: dict[str, object] = field(default_factory=dict)
    # What the runtime knows the agent is doing right now. Beliefs and notes are
    # a cycle old at best, so a human asking mid-cycle gets this instead.
    work: str = ""
    # Resolved once per agent (definition override, else the provider's own
    # default) rather than read from settings here, because a shared provider
    # instance serves every agent on it and each may need a different window.
    num_ctx: int | None = None
    cognitive: CognitiveModelService = field(default_factory=CognitiveModelService)
    # Structured events addressed to this agent since its last cycle, the
    # input the rule engine reacts to without a model call.
    events: tuple[RuntimeEvent, ...] = ()
    last_context_provenance: tuple[ContextSelectionRecord, ...] = ()

    @property
    def goal(self) -> Goal | None:
        return self.definition.mind.next_goal()

    def service(self, name: str) -> object | None:
        return self.services.get(name)

    async def think(
        self,
        instruction: str,
        *,
        goal: Goal | None = None,
        service: CognitiveServiceType = CognitiveServiceType.EXECUTE_STEP,
        reason: ModelInvocationReason = ModelInvocationReason.PLAN_STEP_REQUIRES_REASONING,
        relevant_belief_keys: Sequence[str] = (),
        output_contract: str = "",
    ) -> str:
        """One budgeted model call carrying identity, memory, context and inbox."""
        target = goal or self.goal
        prompt = await self.build_prompt(
            instruction,
            goal=target,
            service=service,
            relevant_belief_keys=relevant_belief_keys,
            output_contract=output_contract,
        )
        raw = await self.cognitive.generate(
            self.provider,
            prompt,
            service=service,
            reason=reason,
            provider_name=self.definition.provider,
            agent_id=self.definition.id,
            goal_id=target.id if target else "",
            system=self.system_prompt(),
            model=self.definition.model_name,
            num_ctx=self.num_ctx,
        )
        return strip_reasoning(raw)

    def system_prompt(self) -> str:
        identity = self.definition.identity or self.definition.name
        return clip(
            f"You are {self.definition.name}. {identity}\n"
            f"Purpose: {self.definition.purpose}\n"
            f"{AUTONOMY_RULES}",
            600,
        )

    async def build_prompt(
        self,
        instruction: str,
        *,
        goal: Goal | None = None,
        service: CognitiveServiceType = CognitiveServiceType.EXECUTE_STEP,
        relevant_belief_keys: Sequence[str] = (),
        output_contract: str = "",
    ) -> str:
        target = goal or self.goal
        query = " ".join(
            part
            for part in (
                instruction,
                target.description if target else "",
                " ".join(relevant_belief_keys),
            )
            if part
        )
        memory = self.select_relevant_text(
            await self.memory.read_memory(self.budget.memory_chars * 4),
            query,
            self.budget.memory_chars,
            recent_fallback=4,
        )
        notes = self.select_relevant_text(
            await self.memory.read_context(self.budget.context_chars * 3),
            query,
            self.budget.context_chars,
            recent_fallback=2,
        )
        include_inbox = service in {
            CognitiveServiceType.CHAT_RESPONSE,
            CognitiveServiceType.INTERPRET_UNSTRUCTURED_INPUT,
        }
        packet = TaskPacket(
            role=self.definition.name,
            operation=service,
            task=instruction,
            goal=target.description if target else "",
            beliefs=self.render_beliefs(relevant_belief_keys),
            intention=self.render_plan(),
            working=clip(self.work.strip(), 700, keep="head"),
            artifacts=tuple(target.artifacts) if target else (),
            recent_failure=target.last_error or "" if target else "",
            world=clip(self.world, 600, keep="head"),
            memory=memory.strip(),
            notes=notes.strip(),
            inbox=self.render_inbox() if self.inbox and include_inbox else "",
            output_contract=output_contract,
        )
        assembly = ContextAssembler(self.budget.prompt_chars).assemble_with_provenance(packet)
        self.last_context_provenance = assembly.provenance
        return assembly.text

    @staticmethod
    def select_relevant_text(
        text: str,
        query: str,
        budget: int,
        *,
        recent_fallback: int = 0,
    ) -> str:
        """Select matching lines, with a tiny recent fallback for legacy notes."""
        if not text or budget <= 0:
            return ""
        terms = {
            token
            for token in re.findall(r"[a-z0-9_.-]{3,}", query.lower())
            if token not in {"the", "and", "for", "with", "this", "that", "step"}
        }
        lines = [line for line in text.splitlines() if line.strip()]
        selected = [
            line for line in lines if terms and any(term in line.lower() for term in terms)
        ]
        if recent_fallback:
            for line in reversed(lines[-recent_fallback:]):
                if line not in selected:
                    selected.append(line)
        return clip("\n".join(selected), budget)

    def render_beliefs(
        self, relevant_keys: Sequence[str] = (), limit: int = 12
    ) -> str:
        """The belief base, freshest last, inside its own budget.

        Beliefs go in the prompt ahead of memory because they are what the agent
        holds true *now*; memory is what it learned once.
        """
        beliefs = sorted(self.definition.mind.beliefs, key=lambda item: item.updated_at)
        if relevant_keys:
            wanted = frozenset(relevant_keys)
            beliefs = [item for item in beliefs if item.key in wanted]
        lines = [f"- {item.statement}" for item in beliefs[-limit:]]
        return clip("\n".join(lines), self.budget.beliefs_chars)

    def render_plan(self) -> str:
        intention = self.definition.mind.current_intention()
        if intention is None or not intention.steps:
            return ""
        current = intention.current
        marker = f"\nYou are on: {current.description}" if current else ""
        plan = f"{intention.render()}{marker}"
        return clip(plan, 600)

    def render_inbox(self, limit: int = 3) -> str:
        recent: Sequence[Message] = self.inbox[-limit:]
        lines = [f"- {item.sender_id}: {' '.join(item.content.split())}" for item in recent]
        return clip("\n".join(lines), self.budget.inbox_chars)


class AgentBehavior(Protocol):
    """What an agent does with a cycle. Replaceable per agent."""

    name: str

    async def cycle(self, context: CycleContext) -> CycleOutcome: ...

    async def respond(self, context: CycleContext, message: Message) -> str: ...
