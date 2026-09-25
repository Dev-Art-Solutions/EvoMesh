"""Conservative promotion of repeated successful traces into procedures.

The learner is a stateless policy over an agent's persisted ``MindState``:
traces live in ``mind.execution_traces`` and procedures in
``mind.procedures``, so what an agent has learned survives the restart every
promotion triggers. The BDI reasoner feeds it every finished model-planned or
learned intention, and asks it for a procedure before it would otherwise pay
for a planning call.
"""

from __future__ import annotations

from evomesh.contracts import (
    ExecutionTrace,
    Goal,
    LearnedProcedure,
    MindState,
    ProcedureStatus,
    now_utc,
)

__all__ = ["ExecutionTrace", "ProcedureLearner", "goal_signature"]

LEARNED_PREFIX = "learned:"


def goal_signature(goal: Goal) -> str:
    """What a later goal must equal for a learned procedure to apply."""
    return " ".join(goal.description.lower().split())


class ProcedureLearner:
    def __init__(self, minimum_successes: int = 3) -> None:
        if minimum_successes < 2:
            raise ValueError("minimum_successes must be at least two")
        self.minimum_successes = minimum_successes

    def candidate(self, mind: MindState, pattern: str) -> LearnedProcedure | None:
        """A procedure for ``pattern``, if its traces earn one automatically:
        at least ``minimum_successes`` successes and not a single failure."""
        matches = [trace for trace in mind.execution_traces if trace.pattern == pattern]
        successes = [trace for trace in matches if trace.succeeded]
        failures = [trace for trace in matches if not trace.succeeded]
        if len(successes) < self.minimum_successes or failures:
            return None
        return self._procedure(
            successes[-1],
            pattern,
            len(successes),
            0,
            duration_seconds=sum(trace.duration_seconds for trace in successes),
        )

    def promote(
        self, mind: MindState, pattern: str, *, human_approved: bool = False
    ) -> LearnedProcedure | None:
        matches = [trace for trace in mind.execution_traces if trace.pattern == pattern]
        if human_approved and matches and matches[-1].succeeded:
            procedure = self._procedure(
                matches[-1],
                pattern,
                sum(trace.succeeded for trace in matches),
                sum(not trace.succeeded for trace in matches),
                duration_seconds=sum(trace.duration_seconds for trace in matches),
            )
            procedure.approved = True
        else:
            procedure = self.candidate(mind, pattern)
        if procedure is not None:
            procedure.status = ProcedureStatus.PROMOTED
            mind.remember_procedure(procedure)
        return procedure

    def observe(self, mind: MindState, trace: ExecutionTrace) -> LearnedProcedure | None:
        """Record one finished execution and return a newly learned procedure.

        A trace of a procedure already learned updates its record instead; one
        that fails as often as half its successes is forgotten, so a procedure
        the world stopped matching goes back to being planned by the model.
        """
        mind.record_trace(trace)
        existing = next(
            (item for item in mind.procedures.values() if item.pattern == trace.pattern),
            None,
        )
        if existing is not None:
            if trace.succeeded:
                existing.successes += 1
            else:
                existing.failures += 1
            existing.total_duration_seconds += trace.duration_seconds
            if trace.validator_passed is True:
                existing.validator_passes += 1
            elif trace.validator_passed is False:
                existing.validator_failures += 1
            existing.confidence = existing.successes / max(
                1, existing.successes + existing.failures
            )
            existing.updated_at = now_utc()
            if (
                trace.validator_passed is False
                or (not trace.succeeded and existing.failures * 2 >= existing.successes)
            ):
                existing.status = ProcedureStatus.DEGRADED
            return None
        if not trace.succeeded:
            return None
        return self.promote(mind, trace.pattern)

    def match(
        self,
        mind: MindState,
        goal: Goal,
        *,
        capabilities: tuple[str, ...] | list[str] = (),
        context: dict[str, object] | None = None,
    ) -> LearnedProcedure | None:
        """The learned procedure for this goal, best record first, if any."""
        signature = goal_signature(goal)
        available = frozenset(capabilities)
        context = context or {}
        matches = [
            item
            for item in mind.procedures.values()
            if item.trigger == signature
            and item.steps
            and (not item.goal_kind or item.goal_kind == goal.kind)
            and item.status is ProcedureStatus.PROMOTED
            and self._parameters_match(item, goal)
            and all(context.get(key) == value for key, value in item.context_predicates.items())
        ]
        for item in matches:
            if not set(item.required_capabilities).issubset(available):
                item.status = ProcedureStatus.DEGRADED
                item.updated_at = now_utc()
        matches = [
            item
            for item in matches
            if set(item.required_capabilities).issubset(available)
        ]
        selected = max(
            matches,
            key=lambda item: (item.successes - item.failures, item.updated_at),
            default=None,
        )
        if selected is not None:
            selected.uses += 1
            selected.model_calls_saved += 1
            selected.last_used_at = now_utc()
            selected.updated_at = selected.last_used_at
        return selected

    @staticmethod
    def _parameters_match(procedure: LearnedProcedure, goal: Goal) -> bool:
        for key, expected_type in procedure.parameter_schema.items():
            if key not in goal.parameters:
                return False
            if type(goal.parameters[key]).__name__ != expected_type:
                return False
        return True

    @staticmethod
    def _procedure(
        exemplar: ExecutionTrace,
        pattern: str,
        successes: int,
        failures: int,
        *,
        duration_seconds: float,
    ) -> LearnedProcedure:
        return LearnedProcedure(
            name=f"{LEARNED_PREFIX}{exemplar.goal_type}:{pattern}",
            trigger=exemplar.context_signature,
            steps=list(exemplar.steps),
            successes=successes,
            failures=failures,
            pattern=pattern,
            goal_kind=exemplar.goal_type,
            parameter_schema={
                key: type(value).__name__ for key, value in exemplar.goal_parameters.items()
            },
            required_capabilities=sorted(
                set(exemplar.capabilities) | set(exemplar.tools)
            ),
            context_predicates=dict(exemplar.context_predicates),
            success_conditions=list(exemplar.success_conditions),
            status=ProcedureStatus.VALIDATED,
            confidence=successes / max(1, successes + failures),
            total_duration_seconds=duration_seconds,
            validator_passes=int(exemplar.validator_passed is True),
            validator_failures=int(exemplar.validator_passed is False),
        )
