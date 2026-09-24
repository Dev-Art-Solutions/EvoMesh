from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from evomesh import cron
from evomesh.bdi import ReflectiveBehavior
from evomesh.cognition import AgentBehavior, CycleContext, CycleOutcome, strip_reasoning
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

# How long a cycle can run before it counts as stuck rather than merely slow.
# The validate stage hands a multi-minute suite off to a background task and
# returns the same cycle (see INSTANT_VALIDATION in behaviors.py), so a
# healthy cycle -- evolution's included -- returns in well under a minute
# even while a real suite is still running. A cycle still in flight past this
# is not "a slow one", it is one caught on a blocking call nothing times out
# on its own -- exactly what left a hung mesh answering /ping for twenty
# minutes with no supervisor any the wiser.
STUCK_CYCLE_MULTIPLE = 3.0
STUCK_CYCLE_FLOOR = 600.0
# The shortest gap between two cycles an early wake (AgentRuntime.wake) may
# leave: a behavior that asks again every time still cannot spin the loop.
WAKE_MIN_GAP = 2.0

# `through_harness` in bdi.py substitutes this filler whenever a harness job's
# own answer was empty, so a human reading a status line never sees a bare
# blank. Right for status; wrong for a notify announcement -- a recurring
# goal whose whole point is that most cycles find nothing to say (see
# NewsAnalyzer's news-impact-analysis skill: "silence is correct") should not
# get a Telegram message every cycle just because this filler is non-empty.
_HARNESS_EMPTY_ANSWER = re.compile(r"^harness job \d+ found nothing to report$")


def _is_silent_outcome(summary: str) -> bool:
    """Whether a recurring goal's outcome is genuinely nothing to announce."""
    text = summary.strip()
    return not text or bool(_HARNESS_EMPTY_ANSWER.fullmatch(text))


