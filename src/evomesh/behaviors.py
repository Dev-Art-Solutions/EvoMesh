"""BDI behaviors for the four built-in system agents.

Each one fills in the same hooks: what it can perceive, what it should want,
what plans it already knows, and how one step is carried out. Only the generic
agent falls back to the model for planning and execution -- Guardian, Evaluator
and Evolver are fully deterministic, so the mesh keeps reasoning even when no
model is reachable at all.

The Evolver shows why a plan beats a state machine: its pipeline *is* a plan, so
it appears in ``/intentions`` as a checklist with a cursor, and a human
promoting a candidate revises a belief the plan depends on, which makes the
agent reconsider on its own rather than being told to.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, cast

from evomesh.bdi import (
    BDIBehavior,
    PlanLibrary,
    PlanRecipe,
    ReflectiveBehavior,
    StepResult,
)
from evomesh.blackboard import Blackboard
from evomesh.cognition import CycleContext
from evomesh.contracts import AgentPhase, Belief, Goal, Intention, PlanStep
from evomesh.coordination import DELEGATED_GOAL_KIND, ContractNet, WorkItem
from evomesh.evolution import (
    BACKLOG_MAX_SECONDS,
    BACKLOG_MAX_STEPS,
    BACKLOG_PICKS,
    MAX_SCOUT_ATTEMPTS,
    MAX_TARGET_ATTEMPTS,
    PICK_IMPROVEMENT,
    PICK_PLAN,
    PICK_SCOUT,
    PICK_TEST,
    PLAN_DIR,
    REVIEW_COMMAND,
    SOURCE_PICKS,
    TEST_ONLY_NOTE,
    BaselineResult,
    CandidateValidator,
    EnvironmentEvolver,
    Generation,
    GenerationExecutor,
    GenerationStatus,
    ObjectivePick,
    PlanNode,
    baseline_candidate,
    excerpt,
    parse_review,
    review_objective,
)
from evomesh.git import GitError
from evomesh.harness_queue import HarnessGateway
from evomesh.improvements import (
    EVIDENCE_BACKLOG_EXHAUSTED,
    EVIDENCE_CODEBASE_ANALYSIS,
    EVIDENCE_HUMAN_REQUEST,
    NOT_PICKABLE_NOW,
    RECURRENCE_SOURCES,
    Candidate,
    ImprovementControl,
    ImprovementStatus,
    PriorityFactors,
    ReviewVerdict,
)
from evomesh.improvements import Improvement as TrackedImprovement
from evomesh.rules import (
    BELIEF_CHANGED_EVENT,
    Rule,
    RuleEffect,
    RuleEffectKind,
    RuleEngine,
    RuleOperator,
    RuleSource,
    RuleTest,
)

logger = logging.getLogger(__name__)

PROVIDER_KEY = "provider.ready"
DEGRADED_KEY = "mesh.degraded"
STAGE_KEY = "evolution.stage"
CANDIDATE_KEY = "evolution.candidate"
VERDICT_KEY = "evolution.verdict"

STAGE_PLAN = "plan"
STAGE_DRAFT = "draft"
STAGE_EVALUATE = "evaluate"
STAGE_DECOMPOSE = "decompose"
STAGE_PROPOSE = "propose"
STAGE_VALIDATE = "validate"
STAGE_REPAIR = "repair"
STAGE_REVIEW = "review"
STAGE_REPORT = "report"
STAGE_AWAIT_HUMAN = "await-human"

# How many consecutive no-file-changed generations pass before a human is
# pinged. Found live 2026-09-23: 41 generations straight (1286-1326) burned a
# full step budget each with nothing written, silent except in mesh.log,
# before the objective source that was starving them (both concrete backlogs
# empty, standing goal text with no file and no anchor) got noticed and
# fixed. A streak this long should reach a human via the same channel a
# restart or a promotion already does (Environment.announce) long before it
# gets anywhere near that count again.
NO_OP_STREAK_ALERT_EVERY = 5

# The Evolver's plan, in the order the pipeline runs. A stage's index here is
# also the plan cursor, so the checklist and the persisted pipeline state cannot
# drift apart. Repair is the one stage that can be skipped or entered several
# times, which moves the cursor backwards -- that is the honest picture of an
# agent that had to go back and fix its own work.
EVOLUTION_STAGES = (STAGE_PLAN, STAGE_PROPOSE, STAGE_VALIDATE, STAGE_REPAIR, STAGE_REPORT)
EVOLUTION_STEPS = (
    "open an isolated candidate generation",
    "propose and apply one mutation",
    "validate the candidate",
    "repair the candidate while validation fails",
    "hand the candidate to the human",
)
# With `auto_plan` on, three stages run between opening the candidate and
# authoring anything: draft a plan, have it reviewed, and recursively split it
# into minimal work items -- see `EvolverBehavior._draft_plan`/`_evaluate_plan`/
# `_decompose`. `STAGE_PROPOSE` then loops once per work item instead of once
# per generation (`_propose`'s `state["work_items"]` handling).
EVOLUTION_STAGES_WITH_PLAN = (
    STAGE_PLAN,
    STAGE_DRAFT,
    STAGE_EVALUATE,
    STAGE_DECOMPOSE,
    STAGE_PROPOSE,
    STAGE_VALIDATE,
    STAGE_REPAIR,
    STAGE_REPORT,
)
EVOLUTION_STEPS_WITH_PLAN = (
    "open an isolated candidate generation",
    "draft a plan for the objective",
    "have the plan reviewed",
    "split the plan into minimal work items",
    "propose and apply one work item",
    "validate the candidate",
    "repair the candidate while validation fails",
    "hand the candidate to the human",
)
# With auto_validate off the validation stage never runs, so neither validation
# nor the repair that only exists to answer it belongs in the checklist.
SKIP_VALIDATION_STAGES = (STAGE_PLAN, STAGE_PROPOSE, STAGE_REPORT)
SKIP_VALIDATION_STEPS = (EVOLUTION_STEPS[0], EVOLUTION_STEPS[1], EVOLUTION_STEPS[-1])
# With `review` on, a candidate that passed validation is read against its
# objective before it is reported -- see `EvolverBehavior._review`.
REVIEW_STEP = "review the change against its objective"
# A reviewer that never says COMPLETE or INCOMPLETE is asked again, up to this
# many attempts in all, before the candidate is discarded for want of a verdict.
REVIEW_ATTEMPTS = 2
# Under a promotion policy the last step is a decision, not a handover.
AUTO_PROMOTE_STEP = "promote or discard the candidate on its verdict"

AWAITING_KEY = "evolution.awaiting_human"

HEALTHY_PREFIXES = ("the model provider is ready", "all ")
INVESTIGATE = "Investigate why "
# The Guardian's plan for a delegated stall diagnosis (no model call).
DIAGNOSE_STALL = "diagnose-stall"

# How long the validate stage waits before deciding the suite is not instant.
# Widened twice already -- 0.05s, then 0.1s -- each passing locally every
# time and failing on GitHub Actions the moment the runner was busy enough
# that a scripted validator's task missed the window. record_mutation's
# aiosqlite write runs on a real worker thread, not just another coroutine
# turn, so a turn-counting loop (tried here and reverted) cannot substitute
# for wall time: a burst of zero-cost event-loop iterations can complete
# before the OS ever schedules that thread, which is worse under load than
# the timeout it replaced. This only ever matters for a scripted test
# validator, never for a real suite (minutes), so a wide margin costs
# nothing in production. Boxed in on both sides by two tests, so it cannot
# grow past either without them changing too: below, test_a_cycle_during_
# validation_returns_at_once asserts a 3-second suite still returns in under
# a second, and above, test_a_validation_that_outruns_its_budget's
# validate_seconds=1.0 (scaled up with this constant) proves a slow suite is
# "blocked", not "failed" -- either one racing past this window would retire
# the whole pipeline stage in the same cycle that started it, breaking the
# one-stage-per-cycle promise (rule 7) both tests exist to check.
INSTANT_VALIDATION = 0.5

RATIONALE_MARKER = "RATIONALE:"

# Where the draft/evaluate/decompose stages are confined to writing -- see
# ``_through_harness``'s ``write_prefix`` for what this enforces and why.
PLAN_WRITE_PREFIX = PLAN_DIR.as_posix()


def _extract_rationale(answer: str) -> str:
    """Pull the one sentence HARNESS_RULES asks the model to end with.

    A model that follows the instruction still wraps it in whatever else it
    wanted to say first -- tool narration, a restated task, both. Keeping the
    whole answer as the "rationale" made every generation's history read like
    a transcript instead of an explanation. This takes the line starting with
    the marker when there is one, and falls back to the full answer only when
    the model never wrote it, so a model that ignores the instruction is no
    worse off than before.
    """
    for line in answer.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith(RATIONALE_MARKER):
            return stripped[len(RATIONALE_MARKER) :].strip()
    return answer


def _source_paths(touched: list[str]) -> str:
    """The touched paths under src/evomesh/, comma-joined ('' for none)."""
    return ", ".join(path for path in touched if "src/evomesh/" in path.replace("\\", "/"))


def _with_recent_failure(
    evolver: EnvironmentEvolver, objective: str, needles: tuple[str, ...]
) -> str:
    """Append how the last attempt at this exact target failed, if any.

    The one idea worth taking from GEPA (github.com/NousResearch/
    hermes-agent-self-evolution) without taking GEPA itself: read execution
    history to understand *why* the last attempt failed, and say so, rather
    than handing the model a fresh, stateless retry that can only rediscover
    the same mistake. No new dependency, no LLM-as-judge, no eval harness --
    just the validation record a discarded generation already leaves on
    disk, read back the same way `recent_backlog_streak` already does.
    """
    failure = evolver.recent_target_failure(needles)
    if failure is None:
        return objective
    return (
        f"{objective}\n\n"
        "The last attempt at this exact target failed. Read this before "
        f"trying again, so you do not repeat it:\n{failure}"
    )


class ArchitectBehavior(ReflectiveBehavior):
    """Reactive only. The Architect must not invent agents nobody asked for."""

    name = "architect"


class GuardianBehavior(BDIBehavior):
    """Perceives mesh health and wants it restored. Never needs the model."""

    name = "guardian"

    async def perceive(self, context: CycleContext) -> list[Belief]:
        states = cast("dict[str, Any]", context.service("runtime_states") or {})
        health = cast("tuple[bool, str]", context.service("provider_health") or (False, "unknown"))
        stalled = sorted(
            str(getattr(state, "name", agent_id))
            for agent_id, state in states.items()
            if getattr(state, "phase", None) in {AgentPhase.ERROR, AgentPhase.OFFLINE}
            and agent_id != context.definition.id
        )
        return [
            Belief(
                key=PROVIDER_KEY,
                statement=(
                    "the model provider is ready"
                    if health[0]
                    else f"the model provider is not ready: {health[1]}"
                ),
                source="environment",
            ),
            Belief(
                key=DEGRADED_KEY,
                statement=(
                    f"agents not running: {', '.join(stalled)}"
                    if stalled
                    else f"all {len(states)} agents are running"
                ),
                source="environment",
            ),
        ]

    def rule_engine(self) -> RuleEngine:
        """Want something new only when the world actually changed: the
        degradation belief was just revised, and says agents are down."""
        return RuleEngine(
            (
                Rule(
                    name="investigate-degradation",
                    when=(
                        RuleTest(RuleSource.EVENT, BELIEF_CHANGED_EVENT, value=DEGRADED_KEY),
                        RuleTest(
                            RuleSource.BELIEF,
                            DEGRADED_KEY,
                            RuleOperator.STARTS_WITH,
                            "agents not running",
                        ),
                    ),
                    then=(
                        RuleEffect(
                            RuleEffectKind.PROPOSE_GOAL,
                            "investigate",
                            f"{INVESTIGATE}{{belief:{DEGRADED_KEY}}}",
                            {"priority": 2},
                        ),
                    ),
                ),
            )
        )

    def library(self) -> PlanLibrary:
        return PlanLibrary(
            (
                PlanRecipe(
                    name=DIAGNOSE_STALL,
                    steps=("diagnose the stalled agent from its runtime state",),
                    goal_kind=DELEGATED_GOAL_KIND,
                    matches=lambda goal, mind: goal.parameters.get("work_type") == "assistance",
                ),
                PlanRecipe(
                    name="investigate-degradation",
                    steps=(
                        "identify which agents stopped",
                        "report the degradation and check whether it cleared",
                    ),
                    matches=lambda goal, mind: goal.description.startswith(INVESTIGATE),
                    context_keys=(DEGRADED_KEY,),
                ),
                PlanRecipe(
                    name="health-sweep",
                    steps=("sweep the mesh and report anything degraded",),
                    matches=lambda goal, mind: goal.recurring,
                    context_keys=(PROVIDER_KEY, DEGRADED_KEY),
                ),
            )
        )

    async def execute(
        self, context: CycleContext, intention: Intention, step: PlanStep
    ) -> StepResult:
        mind = context.definition.mind
        findings = [
            item.statement
            for key in (PROVIDER_KEY, DEGRADED_KEY)
            if (item := mind.belief(key)) is not None
            and not item.statement.startswith(HEALTHY_PREFIXES)
        ]
        if intention.plan == DIAGNOSE_STALL:
            finding = self._diagnose(context, intention)
            return StepResult(summary=finding, fact=finding, achieved=True)
        if intention.plan == "investigate-degradation":
            # The desire that produced this goal is discharged the moment the
            # mesh recovers, so a transient boot wobble cannot leave the
            # Guardian permanently investigating something that is now fine.
            if not findings:
                return StepResult(
                    summary="the degradation cleared; nothing left to investigate",
                    achieved=True,
                )
            return StepResult(summary="; ".join(findings), fact=findings[0])
        if intention.plan != "health-sweep":
            return await super().execute(context, intention, step)
        summary = "; ".join(findings) if findings else "mesh healthy, provider ready"
        return StepResult(summary=summary, fact=findings[0] if findings else "")

    @staticmethod
    def _diagnose(context: CycleContext, intention: Intention) -> str:
        """Answer a delegated stall diagnosis from runtime state alone."""
        goal = context.definition.mind.goal(intention.goal_id)
        inputs = goal.parameters.get("inputs") or {}
        stalled_id = str(inputs.get("stalled_agent_id") or "")
        reason = str((inputs.get("event") or {}).get("reason") or "no progress")
        states = cast("dict[str, Any]", context.service("runtime_states") or {})
        state = states.get(stalled_id)
        if state is None:
            text = f"agent {stalled_id or '?'} is not running; its stall ({reason}) is moot"
        else:
            parts = [f"{state.name} is {state.phase}", f"stall: {reason}"]
            if state.last_error:
                parts.append(f"last error: {state.last_error}")
            if state.last_outcome:
                parts.append(f"last outcome: {state.last_outcome}")
            if (
                not context.service("provider_health")
                or not cast("tuple[bool, str]", context.service("provider_health"))[0]
            ):
                parts.append("the model provider is not ready")
            text = "; ".join(parts)
        return text


class EvaluatorBehavior(BDIBehavior):
    """Perceives the newest candidate's verdict and reports it. No model call."""

    name = "evaluator"

    async def perceive(self, context: CycleContext) -> list[Belief]:
        evolver = cast("EnvironmentEvolver | None", context.service("evolver"))
        if evolver is None:
            return []
        latest = evolver.latest_candidate()
        if latest is None:
            return [
                Belief(
                    key=CANDIDATE_KEY,
                    statement="no candidate generation exists",
                    source="evolution",
                )
            ]
        result = evolver.read_validation(latest)
        if result is None:
            verdict = f"generation {latest.number} has not been validated yet"
        else:
            failing = [
                str(entry.get("command"))
                for entry in result.commands
                if entry.get("exit_code") not in {0, None}
            ]
            detail = f" Failing: {', '.join(failing)}." if failing else ""
            verdict = (
                f"generation {latest.number} validation "
                f"{'passed' if result.passed else 'failed'}.{detail}"
            )
        return [
            Belief(
                key=CANDIDATE_KEY,
                statement=f"generation {latest.number} is the newest candidate",
                source="evolution",
            ),
            Belief(key=VERDICT_KEY, statement=verdict, source="evolution"),
        ]

    async def status(self, context: CycleContext) -> str:
        # Reuse perception rather than re-deriving it: this is the same reading,
        # taken now instead of at the last cycle.
        return "\n".join(f"{item.key}: {item.statement}" for item in await self.perceive(context))

    def library(self) -> PlanLibrary:
        return PlanLibrary(
            (
                PlanRecipe(
                    name="report-verdict",
                    steps=("report the newest candidate's validation verdict",),
                    matches=lambda goal, mind: goal.recurring,
                    context_keys=(CANDIDATE_KEY, VERDICT_KEY),
                ),
            )
        )

    async def execute(
        self, context: CycleContext, intention: Intention, step: PlanStep
    ) -> StepResult:
        if intention.plan != "report-verdict":
            return await super().execute(context, intention, step)
        verdict = context.definition.mind.belief(VERDICT_KEY)
        if verdict is None:
            return StepResult(summary="no candidate generation to evaluate")
        return StepResult(summary=verdict.statement, fact=verdict.statement)


