from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from evomesh.bdi import ReflectiveBehavior
from evomesh.cognition import AgentBehavior, CycleContext, CycleOutcome
from evomesh.contracts import (
    AgentDefinition,
    AgentPhase,
    AgentRuntimeState,
    AgentStatus,
    Autonomy,
    Goal,
    GoalStatus,
    Message,
    now_utc,
)
from evomesh.memory import AgentMemory, MemoryBudget
from evomesh.messaging import MessageBus
from evomesh.models import ModelProvider, ModelUnavailableError
from evomesh.storage import SQLiteRepository

logger = logging.getLogger(__name__)

MAX_INBOX_HISTORY = 6


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentDefinition] = {}

    def register(self, definition: AgentDefinition) -> None:
        if definition.id in self._agents:
            raise ValueError(f"Agent id already registered: {definition.id}")
        if any(item.name.lower() == definition.name.lower() for item in self._agents.values()):
            raise ValueError(f"Agent name already registered: {definition.name}")
        self._agents[definition.id] = definition

    def all(self) -> list[AgentDefinition]:
        return list(self._agents.values())

    def get(self, agent_id_or_name: str) -> AgentDefinition:
        if agent_id_or_name in self._agents:
            return self._agents[agent_id_or_name]
        for agent in self._agents.values():
            if agent.name.lower() == agent_id_or_name.lower():
                return agent
        raise KeyError(agent_id_or_name)


