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
from typing import Any, cast

from evomesh.bdi import (
    BDIBehavior,
    Desire,
    PlanLibrary,
    PlanRecipe,
    ReflectiveBehavior,
    StepResult,
)
from evomesh.cognition import CycleContext
from evomesh.contracts import AgentPhase, Belief, BeliefChange, Intention, PlanStep
from evomesh.evolution import (
    CandidateValidator,
    EnvironmentEvolver,
    Generation,
    GenerationStatus,
    PlanNode,
    excerpt,
)
from evomesh.git import GitError
from evomesh.harness_queue import HarnessGateway

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
STAGE_REPORT = "report"
STAGE_AWAIT_HUMAN = "await-human"

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
# Under a promotion policy the last step is a decision, not a handover.
AUTO_PROMOTE_STEP = "promote or discard the candidate on its verdict"

AWAITING_KEY = "evolution.awaiting_human"

HEALTHY_PREFIXES = ("the model provider is ready", "all ")
INVESTIGATE = "Investigate why "

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


class ArchitectBehavior(ReflectiveBehavior):
    """Reactive only. The Architect must not invent agents nobody asked for."""

    name = "architect"


class GuardianBehavior(BDIBehavior):
    """Perceives mesh health and wants it restored. Never needs the model."""

    name = "guardian"

    async def perceive(self, context: CycleContext) -> list[Belief]:
        states = cast("dict[str, Any]", context.service("runtime_states") or {})
        health = cast(
            "tuple[bool, str]", context.service("provider_health") or (False, "unknown")
        )
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

    async def options(self, context: CycleContext, change: BeliefChange) -> list[Desire]:
        """Want something new only when the world actually changed."""
        degraded = context.definition.mind.belief(DEGRADED_KEY)
        if (
            DEGRADED_KEY in change.keys
            and degraded is not None
            and degraded.statement.startswith("agents not running")
        ):
            return [Desire(f"{INVESTIGATE}{degraded.statement}", priority=2)]
        return []

    def library(self) -> PlanLibrary:
        return PlanLibrary(
            (
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

    def _stages(self) -> tuple[str, ...]:
        if not self.auto_validate:
            return SKIP_VALIDATION_STAGES
        stages = EVOLUTION_STAGES_WITH_PLAN if self.auto_plan else EVOLUTION_STAGES
        if not self.max_repairs:
            return tuple(stage for stage in stages if stage != STAGE_REPAIR)
        return stages

    def _steps(self) -> tuple[str, ...]:
        if not self.auto_validate:
            steps = SKIP_VALIDATION_STEPS
        else:
            steps = EVOLUTION_STEPS_WITH_PLAN if self.auto_plan else EVOLUTION_STEPS
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
            return await self._open(evolver, objective)
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
        if stage == STAGE_REPORT:
            return await self._report(evolver, state)
        return StepResult.blocked(f"unknown evolution stage '{stage}'")

    async def _open(self, evolver: EnvironmentEvolver, objective: str) -> StepResult:
        generation = await evolver.create_candidate(objective)
        next_stage = STAGE_DRAFT if self.auto_plan else STAGE_PROPOSE
        await evolver.set_pipeline_state(
            {
                "stage": next_stage,
                "generation": generation.number,
                "objective": objective,
                "path": str(generation.path),
            }
        )
        return StepResult(
            summary=f"opened candidate generation {generation.number} at {generation.path}",
            fact=f"generation {generation.number} opened for: {objective}",
            phase=AgentPhase.ACTING,
        )

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
        )

    async def _evaluate_plan(
        self, context: CycleContext, evolver: EnvironmentEvolver, state: dict[str, Any]
    ) -> StepResult:
        generation = evolver.candidate(int(state["generation"]))
        root = evolver.current_plan_root(generation)
        plan_text = root.reasoning if root is not None else ""

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
        build = (lambda: evolver.leaf_objective(item)) if item is not None else (
            lambda: evolver.mutation_objective(objective)
        )
        label = item.title if item is not None else objective
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
        on_no_op: Callable[[], Awaitable[tuple[str, dict[str, Any]]]] | None = None,
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

        ``on_no_op`` overrides what happens when the harness wrote nothing.
        Left at its default (``None``), a no-op still discards the whole
        generation (D5, below) -- exactly right for `_propose`/`_repair`,
        where the harness job *is* the generation's one piece of work. Found
        live: `_decompose` shares this same no-op path, but by the time it
        runs there, a no-op job is one stuck node at the end of a queue that
        may already hold a dozen siblings this generation successfully split
        -- discarding the whole candidate over that one node threw away every
        one of them. `_decompose` passes an override that marks the stuck
        node a leaf and carries on with the rest of the queue instead.
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
                build(), agent_id=context.definition.id, root=generation.path, label=label
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
        recorder = record or evolver.record_harness_changes
        standing_objective = str(state.get("objective", ""))
        record_objective = record_key if record_key is not None else standing_objective
        touched = await recorder(
            generation, harness.changes(job), record_objective, rationale, status
        )
        moved = {key: value for key, value in state.items() if key != "job"}
        if not touched:
            if on_no_op is not None:
                stage, extra = await on_no_op()
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
            summary = (
                f"harness job {job.number} finished without changing a file "
                f"({job.describe()}); there is nothing to validate"
            )
            fact = f"generation {generation.number} was authored but changed nothing"
            return await self._discard_no_op_or_report(evolver, generation, moved, summary, fact)
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

    async def _validate(
        self, evolver: EnvironmentEvolver, state: dict[str, Any]
    ) -> StepResult:
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
        recorded = evolver.read_validation(generation)
        failure = recorded.failure() if recorded else None
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
            return await self._through_harness(
                context,
                evolver,
                state,
                generation,
                build=lambda: evolver.repair_objective(failure, touched),
                label=f"repair {attempt}: `{failure.get('command')}` failed",
                status="repaired",
                on_done=lambda changed: (STAGE_VALIDATE, {"repairs": attempt}),
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

    async def _report(
        self, evolver: EnvironmentEvolver, state: dict[str, Any]
    ) -> StepResult:
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
        await evolver.set_pipeline_state({**state, "stage": STAGE_AWAIT_HUMAN})
        return StepResult(
            summary=(
                f"generation {number} is ready for review ({self._verdict(state)}). "
                "Promote it with /evolution promote or drop it with /evolution discard."
            ),
            fact=f"generation {number} is awaiting a human decision",
            phase=AgentPhase.WAITING_HUMAN,
            achieved=True,
        )

    async def _decide(
        self, evolver: EnvironmentEvolver, number: int, *, passed: bool, state: dict[str, Any]
    ) -> StepResult:
        try:
            commit = await evolver.decide_candidate(
                number, promote=passed, objective=str(state.get("objective", ""))
            )
        except GitError as exc:
            # The tree would not take it -- a human's uncommitted work is in the
            # way, or the change does not apply. Park rather than discard: the
            # candidate is fine, the place it was going is not.
            await evolver.set_pipeline_state(
                {**state, "stage": STAGE_AWAIT_HUMAN, "error": str(exc)}
            )
            return StepResult(
                summary=(
                    f"generation {number} validated but could not be applied to the "
                    f"working tree: {exc}. It is left for you to promote by hand."
                ),
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
            + (
                "restarting the mesh into it"
                if self.auto_restart
                else "restart the mesh to run it"
            )
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
