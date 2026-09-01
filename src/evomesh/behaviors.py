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

from pathlib import Path
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
    excerpt,
)

PROVIDER_KEY = "provider.ready"
DEGRADED_KEY = "mesh.degraded"
STAGE_KEY = "evolution.stage"
CANDIDATE_KEY = "evolution.candidate"
VERDICT_KEY = "evolution.verdict"

STAGE_PLAN = "plan"
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
# With auto_validate off the validation stage never runs, so neither validation
# nor the repair that only exists to answer it belongs in the checklist.
SKIP_VALIDATION_STAGES = (STAGE_PLAN, STAGE_PROPOSE, STAGE_REPORT)
SKIP_VALIDATION_STEPS = (EVOLUTION_STEPS[0], EVOLUTION_STEPS[1], EVOLUTION_STEPS[-1])
# Under a promotion policy the last step is a decision, not a handover.
AUTO_PROMOTE_STEP = "promote or discard the candidate on its verdict"

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
    ) -> None:
        super().__init__()
        self.auto_validate = auto_validate
        # Zero turns self-repair off and restores the old behaviour: one shot at
        # validation, then a verdict.
        self.max_repairs = max(0, max_repairs)
        # Decide the candidate's fate from the verdict instead of parking on a
        # human. Only ever acts on a verdict validation actually produced.
        self.auto_promote = auto_promote

    def _stages(self) -> tuple[str, ...]:
        if not self.auto_validate:
            return SKIP_VALIDATION_STAGES
        if not self.max_repairs:
            return tuple(stage for stage in EVOLUTION_STAGES if stage != STAGE_REPAIR)
        return EVOLUTION_STAGES

    def _steps(self) -> tuple[str, ...]:
        if not self.auto_validate:
            steps = SKIP_VALIDATION_STEPS
        elif not self.max_repairs:
            steps = tuple(step for step in EVOLUTION_STEPS if not step.startswith("repair"))
        else:
            steps = EVOLUTION_STEPS
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
            if stage == STAGE_REPAIR:
                return await self._repair(context, evolver, state)
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
            objective,
            context=await context.build_prompt(""),
            # The Evolver's own model, not the mesh default: a human who assigns
            # it a stronger model expects the mutation to come from that one.
            model=context.definition.model_name,
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
        repairs = int(state.get("repairs", 0))
        digest = result.digest()
        # A repair that leaves the failure byte-identical has not moved, and the
        # attempts left would go the same way. Stop and let the human see it.
        stalled = bool(digest) and digest == state.get("failure_digest")
        exhausted = repairs >= self.max_repairs
        blocker = result.environment_blocker()
        repairing = not result.passed and not blocker and not stalled and not exhausted
        await evolver.set_pipeline_state(
            {
                **state,
                "stage": STAGE_REPAIR if repairing else STAGE_REPORT,
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
        moved = {**state, "stage": STAGE_VALIDATE, "repairs": attempt}
        if evolver.repairer.can_repair(failure):
            outcome = await evolver.autofix(generation)
            how = f"ruff --fix: {excerpt(str(outcome.get('output', '')), 120)}"
        else:
            try:
                mutation = await evolver.propose_repair(
                    generation,
                    failure,
                    Path(str(state["file"])) if state.get("file") else None,
                    model=context.definition.model_name,
                )
            except (RuntimeError, ValueError) as exc:
                # The model could not author a fix. Reporting the failure as it
                # stands beats the generic handler, which would send the whole
                # pipeline back to plan and strand this candidate unreviewed.
                await evolver.set_pipeline_state(
                    {**state, "stage": STAGE_REPORT, "error": str(exc)}
                )
                return StepResult(
                    summary=(
                        f"no repair could be authored for generation "
                        f"{generation.number} ({exc}); reporting the failure as it stands"
                    ),
                    phase=AgentPhase.ACTING,
                )
            await evolver.apply_mutation(
                generation, mutation, str(state.get("objective", "")), status="repaired"
            )
            moved["file"] = str(mutation.relative_path)
            how = f"{mutation.relative_path}: {mutation.rationale}"
        await evolver.set_pipeline_state(moved)
        return StepResult(
            summary=(
                f"repair {attempt} of {self.max_repairs} for generation "
                f"{generation.number} after `{failure.get('command')}` failed -- {how}"
            ),
            fact=(
                f"generation {generation.number} repaired itself after "
                f"{failure.get('command')} failed"
            ),
            phase=AgentPhase.ACTING,
        )

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
        await evolver.decide_candidate(number, promote=passed)
        # Not set_pipeline_state: the reset clears the repair counters and the
        # failure digest, so the next candidate starts from a clean slate.
        await evolver.reset_pipeline()
        action = "promoted" if passed else "discarded"
        return StepResult(
            summary=(
                f"{action} generation {number} on its own verdict ({self._verdict(state)}); "
                "the pipeline is free for the next objective"
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
    auto_validate: bool = True, max_repairs: int = 2, auto_promote: bool = False
) -> dict[str, Any]:
    return {
        "architect": ArchitectBehavior(),
        "guardian": GuardianBehavior(),
        "evaluator": EvaluatorBehavior(),
        "evolver": EvolverBehavior(
            auto_validate=auto_validate,
            max_repairs=max_repairs,
            auto_promote=auto_promote,
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