@dataclass
class AgentRuntime:
    """One live agent: a reactive mailbox loop and a proactive goal cycle.

    Both loops share a lock. A small local model handling two concurrent
    requests for the same agent is how you get an agent that answers a chat
    message with half of its own deliberation, so they take turns.
    """

    definition: AgentDefinition
    provider: ModelProvider
    bus: MessageBus
    repository: SQLiteRepository
    memory: AgentMemory
    behavior: AgentBehavior = field(default_factory=ReflectiveBehavior)
    budget: MemoryBudget = field(default_factory=MemoryBudget)
    cycle_seconds: float = 60.0
    start_delay: float = 0.0
    services: Callable[[], dict[str, Any]] = dict
    world_context: Callable[[], str] = lambda: ""
    on_response: Callable[[Message], None] | None = None
    state: AgentRuntimeState = field(init=False)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _inbox: list[Message] = field(default_factory=list, init=False)
    _last_cycle_started: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.state = AgentRuntimeState(
            agent_id=self.definition.id, name=self.definition.name, phase=AgentPhase.OFFLINE
        )

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        self.state.phase = AgentPhase.STARTING
        self.definition.status = AgentStatus.ACTIVE
        self.definition.touch()
        await self.memory.ensure()
        await self.repository.save_agent(self.definition)
        self.bus.register(self.definition.id)
        self._tasks = [
            asyncio.create_task(self._message_loop(), name=f"agent:{self.definition.slug}:inbox"),
            asyncio.create_task(self._cycle_loop(), name=f"agent:{self.definition.slug}:cycle"),
        ]
        self.state.phase = AgentPhase.IDLE
        self._refresh_goal()

    async def stop(self, *, persist_status: bool = True) -> None:
        """Stop the loops. Only persist STOPPED when a human actually asked.

        A process shutdown is not a decision to disable the agent; persisting it
        as STOPPED is what made every agent come back dead after a restart.
        """
        if persist_status:
            self.definition.status = AgentStatus.STOPPED
            self.definition.touch()
        await self.repository.save_agent(self.definition)
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        self.state.phase = AgentPhase.OFFLINE
        self.state.goal = None

    # -- reactive path --------------------------------------------------

    async def _message_loop(self) -> None:
        while True:
            incoming = await self.bus.receive(self.definition.id)
            try:
                await self._handle(incoming)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a bad message must not kill the agent
                logger.exception("Agent %s failed to handle a message", self.definition.name)
                self.state.last_error = str(exc)
                self.state.phase = AgentPhase.ERROR

    async def _handle(self, incoming: Message) -> None:
        self._inbox = [*self._inbox, incoming][-MAX_INBOX_HISTORY:]
        if incoming.metadata.get("broadcast") and incoming.sender_id != "human":
            # Ambient chatter informs the next cycle; it does not deserve a reply.
            return
        async with self._lock:
            self.state.phase = AgentPhase.THINKING
            error = False
            try:
                response = await self.behavior.respond(self._context(), incoming)
            except (ModelUnavailableError, RuntimeError, ValueError) as exc:
                error = True
                response = (
                    f"Model error for {self.definition.provider}:"
                    f"{self.definition.model_name}: {exc}"
                )
                self.state.last_error = str(exc)
            self.state.phase = AgentPhase.ERROR if error else AgentPhase.IDLE
        outgoing = Message(
            sender_id=self.definition.id,
            recipient_id=incoming.sender_id,
            conversation_id=incoming.conversation_id,
            correlation_id=incoming.id,
            content=response,
            metadata={"error": error},
        )
        await self.bus.send(outgoing)
        if self.on_response:
            self.on_response(outgoing)

    # -- proactive path -------------------------------------------------

    async def _cycle_loop(self) -> None:
        # Stagger the first tick so a mesh of agents does not stampede one
        # small model on boot, but still run immediately rather than sleeping
        # a full interval before the agent ever touches its goal.
        await asyncio.sleep(self.start_delay)
        while True:
            # A manual /cycle counts as this tick's work, so re-check rather than
            # firing a second deliberation the moment the sleep ends.
            if self._due_in() <= 0:
                try:
                    await self.run_cycle()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # one bad cycle must not end the agent's life
                    logger.exception("Cycle failed for %s", self.definition.name)
                    self.state.last_error = str(exc)
                    self.state.phase = AgentPhase.ERROR
            await asyncio.sleep(max(0.5, min(self._due_in(), self.cycle_seconds)))

    def _due_in(self) -> float:
        return self._last_cycle_started + max(1.0, self.cycle_seconds) - time.monotonic()

    async def run_cycle(self) -> CycleOutcome:
        self._last_cycle_started = time.monotonic()
        if self.definition.autonomy is Autonomy.REACTIVE:
            self._refresh_goal()
            return CycleOutcome.idle("Reactive agent: cycles only when messaged.")
        async with self._lock:
            goal = self.definition.mind.next_goal()
            self.state.phase = AgentPhase.THINKING
            outcome = await self.behavior.cycle(self._context())
            await self._apply(outcome, goal)
            return outcome

    async def _apply(self, outcome: CycleOutcome, goal: Goal | None) -> None:
        worked_before = bool(goal.notes) if goal else False
        self.state.cycles += 1
        self.state.last_cycle_at = now_utc()
        self.state.last_outcome = outcome.summary
        self.state.phase = outcome.phase
        self.state.last_error = outcome.error
        if goal is not None:
            if outcome.error:
                goal.attempts += 1
                goal.last_error = outcome.error
                if not goal.recurring and goal.attempts >= goal.max_attempts:
                    goal.status = GoalStatus.FAILED
            elif outcome.worked:
                goal.status = GoalStatus.ACTIVE
                goal.last_error = None
            if outcome.step:
                # Intentions belong to the BDI reasoner; recording one here
                # would drop the agent's commitment on every single cycle.
                goal.note(outcome.step)
            if outcome.goal_done and not goal.recurring:
                if worked_before:
                    goal.status = GoalStatus.DONE
                else:
                    # Small models rubber-stamp DONE the first time they read a
                    # goal. Make one show its work twice before the goal closes.
                    goal.note("claimed complete on the first cycle; re-checking")
        if outcome.fact:
            # Beliefs come from perception; a cycle's takeaway is durable memory.
            # Writing it into the belief base too stacks a keyless near-duplicate
            # beside the structured belief the behavior already perceives.
            await self.memory.remember(outcome.fact, source=self.behavior.name)
        await self._write_context(outcome, goal)
        await self.memory.compact(self._summarize)
        self.definition.touch()
        await self.repository.save_agent(self.definition)
        self._refresh_goal()

    async def _write_context(self, outcome: CycleOutcome, goal: Goal | None) -> None:
        mind = self.definition.mind
        remaining = [
            f"- [{item.priority}] {item.description} ({item.status})"
            for item in mind.open_goals()
        ]
        beliefs = [
            f"- {item.key}: {item.statement}"
            for item in sorted(mind.beliefs, key=lambda item: item.updated_at)[-10:]
        ]
        intention = mind.current_intention()
        await self.memory.write_context(
            {
                "Current goal": goal.description if goal else "none",
                "Committed plan": (
                    f"{intention.plan} ({intention.cursor}/{len(intention.steps)} done)\n"
                    f"{intention.render()}"
                    if intention
                    else "none"
                ),
                "Beliefs": "\n".join(beliefs) or "none",
                "Last cycle": outcome.summary,
                "Next step": outcome.step or "decide on the next step",
                "Open goals": "\n".join(remaining) or "none",
                "Recent inbox": "\n".join(
                    f"- {item.sender_id}: {' '.join(item.content.split())[:200]}"
                    for item in self._inbox[-3:]
                ),
                "Status": self.state.describe(),
            }
        )

    async def _summarize(self, text: str) -> str:
        return await self.provider.generate(
            f"Compress these notes into at most 5 short bullet facts. Keep only what is "
            f"still true and useful.\n\n{text}",
            system="You compress an agent's long-term memory. Output bullets only.",
            model=self.definition.model_name,
        )

    # -- helpers --------------------------------------------------------

    def _context(self) -> CycleContext:
        return CycleContext(
            definition=self.definition,
            provider=self.provider,
            memory=self.memory,
            budget=self.budget,
            world=self.world_context(),
            inbox=list(self._inbox),
            services=self.services(),
        )

    def _refresh_goal(self) -> None:
        goal = self.definition.mind.next_goal()
        self.state.goal = goal.description if goal else None


