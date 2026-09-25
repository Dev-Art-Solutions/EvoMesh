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

import asyncio
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from evomesh.cognition import (
    CycleContext,
    CycleOutcome,
    parse_cycle_reply,
    strip_reasoning,
)
from evomesh.cognitive_services import CognitiveServiceType, ModelInvocationReason
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
from evomesh.events import Event, EventBus, EventType
from evomesh.goal_manager import GoalEvaluationContext, GoalManager
from evomesh.harness_queue import HarnessGateway
from evomesh.memory import clip
from evomesh.models import ModelUnavailableError
from evomesh.procedural_learning import (
    LEARNED_PREFIX,
    ExecutionTrace,
    ProcedureLearner,
    goal_signature,
)
from evomesh.rules import (
    BELIEF_CHANGED_EVENT,
    RuleEffect,
    RuleEngine,
    RuleResult,
    RuntimeEvent,
    rules_from_config,
)

logger = logging.getLogger(__name__)

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
    # Both the news-watcher and news-analyzer templates phrase their goal as
    # "Fetch the latest financial headlines with news_fetch ..." -- without
    # this verb that step never qualified for through_harness(), so the tool
    # it names could never actually be called; the model just reported (truthfully)
    # that it had no way to call it.
    "fetch",
    "gather",
)

# Told to an agent that has tools, since it decides which of its steps get them.
PLAN_TOOL_HINT = (
    "You have tools, but a step is only carried out with them if it STARTS with "
    "one of these words: "
    + ", ".join(sorted({verb.strip().capitalize() for verb in HARNESS_VERBS}))
    + ". Start every step that must look something up or use a tool with one of "
    "them, and name the tool when you know it."
)

# How long a reactive chat question may wait on a harness job before falling
# back to answering from memory -- comfortably under the console's own 300s
# reply wait, so a human sees some answer rather than only ever a timeout.
HARNESS_RESPOND_TIMEOUT_SECONDS = 180.0
HARNESS_RESPOND_POLL_SECONDS = 0.5

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
class PlanStatistics:
    selected: int = 0
    succeeded: int = 0
    failed: int = 0


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
    goal_kind: str | None = None
    preconditions: tuple[Callable[[Goal, MindState], bool], ...] = ()
    context_keys: tuple[str, ...] = ()
    action: str = "think"
    provenance: str = "python"
    statistics: PlanStatistics = field(default_factory=PlanStatistics, compare=False)

    def applicable(self, goal: Goal, mind: MindState) -> bool:
        if self.goal_kind is not None and goal.kind != self.goal_kind:
            return False
        return self.matches(goal, mind) and all(check(goal, mind) for check in self.preconditions)

    def selected(self) -> None:
        object.__setattr__(
            self,
            "statistics",
            PlanStatistics(
                selected=self.statistics.selected + 1,
                succeeded=self.statistics.succeeded,
                failed=self.statistics.failed,
            ),
        )

    def record(self, *, success: bool) -> None:
        object.__setattr__(
            self,
            "statistics",
            PlanStatistics(
                selected=self.statistics.selected,
                succeeded=self.statistics.succeeded + int(success),
                failed=self.statistics.failed + int(not success),
            ),
        )


