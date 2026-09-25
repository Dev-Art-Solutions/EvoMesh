"""Evidence-backed improvement backlog and bounded coordination policy."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from evomesh.contracts import now_utc
from evomesh.coordination import WorkBudget, WorkItem, WorkStatus
from evomesh.events import Event, EventType


class ImprovementStatus(StrEnum):
    PROPOSED = "proposed"
    TRIAGED = "triaged"
    READY = "ready"
    ACTIVE = "active"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"
    INEFFECTIVE = "ineffective"


class ReviewVerdict(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    WRONG_DIRECTION = "wrong_direction"
    UNSAFE = "unsafe"


class Evidence(BaseModel):
    kind: str
    reference: str
    value: Any = None
    source: str
    created_at: datetime = Field(default_factory=now_utc)


class PriorityFactors(BaseModel):
    impact: float = 1.0
    urgency: float = 1.0
    confidence: float = 1.0
    recurrence: float = 1.0
    strategic_value: float = 1.0
    estimated_effort: float = 1.0
    risk: float = 1.0

    @property
    def score(self) -> float:
        benefit = (
            max(0.0, self.impact)
            * max(0.0, self.urgency)
            * max(0.0, self.confidence)
            * max(0.0, self.recurrence)
            * max(0.0, self.strategic_value)
        )
        return benefit / max(0.01, self.estimated_effort * self.risk)


class VerificationPlan(BaseModel):
    metric: str
    baseline: float
    target: float
    direction: str = "at_most"
    minimum_observations: int = 1
    observations: list[float] = Field(default_factory=list)

    def verdict(self) -> bool | None:
        if len(self.observations) < self.minimum_observations:
            return None
        measured = sum(self.observations) / len(self.observations)
        return measured <= self.target if self.direction == "at_most" else measured >= self.target


class Improvement(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:10])
    title: str
    problem: str
    evidence: list[Evidence]
    component: str
    category: str = "runtime"
    factors: PriorityFactors = Field(default_factory=PriorityFactors)
    source: str
    created_by: str
    status: ImprovementStatus = ImprovementStatus.PROPOSED
    success_criteria: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    related_items: list[str] = Field(default_factory=list)
    work_item_ids: list[str] = Field(default_factory=list)
    verification: VerificationPlan | None = None
    rejection_reason: str = ""
    review_verdict: ReviewVerdict | None = None
    validation_passed: bool | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def _require_evidence(self) -> Improvement:
        if not self.evidence:
            raise ValueError("an improvement requires evidence")
        return self

    @property
    def fingerprint(self) -> str:
        normalized = " ".join(
            f"{self.component} {self.category} {self.problem}".lower().split()
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


class ImprovementBacklog:
    def __init__(self) -> None:
        self.items: dict[str, Improvement] = {}
        self.work_items: dict[str, WorkItem] = {}

    def add(self, improvement: Improvement) -> Improvement:
        duplicate = next(
            (item for item in self.items.values() if item.fingerprint == improvement.fingerprint),
            None,
        )
        if duplicate:
            known = {(e.kind, e.reference, e.source) for e in duplicate.evidence}
            duplicate.evidence.extend(
                e for e in improvement.evidence if (e.kind, e.reference, e.source) not in known
            )
            duplicate.updated_at = now_utc()
            return duplicate
        self.items[improvement.id] = improvement
        return improvement

    def ready(self) -> list[Improvement]:
        return sorted(
            (item for item in self.items.values() if item.status is ImprovementStatus.READY),
            key=lambda item: (-item.factors.score, item.created_at, item.id),
        )

    def dump(self) -> dict[str, Any]:
        return {
            "items": [item.model_dump(mode="json") for item in self.items.values()],
            "work_items": [item.model_dump(mode="json") for item in self.work_items.values()],
        }

    @classmethod
    def load(cls, payload: object) -> ImprovementBacklog:
        backlog = cls()
        if not isinstance(payload, dict):
            return backlog
        for raw in payload.get("items", []):
            item = Improvement.model_validate(raw)
            backlog.items[item.id] = item
        for raw in payload.get("work_items", []):
            work = WorkItem.model_validate(raw)
            backlog.work_items[work.id] = work
        return backlog

    def json(self) -> str:
        return json.dumps(self.dump(), sort_keys=True)


class ImprovementScout:
    """Turns deterministic runtime evidence into proposals; never edits code."""

    def from_event(self, event: Event) -> Improvement | None:
        if event.type not in {EventType.AGENT_STALLED, EventType.TASK_FAILED}:
            return None
        reason = str(event.payload.get("reason") or event.type.value)
        return Improvement(
            title=f"Prevent recurring {event.type.value.replace('_', ' ')}",
            problem=reason,
            evidence=[
                Evidence(
                    kind="runtime_event",
                    reference=f"{event.type.value}:{event.goal_id}",
                    value=event.payload,
                    source=event.source,
                )
            ],
            component=str(event.payload.get("component") or "runtime"),
            factors=PriorityFactors(recurrence=float(event.payload.get("repeats", 1))),
            source="runtime",
            created_by="improvement_scout",
            success_criteria=["The evidenced failure no longer repeats beyond its threshold."],
        )


class ImprovementTriage:
    def triage(self, improvement: Improvement) -> ImprovementStatus:
        if not improvement.evidence:
            improvement.status = ImprovementStatus.REJECTED
            improvement.rejection_reason = "no evidence"
        elif not improvement.success_criteria:
            improvement.status = ImprovementStatus.BLOCKED
            improvement.rejection_reason = "success criteria required"
        else:
            improvement.status = ImprovementStatus.READY
        improvement.updated_at = now_utc()
        return improvement.status


class ImprovementCoordinator:
    def __init__(
        self,
        backlog: ImprovementBacklog,
        *,
        max_active_improvements: int = 1,
        max_active_work_items: int = 3,
    ) -> None:
        self.backlog = backlog
        self.max_active_improvements = max_active_improvements
        self.max_active_work_items = max_active_work_items

    def activate_next(self) -> Improvement | None:
        active = sum(
            item.status is ImprovementStatus.ACTIVE for item in self.backlog.items.values()
        )
        if active >= self.max_active_improvements:
            return None
        for item in self.backlog.ready():
            if all(
                self.backlog.items.get(dependency)
                and self.backlog.items[dependency].status is ImprovementStatus.VERIFIED
                for dependency in item.dependencies
            ):
                item.status = ImprovementStatus.ACTIVE
                item.updated_at = now_utc()
                return item
        return None

    def create_work_item(
        self,
        improvement: Improvement,
        objective: str,
        *,
        capabilities: list[str],
        budget: WorkBudget | None = None,
    ) -> WorkItem | None:
        active = sum(
            item.status in {WorkStatus.ASSIGNED, WorkStatus.ACTIVE}
            for item in self.backlog.work_items.values()
        )
        if active >= self.max_active_work_items:
            improvement.status = ImprovementStatus.BLOCKED
            improvement.rejection_reason = "work-in-progress limit reached"
            return None
        work = WorkItem(
            parent_goal_id=improvement.id,
            improvement_id=improvement.id,
            objective=objective,
            required_capabilities=capabilities,
            success_conditions=list(improvement.success_criteria),
            budget=budget or WorkBudget(),
        )
        self.backlog.work_items[work.id] = work
        improvement.work_item_ids.append(work.id)
        return work

    def record_review(
        self, improvement: Improvement, verdict: ReviewVerdict
    ) -> None:
        improvement.review_verdict = verdict
        improvement.updated_at = now_utc()

    def refresh(self, improvement: Improvement) -> ImprovementStatus:
        work = [
            self.backlog.work_items[item_id]
            for item_id in improvement.work_item_ids
            if item_id in self.backlog.work_items
        ]
        if any(item.status is WorkStatus.FAILED for item in work):
            improvement.status = ImprovementStatus.NEEDS_HUMAN
            improvement.rejection_reason = "a work item exhausted its attempt budget"
            improvement.updated_at = now_utc()
        return improvement.status

    def record_validation(self, improvement: Improvement, *, passed: bool) -> None:
        improvement.validation_passed = passed
        improvement.updated_at = now_utc()

    def begin_verification(self, improvement: Improvement) -> bool:
        work = [self.backlog.work_items[item_id] for item_id in improvement.work_item_ids]
        ready = (
            bool(work)
            and all(item.status is WorkStatus.COMPLETED for item in work)
            and improvement.review_verdict is ReviewVerdict.COMPLETE
            and improvement.validation_passed is True
        )
        if ready:
            improvement.status = ImprovementStatus.VERIFYING
            improvement.updated_at = now_utc()
        return ready

    def observe(self, improvement: Improvement, value: float) -> ImprovementStatus:
        if improvement.verification is None:
            improvement.status = ImprovementStatus.VERIFIED
            return improvement.status
        improvement.verification.observations.append(value)
        verdict = improvement.verification.verdict()
        if verdict is not None:
            improvement.status = (
                ImprovementStatus.VERIFIED if verdict else ImprovementStatus.INEFFECTIVE
            )
        improvement.updated_at = now_utc()
        return improvement.status
