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
)

PROVIDER_KEY = "provider.ready"
DEGRADED_KEY = "mesh.degraded"
STAGE_KEY = "evolution.stage"
CANDIDATE_KEY = "evolution.candidate"
VERDICT_KEY = "evolution.verdict"

STAGE_PLAN = "plan"
STAGE_PROPOSE = "propose"
STAGE_VALIDATE = "validate"
STAGE_REPORT = "report"
STAGE_AWAIT_HUMAN = "await-human"

# The Evolver's plan, in the order the pipeline runs. A stage's index here is
# also the plan cursor, so the checklist and the persisted pipeline state cannot
# drift apart.
EVOLUTION_STAGES = (STAGE_PLAN, STAGE_PROPOSE, STAGE_VALIDATE, STAGE_REPORT)
EVOLUTION_STEPS = (
    "open an isolated candidate generation",
    "propose and apply one mutation",
    "validate the candidate",
    "hand the candidate to the human",
)
# With auto_validate off the validation stage never runs, so it must not
# appear in the checklist either.
SKIP_VALIDATION_STAGES = (STAGE_PLAN, STAGE_PROPOSE, STAGE_REPORT)
SKIP_VALIDATION_STEPS = (EVOLUTION_STEPS[0], EVOLUTION_STEPS[1], EVOLUTION_STEPS[3])

AWAITING_KEY = "evolution.awaiting_human"

HEALTHY_PREFIXES = ("the model provider is ready", "all ")
INVESTIGATE = "Investigate why "


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

    def __init__(self, auto_validate: bool = True) -> None:
        super().__init__()
        self.auto_validate = auto_validate

    def _stages(self) -> tuple[str, ...]:
        return EVOLUTION_STAGES if self.auto_validate else SKIP_VALIDATION_STAGES

    def _steps(self) -> tuple[str, ...]:
        return EVOLUTION_STEPS if self.auto_validate else SKIP_VALIDATION_STEPS

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
            return StepResult.waiting(
                f"generation {state.get('generation')} is waiting for a human to "
                f"promote or discard it ({self._verdict(state)})"
            )
        # The persisted stage is the single source of truth, so the plan cursor
        # is pinned to it and the checklist can never disagree with reality.
        stages = self._stages()
        if stage in stages:
            intention.cursor = stages.index(stage)
        goal = context.goal
        objective = str(state.get("objective") or (goal.description if goal else ""))
        try:
            if stage == STAGE_PLAN:
                return await self._open(evolver, objective)
            if stage == STAGE_PROPOSE:
                return await self._propose(context, evolver, state)
            if stage == STAGE_VALIDATE:
                return await self._validate(evolver, state)
            if stage == STAGE_REPORT:
                return await self._report(evolver, state)
        except (RuntimeError, ValueError, OSError) as exc:
            await evolver.set_pipeline_state({**state, "stage": STAGE_PLAN, "error": str(exc)})
            return StepResult(summary=f"stage '{stage}' failed: {exc}", failed=True)
        return StepResult.blocked(f"unknown evolution stage '{stage}'")

    async def _open(self, evolver: EnvironmentEvolver, objective: str) -> StepResult:
        generation = await evolver.create_candidate(objective)
        await evolver.set_pipeline_state(
            {
                "stage": STAGE_PROPOSE,
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

    async def _propose(
        self, context: CycleContext, evolver: EnvironmentEvolver, state: dict[str, Any]
    ) -> StepResult:
        generation = evolver.candidate(int(state["generation"]))
        objective = str(state["objective"])
        mutation = await evolver.propose_mutation(
            objective, context=await context.build_prompt("")
        )
        await evolver.apply_mutation(generation, mutation, objective)
        next_stage = STAGE_VALIDATE if self.auto_validate else STAGE_REPORT
        await evolver.set_pipeline_state(
            {**state, "stage": next_stage, "file": str(mutation.relative_path)}
        )
        return StepResult(
            summary=f"applied a mutation to {mutation.relative_path}: {mutation.rationale}",
            fact=f"generation {generation.number} changed {mutation.relative_path}",
            phase=AgentPhase.ACTING,
        )

    async def _validate(
        self, evolver: EnvironmentEvolver, state: dict[str, Any]
    ) -> StepResult:
        generation = evolver.candidate(int(state["generation"]))
        result = await evolver.validate(generation)
        await evolver.set_pipeline_state(
            {**state, "stage": STAGE_REPORT, "passed": result.passed}
        )
        verdict = "passed" if result.passed else "failed"
        return StepResult(
            summary=f"validation {verdict} for generation {generation.number}",
            fact=f"generation {generation.number} validation {verdict}",
            phase=AgentPhase.ACTING,
        )

    async def _report(
        self, evolver: EnvironmentEvolver, state: dict[str, Any]
    ) -> StepResult:
        number = int(state["generation"])
        # None means validation never ran, which is not the same as failing it.
        passed = state.get("passed")
        await evolver.finish_candidate(number, passed=passed is not False)
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

    @staticmethod
    def _verdict(state: dict[str, Any]) -> str:
        passed = state.get("passed")
        if passed is None:
            return "not validated"
        return "validation passed" if passed else "validation failed"


def default_behaviors(auto_validate: bool = True) -> dict[str, Any]:
    return {
        "architect": ArchitectBehavior(),
        "guardian": GuardianBehavior(),
        "evaluator": EvaluatorBehavior(),
        "evolver": EvolverBehavior(auto_validate=auto_validate),
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