class PlanLibrary:
    """Means-ends reasoning by lookup. No model call, no invented steps."""

    def __init__(self, recipes: Sequence[PlanRecipe] = ()) -> None:
        self.recipes = tuple(recipes)

    def select(self, goal: Goal, mind: MindState) -> PlanRecipe | None:
        for recipe in self.recipes:
            try:
                if recipe.applicable(goal, mind):
                    recipe.selected()
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
    # See CycleOutcome.again.
    again: bool = False

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
    learner: ProcedureLearner = field(default_factory=ProcedureLearner)

    async def cycle(self, behavior: BDIBehavior, context: CycleContext) -> CycleOutcome:
        mind = context.definition.mind

        percepts = await behavior.perceive(context)
        change = mind.revise(percepts)
        inputs = (
            *context.events,
            *(RuntimeEvent(BELIEF_CHANGED_EVENT, {"value": key}) for key in sorted(change.keys)),
        )
        goals_before = {goal.id for goal in mind.goals}
        rules = self._rule_engine(behavior, context).fire(mind, inputs)
        if rules.derived_beliefs:
            change = BeliefChange(
                added=change.added
                + tuple(
                    belief.key
                    for belief in rules.derived_beliefs
                    if belief.key not in change.keys
                ),
                updated=change.updated,
            )
        self._adopt_desires(mind, await behavior.options(context, change))
        await self._dispatch(behavior, context, rules, len(inputs), change, goals_before)
        artifact_root = (
            Path(context.definition.harness_root)
            if context.definition.harness_root
            else None
        )
        evaluation = GoalEvaluationContext(artifact_root=artifact_root)
        manager = GoalManager(mind)
        for goal in mind.goals:
            if goal.is_open and (goal.success_conditions or goal.failure_conditions):
                manager.evaluate(goal, evaluation)

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

    # -- rules -------------------------------------------------------------

    @staticmethod
    def _rule_engine(behavior: BDIBehavior, context: CycleContext) -> RuleEngine:
        engine = behavior.rule_engine()
        try:
            return engine.with_rules(rules_from_config(context.definition.rules))
        except ValueError as exc:
            # Refused where they are set (templates, console); a row that got
            # past that must not stop the agent thinking, only its own rules.
            logger.warning("%s: ignoring its rules: %s", context.definition.name, exc)
            return engine

    async def _dispatch(
        self,
        behavior: BDIBehavior,
        context: CycleContext,
        rules: RuleResult,
        consumed: int,
        change: BeliefChange,
        goals_before: set[str],
    ) -> None:
        """Make what this cycle's rules and revision produced visible: rule
        events and the mesh-level facts (belief changes, new goals) go on the
        event bus, requested actions go to the behavior."""
        bus = context.service("events")
        agent_id = context.definition.id
        if isinstance(bus, EventBus):
            for key in sorted(change.keys):
                await bus.publish(
                    Event(EventType.BELIEF_CHANGED, "bdi", agent_id, payload={"key": key})
                )
            for goal in context.definition.mind.goals:
                if goal.id not in goals_before:
                    await bus.publish(
                        Event(
                            EventType.GOAL_CREATED,
                            "bdi",
                            agent_id,
                            goal.id,
                            {"description": goal.description, "kind": goal.kind},
                        )
                    )
            for event in rules.events[consumed:]:
                await bus.publish(
                    Event(
                        EventType.RULE_EVENT,
                        "rules",
                        agent_id,
                        payload={"type": event.type, **event.payload},
                    )
                )
        for action in rules.requested_actions:
            await behavior.on_rule_action(context, action)

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
        goal = GoalManager(mind).next_goal()
        if goal is None:
            for item in mind.intentions:
                if item.status is IntentionStatus.ACTIVE:
                    item.finish(IntentionStatus.DROPPED)
            return None
        recipe = behavior.library().select(goal, mind)
        if recipe is not None:
            mind.record_plan_selected(recipe.name)
            return mind.commit(
                goal.id,
                recipe.steps,
                plan=recipe.name,
                action=recipe.action,
                context_keys=recipe.context_keys,
            )
        if goal.cron or goal.interval_seconds:
            # A goal on a schedule is an appointment kept the same way every
            # time, not a fresh essay prompt every occurrence -- unlike a bare
            # `recurring=True` with no schedule, which re-plans with the model
            # on purpose (see test_a_finished_plan_is_marked_achieved...).
            # Letting a small model re-paraphrase a scheduled goal is how a
            # step stops starting with the verb through_harness() keys off of
            # (HARNESS_VERBS): the goal author already wrote a step-shaped
            # sentence naming the tool to use, and committing it verbatim both
            # saves the planning call and keeps the harness objective the
            # full goal, not a paraphrase that dropped the part naming the
            # tool.
            return mind.commit(goal.id, [goal.description], plan="ad-hoc")
        procedure = self.learner.match(mind, goal)
        if procedure is not None:
            # A plan this agent already made for this exact goal, and saw
            # succeed repeatedly: reusing it is the planning call not paid.
            mind.record_plan_selected(procedure.name)
            return mind.commit(goal.id, procedure.steps, plan=procedure.name)
        steps = await self._plan_with_model(context, goal)
        return mind.commit(goal.id, steps, plan="model" if len(steps) > 1 else "ad-hoc")

    def _learn(
        self, context: CycleContext, mind: MindState, intention: Intention, *, succeeded: bool
    ) -> None:
        """Feed a finished model-made or learned plan to procedural learning.
        Library plans are known already and ad-hoc ones have nothing to reuse."""
        if intention.plan != "model" and not intention.plan.startswith(LEARNED_PREFIX):
            return
        if not _has_goal(mind, intention):
            return
        goal = mind.goal(intention.goal_id)
        self.learner.observe(
            mind,
            ExecutionTrace(
                goal_type=goal.kind,
                context_signature=goal_signature(goal),
                plan_name=intention.plan,
                steps=[step.description for step in intention.steps],
                agents=[context.definition.id],
                succeeded=succeeded,
            ),
        )

    async def _plan_with_model(self, context: CycleContext, goal: Goal) -> list[str]:
        """One planning call per goal. A model that is down still yields a plan.

        For an agent granted the harness, whether a step runs with tools is
        decided by its first word (HARNESS_VERBS, see through_harness) -- a rule
        the model was never told. So the planning prompt says it, and a plan
        that paraphrased a tool-shaped goal ("Fetch ... with news_fetch") into
        steps none of which qualify is dropped for the goal itself, verbatim:
        a small model's plan must not be what takes its tools away.
        """
        tooled = bool(context.definition.harness_root)
        instruction = f"{PLAN_FORMAT}\n{PLAN_TOOL_HINT}" if tooled else PLAN_FORMAT
        try:
            raw = await context.think(
                instruction,
                goal=goal,
                service=CognitiveServiceType.CREATE_NOVEL_PLAN,
                reason=ModelInvocationReason.NO_PLAN_MATCH,
            )
        except (ModelUnavailableError, RuntimeError, ValueError):
            return [goal.description]
        steps = parse_plan(raw, self.max_steps)
        if (
            tooled
            and goal.description.strip().lower().startswith(HARNESS_VERBS)
            and not any(step.strip().lower().startswith(HARNESS_VERBS) for step in steps)
        ):
            return [goal.description]
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
            mind.record_plan_outcome(intention.plan, success=False)
            self._learn(context, mind, intention, succeeded=False)
            return CycleOutcome.failed(f"{position} failed: {exc}")

        goal = mind.goal(intention.goal_id) if _has_goal(mind, intention) else None
        if result.impossible:
            intention.finish(IntentionStatus.IMPOSSIBLE)
            mind.record_plan_outcome(intention.plan, success=False)
            self._learn(context, mind, intention, succeeded=False)
            if goal is not None and not goal.recurring:
                goal.status = GoalStatus.BLOCKED
                goal.blocked_reason = result.impossible
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
                again=result.again,
            )

        intention.advance(result.summary, failed=result.failed)
        achieved = result.achieved or intention.exhausted
        if achieved and goal is not None and goal.success_conditions:
            root = (
                Path(context.definition.harness_root)
                if context.definition.harness_root
                else None
            )
            achieved = (
                GoalManager(mind).evaluate(
                    goal, GoalEvaluationContext(artifact_root=root)
                )
                is GoalStatus.DONE
            )
        if achieved:
            intention.finish(IntentionStatus.ACHIEVED)
            mind.record_plan_outcome(intention.plan, success=not result.failed)
            self._learn(context, mind, intention, succeeded=not result.failed)
        elif intention.exhausted:
            # The procedure finished, but its explicit predicate did not. It
            # may be reconsidered next cycle; it must not silently certify the
            # goal merely because there are no steps left.
            intention.finish(IntentionStatus.DROPPED)
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
            again=result.again,
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

    def rule_engine(self) -> RuleEngine:
        """Rules built into this behavior; the agent's own configured rules
        are added to them each cycle."""
        return RuleEngine()

    async def on_rule_action(self, context: CycleContext, action: RuleEffect) -> None:
        """Carry out a rule's REQUEST_ACTION. Built in: ``announce`` (tell the
        humans ``value``) and ``wake`` (run the agent named ``value`` now)."""
        environment = context.service("environment")
        if action.key == "announce" and environment is not None:
            await cast("Any", environment).announce(str(action.value))
            return
        if action.key == "wake" and environment is not None:
            try:
                target = cast("Any", environment).registry.get(str(action.value))
            except KeyError:
                logger.warning("rule asked to wake unknown agent %s", action.value)
                return
            if runtime := cast("Any", environment).runtimes.get(target.id):
                runtime.wake()
            return
        logger.warning(
            "%s: no handler for rule action %r", context.definition.name, action.key
        )

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
        raw = await context.think(
            instruction,
            service=CognitiveServiceType.EXECUTE_STEP,
            reason=ModelInvocationReason.PLAN_STEP_REQUIRES_REASONING,
            relevant_belief_keys=intention.context_keys,
        )
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
            learn_hint = (
                " If this took combining more than one tool in a way none of "
                "your own skills already cover, and the same kind of step is "
                "likely to recur, call learn_skill once you actually have the "
                "result -- the sequence you just used, not one you only "
                "planned. patch_skill fixes one exact piece of a skill you "
                "already wrote, cheaper than resending the whole thing."
                if context.definition.can_learn_skills
                else ""
            )
            job = harness.submit(
                f"{step.description}\n\n"
                f"Work inside this directory and report what you found.{learn_hint}",
                agent_id=context.definition.id,
                root=Path(root),
                label=step.description,
                # This step polls `harness.job(step.job)` again every cycle
                # until it finishes -- an inbox delivery on top of that would
                # hand the agent its own step's result a second time, as a
                # fresh "message" from "harness" for respond() to answer.
                notify=False,
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
        if answer := await self._respond_through_harness(context, message):
            return answer
        instruction = (
            "Answer the last INBOX message directly, in at most four sentences. "
            "Use BELIEFS, MEMORY and YOUR WORKING NOTES as established fact. "
            "If you are asked what you are working on, answer from CURRENT WORK: "
            "name the goal, the step you are on and the stage you have reached, "
            "and never invent progress that CURRENT WORK does not show. "
            "If you want to hand back a file you already created, name it on "
            "its own line as exactly: FILE: <path>, a path inside your own "
            "workspace -- never a file you have not actually written."
        )
        if detail := await self.status(context):
            context.work = f"{context.work}\n{detail}".strip()
        return await context.think(
            instruction,
            service=CognitiveServiceType.CHAT_RESPONSE,
            reason=ModelInvocationReason.HUMAN_CHAT_REQUIRES_RESPONSE,
        )

    async def _respond_through_harness(
        self, context: CycleContext, message: Message
    ) -> str | None:
        """Let a direct question actually call a tool, not just answer from
        memory -- the same capability gap through_harness() closes for a plan
        step, closed here for a human's own question.

        Only for an agent already granted harness access: that is the
        existing, deliberate capability grant (see AgentDefinition.harness_root),
        not a new one this method invents. None return here means "answer from
        memory instead", the previous behavior -- an agent with no harness
        grant, or one already mid-job on something else, is unaffected.
        """
        root = context.definition.harness_root
        harness = context.service("harness")
        if not root or harness is None or not isinstance(harness, HarnessGateway):
            return None
        if harness.open_job_for(context.definition.id) is not None:
            # Already has a job in flight for something else -- answering from
            # memory this once beats hijacking that job or queuing a second one
            # (the queue allows only one open job per agent anyway).
            return None
        # The harness job already gets every installed skill's one-line
        # catalog entry prepended (environment.py's _run_harness_job) -- but
        # that list is mesh-wide, easy to skim past, and nothing points the
        # model at *this agent's own* skills specifically. Found live,
        # repeatedly: NewsAnalyzer answering a chat question with a full
        # narrated report (headers, a "let me work through each" preamble,
        # per-headline reasoning) despite its own news-impact-analysis skill
        # spelling out, with BAD/GOOD examples, exactly the one-line format a
        # chat answer should take -- the skill was simply never read for a
        # reactive question, only during its recurring goal. Naming this
        # agent's own bundled skills here, imperatively, is what a generic
        # "you have tools" catalog entry cannot do.
        # A harness job otherwise sees only this one message -- context.inbox
        # (up to MAX_INBOX_HISTORY, agents.py) is what the plain non-harness
        # respond() fallback already gets via render_inbox(), and the harness
        # path needs the same thing for the same reason: "send it as a PDF"
        # names no content of its own at all, and a job that cannot see "give
        # me the last 10 news" two messages back has nothing to build one
        # from. Found live: NewsWatcher asked what a bare "send it as PDF"
        # should contain, then -- given "the 10 news" with still no memory of
        # "as a PDF" -- just answered in chat text again instead of ever
        # reaching document_write. Excludes the current message itself
        # (already named above) so it is not repeated.
        history_hint = ""
        if prior := context.inbox[:-1][-3:]:
            lines = "\n".join(
                f"- {item.sender_id}: {' '.join(item.content.split())}" for item in prior
            )
            history_hint = f"\n\nRecent messages before this one, oldest first:\n{lines}"
        skills_hint = ""
        if context.definition.skills:
            names = ", ".join(context.definition.skills)
            skills_hint = (
                f"\n\nThis agent's own skills: {names}. If one of them governs "
                "this kind of question, read it first (the `read` tool) and "
                "follow its formatting rules exactly -- do not answer "
                "generically when a skill already specifies the answer's shape."
            )
        learn_hint = ""
        if context.definition.can_learn_skills:
            learn_hint = (
                "\n\nIf answering this took combining more than one tool in a "
                "way none of your own skills above already cover, and the same "
                "question is likely to come up again, call learn_skill once "
                "you have the answer -- write down the actual sequence you "
                "just used, not a plan for one you did not run. For a small "
                "fix to a skill you already wrote, patch_skill (one exact "
                "text replacement) is cheaper than resending the whole thing."
            )
        job = harness.submit(
            f"Answer this question directly: {message.content.strip()}"
            f"{history_hint}\n\n"
            "Use a tool only if you actually need to -- if you already know "
            "the answer, or the question needs no live data, just answer.\n\n"
            "This is a chat reply to a human, not a report or a work log. Do "
            "not narrate your own process (no 'Here's what I found', no "
            "'## Analysis' headers, no listing what you checked and ruled "
            "out) and do not pad a short fact into a structured writeup. "
            "Say the answer, plainly, the way you would say it out loud. If "
            "you were asked for a document (a PDF, spreadsheet, etc.) and "
            "created one with document_write, name it on its own line as "
            f"exactly: FILE: <path> -- that is what hands it back."
            f"{skills_hint}{learn_hint}",
            agent_id=context.definition.id,
            root=Path(root),
            label=message.content.strip()[:80],
            # This call already returns the answer to whoever asked, once the
            # wait below gets it -- an inbox delivery on top of that would
            # hand the agent its own answer back as a new inbound "message",
            # which respond() then tries to answer too, and so on forever.
            # Flipped back on below only if the wait times out and delivery
            # becomes the sole way the asker ever sees this job finish.
            notify=False,
            # A human is waiting on this one, synchronously -- it cuts ahead
            # of whatever background work (the Evolver's pipeline, another
            # agent's own plan step) is still queued, though never ahead of
            # a job already running. See HarnessQueue's own priority order.
            priority=True,
        )
        elapsed = 0.0
        while job.open and elapsed < HARNESS_RESPOND_TIMEOUT_SECONDS:
            await asyncio.sleep(HARNESS_RESPOND_POLL_SECONDS)
            elapsed += HARNESS_RESPOND_POLL_SECONDS
        if job.open:
            job.notify = True
            return (
                f"Still working on that (harness job {job.number}) -- ask again "
                "in a moment, or check /harness status."
            )
        if job.result is None:
            return f"The harness job did not finish: {job.detail or 'unknown error'}"
        if job.result.outcome == "answered":
            return job.result.answer.strip() or "The harness job found nothing to report."
        return f"[{job.result.outcome}] {job.result.detail}"


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
