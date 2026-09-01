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

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from evomesh.contracts import AgentDefinition, AgentPhase, Goal, Message
from evomesh.memory import AgentMemory, MemoryBudget, clip
from evomesh.models import ModelProvider

REASONING_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
REASONING_START = re.compile(r"<(think|thinking|reasoning)>", re.IGNORECASE)
REASONING_END = re.compile(r"</(think|thinking|reasoning)>", re.IGNORECASE)
FIELD_PATTERN = re.compile(
    r"^[*_#>\s-]*(STEP|RESULT|FACT|DONE|STATUS)[*_\s]*[:\-]\s*(.*)$", re.IGNORECASE
)
MARKDOWN_EDGE = " *_`"

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

    @classmethod
    def idle(cls, summary: str) -> CycleOutcome:
        return cls(summary=summary, phase=AgentPhase.IDLE)

    @classmethod
    def failed(cls, error: str) -> CycleOutcome:
        return cls(summary=error, error=error, phase=AgentPhase.ERROR, worked=True)


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

    @property
    def goal(self) -> Goal | None:
        return self.definition.mind.next_goal()

    def service(self, name: str) -> object | None:
        return self.services.get(name)

    async def think(self, instruction: str, *, goal: Goal | None = None) -> str:
        """One budgeted model call carrying identity, memory, context and inbox."""
        prompt = await self.build_prompt(instruction, goal=goal)
        raw = await self.provider.generate(
            prompt,
            system=self.system_prompt(),
            model=self.definition.model_name,
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

    async def build_prompt(self, instruction: str, *, goal: Goal | None = None) -> str:
        target = goal or self.goal
        memory = await self.memory.read_memory(self.budget.memory_chars)
        notes = await self.memory.read_context(self.budget.context_chars)
        sections: list[str] = []
        if target:
            sections.append(f"GOAL: {target.description}")
        beliefs = self.render_beliefs()
        if beliefs:
            sections.append("BELIEFS (what you currently hold true):\n" + beliefs)
        plan = self.render_plan()
        if plan:
            sections.append("YOUR COMMITTED PLAN:\n" + plan)
        if self.work.strip():
            sections.append(
                "CURRENT WORK (live from the runtime, more current than MEMORY):\n"
                + clip(self.work.strip(), 700, keep="head")
            )
        if self.world:
            sections.append("WORLD:\n" + clip(self.world, 600, keep="head"))
        if memory.strip():
            sections.append("MEMORY (things you already know):\n" + memory)
        if notes.strip():
            sections.append("YOUR WORKING NOTES:\n" + notes)
        if self.inbox:
            sections.append("INBOX:\n" + self.render_inbox())
        sections.append(instruction)
        return clip("\n\n".join(sections), self.budget.prompt_chars)

    def render_beliefs(self, limit: int = 12) -> str:
        """The belief base, freshest last, inside its own budget.

        Beliefs go in the prompt ahead of memory because they are what the agent
        holds true *now*; memory is what it learned once.
        """
        beliefs = sorted(self.definition.mind.beliefs, key=lambda item: item.updated_at)
        lines = [f"- {item.statement}" for item in beliefs[-limit:]]
        return clip("\n".join(lines), self.budget.beliefs_chars)

    def render_plan(self) -> str:
        intention = self.definition.mind.current_intention()
        if intention is None or not intention.steps:
            return ""
        current = intention.current
        marker = f"\nYou are on: {current.description}" if current else ""
        return clip(f"{intention.render()}{marker}", 600)

    def render_inbox(self, limit: int = 3) -> str:
        recent: Sequence[Message] = self.inbox[-limit:]
        lines = [f"- {item.sender_id}: {' '.join(item.content.split())}" for item in recent]
        return clip("\n".join(lines), self.budget.inbox_chars)


class AgentBehavior(Protocol):
    """What an agent does with a cycle. Replaceable per agent."""

    name: str

    async def cycle(self, context: CycleContext) -> CycleOutcome: ...

    async def respond(self, context: CycleContext, message: Message) -> str: ...
