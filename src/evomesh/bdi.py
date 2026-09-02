"""The practical-reasoning loop: beliefs, desires, intentions.

This is the Rao and Georgeff interpreter, not a set of BDI-shaped fields:

    percepts := perceive()
    B        := brf(B, percepts)          belief revision
    D        := options(B, I)             which goals are worth having
    I        := filter(B, D, I)           commit to one, with a plan
    execute one step of the plan
    drop I when it is achieved or has become impossible

The two properties that make it BDI rather than a loop with nice names are
commitment and reconsideration. An agent that re-decides everything every tick
has no intentions, only impulses; an agent that never re-decides is blind to a
world that moved. So a plan is adopted once and executed across cycles, and it
is reconsidered only on a specific trigger: the plan ran out, the goal changed,
or a belief the plan depends on was revised.

That also happens to be what makes this affordable on a small local model. One
planning call per goal, then cheap per-step execution, instead of re-deriving
the whole situation from scratch on every cycle. Library plans and deterministic
steps cost no model call at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from evomesh.cognition import (
    CycleContext,
    CycleOutcome,
    parse_cycle_reply,
    strip_reasoning,
)
from evomesh.contracts import (
    AgentPhase,
    Belief,
    BeliefChange,
    Goal,
    GoalStatus,
    Intention,
    IntentionStatus,
    Message,
    MindState,
    PlanStep,
)
from evomesh.harness_queue import HarnessGateway
from evomesh.memory import clip
from evomesh.models import ModelUnavailableError

MAX_PLAN_STEPS = 4

PLAN_FORMAT = (
    "Break this goal into 2 to 4 short steps that can each be done one at a time.\n"
    "Reply with one numbered step per line and nothing else:\n"
    "1. <step>\n"
    "2. <step>\n"
    "3. <step>"
)

STEP_LINE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s*(.+?)\s*$")
# A "WORD:" opener is a reply field, never a plan step.
FIELD_LINE = re.compile(r"^[A-Za-z][A-Za-z ]{0,14}:")

# A plan step that starts with one of these is a step that needs to look at
# something, so a granted agent takes it with tools instead of with a prompt.
# Decided by a prefix rather than by asking the model: that would be one extra
# inference per cycle to answer a question this list answers for free.
HARNESS_VERBS = (
    "investigate",
    "read ",
    "find ",
    "search",
    "check ",
    "look ",
    "inspect",
    "review",
    "identify",
    "locate",
    "diagnose",
)

RECONSIDER_NO_INTENTION = "nothing committed yet"
RECONSIDER_PLAN_DONE = "the plan finished"
RECONSIDER_GOAL_CLOSED = "the goal is no longer open"
RECONSIDER_BETTER_GOAL = "a higher-priority goal appeared"


def parse_plan(raw: str, limit: int = MAX_PLAN_STEPS) -> list[str]:
    """Read a numbered plan out of whatever the model actually returned."""
    text = strip_reasoning(raw)
    numbered = [
        match.group(1).strip(" *_`").strip()
        for line in text.splitlines()
        if (match := STEP_LINE.match(line))
    ]
    if numbered:
        # A numbered list is a plan. Never fall back after finding one, or a
        # step too short to keep would be re-admitted with its bullet attached.
        steps = [item for item in numbered if len(item) > 2]
    else:
        # An unnumbered answer can still be a plan, but only if its lines look
        # like steps. Without this guard a model that ignores the format and
        # answers "STEP: ... RESULT: ..." turns its own field names into a plan.
        steps = [
            stripped
            for line in text.splitlines()
            if len(stripped := line.strip(" *_`").strip()) > 2
            and not FIELD_LINE.match(stripped)
        ]
    return [clip(step, 200) for step in steps[:limit]]


@dataclass(frozen=True)
class Desire:
    """A goal the agent would like to have. Not yet a commitment."""

    description: str
    priority: int = 5
    recurring: bool = False


@dataclass(frozen=True)
class PlanRecipe:
    """A library plan: what it is for, and the steps it expands to.

    ``context_keys`` names the beliefs the plan assumes. When one of them is
    revised the agent reconsiders, which is how a committed plan notices that
    the world stopped matching it.
    """

    name: str
    steps: tuple[str, ...]
    matches: Callable[[Goal, MindState], bool] = lambda goal, mind: True
    context_keys: tuple[str, ...] = ()
    action: str = "think"


class PlanLibrary:
    """Means-ends reasoning by lookup. No model call, no invented steps."""

    def __init__(self, recipes: Sequence[PlanRecipe] = ()) -> None:
        self.recipes = tuple(recipes)

    def select(self, goal: Goal, mind: MindState) -> PlanRecipe | None:
        for recipe in self.recipes:
            try:
                if recipe.matches(goal, mind):
                    return recipe
            except (KeyError, AttributeError, TypeError):
                continue
        return None


@dataclass
class StepResult:
    """What executing one plan step produced."""

    summary: str
    fact: str = ""
    achieved: bool = False
    failed: bool = False
    hold: bool = False
    impossible: str | None = None
    phase: AgentPhase = AgentPhase.IDLE

    @classmethod
    def blocked(cls, reason: str) -> StepResult:
        return cls(summary=reason, impossible=reason, phase=AgentPhase.ERROR)

    @classmethod
    def waiting(cls, reason: str) -> StepResult:
        """The intention stands but cannot progress until the world changes.

        The step is not consumed, so the agent keeps its commitment instead of
        completing and re-adopting a plan on every cycle it spends waiting.
        """
        return cls(summary=reason, hold=True, phase=AgentPhase.WAITING_HUMAN)


@dataclass
class BDIReasoner:
    """One turn of the interpreter per cycle."""

    max_steps: int = MAX_PLAN_STEPS

    async def cycle(self, behavior: BDIBehavior, context: CycleContext) -> CycleOutcome:
        mind = context.definition.mind

        percepts = await behavior.perceive(context)
        change = mind.revise(percepts)
        self._adopt_desires(mind, await behavior.options(context, change))

        intention = mind.current_intention()
        reason = self.reconsider(intention, mind, change)
        if reason is not None:
            intention = await self.deliberate(behavior, context, mind)
        if intention is None:
            return CycleOutcome.idle("No open goal. Waiting for one.")

        step = intention.current
        if step is None:
            return CycleOutcome.idle("The committed plan has no runnable step.")
        return await self._execute(behavior, context, mind, intention, step, reason)

    # -- option generation ----------------------------------------------

    def _adopt_desires(self, mind: MindState, desires: Sequence[Desire]) -> None:
        for desire in desires:
            if any(
                goal.description == desire.description and goal.is_open
                for goal in mind.goals
            ):
                continue
            mind.add_goal(
                desire.description, priority=desire.priority, recurring=desire.recurring
            )

    # -- reconsideration -------------------------------------------------

    def reconsider(
        self, intention: Intention | None, mind: MindState, change: BeliefChange
    ) -> str | None:
        """Should the agent re-deliberate? Cheap by design: never calls a model."""
        if intention is None:
            return RECONSIDER_NO_INTENTION
        if intention.status is not IntentionStatus.ACTIVE:
            return RECONSIDER_NO_INTENTION
        if intention.exhausted:
            return RECONSIDER_PLAN_DONE
        try:
            goal = mind.goal(intention.goal_id)
        except KeyError:
            return RECONSIDER_GOAL_CLOSED
        if not goal.is_open:
            return RECONSIDER_GOAL_CLOSED
        best = mind.next_goal()
        if best is not None and best.id != goal.id and best.priority < goal.priority:
            return RECONSIDER_BETTER_GOAL
        touched = change.keys & frozenset(intention.context_keys)
        if touched:
            return f"beliefs changed: {', '.join(sorted(touched))}"
        return None

    # -- deliberation and means-ends reasoning ---------------------------

    async def deliberate(
        self, behavior: BDIBehavior, context: CycleContext, mind: MindState
    ) -> Intention | None:
        goal = mind.next_goal()
        if goal is None:
            for item in mind.intentions:
                if item.status is IntentionStatus.ACTIVE:
                    item.finish(IntentionStatus.DROPPED)
            return None
        recipe = behavior.library().select(goal, mind)
        if recipe is not None:
            return mind.commit(
                goal.id,
                recipe.steps,
                plan=recipe.name,
                action=recipe.action,
                context_keys=recipe.context_keys,
            )
        steps = await self._plan_with_model(context, goal)
        return mind.commit(goal.id, steps, plan="model" if len(steps) > 1 else "ad-hoc")

    async def _plan_with_model(self, context: CycleContext, goal: Goal) -> list[str]:
        """One planning call per goal. A model that is down still yields a plan."""
        try:
            raw = await context.think(PLAN_FORMAT, goal=goal)
        except (ModelUnavailableError, RuntimeError, ValueError):
            return [goal.description]
        steps = parse_plan(raw, self.max_steps)
        return steps or [goal.description]

    # -- execution --------------------------------------------------------

    async def _execute(
        self,
        behavior: BDIBehavior,
        context: CycleContext,
        mind: MindState,
        intention: Intention,
        step: PlanStep,
        reason: str | None,
    ) -> CycleOutcome:
        position = f"step {intention.cursor + 1}/{len(intention.steps)}"
        try:
            result = await behavior.execute(context, intention, step)
        except (ModelUnavailableError, RuntimeError, ValueError) as exc:
            intention.advance(str(exc), failed=True)
            return CycleOutcome.failed(f"{position} failed: {exc}")

        goal = mind.goal(intention.goal_id) if _has_goal(mind, intention) else None
        if result.impossible:
            intention.finish(IntentionStatus.IMPOSSIBLE)
            if goal is not None and not goal.recurring:
                goal.status = GoalStatus.BLOCKED
            return CycleOutcome(
                summary=f"{position} is impossible: {result.impossible}",
                step=step.description,
                fact=result.fact,
                phase=result.phase,
                error=result.impossible,
                worked=True,
            )

        if result.hold:
            return CycleOutcome(
                summary=result.summary,
                step=step.description,
                fact=result.fact,
                phase=result.phase,
                worked=True,
            )

        intention.advance(result.summary, failed=result.failed)
        achieved = result.achieved or intention.exhausted
        if achieved:
            intention.finish(IntentionStatus.ACHIEVED)
        summary = result.summary or step.description
        if reason is not None and reason != RECONSIDER_NO_INTENTION:
            summary = f"{summary} (re-planned: {reason})"
        return CycleOutcome(
            summary=summary,
            step=step.description,
            fact=result.fact,
            goal_done=achieved,
            phase=result.phase,
            worked=True,
        )


def _has_goal(mind: MindState, intention: Intention) -> bool:
    return any(goal.id == intention.goal_id for goal in mind.goals)


class BDIBehavior:
    """Base for every agent behavior. Subclasses override the BDI hooks.

    ``perceive`` says what the agent can observe, ``options`` what it should
    want, ``library`` how it already knows to do things, and ``execute`` how a
    single step is carried out. Anything left alone falls back to the model.
    """

    name = "bdi"

    def __init__(self, reasoner: BDIReasoner | None = None) -> None:
        self.reasoner = reasoner or BDIReasoner()

    # -- hooks -----------------------------------------------------------

    async def perceive(self, context: CycleContext) -> list[Belief]:
        """Default percepts: who spoke to the agent since the last cycle."""
        percepts: list[Belief] = []
        for message in context.inbox[-2:]:
            percepts.append(
                Belief(
                    key=f"inbox.{message.sender_id}",
                    statement=clip(" ".join(message.content.split()), 240),
                    source=message.sender_id,
                )
            )
        return percepts

    async def options(
        self, context: CycleContext, change: BeliefChange
    ) -> list[Desire]:
        return []

    def library(self) -> PlanLibrary:
        return PlanLibrary()

    async def execute(
        self, context: CycleContext, intention: Intention, step: PlanStep
    ) -> StepResult:
        if (harness_step := await self.through_harness(context, step)) is not None:
            return harness_step
        instruction = (
            f"Your current plan is:\n{intention.render()}\n\n"
            f"Do only this step now: {step.description}\n\n"
            "Reply with exactly these three lines and nothing else:\n"
            "RESULT: <what you did or concluded, at most two sentences>\n"
            "FACT: <one durable fact worth remembering, or NONE>\n"
            "STATUS: <done, or blocked if the step cannot be done at all>"
        )
        raw = await context.think(instruction)
        reply = parse_cycle_reply(raw)
        if reply.blocked:
            return StepResult.blocked(reply.result or "the model reported it is blocked")
        return StepResult(
            summary=reply.result or reply.step or step.description,
            fact=reply.fact,
            phase=AgentPhase.IDLE,
        )

    async def through_harness(
        self, context: CycleContext, step: PlanStep
    ) -> StepResult | None:
        """Take this step with tools, if the agent was granted them and it looks
        like a step that needs to look at something.

        Deliberately decided by a verb rather than by asking the model. That
        would be one extra inference per cycle to answer a question a prefix
        answers for free, and on a 4B model the answer would be noise -- rule 6
        again: the model is the fallback, not the router.
        """
        root = context.definition.harness_root
        harness = context.service("harness")
        if not root or harness is None or not isinstance(harness, HarnessGateway):
            return None
        wanted = step.description.strip().lower()
        if not wanted.startswith(HARNESS_VERBS):
            return None
        # The job is remembered on the *step*, not on the agent: when it
        # finishes, the step that asked for it is the one that consumes the
        # answer, and a finished job must not be mistaken for "no job yet".
        job = harness.job(step.job) if step.job else None
        if job is None:
            job = harness.submit(
                f"{step.description}\n\nWork inside this directory and report what you found.",
                agent_id=context.definition.id,
                root=Path(root),
                label=step.description,
            )
            step.job = job.number
            if job.open:
                return StepResult(
                    summary=f"harness job {job.number} is looking into: {step.description}",
                    phase=AgentPhase.AWAITING_HARNESS,
                    hold=True,
                )
        if job.open:
            return StepResult(
                summary=f"harness job {job.number} is still working",
                phase=AgentPhase.AWAITING_HARNESS,
                hold=True,
            )
        answer = job.result.answer.strip() if job.result else job.detail
        # The finding goes into memory as the step's own outcome. An agent that
        # investigated something and did not remember it has investigated nothing.
        return StepResult(
            summary=answer or f"harness job {job.number} found nothing to report",
            fact=answer.splitlines()[0] if answer else "",
            phase=AgentPhase.IDLE,
        )

    # -- the runtime contract ---------------------------------------------

    async def cycle(self, context: CycleContext) -> CycleOutcome:
        return await self.reasoner.cycle(self, context)

    async def status(self, context: CycleContext) -> str:
        """Work in flight that only this behavior can describe.

        The runtime already reports phase, goal and step. A behavior that drives
        a pipeline of its own adds the stage that pipeline is on, read now
        rather than remembered from the last cycle.
        """
        return ""

    async def respond(self, context: CycleContext, message: Message) -> str:
        instruction = (
            "Answer the last INBOX message directly, in at most four sentences. "
            "Use BELIEFS, MEMORY and YOUR WORKING NOTES as established fact. "
            "If you are asked what you are working on, answer from CURRENT WORK: "
            "name the goal, the step you are on and the stage you have reached, "
            "and never invent progress that CURRENT WORK does not show."
        )
        if detail := await self.status(context):
            context.work = f"{context.work}\n{detail}".strip()
        return await context.think(instruction)


class ReflectiveBehavior(BDIBehavior):
    """The default agent: plans with the model, executes with the model."""

    name = "reflective"


@dataclass
class DeterministicBehavior(BDIBehavior):
    """Convenience base for agents whose steps are code, not prompts."""

    name: str = "deterministic"
    plans: tuple[PlanRecipe, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        BDIBehavior.__init__(self)

    def library(self) -> PlanLibrary:
        return PlanLibrary(self.plans)