def fallback_candidate(objective: str, pick: str, goal: Goal | None) -> Candidate:
    """An objective the pipeline chose outside the ranked backlog, as
    evidence: a human's own goal, a scout for more work, or maintenance."""
    headline = objective.strip().splitlines()[0][:160] if objective.strip() else "objective"
    if goal is not None and not goal.recurring:
        kind, factors = EVIDENCE_HUMAN_REQUEST, PriorityFactors(strategic_value=2.0)
    elif pick == PICK_SCOUT:
        kind, factors = EVIDENCE_BACKLOG_EXHAUSTED, PriorityFactors(confidence=0.5)
    else:
        kind, factors = EVIDENCE_CODEBASE_ANALYSIS, PriorityFactors(confidence=0.5)
    return Candidate(
        ref=f"{kind}:{headline.lower()}",
        kind=kind,
        title=headline,
        problem=headline,
        component="evomesh",
        evidence={"pick": pick or "objective"},
        factors=factors,
    )


class EvolverBehavior(BDIBehavior):
    """The mutation pipeline as a committed plan, one stage per cycle."""

    name = "evolver"

    def __init__(
        self,
        auto_validate: bool = True,
        max_repairs: int = 2,
        auto_promote: bool = False,
        auto_restart: bool = True,
        validate_seconds: float = 1800.0,
        auto_plan: bool = False,
        plan_max_steps: int | None = None,
        plan_max_seconds: float | None = None,
        review: bool = False,
        review_max_steps: int | None = None,
        review_max_seconds: float | None = None,
        baseline_tests: bool = False,
        test_backlog: bool = True,
        scout_when_idle: bool = True,
    ) -> None:
        super().__init__()
        self.auto_validate = auto_validate
        # Zero turns self-repair off and restores the old behaviour: one shot at
        # validation, then a verdict.
        self.max_repairs = max(0, max_repairs)
        # Decide the candidate's fate from the verdict instead of parking on a
        # human. Only ever acts on a verdict validation actually produced.
        self.auto_promote = auto_promote
        # Only used to word the summary honestly: the restart itself is the
        # Environment's to ask for and the launcher's to perform.
        self.auto_restart = auto_restart
        # A validation that never ends is a stage that never ends. Past this the
        # run is stopped and reported as blocked -- the candidate got no verdict,
        # and a suite this machine could not finish is not its fault.
        self.validate_seconds = validate_seconds
        # Off by default: draft/evaluate/decompose a plan before authoring
        # anything, instead of asking the harness for one mutation directly.
        # No revision or depth limit -- the model alone decides when a draft
        # is approved and when an item is minimal (see docs/evolution/*.md
        # generation history for why the flat rationale alone was not enough).
        self.auto_plan = auto_plan
        # Passed through from harness.plan_max_steps/plan_max_seconds; kept as
        # attributes rather than read from settings again here, since a
        # behavior has no config object of its own to reach.
        self.plan_max_steps = plan_max_steps
        self.plan_max_seconds = plan_max_seconds
        # Off by default: after validation passes, a read-only harness job
        # reads the diff against the objective, and an INCOMPLETE verdict is
        # repaired like a failing command (same max_repairs budget) or, once
        # that is spent, discarded. Slower per generation, on purpose.
        self.review = review and auto_validate
        self.review_max_steps = review_max_steps
        self.review_max_seconds = review_max_seconds
        # Run the whole suite on the live tree before picking an objective; a
        # red suite becomes the objective. Off here (a behavior under test has
        # no real tree), on in settings.
        self.baseline_tests = baseline_tests
        # The untested-export fallback: "write ONE small test". With it off,
        # an evolver with nothing substantive to do waits instead. On here for
        # the existing pipeline tests, off in settings.
        self.test_backlog = test_backlog
        # Whether nothing eligible means "go find something" (a scout, the
        # dead-module backlog) or IDLE. On here for the existing pipeline
        # tests, off in settings (closure plan 18.1).
        self.scout_when_idle = scout_when_idle
        # The tree key a human was last told about, so a stall is announced
        # once, not every cycle it lasts.
        self._announced: str = ""
        # The improvement backlog's control plane, when the environment has
        # one (read from the cycle's services before each stage).
        self._improvements: ImprovementControl | None = None

    def _stages(self) -> tuple[str, ...]:
        if not self.auto_validate:
            return SKIP_VALIDATION_STAGES
        stages = EVOLUTION_STAGES_WITH_PLAN if self.auto_plan else EVOLUTION_STAGES
        if self.review:
            at = stages.index(STAGE_REPAIR)
            stages = (*stages[:at], STAGE_REVIEW, *stages[at:])
        if not self.max_repairs:
            return tuple(stage for stage in stages if stage != STAGE_REPAIR)
        return stages

    def _steps(self) -> tuple[str, ...]:
        if not self.auto_validate:
            steps = SKIP_VALIDATION_STEPS
        else:
            steps = EVOLUTION_STEPS_WITH_PLAN if self.auto_plan else EVOLUTION_STEPS
            if self.review:
                at = next(i for i, step in enumerate(steps) if step.startswith("repair"))
                steps = (*steps[:at], REVIEW_STEP, *steps[at:])
            if not self.max_repairs:
                steps = tuple(step for step in steps if not step.startswith("repair"))
        if self.auto_promote:
            return (*steps[:-1], AUTO_PROMOTE_STEP)
        return steps

    async def perceive(self, context: CycleContext) -> list[Belief]:
        evolver = cast("EnvironmentEvolver | None", context.service("evolver"))
        if evolver is None:
            return []
        state = await evolver.pipeline_state()
        stage = str(state.get("stage", STAGE_PLAN))
        return [
            Belief(key=STAGE_KEY, statement=stage, source="evolution"),
            # Unlike the stage, this flips only twice per pass: when the
            # candidate is handed over, and when a human releases it. That makes
            # it the one belief worth reconsidering a committed plan over.
            Belief(
                key=AWAITING_KEY,
                statement="yes" if stage == STAGE_AWAIT_HUMAN else "no",
                source="evolution",
            ),
        ]

    async def status(self, context: CycleContext) -> str:
        evolver = cast("EnvironmentEvolver | None", context.service("evolver"))
        if evolver is None:
            return "evolution: no candidate workspace is attached, so nothing can be built"
        state = await evolver.pipeline_state()
        if not state:
            return (
                "evolution: no candidate generation is open yet; the next cycle "
                "opens one and starts proposing a change"
            )
        stage = str(state.get("stage", STAGE_PLAN))
        lines = [
            f"evolution stage: {stage} ({self._stage_meaning(stage)})",
            f"candidate generation: {state.get('generation', 'none')}",
            f"objective: {state.get('objective') or 'none'}",
        ]
        if changed := state.get("file"):
            lines.append(f"file changed in this candidate: {changed}")
        if path := state.get("path"):
            lines.append(f"candidate workspace: {path}")
        lines.append(f"validation: {self._verdict(state)}")
        if error := state.get("error"):
            lines.append(f"last pipeline error: {error}")
        return "\n".join(lines)

    def _stage_meaning(self, stage: str) -> str:
        report = (
            "promoting or discarding the candidate on its verdict"
            if self.auto_promote
            else "writing up the verdict for a human"
        )
        return {
            STAGE_PLAN: "about to copy the mesh into a fresh candidate generation",
            STAGE_DRAFT: "drafting a plan for the objective before writing any code",
            STAGE_EVALUATE: "having the plan reviewed before it is split into work items",
            STAGE_DECOMPOSE: "splitting the approved plan into minimal work items",
            STAGE_PROPOSE: "asking the model for one small, safe file change",
            STAGE_VALIDATE: "running sync, ruff, pyright, pytest and the smoke test",
            STAGE_REPAIR: "fixing what validation reported, with the linter or the model",
            STAGE_REPORT: report,
            STAGE_AWAIT_HUMAN: "waiting for a human to promote or discard it",
        }.get(stage, "unknown stage")

    def library(self) -> PlanLibrary:
        return PlanLibrary(
            (
                PlanRecipe(
                    name="evolve-generation",
                    steps=self._steps(),
                    # Not the stage: that belief moves because this plan advanced
                    # it, and a plan that reconsiders every time it makes progress
                    # has no commitment at all -- it would abandon and re-adopt
                    # itself once per cycle. Only the human handover matters.
                    context_keys=(AWAITING_KEY,),
                ),
            )
        )

    async def execute(
        self, context: CycleContext, intention: Intention, step: PlanStep
    ) -> StepResult:
        evolver = cast("EnvironmentEvolver | None", context.service("evolver"))
        if evolver is None:
            return StepResult.blocked("no evolution workspace is attached")
        restarting = getattr(context.service("environment"), "restart_requested", None)
        if isinstance(restarting, asyncio.Event) and restarting.is_set():
            # Found live 2026-09-24, once stages chain without waiting out a
            # cycle: a promotion asks for a restart, and the very next cycle
            # opened generation 1393 while the process was shutting down --
            # a worktree and an open candidate nothing would ever finish.
            # Nothing new starts once this process is on its way out.
            return StepResult(
                summary="a restart is under way; nothing new starts before it",
                hold=True,
                phase=AgentPhase.ACTING,
            )
        state = await evolver.pipeline_state()
        stage = str(state.get("stage", STAGE_PLAN))
        if stage == STAGE_AWAIT_HUMAN:
            # Under auto_promote this stage is only ever entered two ways: no
            # verdict to act on (state["error"] absent, state["passed"] is
            # None), which genuinely needs a human, or _decide's tree-was-dirty
            # GitError (state["error"] present), which is the environment's
            # fault, not the candidate's. The second kind is worth retrying on
            # its own -- a human fixing their working tree should not also have
            # to remember to run /evolution promote.
            if self.auto_promote and state.get("error") and state.get("passed") is not None:
                number = int(state["generation"])
                return await self._decide(
                    evolver, number, passed=bool(state["passed"]), state=state
                )
            holding = StepResult.waiting(
                f"generation {state.get('generation')} is waiting for a human to "
                f"promote or discard it ({self._verdict(state)})"
            )
            logger.info("Evolution is holding: %s", holding.summary)
            return holding
        # The persisted stage is the single source of truth, so the plan cursor
        # is pinned to it and the checklist can never disagree with reality.
        stages = self._stages()
        if stage in stages:
            intention.cursor = stages.index(stage)
        control = context.service("improvements")
        self._improvements = control if isinstance(control, ImprovementControl) else None
        goal = context.goal
        objective = str(state.get("objective") or (goal.description if goal else ""))
        try:
            result = await self._run_stage(context, evolver, state, stage, objective)
        except (RuntimeError, ValueError, OSError) as exc:
            await evolver.set_pipeline_state({**state, "stage": STAGE_PLAN, "error": str(exc)})
            logger.warning("Evolution stage %s failed, back to plan: %s", stage, exc)
            return StepResult(summary=f"stage '{stage}' failed: {exc}", failed=True)
        # One line per stage the pipeline runs. Until this existed the pipeline
        # was silent: the stage lived only in the database, nothing was written
        # down when it moved, and a mesh that produced no generation for nine
        # hours looked exactly like one that was busy. A stage that does not
        # move is the thing worth seeing, so it is logged too.
        moved = str((await evolver.pipeline_state()).get("stage", STAGE_PLAN))
        logger.info("Evolution stage %s -> %s: %s", stage, moved, result.summary)
        if moved == STAGE_AWAIT_HUMAN and stage != STAGE_AWAIT_HUMAN:
            # Only on the transition into the stage, never on the cycles that
            # follow while parked there (those return early via the holding
            # branch above `_run_stage`, so this never repeats) -- a human
            # should hear about this once, not every couple of minutes for as
            # long as they are away from the console.
            environment = cast("Any", context.service("environment"))
            if environment is not None:
                await environment.announce(f"Evolution needs you: {result.summary}")
        elif moved != stage and not result.impossible:
            # The next stage starts from what this one just finished, and
            # waits on nothing yet: run it now. A stage still waiting on its
            # lane does not move, and is woken by the lane (AgentRuntime.wake).
            result.again = True
        return result

    async def _run_stage(
        self,
        context: CycleContext,
        evolver: EnvironmentEvolver,
        state: dict[str, Any],
        stage: str,
        objective: str,
    ) -> StepResult:
        if stage == STAGE_PLAN:
            return await self._open(context, evolver, objective)
        number = state.get("generation")
        if isinstance(number, int) and evolver.workspace.supervisor.outcome(number) is not None:
            # Decided outside this pipeline (a human's /evolution discard, a
            # stop mid-stage): every later stage would ask for a candidate
            # that no longer exists and fail the same way each cycle.
            decided = evolver.workspace.supervisor.outcome(number)
            await evolver.set_pipeline_state({"stage": STAGE_PLAN})
            return StepResult(
                summary=f"generation {number} was already {decided} outside the pipeline; "
                "the pipeline is free for the next objective",
                phase=AgentPhase.ACTING,
            )
        if stage == STAGE_DRAFT:
            return await self._draft_plan(context, evolver, state)
        if stage == STAGE_EVALUATE:
            return await self._evaluate_plan(context, evolver, state)
        if stage == STAGE_DECOMPOSE:
            return await self._decompose(context, evolver, state)
        if stage == STAGE_PROPOSE:
            return await self._propose(context, evolver, state)
        if stage == STAGE_VALIDATE:
            return await self._validate(evolver, state)
        if stage == STAGE_REPAIR:
            return await self._repair(context, evolver, state)
        if stage == STAGE_REVIEW:
            return await self._review(context, evolver, state)
        if stage == STAGE_REPORT:
            return await self._report(evolver, state)
        return StepResult.blocked(f"unknown evolution stage '{stage}'")

    async def _open(
        self, context: CycleContext, evolver: EnvironmentEvolver, objective: str
    ) -> StepResult:
        goal = context.goal
        substantive: dict[str, Any] = {}
        tracked: TrackedImprovement | None = None
        if goal is not None and goal.recurring:
            # The standing goal names no file and no change -- concrete beats
            # vague for a small model with a step budget, and the dead-module
            # list is already documented (codebase.py) as "the Evolver's
            # backlog". A human's own `/evolution start "<objective>"` is a
            # one-shot goal (recurring=False), so it is never overridden here.
            #
            # Seeded from total_created(), not len(candidates()) -- the
            # latter only counts still-open candidates and resets to ~0 on
            # every discard, so `seed % len(backlog)` stopped rotating at
            # all the moment a discard became the common case. Found live:
            # cycles.py, the sole entry left once the rest of the original
            # ten-module backlog had already been wired in, handed to five
            # generations straight.
            seed = evolver.workspace.supervisor.total_created()
            baseline_pick: ObjectivePick | None = None
            baseline_result: BaselineResult | None = None
            if self.baseline_tests:
                baseline = baseline_result = await evolver.baseline(self.validate_seconds)
                if baseline is None:
                    return StepResult(
                        summary=(
                            "running the whole test suite on the live tree before "
                            "choosing what to evolve"
                        ),
                        phase=AgentPhase.ACTING,
                        hold=True,
                    )
                if baseline.blocked and baseline.key:
                    logger.warning(
                        "Baseline test run gave no verdict, evolving anyway: %s",
                        baseline.output[-500:],
                    )
                elif not baseline.passed:
                    baseline_pick = evolver.baseline_pick(baseline)
                    if baseline_pick is None:
                        return await self._stall(
                            context,
                            baseline.key,
                            f"{len(baseline.failures)} test(s) fail on the live tree "
                            f"and {MAX_TARGET_ATTEMPTS} generations could not fix them "
                            "-- evolution is paused until a human does: "
                            + ", ".join(baseline.failures[:5]),
                        )
            # Substantive work first -- a traceback the mesh actually logged,
            # then docs/evolution/improvements.md. Both backlogs below are
            # maintenance: with only them, the best any generation could do
            # was add one test, and ~30 straight did exactly that
            # (2026-09-24) while the system itself never changed.
            scout_cap = MAX_SCOUT_ATTEMPTS if self.test_backlog else None
            if self._improvements is not None:
                # Ranked by the backlog's explicit priority, not rotated by
                # seed; discovery (a scout) only when nothing evidenced is left.
                pick, tracked = await self._prioritized(
                    self._improvements, evolver, baseline_result, baseline_pick
                )
                if pick is None and self.scout_when_idle:
                    pick = evolver.scout_pick(seed, scout_cap)
            else:
                pick = baseline_pick or evolver.substantive_objective(
                    seed, scout_cap=scout_cap, scout=self.scout_when_idle
                )
            # Dead-module maintenance is a suspicion, not evidence: it is
            # only ever looked for when a human asked for idle exploration.
            target = (
                evolver.backlog_target(seed) if pick is None and self.scout_when_idle else None
            )
            nudge_delete = target is not None and evolver.recent_backlog_streak(target.name) >= 3
            backlog = (
                evolver.backlog_objective(seed, nudge_delete=nudge_delete)
                if pick is None and self.scout_when_idle
                else None
            )
            if pick is not None:
                objective = _with_recent_failure(evolver, pick.objective, (pick.needle,))
                substantive = {"pick": pick.kind, "pick_key": pick.key}
                if pick.step:
                    substantive["pick_step"] = pick.step
                if pick.work:
                    substantive["work"] = dict(pick.work)
            elif backlog is not None:
                objective = backlog
                if target is not None:
                    objective = _with_recent_failure(
                        evolver,
                        objective,
                        (
                            f"Wire src/evomesh/{target.name}.py",
                            f"Delete src/evomesh/{target.name}.py",
                        ),
                    )
            else:
                # Found live 2026-09-23: after ~1200 generations the dead-module
                # backlog above ran dry (0 orphans left, backlog_objective always
                # None), and every generation since fell through all the way to
                # this goal's own bare text -- no file, no anchor -- so the model
                # free-explored instead of converging (scratch files, junk test
                # markers, never a real edit). The untested-export backlog is the
                # next concrete source once the first one is empty, not a
                # replacement for it -- checked second, same rotation scheme.
                untested = evolver.untested_objective(seed) if self.test_backlog else None
                if untested is None and not self.test_backlog:
                    return await self._stall(
                        context,
                        f"idle:{seed}",
                        "nothing substantive to evolve: no failing tests, no open "
                        "item in docs/evolution/improvements.md, and no scout "
                        "target left -- add an item there to steer the mesh",
                    )
                if untested is not None:
                    objective = untested
                    substantive = {"pick": PICK_TEST}
                    pair = evolver.untested_target(seed)
                    if pair is not None:
                        module, name = pair
                        substantive["work"] = {
                            "path": f"src/evomesh/{module.name}.py",
                            "symbol": name.removesuffix("()"),
                            "tests": f"tests/test_{module.name}.py",
                        }
                        needle = (
                            f"Write ONE small, mechanical test for `{name}` in "
                            f"`src/evomesh/{module.name}.py`."
                        )
                        objective = _with_recent_failure(evolver, objective, (needle,))
        if self._improvements is not None and tracked is None:
            # Everything the pipeline opens is an improvement with evidence,
            # including its own fallbacks and a human's objective (B-009).
            tracked = await self._improvements.adopt(
                fallback_candidate(objective, str(substantive.get("pick") or ""), goal)
            )
            if tracked is None:
                return await self._stall(
                    context,
                    f"exhausted:{objective[:80]}",
                    "this objective used up its attempts and waits for a human: "
                    "/improvements release <id> to try it again",
                )
        # At this stage no candidate is in flight, by definition. One still open
        # that was never worked on -- no change, no verdict -- is an orphan of a
        # restart that landed mid-open, and prune_stale() protects open
        # candidates forever. One with work in it is left alone: a human who
        # reset the pipeline may still want to promote it by hand.
        supervisor = evolver.workspace.supervisor
        for orphan in supervisor.candidates():
            if (
                orphan.status is GenerationStatus.CANDIDATE
                and not orphan.changes
                and evolver.read_validation(orphan) is None
            ):
                logger.info(
                    "generation %s was left open with nothing driving it; discarding it",
                    orphan.number,
                )
                supervisor.discard(orphan.number)
        generation = await evolver.create_candidate(objective)
        # create_candidate()/prune_stale() may just have deleted old
        # generation directories, and a harness job's filesystem grant
        # (environment.py's submit_harness_job) is scoped to exactly one of
        # those -- without this, the grant outlives the directory it was
        # for, forever (found live: 10193 filesystem_grants rows in
        # state.db, one for nearly every harness job this mesh has ever
        # run). Best-effort: a sweep failing here is nothing to block a new
        # generation over.
        permissions = cast("Any", context.service("permissions"))
        if permissions is not None:
            with suppress(Exception):
                await permissions.prune_missing_paths()
        if tracked is not None and self._improvements is not None:
            work = await self._improvements.begin(
                tracked,
                objective=objective,
                route=self._route(context),
                executor=GenerationExecutor(evolver.workspace.supervisor),
                workspace=str(generation.path),
                reference=str(generation.number),
                stage=f"step:{substantive['pick_step']}"
                if substantive.get("pick_step")
                else None,
            )
            if work is not None:
                substantive["improvement_id"] = tracked.id
                substantive["work_item_id"] = work.id
                board = context.service("blackboard")
                if isinstance(board, Blackboard):
                    board.publish_work(work)
        next_stage = STAGE_DRAFT if self.auto_plan else STAGE_PROPOSE
        await evolver.set_pipeline_state(
            {
                "stage": next_stage,
                "generation": generation.number,
                "objective": objective,
                "path": str(generation.path),
                **substantive,
            }
        )
        return StepResult(
            summary=f"opened candidate generation {generation.number} at {generation.path}",
            fact=f"generation {generation.number} opened for: {objective}",
            phase=AgentPhase.ACTING,
        )

    @staticmethod
    async def _prioritized(
        control: ImprovementControl,
        evolver: EnvironmentEvolver,
        baseline: BaselineResult | None,
        baseline_pick: ObjectivePick | None,
    ) -> tuple[ObjectivePick | None, TrackedImprovement | None]:
        """Settle the last generation's work item, fold what the evolver can
        see now into the backlog, and take the best-scoring improvement."""
        pairs = evolver.substantive_candidates()
        if baseline_pick is not None and baseline is not None:
            pairs.insert(0, (baseline_pick, baseline_candidate(baseline)))
        present = evolver.evidence_refs(baseline)
        await control.settle(GenerationExecutor(evolver.workspace.supervisor), present)
        await control.sync(
            [candidate for _, candidate in pairs], present, evolver.observations(baseline)
        )
        by_ref = {candidate.ref: pick for pick, candidate in pairs}
        while (chosen := control.choose()) is not None:
            if chosen.source_ref in by_ref:
                return by_ref[chosen.source_ref], chosen
            if chosen.source in RECURRENCE_SOURCES:
                return evolver.improvement_pick(chosen), chosen
            # Still evidenced, but set aside for now (attempted too often
            # recently): not this generation's work.
            chosen.status = ImprovementStatus.BLOCKED
            chosen.rejection_reason = NOT_PICKABLE_NOW
        await control.save()
        return None, None

    @staticmethod
    def _route(context: CycleContext) -> Callable[[WorkItem], str | None]:
        """Award a code work item by capability. Only the agent running
        the candidate pipeline can carry one out, so it is awarded to this
        agent when it is among the capable bidders, and to nobody otherwise."""
        contract_net = context.service("contract_net")
        environment = cast("Any", context.service("environment"))
        board = context.service("blackboard")

        def route(work: WorkItem) -> str | None:
            if not isinstance(contract_net, ContractNet):
                return context.definition.id
            bids = contract_net.bids(
                work,
                states=environment.runtime_states() if environment is not None else {},
                active_work=list(board.work_items.values())
                if isinstance(board, Blackboard)
                else [],
                history=board.work_history() if isinstance(board, Blackboard) else None,
            )
            return (
                context.definition.id
                if any(bid.agent_id == context.definition.id for bid in bids)
                else None
            )

        return route

    async def _stall(self, context: CycleContext, key: str, reason: str) -> StepResult:
        """Wait instead of opening a generation, telling a human once per ``key``."""
        if key != self._announced:
            self._announced = key
            logger.warning("Evolution is waiting: %s", reason)
            environment = cast("Any", context.service("environment"))
            if environment is not None:
                await environment.announce(f"Evolution is waiting: {reason}")
        return StepResult.waiting(reason)

    async def _draft_plan(
        self, context: CycleContext, evolver: EnvironmentEvolver, state: dict[str, Any]
    ) -> StepResult:
        generation = evolver.candidate(int(state["generation"]))
        objective = str(state["objective"])
        revision = int(state.get("plan_revision", 0))
        label = f"draft a plan (revision {revision + 1})" if revision else "draft a plan"
        return await self._through_harness(
            context,
            evolver,
            state,
            generation,
            build=lambda: evolver.draft_plan_objective_text(objective),
            label=label,
            status="planned",
            record=evolver.record_plan_draft,
            on_done=lambda touched: (STAGE_EVALUATE, {}),
            write_prefix=PLAN_WRITE_PREFIX,
            max_steps=self.plan_max_steps,
            max_seconds=self.plan_max_seconds,
        )

    # Found live: a harness job can cap out having written nothing for reasons
    # that have nothing to do with the plan under review -- the local model
    # repeating one `ls` call three times in a row despite the harness's own
    # correction, the first time this stage ever ran after a fix that got the
    # draft stage landing again. Discarding the whole generation over that
    # throws away a plan that was never actually reviewed, so this stage gets
    # a few free retries (a fresh harness job each time) before falling
    # through to the D5 discard every other stage uses unconditionally.
    EVAL_MAX_NO_OP_RETRIES = 2

    async def _evaluate_plan(
        self, context: CycleContext, evolver: EnvironmentEvolver, state: dict[str, Any]
    ) -> StepResult:
        generation = evolver.candidate(int(state["generation"]))
        root = evolver.current_plan_root(generation)
        plan_text = root.reasoning if root is not None else ""

        fabricated = evolver.fabricated_plan_references(plan_text) if plan_text else []
        if fabricated:
            # Caught mechanically, before a harness job (and a whole model
            # turn) is spent reaching the same verdict the evaluator would
            # have given anyway -- every rejection tonight named exactly this.
            await evolver.mechanical_reject_plan(generation, fabricated)
            revision = int(state.get("plan_revision", 0)) + 1
            await evolver.set_pipeline_state(
                {**state, "stage": STAGE_DRAFT, "plan_revision": revision}
            )
            return StepResult(
                summary=(
                    f"generation {generation.number}'s plan names "
                    f"{', '.join(fabricated)}, which do not exist -- rejected "
                    "without spending a harness job"
                ),
                phase=AgentPhase.ACTING,
            )

        def on_done(touched: list[str]) -> tuple[str, dict[str, Any]]:
            # Read `root` itself, not another `current_plan_root` lookup:
            # `record_plan_eval` marks a rejected root superseded the moment
            # it is rejected (so a human reading the plan mid-redraft never
            # sees a plan that was already turned down), which means the
            # lookup would no longer find it at all by the time this runs.
            if root is not None and root.approved is False:
                revision = int(state.get("plan_revision", 0)) + 1
                return (STAGE_DRAFT, {"plan_revision": revision})
            queue = [root.id] if root is not None else []
            return (STAGE_DECOMPOSE, {"plan_queue": queue})

        async def on_no_op() -> tuple[str, dict[str, Any]] | None:
            retries = int(state.get("eval_retries", 0))
            if retries >= self.EVAL_MAX_NO_OP_RETRIES:
                return None
            return (STAGE_EVALUATE, {"eval_retries": retries + 1})

        return await self._through_harness(
            context,
            evolver,
            state,
            generation,
            build=lambda: evolver.evaluate_plan_objective_text(plan_text),
            label="evaluate the plan",
            status="evaluated",
            record=evolver.record_plan_eval,
            on_done=on_done,
            on_no_op=on_no_op,
            write_prefix=PLAN_WRITE_PREFIX,
            max_steps=self.plan_max_steps,
            max_seconds=self.plan_max_seconds,
        )

    async def _decompose(
        self, context: CycleContext, evolver: EnvironmentEvolver, state: dict[str, Any]
    ) -> StepResult:
        generation = evolver.candidate(int(state["generation"]))
        queue = list(state.get("plan_queue", []))
        if not queue:
            root = evolver.current_plan_root(generation)
            queue = [root.id] if root is not None else []
        if not queue:
            # Draft/evaluate produced no usable plan at all -- fall back to
            # the flat path rather than getting stuck with nothing to split.
            await evolver.set_pipeline_state({**state, "stage": STAGE_PROPOSE, "work_items": []})
            return StepResult(
                summary=(
                    f"generation {generation.number} has no plan to decompose; "
                    "proposing the standing objective directly"
                ),
                phase=AgentPhase.ACTING,
            )
        node_id = queue[0]
        found = evolver.plan_node(generation, node_id)
        if found is None:
            await evolver.set_pipeline_state(
                {**state, "stage": STAGE_DECOMPOSE, "plan_queue": queue[1:]}
            )
            return StepResult(
                summary=f"work item {node_id} vanished from the plan; skipping it",
                phase=AgentPhase.ACTING,
            )
        node: PlanNode = found

        def next_stage_after(remaining: list[str]) -> tuple[str, dict[str, Any]]:
            if remaining:
                return (STAGE_DECOMPOSE, {"plan_queue": remaining})
            leaves = [item.id for item in generation.plan if item.kind == "leaf"]
            return (STAGE_PROPOSE, {"plan_queue": [], "work_items": leaves})

        def on_done(touched: list[str]) -> tuple[str, dict[str, Any]]:
            current = evolver.plan_node(generation, node_id)
            remaining = queue[1:]
            if current is not None and current.kind == "split":
                children = [child.id for child in generation.plan if child.parent_id == node_id]
                remaining = children + remaining
            return next_stage_after(remaining)

        async def on_no_op() -> tuple[str, dict[str, Any]]:
            # Found live: a decompose job that answers without writing its
            # node file used to discard the whole generation (D5) -- losing
            # every sibling a long-running decompose had already split, over
            # one stuck node at the end of the queue. Marking it a leaf and
            # moving on keeps that work instead of throwing it away.
            await evolver.mark_plan_node_undecomposed(generation, node_id)
            return next_stage_after(queue[1:])

        return await self._through_harness(
            context,
            evolver,
            state,
            generation,
            build=lambda: evolver.decompose_plan_objective_text(node),
            label=f"decompose {node_id}: {node.title}",
            status="decomposed",
            record=evolver.record_plan_decompose,
            record_key=node_id,
            on_done=on_done,
            on_no_op=on_no_op,
            write_prefix=PLAN_WRITE_PREFIX,
            max_steps=self.plan_max_steps,
            max_seconds=self.plan_max_seconds,
        )

    async def _propose(
        self, context: CycleContext, evolver: EnvironmentEvolver, state: dict[str, Any]
    ) -> StepResult:
        """Submit a harness job, wait for it across cycles, then record it.

        Three possible cycles, one stage. The run itself happens in the worker,
        so a tick never becomes a ten-minute authoring session -- which is what
        lets rule 7 survive a model that reads twenty files before it edits one.

        With a plan tree behind this generation, this stage runs once per
        remaining work item in ``state["work_items"]`` instead of once for the
        whole generation: it authors the first item, pops it off the queue
        regardless of what validation later makes of it (repair already fixes
        whatever it broke without needing to know which item produced it), and
        ``_validate`` loops back here for the next item once the current one
        passes.
        """
        generation = evolver.candidate(int(state["generation"]))
        objective = str(state["objective"])
        work_items = list(state.get("work_items", []))
        item = evolver.plan_node(generation, work_items[0]) if work_items else None
        pick = state.get("pick")
        # A step, a plan or a scout: the job is built around the code it is
        # about, from the candidate's own files, instead of the package map,
        # the skills catalog and the long rules (see EnvironmentEvolver.work_order).
        work = cast("dict[str, Any]", state.get("work") or {})
        anchored = item is None and bool(work)

        def build() -> str:
            if item is not None:
                return evolver.leaf_objective(item)
            if anchored and (order := evolver.work_order(generation, objective, str(pick), work)):
                return order
            return evolver.mutation_objective(objective)

        label = item.title if item is not None else objective

        def accept(touched: list[str]) -> str | None:
            if pick == PICK_TEST and (bent := _source_paths(touched)):
                return f"it was asked for a test and changed the code under test ({bent})"
            if pick in SOURCE_PICKS and not any(
                "src/evomesh/" in path.replace("\\", "/") for path in touched
            ):
                return (
                    "it answered a substantive objective without touching "
                    f"src/evomesh/ (only {', '.join(touched)})"
                )
            step_path = str(work.get("path", ""))
            if (
                pick == PICK_IMPROVEMENT
                and step_path
                and not any(path.replace("\\", "/").endswith(step_path) for path in touched)
            ):
                # Found live: a job that edits the right text in the wrong
                # file. The step names one file; landing a change elsewhere
                # would tick a step that never happened.
                return f"its step changes {step_path}, and it changed only {', '.join(touched)}"
            if pick == PICK_PLAN:
                return evolver.vet_plan(generation, str(state.get("pick_key", "")))
            if pick == PICK_SCOUT:
                kept, dropped = evolver.vet_scouted_items(generation)
                for dropped_item, reason in dropped:
                    logger.info(
                        "generation %s: scouted item %r dropped: %s",
                        generation.number,
                        dropped_item.title,
                        reason,
                    )
                if not kept:
                    return "the scout added no backlog item with steps anchored in real code"
            return None

        async def on_no_op() -> tuple[str, dict[str, Any]] | None:
            # No plan tree behind this generation: the harness job was its
            # one piece of work, so nothing here changes -- fall through to
            # the default whole-generation discard.
            if not work_items:
                return None
            remaining = work_items[1:]
            if remaining:
                return (STAGE_PROPOSE, {"work_items": remaining})
            # The queue is empty. If an earlier item in this same generation
            # already validated, report that real, recorded work instead of
            # discarding it over the one item that failed to author.
            if state.get("passed") is True:
                return (STAGE_REPORT, {"work_items": []})
            return None

        return await self._through_harness(
            context,
            evolver,
            state,
            generation,
            build=build,
            label=label,
            status="applied",
            on_done=lambda touched: (
                STAGE_VALIDATE if self.auto_validate else STAGE_REPORT,
                {
                    "file": touched[0] if touched else "",
                    **({"work_items": work_items[1:]} if work_items else {}),
                },
            ),
            on_no_op=on_no_op,
            accept=accept,
            # A scout or a plan only reads: its answer is the backlog entry,
            # and the pipeline writes it (see apply_backlog_answer).
            write_prefix="docs/evolution" if pick in BACKLOG_PICKS else None,
            catalog=not anchored,
            allow_write=pick not in BACKLOG_PICKS,
            apply_answer=(
                (
                    lambda answer: evolver.apply_backlog_answer(
                        generation, str(pick), str(state.get("pick_key", "")), answer
                    )
                )
                if pick in BACKLOG_PICKS
                else None
            ),
            # Only a decomposed leaf gets the tight budget: it was already
            # split down to "one small change to one module that already
            # runs" (PLAN_DECOMPOSE_RULES), so it should not need more room
            # than draft/evaluate/decompose did to make that one edit. A
            # generation with no plan tree behind it is asking for whatever
            # `objective` describes, which may be exactly as open-ended as a
            # repair -- that path keeps the full harness.max_steps budget.
            max_steps=(
                self.plan_max_steps
                if item is not None
                else BACKLOG_MAX_STEPS
                if pick in BACKLOG_PICKS
                else None
            ),
            max_seconds=(
                self.plan_max_seconds
                if item is not None
                else BACKLOG_MAX_SECONDS
                if pick in BACKLOG_PICKS
                else None
            ),
        )

    async def _through_harness(
        self,
        context: CycleContext,
        evolver: EnvironmentEvolver,
        state: dict[str, Any],
        generation: Generation,
        *,
        build: Callable[[], str],
        label: str,
        status: str,
        on_done: Callable[[list[str]], tuple[str, dict[str, Any]]],
        record: Callable[..., Any] | None = None,
        record_key: str | None = None,
        on_no_op: Callable[[], Awaitable[tuple[str, dict[str, Any]] | None]] | None = None,
        write_prefix: str | None = None,
        max_steps: int | None = None,
        max_seconds: float | None = None,
        accept: Callable[[list[str]], str | None] | None = None,
        catalog: bool = True,
        allow_write: bool = True,
        apply_answer: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> StepResult:
        """Submit a harness job, resume it across cycles, then record it.

        ``record`` defaults to ``evolver.record_harness_changes``, the only
        recorder that existed before the plan tree did; the draft/evaluate/
        decompose stages pass their own (``record_plan_draft`` and siblings),
        which read a fixed, known file back off disk instead of trusting a
        diff to carry planning prose. ``record_key`` overrides what gets
        passed as that recorder's ``objective`` argument -- every recorder
        before this one used it for the generation's standing objective, but
        `_decompose` needs to say *which node* it just asked the harness to
        split, and the pipeline `state` dict that would otherwise carry it is
        not part of a recorder's signature.

        ``write_prefix`` narrows the harness's write/edit/delete tools to that
        one directory inside the candidate -- the plan draft/evaluate/decompose
        stages pass ``docs/evolution/plans`` since that is the whole of what
        each is asked to write, so a model that ignores the prose instruction
        not to touch a source file gets a named tool refusal instead of a
        stray file landing in the candidate (found live: an evaluate job wrote
        a throwaway script under ``src/`` despite being told not to).

        ``on_no_op`` overrides what happens when the harness wrote nothing.
        Left at its default (``None``), or returning ``None`` itself, a no-op
        discards the whole generation (D5, below) -- exactly right whenever
        the harness job *is* the generation's one piece of work: `_repair`,
        and `_propose` with no plan tree behind it or no validated progress
        yet to lose. Found live, twice: `_decompose` shares this same no-op
        path, but a no-op there is one stuck node at the end of a queue that
        may already hold a dozen siblings this generation successfully split;
        `_propose` shares it too, but with a plan tree, a no-op there is one
        unauthored work item that may follow one already validated earlier in
        the same generation. Discarding the whole candidate over either threw
        away real, already-recorded progress. `_decompose` marks the stuck
        node a leaf and carries on with the rest of the queue; `_propose`
        skips to the next work item, or -- once the queue is empty -- reports
        what already validated instead of discarding it.

        ``accept`` gets the touched paths once a job has really changed
        something and may return a reason to treat it as a no-op anyway --
        see `_propose`, where a substantive objective answered with only a
        test, or a scout whose items all name code that does not exist,
        would otherwise validate trivially and land as exactly the kind of
        generation the objective exists to replace.
        """
        harness = context.service("harness")
        if not isinstance(harness, HarnessGateway):
            return StepResult.blocked(
                "the harness is off, so this generation cannot be authored. "
                "Set harness.enabled and harness.allow_write in evomesh.yaml."
            )
        number = state.get("job")
        job = harness.job(int(number)) if number else None
        if job is None:
            job = harness.submit(
                build(),
                agent_id=context.definition.id,
                root=generation.path,
                label=label,
                write_prefix=write_prefix,
                max_steps=max_steps,
                max_seconds=max_seconds,
                catalog=catalog,
                allow_write=allow_write,
                # This pipeline polls `harness.job(state["job"])` again every
                # cycle until it finishes (see below) -- an inbox delivery on
                # top of that would hand the Evolver its own stage result a
                # second time, as a fresh "message" for respond() to answer.
                notify=False,
            )
            await evolver.set_pipeline_state({**state, "job": job.number})
            # Falls through when the job is somehow already finished, which is
            # never true of a real worker and always true of a synchronous one.
            if job.open:
                return StepResult(
                    summary=(
                        f"handed generation {generation.number} to harness job "
                        f"{job.number}; it reads the candidate and edits it while "
                        "this cycle carries on"
                    ),
                    phase=AgentPhase.AWAITING_HARNESS,
                )
        if job.open:
            return StepResult(
                summary=f"harness job {job.number} is still working: {job.describe()}",
                phase=AgentPhase.AWAITING_HARNESS,
            )
        answer = job.result.answer.strip() if job.result else job.detail
        rationale = _extract_rationale(answer)
        if self._improvements is not None:
            for line in answer.splitlines():
                if line.strip().upper().startswith("PROPOSAL:"):
                    await self._improvements.propose_discovery(
                        line.strip()[len("PROPOSAL:") :],
                        generation=generation.number,
                        job=job.number,
                    )
        recorder = record or evolver.record_harness_changes
        standing_objective = str(state.get("objective", ""))
        record_objective = record_key if record_key is not None else standing_objective
        entries = harness.changes(job)
        if apply_answer is not None:
            # A read-only job whose answer is the change: the pipeline writes it.
            entries = [*entries, *apply_answer(answer)]
        touched = await recorder(generation, entries, record_objective, rationale, status)
        # `touched` is the session's own log of what it wrote, not what is
        # still there -- a job that edits a file and then edits it back within
        # the same session reports both, non-empty, even though the working
        # tree nets to byte-identical with its parent. Left unchecked, that
        # candidate goes on to validate (trivially: nothing changed, so
        # nothing broke) and can be promoted and committed for real -- the
        # same D5 failure this stage already catches for a job that wrote
        # nothing at all, just reached from the other side.
        if touched and await evolver.candidate_changed_nothing(generation):
            touched = []
        if touched and accept is not None:
            rejection = accept(touched)
            if rejection is not None:
                logger.info(
                    "generation %s: %s; treating it as a no-op", generation.number, rejection
                )
                touched = []
        moved = {key: value for key, value in state.items() if key != "job"}
        if not touched:
            if on_no_op is not None:
                override = await on_no_op()
                if override is not None:
                    stage, extra = override
                    await evolver.set_pipeline_state({**moved, **extra, "stage": stage})
                    return StepResult(
                        summary=(
                            f"harness job {job.number} finished without changing a file "
                            f"({job.describe()}); continuing with what was already decided"
                        ),
                        phase=AgentPhase.ACTING,
                    )
            # D5: a candidate that changed nothing would validate, and a
            # generation that passes while changing nothing is the dead-module
            # failure wearing a verdict.
            await evolver.set_pipeline_state({**moved, "stage": STAGE_REPORT, "passed": None})
            streak = evolver.record_no_op()
            summary = (
                f"harness job {job.number} finished without changing a file "
                f"({job.describe()}); there is nothing to validate"
            )
            if streak > 0 and streak % NO_OP_STREAK_ALERT_EVERY == 0:
                environment = context.service("environment")
                if environment is not None:
                    with suppress(Exception):
                        await cast("Any", environment).announce(
                            f"evolution: {streak} generations in a row wrote no file "
                            f"(latest: generation {generation.number}, {job.describe()}). "
                            "The standing objective or step budget may be too tight for "
                            "the current model -- check /evolution status."
                        )
            fact = f"generation {generation.number} was authored but changed nothing"
            return await self._discard_no_op_or_report(evolver, generation, moved, summary, fact)
        evolver.reset_no_op_streak()
        stage, extra = on_done(touched)
        await evolver.set_pipeline_state({**moved, **extra, "stage": stage})
        return StepResult(
            summary=(
                f"harness job {job.number} changed {', '.join(touched)} in generation "
                f"{generation.number}: {excerpt(rationale, 160)}"
            ),
            fact=f"generation {generation.number} changed {', '.join(touched)}",
            phase=AgentPhase.ACTING,
        )

    async def _validate(self, evolver: EnvironmentEvolver, state: dict[str, Any]) -> StepResult:
        generation = evolver.candidate(int(state["generation"]))
        run = evolver.validation_run(generation.number)
        if run is None:
            # Started here and consumed on a later cycle, so the tick stays a
            # tick. The mailbox and the cycle share one lock, so awaiting the
            # suite inline is what made the Evolver stop answering for minutes.
            run = evolver.begin_validation(generation, self.validate_seconds)
            # A moment's grace, so a suite that finishes instantly is taken here
            # rather than a cycle later. Never true of a real validation, which
            # is minutes -- always true of a scripted one, which is what keeps
            # the pipeline tests at one stage per cycle.
            await asyncio.wait({run.task}, timeout=INSTANT_VALIDATION)
            if run.running:
                return StepResult(
                    summary=f"started the suite on generation {generation.number}",
                    phase=AgentPhase.ACTING,
                    hold=True,
                )
        if run.running:
            return StepResult(summary=run.describe(), phase=AgentPhase.ACTING, hold=True)
        result = await evolver.take_validation(run)
        repairs = int(state.get("repairs", 0))
        digest = result.digest()
        # A repair that leaves the failure byte-identical has not moved, and the
        # attempts left would go the same way. Stop and let the human see it.
        stalled = bool(digest) and digest == state.get("failure_digest")
        # The budget bounds *model* repairs, because a model repair costs a
        # generation's time and can make things worse. Ruff's own fixer costs
        # nothing and cannot, so it is never refused for being over budget.
        #
        # Found the first time the whole loop ran: the model diagnosed the real
        # failure and fixed it, ruff then objected to the import order it had
        # produced, and the candidate went to a human over a finding the linter
        # would have fixed for free.
        # `max_repairs: 0` still means off. A human who turned self-repair off
        # asked for one shot and a verdict, not for a cheaper kind of repair.
        free = bool(self.max_repairs) and evolver.repairer.can_repair(result.failure())
        exhausted = repairs >= self.max_repairs and not free
        blocker = result.environment_blocker()
        repairing = not result.passed and not blocker and not stalled and not exhausted
        # A passing leaf with more work items queued goes back to STAGE_PROPOSE
        # for the next one instead of straight to STAGE_REPORT; a repair, a
        # blocked run, or a stalled/exhausted failure never continues onto the
        # next item -- there is no point building more on a foundation that
        # just failed its own verdict.
        more_work = result.passed and bool(state.get("work_items"))
        next_stage = STAGE_REPAIR if repairing else (STAGE_PROPOSE if more_work else STAGE_REPORT)
        if next_stage == STAGE_REPORT and result.passed and self.review and not blocker:
            next_stage = STAGE_REVIEW
        await evolver.set_pipeline_state(
            {
                **state,
                "stage": next_stage,
                # A host failure is not a verdict on the candidate, so it is
                # reported as unvalidated rather than failed. None is what the
                # report stage already reads as "validation never happened".
                "passed": None if blocker else result.passed,
                "environment": blocker,
                "failure_digest": digest,
            }
        )
        if self._improvements is not None and state.get("improvement_id") and not blocker:
            await self._improvements.record_validation(
                str(state["improvement_id"]),
                passed=result.passed,
                revision=await evolver.candidate_revision(generation),
            )
        if blocker:
            command = (result.failure() or {}).get("command")
            return StepResult(
                summary=(
                    f"validation of generation {generation.number} was blocked by this "
                    f"machine, not by the candidate: `{command}` reported {blocker}. "
                    "Nothing is repaired, because no rewrite of the candidate would help."
                ),
                fact=f"generation {generation.number} could not be validated here",
                phase=AgentPhase.ACTING,
            )
        return StepResult(
            summary=(
                f"validation {self._outcome(result.passed, repairs)} for generation "
                f"{generation.number}{self._next_move(repairing, stalled, exhausted, repairs)}"
            ),
            fact=(
                f"generation {generation.number} validation "
                f"{'passed' if result.passed else 'failed'}"
            ),
            phase=AgentPhase.ACTING,
        )

    @staticmethod
    def _outcome(passed: bool, repairs: int) -> str:
        verdict = "passed" if passed else "failed"
        if passed and repairs:
            return f"{verdict} after {repairs} repair{'s' if repairs != 1 else ''}"
        return verdict

    def _next_move(self, repairing: bool, stalled: bool, exhausted: bool, repairs: int) -> str:
        if repairing:
            return f"; repairing it (attempt {repairs + 1} of {self.max_repairs})"
        if stalled:
            return "; the last repair changed nothing, so it stops here"
        if exhausted and repairs:
            return f"; {repairs} repair attempt{'s' if repairs != 1 else ''} did not fix it"
        return ""

    async def _repair(
        self, context: CycleContext, evolver: EnvironmentEvolver, state: dict[str, Any]
    ) -> StepResult:
        generation = evolver.candidate(int(state["generation"]))
        # A review verdict is not on disk the way a validation failure is --
        # the suite passed -- so it rides in the pipeline state instead.
        review_failure = state.get("review_failure")
        recorded = evolver.read_validation(generation)
        failure: dict[str, object] | None = (
            review_failure
            if isinstance(review_failure, dict)
            else (recorded.failure() if recorded else None)
        )
        if failure is None:
            # Nothing on record to repair. The candidate still deserves a
            # verdict, so fall through rather than looping on an empty stage.
            await evolver.set_pipeline_state({**state, "stage": STAGE_REPORT})
            return StepResult(
                summary=f"generation {generation.number} has no recorded failure to repair",
                phase=AgentPhase.ACTING,
            )
        attempt = int(state.get("repairs", 0)) + 1
        if not evolver.repairer.can_repair(failure):
            # The model repairs by reading the candidate, not by rewriting a file
            # it was shown. The attempt is only counted once the job comes back,
            # so waiting for the worker never burns the repair budget.
            touched = [change.path for change in generation.changes]
            test_only = state.get("pick") == PICK_TEST

            def build() -> str:
                # A work order: the code the failure points at, short rules, no
                # package map and no skills catalog (see repair_objective).
                objective = evolver.repair_objective(failure, touched, generation.path)
                return f"{objective}\n\n{TEST_ONLY_NOTE}" if test_only else objective

            def accept(changed: list[str]) -> str | None:
                if test_only and (bent := _source_paths(changed)):
                    return f"a repair of a test changed the code under test ({bent})"
                return None

            return await self._through_harness(
                context,
                evolver,
                state,
                generation,
                build=build,
                label=f"repair {attempt}: `{failure.get('command')}` failed",
                status="repaired",
                on_done=lambda changed: (
                    STAGE_VALIDATE,
                    {"repairs": attempt, "review_failure": None},
                ),
                accept=accept,
                catalog=False,
            )
        # The linter's own fixer does not spend the budget. The budget exists to
        # bound how often a *model* is allowed to rewrite the candidate; a
        # mechanical fix costs nothing and cannot make the candidate worse, and
        # a repair that leaves the failure byte-identical is already caught by
        # the stall check rather than by the counter.
        outcome = await evolver.autofix(generation)
        how = f"ruff --fix: {excerpt(str(outcome.get('output', '')), 120)}"
        if await evolver.candidate_changed_nothing(generation):
            # D5 again, one stage later: the propose stage's edit was real, but
            # the fixer just deleted exactly what it added. Validating a
            # candidate identical to the parent it was copied from would
            # "pass" for the same reason nothing failed for it in the first
            # place, and a generation that passes while changing nothing is
            # the dead-module failure wearing a verdict, however it got there.
            await evolver.set_pipeline_state({**state, "stage": STAGE_REPORT, "passed": None})
            summary = (
                f"generation {generation.number} has nothing left to validate -- "
                f"the free repair undid the only change it had ({how})"
            )
            fact = f"generation {generation.number} was repaired down to no change at all"
            return await self._discard_no_op_or_report(evolver, generation, state, summary, fact)
        await evolver.set_pipeline_state({**state, "stage": STAGE_VALIDATE})
        return StepResult(
            summary=(
                f"free repair for generation {generation.number} after "
                f"`{failure.get('command')}` failed -- {how}"
            ),
            fact=(
                f"generation {generation.number} repaired itself after "
                f"{failure.get('command')} failed"
            ),
            phase=AgentPhase.ACTING,
        )

    async def _review(
        self, context: CycleContext, evolver: EnvironmentEvolver, state: dict[str, Any]
    ) -> StepResult:
        """Read the validated change against its objective before it lands.

        Validation proves the candidate is valid and breaks nothing; only this
        asks whether it does what it was for. A read-only harness job (no
        write tools at all) gets the diff inline and may read whatever else it
        needs, then ends on ``VERDICT: COMPLETE`` or ``VERDICT: INCOMPLETE:
        <what is missing>``. INCOMPLETE goes to repair with that sentence as
        the failure, under the same ``max_repairs`` budget a failing command
        spends; once that is gone, the candidate is discarded rather than
        landed half-done. Like `_through_harness`, the job runs in the worker
        and this stage only submits, polls and reads the answer.
        """
        generation = evolver.candidate(int(state["generation"]))
        harness = context.service("harness")
        if not isinstance(harness, HarnessGateway):
            # No worker, no reviewer. The gate cannot run, so it must not be
            # what stops a generation that validated.
            await evolver.set_pipeline_state({**state, "stage": STAGE_REPORT})
            return StepResult(
                summary=(
                    f"generation {generation.number} validated; the harness is off, so "
                    "it is reported on validation alone"
                ),
                phase=AgentPhase.ACTING,
            )
        objective = str(state.get("objective", ""))
        number = state.get("review_job")
        job = harness.job(int(number)) if number else None
        if job is None:
            diff = await evolver.candidate_diff(generation)
            job = harness.submit(
                review_objective(objective, diff),
                agent_id=context.definition.id,
                root=generation.path,
                label=f"review generation {generation.number}",
                allow_write=False,
                max_steps=self.review_max_steps,
                max_seconds=self.review_max_seconds,
                notify=False,
                # The diff and the objective are the whole of a review; the
                # news and trading skills are not.
                catalog=False,
            )
            await evolver.set_pipeline_state({**state, "review_job": job.number})
            if job.open:
                return StepResult(
                    summary=(
                        f"handed generation {generation.number} to review job {job.number}, "
                        "which reads the change against its objective"
                    ),
                    phase=AgentPhase.AWAITING_HARNESS,
                )
        if job.open:
            return StepResult(
                summary=f"review job {job.number} is still working: {job.describe()}",
                phase=AgentPhase.AWAITING_HARNESS,
            )
        answer = job.result.answer if job.result else job.detail
        verdict, reason = parse_review(answer or "")
        moved = {key: value for key, value in state.items() if key != "review_job"}
        repairs = int(state.get("repairs", 0))
        if self._improvements is not None and state.get("improvement_id") and verdict is not None:
            await self._improvements.record_review(
                str(state["improvement_id"]),
                ReviewVerdict.COMPLETE if verdict else ReviewVerdict.INCOMPLETE,
                await evolver.candidate_revision(generation),
            )
        if verdict is True:
            await evolver.set_pipeline_state({**moved, "stage": STAGE_REPORT})
            return StepResult(
                summary=f"review of generation {generation.number}: complete",
                fact=f"generation {generation.number} was reviewed as complete",
                phase=AgentPhase.ACTING,
            )
        if verdict is None:
            attempts = int(state.get("review_attempts", 0)) + 1
            if attempts < REVIEW_ATTEMPTS:
                await evolver.set_pipeline_state({**moved, "review_attempts": attempts})
                return StepResult(
                    summary=(
                        f"review job {job.number} gave no verdict ({job.describe()}); asking again"
                    ),
                    phase=AgentPhase.ACTING,
                )
            await evolver.set_pipeline_state({**moved, "stage": STAGE_REPORT, "passed": False})
            return StepResult(
                summary=(
                    f"generation {generation.number} got no review verdict in "
                    f"{attempts} attempts; not landing an unreviewed change"
                ),
                fact=f"generation {generation.number} could not be reviewed",
                phase=AgentPhase.ACTING,
            )
        logger.info("generation %s reviewed as incomplete: %s", generation.number, reason)
        if repairs < self.max_repairs:
            await evolver.set_pipeline_state(
                {
                    **moved,
                    "stage": STAGE_REPAIR,
                    "review_attempts": 0,
                    "review_failure": {
                        "command": REVIEW_COMMAND,
                        "exit_code": 1,
                        "output": reason,
                        "objective": objective,
                    },
                }
            )
            return StepResult(
                summary=(
                    f"review of generation {generation.number}: incomplete -- "
                    f"{excerpt(reason, 200)}; finishing it (attempt {repairs + 1} of "
                    f"{self.max_repairs})"
                ),
                fact=f"generation {generation.number} was reviewed as incomplete",
                phase=AgentPhase.ACTING,
            )
        await evolver.set_pipeline_state({**moved, "stage": STAGE_REPORT, "passed": False})
        return StepResult(
            summary=(
                f"review of generation {generation.number}: still incomplete after "
                f"{repairs} repair attempt{'s' if repairs != 1 else ''} -- "
                f"{excerpt(reason, 200)}; discarding rather than landing it half-done"
            ),
            fact=f"generation {generation.number} was discarded as incomplete",
            phase=AgentPhase.ACTING,
        )

    async def _discard_no_op_or_report(
        self,
        evolver: EnvironmentEvolver,
        generation: Generation,
        state: dict[str, Any],
        summary: str,
        fact: str,
    ) -> StepResult:
        """A candidate that ends up with no diff at all, from either D5 case
        (the harness wrote nothing, or the free repair undid the only edit).

        Unlike a genuine "not validated" (host blocked the run, or validation
        is off), there is nothing here a human could lose: the candidate is
        byte-identical to its parent, so discarding it ships nothing and loses
        no work. Safe for auto_promote to decide on its own instead of parking
        it in await-human next to a candidate that actually needs a human's
        judgment.
        """
        if self.auto_promote:
            await evolver.finish_candidate(generation.number, passed=False)
            decision = await self._decide(evolver, generation.number, passed=False, state=state)
            return StepResult(
                summary=f"{summary}; {decision.summary}",
                fact=decision.fact,
                phase=decision.phase,
                achieved=decision.achieved,
            )
        return StepResult(summary=summary, fact=fact, phase=AgentPhase.ACTING)

    async def _report(self, evolver: EnvironmentEvolver, state: dict[str, Any]) -> StepResult:
        number = int(state["generation"])
        # None means validation never ran, which is not the same as failing it.
        passed = state.get("passed")
        await evolver.finish_candidate(number, passed=passed is not False)
        # A policy may only act on a verdict validation actually produced. With
        # no verdict -- validation switched off, or this machine blocking the
        # run -- promoting would ship unchecked code and discarding would throw
        # away work for the host's fault, so it still stops for a human.
        if self.auto_promote and passed is not None:
            return await self._decide(evolver, number, passed=bool(passed), state=state)
        awaiting = (
            f"generation {number} is ready for review ({self._verdict(state)}). "
            "Promote it with /evolution promote or drop it with /evolution discard."
        )
        # Stored, not just returned: /evolution status (console or Telegram) reads
        # this back so a human who missed the one-time chat announcement, or who
        # only just opened the mesh, can still ask instead of digging through logs.
        await evolver.set_pipeline_state(
            {**state, "stage": STAGE_AWAIT_HUMAN, "awaiting": awaiting}
        )
        return StepResult(
            summary=awaiting,
            fact=f"generation {number} is awaiting a human decision",
            phase=AgentPhase.WAITING_HUMAN,
            achieved=True,
        )

    async def _decide(
        self, evolver: EnvironmentEvolver, number: int, *, passed: bool, state: dict[str, Any]
    ) -> StepResult:
        if passed and state.get("pick") == PICK_TEST:
            generation = evolver.candidate(number)
            if await evolver.candidate_changed_source(generation):
                # A repair (or anything else) bent the code under test to fit
                # the test it was asked to write -- see PICK_TEST.
                logger.info(
                    "generation %s was asked for a test and changed src/evomesh/; "
                    "discarding instead of promoting",
                    number,
                )
                passed = False
        if passed and state.get("pick") in SOURCE_PICKS:
            generation = evolver.candidate(number)
            if await evolver.candidate_changed_source(generation) is False:
                # Validated, but only because what is left is a test or a doc:
                # a repair undid the source edit this substantive objective
                # was answered with. Landing it would be the test-only
                # generation this objective exists to replace.
                logger.info(
                    "generation %s validated without any change left under "
                    "src/evomesh/; discarding instead of promoting",
                    number,
                )
                passed = False
            elif state.get("pick") == PICK_IMPROVEMENT:
                # Ticked inside the candidate, so it lands in the very commit
                # that implemented it.
                title = str(state.get("pick_key", ""))
                if step := int(state.get("pick_step", 0) or 0):
                    evolver.tick_step(generation, title, step)
                else:
                    evolver.tick_improvement(generation, title)
        try:
            commit = await evolver.decide_candidate(
                number, promote=passed, objective=str(state.get("objective", ""))
            )
        except GitError as exc:
            # Uncommitted human work in the checkout is never this pipeline's
            # call to make, auto_promote or not: the risk there is clobbering
            # work a human has not committed, which has nothing to do with
            # whether this candidate is any good. Every other GitError here --
            # a cherry-pick conflict because the tree moved on, or a candidate
            # that turned out to change nothing to apply -- is a fact about
            # the CANDIDATE, the exact kind of verdict auto_promote already
            # decides on its own.
            blocked_by_human_work = "uncommitted changes" in str(exc)
            if self.auto_promote and not blocked_by_human_work:
                # The candidate is fine, the place it was going is not, and it
                # is a copy on disk -- discarding it loses nothing a fresh
                # candidate against the tree as it now stands would not redo
                # anyway, so this parks for nobody rather than sitting idle
                # for a human to make exactly this call by hand.
                evolver.workspace.supervisor.discard(number)
                await evolver.reset_pipeline()
                return StepResult(
                    summary=(
                        f"generation {number} validated but could not be applied to "
                        f"the working tree ({exc}); discarded rather than parked, "
                        "since auto_promote means this pipeline decides for itself"
                    ),
                    fact=f"generation {number} discarded: could not be applied to the tree",
                    phase=AgentPhase.ACTING,
                    achieved=True,
                )
            # The tree would not take it -- a human's uncommitted work is in the
            # way, or the change does not apply. Park rather than discard: the
            # candidate is fine, the place it was going is not.
            awaiting = (
                f"generation {number} validated but could not be applied to the "
                f"working tree: {exc}. It is left for you to promote by hand."
            )
            await evolver.set_pipeline_state(
                {**state, "stage": STAGE_AWAIT_HUMAN, "error": str(exc), "awaiting": awaiting}
            )
            return StepResult(
                summary=awaiting,
                fact=f"generation {number} could not be applied to the tree",
                phase=AgentPhase.WAITING_HUMAN,
                achieved=True,
            )
        # Not set_pipeline_state: the reset clears the repair counters and the
        # failure digest, so the next candidate starts from a clean slate.
        await evolver.reset_pipeline()
        action = "promoted" if passed else "discarded"
        landed = (
            f" as {commit[:8]} ({evolver.last_publish}), "
            + ("restarting the mesh into it" if self.auto_restart else "restart the mesh to run it")
            if commit
            else ""
        )
        return StepResult(
            summary=(
                f"{action} generation {number} on its own verdict ({self._verdict(state)})"
                f"{landed}; the pipeline is free for the next objective"
            ),
            fact=f"generation {number} was {action} by policy, with no human asked",
            phase=AgentPhase.ACTING,
            achieved=True,
        )

    @staticmethod
    def _verdict(state: dict[str, Any]) -> str:
        if blocker := state.get("environment"):
            return f"not validated: this machine blocked the run ({blocker})"
        passed = state.get("passed")
        if passed is None:
            return "not validated"
        verdict = "validation passed" if passed else "validation failed"
        repairs = int(state.get("repairs", 0))
        if not repairs:
            return verdict
        attempts = f"{repairs} repair attempt{'s' if repairs != 1 else ''}"
        return f"{verdict} after {attempts}"


def default_behaviors(
    auto_validate: bool = True,
    max_repairs: int = 2,
    auto_promote: bool = False,
    auto_restart: bool = True,
    validate_seconds: float = 1800.0,
    auto_plan: bool = False,
    plan_max_steps: int | None = None,
    plan_max_seconds: float | None = None,
    review: bool = False,
    review_max_steps: int | None = None,
    review_max_seconds: float | None = None,
    baseline_tests: bool = False,
    test_backlog: bool = True,
    scout_when_idle: bool = True,
) -> dict[str, Any]:
    return {
        "architect": ArchitectBehavior(),
        "guardian": GuardianBehavior(),
        "evaluator": EvaluatorBehavior(),
        "evolver": EvolverBehavior(
            auto_validate=auto_validate,
            max_repairs=max_repairs,
            auto_promote=auto_promote,
            auto_restart=auto_restart,
            validate_seconds=validate_seconds,
            auto_plan=auto_plan,
            plan_max_steps=plan_max_steps,
            plan_max_seconds=plan_max_seconds,
            review=review,
            review_max_steps=review_max_steps,
            review_max_seconds=review_max_seconds,
            baseline_tests=baseline_tests,
            test_backlog=test_backlog,
            scout_when_idle=scout_when_idle,
        ),
    }


__all__ = [
    "ArchitectBehavior",
    "CandidateValidator",
    "EvaluatorBehavior",
    "EvolverBehavior",
    "Generation",
    "GenerationStatus",
    "GuardianBehavior",
    "default_behaviors",
]
