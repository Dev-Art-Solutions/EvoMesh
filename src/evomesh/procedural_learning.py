"""Conservative promotion of repeated successful traces into procedures."""

from __future__ import annotations

import hashlib
from datetime import datetime

from pydantic import BaseModel, Field

from evomesh.contracts import LearnedProcedure, MindState, now_utc


class ExecutionTrace(BaseModel):
    goal_type: str
    context_signature: str
    plan_name: str
    steps: list[str]
    tools: list[str] = Field(default_factory=list)
    agents: list[str] = Field(default_factory=list)
    succeeded: bool
    model_calls: int = 0
    duration_seconds: float = 0.0
    created_at: datetime = Field(default_factory=now_utc)

    @property
    def pattern(self) -> str:
        material = "|".join((self.goal_type, self.context_signature, *self.steps))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


class ProcedureLearner:
    def __init__(self, minimum_successes: int = 3) -> None:
        if minimum_successes < 2:
            raise ValueError("minimum_successes must be at least two")
        self.minimum_successes = minimum_successes
        self.traces: list[ExecutionTrace] = []

    def record(self, trace: ExecutionTrace) -> None:
        self.traces.append(trace)

    def candidate(self, pattern: str) -> LearnedProcedure | None:
        matches = [trace for trace in self.traces if trace.pattern == pattern]
        successes = [trace for trace in matches if trace.succeeded]
        failures = [trace for trace in matches if not trace.succeeded]
        if len(successes) < self.minimum_successes or failures:
            return None
        exemplar = successes[-1]
        return LearnedProcedure(
            name=f"learned:{exemplar.goal_type}:{pattern}",
            trigger=exemplar.context_signature,
            steps=exemplar.steps,
            successes=len(successes),
            failures=0,
        )

    def promote(
        self, mind: MindState, pattern: str, *, human_approved: bool = False
    ) -> LearnedProcedure | None:
        matches = [trace for trace in self.traces if trace.pattern == pattern]
        if human_approved and matches and matches[-1].succeeded:
            exemplar = matches[-1]
            procedure = LearnedProcedure(
                name=f"learned:{exemplar.goal_type}:{pattern}",
                trigger=exemplar.context_signature,
                steps=exemplar.steps,
                successes=sum(trace.succeeded for trace in matches),
                failures=sum(not trace.succeeded for trace in matches),
            )
        else:
            procedure = self.candidate(pattern)
        if procedure is not None:
            mind.remember_procedure(procedure)
        return procedure