SYSTEM_AGENTS: tuple[tuple[str, str, str, str, Autonomy], ...] = (
    (
        "architect",
        "Agent Architect",
        "Turn a human's description into a working agent definition in one pass.",
        "Produce a complete agent definition from whatever the human already said, "
        "asking at most one question.",
        Autonomy.REACTIVE,
    ),
    (
        "guardian",
        "Guardian",
        "Validate definitions, permissions, and environment health.",
        "Keep a current picture of mesh health and report anything degraded.",
        Autonomy.CYCLIC,
    ),
    (
        "evaluator",
        "Evaluator",
        "Run deterministic checks and scenario evaluations.",
        "Report the validation verdict of the newest candidate generation.",
        Autonomy.CYCLIC,
    ),
    (
        "evolver",
        "Environment Evolver",
        "Create and validate isolated candidate generations.",
        "Improve EvoMesh by one validated candidate generation at a time.",
        Autonomy.CYCLIC,
    ),
)


def system_agent_definitions(
    provider: str,
    model: str,
    overrides: dict[str, tuple[str, str]] | None = None,
) -> list[AgentDefinition]:
    """Bootstrap the built-in agents, each already carrying its standing goal.

    The goal is seeded here rather than left for a human to type, because an
    agent with no goal has nothing to do on its first cycle -- which is how the
    Evolver ended up never starting.
    """
    overrides = overrides or {}
    definitions: list[AgentDefinition] = []
    for agent_id, name, purpose, goal, autonomy in SYSTEM_AGENTS:
        definition = AgentDefinition(
            id=agent_id,
            name=name,
            type="system",
            created_by="bootstrap",
            identity=name,
            purpose=purpose,
            provider=overrides.get(agent_id, (provider, model))[0],
            model_name=overrides.get(agent_id, (provider, model))[1],
            autonomy=autonomy,
            status=AgentStatus.ACTIVE,
        )
        definition.mind.add_goal(goal, priority=3, recurring=True)
        definitions.append(definition)
    return definitions
