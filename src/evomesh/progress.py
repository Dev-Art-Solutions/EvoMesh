"""Deterministic progress and stall detection for goal execution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from evomesh.cognition import CycleOutcome
from evomesh.contracts import Goal


@dataclass(frozen=True)
class ProgressSignal:
    stalled: bool
    signature: str
    repeats: int
    reason: str = ""


@dataclass
class ProgressTracker:
    """Detect repeated failures/no-progress without asking a model."""

    failure_threshold: int = 3
    no_progress_threshold: int = 4
    _last: dict[str, str] = field(default_factory=dict)
    _repeats: dict[str, int] = field(default_factory=dict)

    def observe(self, goal: Goal, outcome: CycleOutcome) -> ProgressSignal:
        signature = self._signature(goal, outcome)
        previous = self._last.get(goal.id)
        repeats = self._repeats.get(goal.id, 0) + 1 if previous == signature else 1
        self._last[goal.id] = signature
        self._repeats[goal.id] = repeats
        threshold = self.failure_threshold if outcome.error else self.no_progress_threshold
        no_progress = not outcome.goal_done and not outcome.fact and not outcome.step
        # Edge-triggered: the cycle that reaches the threshold is the stall.
        # Every identical cycle after it is the same stall, and re-signalling
        # it sent a fresh assistance work item -- a new persisted goal on the
        # helper -- once per cycle for as long as the failure lasted.
        stalled = repeats == threshold and (bool(outcome.error) or no_progress)
        reason = ""
        if stalled:
            reason = (
                f"same failure repeated {repeats} times"
                if outcome.error
                else f"no new step, fact, artifact, or completion for {repeats} cycles"
            )
        return ProgressSignal(stalled, signature, repeats, reason)

    @staticmethod
    def _signature(goal: Goal, outcome: CycleOutcome) -> str:
        material = "|".join(
            (
                outcome.error or "",
                outcome.step.strip(),
                outcome.fact.strip(),
                outcome.summary.strip(),
                str(goal.progress),
                ",".join(goal.artifacts),
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def clear(self, goal_id: str) -> None:
        self._last.pop(goal_id, None)
        self._repeats.pop(goal_id, None)