def _apply_report_pattern(summary: str, pattern: str) -> str:
    """Keep only the lines of a goal's report that match its report_pattern.

    A skill can ask a model for a strict per-line format and tell it, in
    plain language, never to narrate what it just did -- but that is a
    request, not an enforcement. A model that ignores it (e.g. NewsAnalyzer
    reporting "appended this cycle's assessment to the scratch log as the
    Nth entry" instead of a headline-impact line) would otherwise reach the
    human verbatim. This is the deterministic backstop: anything that is not
    a line the goal's own pattern recognizes as its report format is dropped
    silently rather than announced.
    """
    try:
        # Case carries no meaning in any report_pattern this project ships
        # (bullish/bearish/neutral, low/medium/high) -- found live: 106 of
        # 106 "matched nothing" cycles for NewsAnalyzer, several of them a
        # well-formed report line dropped for nothing but "Bullish" where
        # the pattern wanted "bullish". A model capitalizing the first word
        # of a sentence is a formatting tic, not a sign it ignored the
        # format contract, and treating it as one meant every genuinely
        # good report from that model was silently thrown away forever.
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error:
        logger.warning("goal has an invalid report_pattern, skipping filter: %r", pattern)
        return summary
    kept = [line for line in summary.splitlines() if compiled.fullmatch(line.strip())]
    return "\n".join(kept)


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

    def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)


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
    # Resolved once at start_agent(), same as cycle_seconds: a model swap or a
    # num_ctx change restarts the runtime, so re-resolving mid-life would only
    # ever return what this already holds.
    num_ctx: int | None = None
    start_delay: float = 0.0
    services: Callable[[], dict[str, Any]] = dict
    world_context: Callable[[], str] = lambda: ""
    on_response: Callable[[Message], None] | None = None
    # Set from Environment.announce at start_agent() -- the same fan-out a
    # restart or a promotion already uses, so a goal's summary reaches every
    # channel listening for one (Telegram today, the control port's pull-based
    # /notifications for the desktop Control Center) without a second path.
    announce: Callable[[str], Awaitable[None]] | None = None
    state: AgentRuntimeState = field(init=False)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _inbox: list[Message] = field(default_factory=list, init=False)
    _last_cycle_started: float = field(default=0.0, init=False)
    _last_cycle_finished: float = field(default=0.0, init=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)

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
            # A plain message replies to its sender's own mailbox -- but that
            # mailbox is also this agent's own _message_loop's, for a human
            # asking through the console. An agent asking another agent (see
            # harness_tools.tool_ask_agent) is a second listener on its own
            # mailbox at the same time (its _message_loop never stops), so a
            # bare reply-to-sender there would race the tool call for the
            # very reply it is waiting on. reply_to opts into a private,
            # one-shot mailbox nothing else is listening on instead.
            recipient_id=incoming.metadata.get("reply_to") or incoming.sender_id,
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
            await self._nap(max(0.5, min(self._due_in(), self.cycle_seconds)))

    def wake(self) -> None:
        """Run the next cycle now rather than at the end of this interval.

        For work that just stopped waiting: a harness job or a validation run
        finishing, or a cycle whose next step waits on nothing. Found
        2026-09-24: generation 1382's edit took 37 seconds and the generation
        fourteen minutes, most of it five stages each waiting out a 120-second
        interval for work that had already finished. Still one cycle at a
        time, never sooner than WAKE_MIN_GAP after the last one started.
        """
        self._wake.set()

    async def _nap(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except TimeoutError:
            return
        self._wake.clear()
        since = time.monotonic() - self._last_cycle_started
        if since < WAKE_MIN_GAP:
            await asyncio.sleep(WAKE_MIN_GAP - since)
        # Due now: the loop's own _due_in() check is what runs the cycle.
        self._last_cycle_started = time.monotonic() - max(1.0, self.cycle_seconds)

    def _due_in(self) -> float:
        return self._last_cycle_started + max(1.0, self.cycle_seconds) - time.monotonic()

    async def run_cycle(self) -> CycleOutcome:
        self._last_cycle_started = time.monotonic()
        try:
            if self.definition.autonomy is Autonomy.REACTIVE:
                self._refresh_goal()
                return CycleOutcome.idle("Reactive agent: cycles only when messaged.")
            async with self._lock:
                goal = self.definition.mind.next_goal()
                self.state.phase = AgentPhase.THINKING
                outcome = await self.behavior.cycle(self._context())
                await self._apply(outcome, goal)
                if outcome.again:
                    self.wake()
                return outcome
        finally:
            # Recorded even on a raised exception: an agent that failed its
            # cycle came back and is not the mesh this exists to catch --
            # only one still inside behavior.cycle() past all reason is.
            self._last_cycle_finished = time.monotonic()

    def stuck_for(self) -> float | None:
        """Seconds a cycle has been running past its own budget, or None if healthy."""
        if self._last_cycle_started <= self._last_cycle_finished:
            return None
        running_for = time.monotonic() - self._last_cycle_started
        threshold = max(STUCK_CYCLE_FLOOR, self.cycle_seconds * STUCK_CYCLE_MULTIPLE)
        return running_for if running_for > threshold else None

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
            if outcome.goal_done and (goal.cron or goal.interval_seconds):
                # Independent of whether this goal is recurring: a one-shot
                # goal still only means "don't re-plan the instant this
                # finishes" until this fires, and a recurring one is exactly
                # what this exists for -- checked again on its own schedule,
                # not on the agent's very next tick.
                if goal.cron:
                    goal.next_attempt_at = cron.next_after(goal.cron, now_utc())
                elif goal.interval_seconds:
                    goal.next_attempt_at = now_utc() + timedelta(seconds=goal.interval_seconds)
            if outcome.goal_done and not goal.recurring:
                if worked_before:
                    goal.status = GoalStatus.DONE
                else:
                    # Small models rubber-stamp DONE the first time they read a
                    # goal. Make one show its work twice before the goal closes.
                    goal.note("claimed complete on the first cycle; re-checking")
            if goal.notify and self.announce:
                # A one-shot goal's first goal_done is the rubber stamp above,
                # not a real finish -- only a genuine completion (or any
                # completion of a recurring goal, which has no such stamp) is
                # worth reporting as done rather than as one more step.
                genuinely_done = outcome.goal_done and (goal.recurring or worked_before)
                # A goal whose skill says "silence is the correct, common
                # outcome" (news-impact-analysis, news-triage, ...) means it
                # literally: a recurring cycle that found nothing to say
                # should send nothing, not a "found nothing to report" filler
                # every single cycle forever.
                summary = outcome.summary
                # Only worth running -- and only meaningful to run -- on the
                # cycle that actually finished. Every earlier cycle's summary
                # is mid-plan chatter ("called news_fetch", "still reading
                # headline 3 of 8"), never the report itself, so filtering it
                # here only ever produced empty and _sanitize_report's extra
                # model call was spent for nothing on every single tick a
                # cron goal was in progress -- found live: NewsAnalyzer,
                # which runs 3-4 cycles per cron window before finishing,
                # paying for the sanitizer 3-4 times as often as it needed to.
                if genuinely_done and goal.recurring and goal.report_pattern:
                    filtered = _apply_report_pattern(summary, goal.report_pattern)
                    if not filtered.strip() and not _is_silent_outcome(summary):
                        # The regex found nothing to keep, but the model did
                        # not report genuine silence either -- it said
                        # something, just not shaped the way the pattern
                        # wants (mixed narration, wrapped lines, drifted
                        # punctuation). Ask the model to pull its own report
                        # back out, then re-apply the same regex to whatever
                        # it returns -- never trust that pass unfiltered.
                        try:
                            distilled = await self._sanitize_report(
                                summary, goal.report_pattern
                            )
                        except (ModelUnavailableError, RuntimeError, ValueError):
                            logger.exception(
                                "%s: report sanitizer call failed, staying silent",
                                self.definition.name,
                            )
                            distilled = ""
                        filtered = _apply_report_pattern(distilled, goal.report_pattern)
                    if not filtered.strip() and summary.strip():
                        # A human who never sees this agent announce anything
                        # has no way to tell "it never found anything" from
                        # "the pattern is silently eating everything it
                        # finds" -- this is the one place that distinction is
                        # still knowable, so it goes to the log even though
                        # it never reaches chat.
                        logger.info(
                            "%s: report_pattern %r matched nothing in this cycle's "
                            "raw answer, staying silent -- raw answer was: %r",
                            self.definition.name,
                            goal.report_pattern,
                            summary,
                        )
                    summary = filtered
                silent = goal.recurring and _is_silent_outcome(summary)
                if genuinely_done and not silent:
                    # A recurring goal "finishes" every cycle by design, so
                    # re-quoting its whole description (often a paragraph,
                    # e.g. NewsAnalyzer's) ahead of the actual answer on every
                    # single notification is pure noise repeated forever --
                    # the human already knows what their standing goal is.
                    # Only a genuine one-shot completion is worth naming.
                    await self.announce(
                        f"{self.definition.name}: {summary}"
                        if goal.recurring
                        else f'{self.definition.name} finished "{goal.description}": '
                        f"{outcome.summary}"
                    )
                elif outcome.error:
                    await self.announce(
                        f'{self.definition.name} hit an error on "{goal.description}": '
                        f"{outcome.error}"
                    )
                elif outcome.step:
                    # Mid-flight chatter ("harness job N is looking into...",
                    # "still working") is mechanics, not the answer a human
                    # asked a recurring goal to produce -- worth having in the
                    # logs, not worth interrupting someone over. Only the
                    # goal's actual finish (or an error) reaches announce().
                    logger.info(
                        '%s progress on "%s": %s',
                        self.definition.name,
                        goal.description,
                        outcome.step,
                    )
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

    async def _sanitize_report(self, raw: str, report_pattern: str) -> str:
        """LLM fallback for when a regex line-filter finds nothing to keep.

        _apply_report_pattern() is the cheap, deterministic filter and stays
        the first line of defense, but it only ever keeps a line that already
        matches the goal's report_pattern exactly -- a model that runs its
        report and its narration together on one line, wraps a "why" clause
        across two lines, or drifts slightly off the punctuation the pattern
        expects (an em dash for "--", a smart quote) leaves every line
        rejected and the regex filter alone would announce silence even
        though the model actually found something to say. This asks the same
        model, in a fresh call with no goal/tool context to narrate about, to
        pull just the report out of its own raw answer -- then the raw regex
        filter still runs on *that* output before anything is announced, so
        this step only ever narrows what gets sent, never bypasses the format
        contract.
        """
        raw = await self.provider.generate(
            "Extract only the lines that already match this exact format from the "
            f"text below; output nothing else, not even an introduction:\n\n"
            f"FORMAT (regex): {report_pattern}\n\n"
            f"TEXT:\n{raw}\n\n"
            "If no line in TEXT matches, output nothing.",
            system=(
                "You are a strict text filter, not an assistant. You never explain, "
                "apologize, or add commentary -- you output only the matching lines, "
                "verbatim, or nothing at all."
            ),
            model=self.definition.model_name,
            num_ctx=self.num_ctx,
        )
        return strip_reasoning(raw)

    async def _summarize(self, text: str) -> str:
        raw = await self.provider.generate(
            f"Compress these notes into at most 5 short bullet facts. Keep only what is "
            f"still true and useful.\n\n{text}",
            system="You compress an agent's long-term memory. Output bullets only.",
            model=self.definition.model_name,
            num_ctx=self.num_ctx,
        )
        return strip_reasoning(raw)

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
            work=self._work_summary(),
            num_ctx=self.num_ctx,
        )

    def _work_summary(self) -> str:
        """What this agent is doing right now, in the runtime's own words.

        A human who asks "what are you working on?" is asking about the live
        loop, not about what the model remembers, so the answer is assembled
        from state the runtime owns and only phrased by the model.
        """
        lines = [f"phase: {self.state.phase}", f"cycles completed: {self.state.cycles}"]
        if goal := self.definition.mind.next_goal():
            lines.append(f"goal in hand: {goal.description}")
        intention = self.definition.mind.current_intention()
        if intention is not None and (step := intention.current) is not None:
            lines.append(f"step in hand: {step.description}")
        if self.state.last_outcome:
            lines.append(f"last finished step: {self.state.last_outcome}")
        if self.state.last_error:
            lines.append(f"last error: {self.state.last_error}")
        if self.definition.autonomy is Autonomy.CYCLIC:
            lines.append(f"next cycle in about {max(0, round(self._due_in()))}s")
        return "\n".join(lines)

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
    overrides: dict[str, tuple[str, str, int | None]] | None = None,
) -> list[AgentDefinition]:
    """Bootstrap the built-in agents, each already carrying its standing goal.

    The goal is seeded here rather than left for a human to type, because an
    agent with no goal has nothing to do on its first cycle -- which is how the
    Evolver ended up never starting.
    """
    overrides = overrides or {}
    definitions: list[AgentDefinition] = []
    for agent_id, name, purpose, goal, autonomy in SYSTEM_AGENTS:
        chosen = overrides.get(agent_id, (provider, model, None))
        definition = AgentDefinition(
            id=agent_id,
            name=name,
            type="system",
            created_by="bootstrap",
            identity=name,
            purpose=purpose,
            provider=chosen[0],
            model_name=chosen[1],
            num_ctx=chosen[2],
            autonomy=autonomy,
            status=AgentStatus.ACTIVE,
        )
        definition.mind.add_goal(goal, priority=3, recurring=True)
        definitions.append(definition)
    return definitions
