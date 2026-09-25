"""Evidence-backed improvement backlog and bounded coordination policy."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from evomesh.contracts import now_utc
from evomesh.coordination import WorkBudget, WorkItem, WorkStatus
from evomesh.events import Event, EventType

logger = logging.getLogger(__name__)

# Evidence kinds a candidate improvement can come from.
EVIDENCE_FAILING_TESTS = "failing_tests"
EVIDENCE_RUNTIME_FAULT = "runtime_fault"
EVIDENCE_HUMAN_BACKLOG = "human_backlog"
EVIDENCE_RUNTIME_EVENT = "runtime_event"
# Improvement.source of a proposal the scout made from a runtime event.
RUNTIME_SOURCE = "runtime"

# A runtime-event proposal becomes workable only once it has recurred this
# often: one stall is weather, three of the same is a problem.
RUNTIME_EVENT_READY_OCCURRENCES = 3
# Failures that are the machine's, not the code's: never an improvement.
ENVIRONMENTAL_MARKERS = (
    "provider",
    "unavailable",
    "timed out",
    "timeout",
    "connection",
    "not running",
)
# The capability an evolution work item is routed by.
CODE_EDIT_CAPABILITY = "code.edit"
# Why a still-evidenced improvement was passed over this time.
NOT_PICKABLE_NOW = "evidenced, but set aside after recent attempts"


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
    # What produced it (a failing-test set, a logged fault, a backlog item's
    # title, a runtime event): its identity across proposals, and what
    # verification checks is gone.
    source_ref: str = ""
    occurrences: int = 1
    last_seen_at: datetime = Field(default_factory=now_utc)
    verification_started_at: datetime | None = None
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
        identity = self.source_ref or f"{self.component} {self.category} {self.problem}"
        normalized = " ".join(identity.lower().split())
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
            duplicate.occurrences += 1
            duplicate.last_seen_at = now_utc()
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
        agent = event.agent_id or event.source
        return Improvement(
            source_ref=f"{event.type.value}:{agent}:{' '.join(reason.lower().split())}",
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
            source=RUNTIME_SOURCE,
            created_by="improvement_scout",
            verification=VerificationPlan(
                metric="recurrence", baseline=1.0, target=0.0, minimum_observations=3
            ),
            success_criteria=["The evidenced failure no longer repeats beyond its threshold."],
        )


class ImprovementTriage:
    def triage(self, improvement: Improvement) -> ImprovementStatus:
        from_runtime = improvement.source == RUNTIME_SOURCE
        if not improvement.evidence:
            improvement.status = ImprovementStatus.REJECTED
            improvement.rejection_reason = "no evidence"
        elif not improvement.success_criteria:
            improvement.status = ImprovementStatus.BLOCKED
            improvement.rejection_reason = "success criteria required"
        elif from_runtime and any(
            marker in improvement.problem.lower() for marker in ENVIRONMENTAL_MARKERS
        ):
            improvement.status = ImprovementStatus.REJECTED
            improvement.rejection_reason = "environmental, not a code problem"
        elif from_runtime and improvement.occurrences < RUNTIME_EVENT_READY_OCCURRENCES:
            improvement.status = ImprovementStatus.TRIAGED
            improvement.rejection_reason = (
                f"seen {improvement.occurrences} time(s); ready at "
                f"{RUNTIME_EVENT_READY_OCCURRENCES}"
            )
        else:
            improvement.status = ImprovementStatus.READY
            improvement.rejection_reason = ""
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

    def record_review(self, improvement: Improvement, verdict: ReviewVerdict) -> None:
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

    def begin_verification(self, improvement: Improvement, *, require_review: bool = True) -> bool:
        """Only work that is all completed, independently reviewed as
        complete and deterministically validated is measured -- never marked
        verified on the implementer's word."""
        work = [
            self.backlog.work_items[item_id]
            for item_id in improvement.work_item_ids
            if item_id in self.backlog.work_items
        ]
        ready = (
            bool(work)
            and all(item.status in {WorkStatus.COMPLETED, WorkStatus.CANCELLED} for item in work)
            and any(item.status is WorkStatus.COMPLETED for item in work)
            and (not require_review or improvement.review_verdict is ReviewVerdict.COMPLETE)
            and improvement.validation_passed is True
        )
        if ready:
            improvement.status = ImprovementStatus.VERIFYING
            improvement.verification_started_at = now_utc()
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


@dataclass(frozen=True)
class Candidate:
    """One piece of evidence the mesh can act on, as a proposal.

    ``ref`` is the stable identity of its source (the same failing tests,
    fault or backlog item every time it is seen), so re-proposing it merges
    into the existing improvement instead of creating a new one.
    """

    ref: str
    kind: str
    title: str
    problem: str
    component: str
    evidence: object
    factors: PriorityFactors
    # How many opens after deployment the evidence must stay gone.
    observations: int = 1

    def improvement(self) -> Improvement:
        return Improvement(
            title=self.title,
            problem=self.problem,
            evidence=[
                Evidence(kind=self.kind, reference=self.ref, value=self.evidence, source="evolver")
            ],
            component=self.component,
            category=self.kind,
            factors=self.factors,
            source=self.kind,
            created_by="improvement_scout",
            source_ref=self.ref,
            success_criteria=[f"The {self.kind.replace('_', ' ')} evidence is gone after deploy."],
            verification=VerificationPlan(
                metric="evidence_present",
                baseline=1.0,
                target=0.0,
                minimum_observations=self.observations,
            ),
        )


EVOLUTION_OUTCOME_PROMOTED = "promoted"
EVOLUTION_OUTCOME_DISCARDED = "discarded"


class ImprovementControl:
    """The backlog as the control plane of self-improvement.

    Observe -> evidence -> backlog -> prioritize -> delegate -> implement ->
    independently review -> validate -> deploy -> measure. The evolver's
    candidate pipeline stays the execution mechanism; this decides what it
    works on, bounds how often, and judges whether the result worked.
    """

    def __init__(
        self,
        backlog: ImprovementBacklog,
        coordinator: ImprovementCoordinator,
        triage: ImprovementTriage,
        scout: ImprovementScout,
        *,
        save: Callable[[], Awaitable[None]],
        announce: Callable[[str], Awaitable[None]] | None = None,
        require_review: bool = True,
    ) -> None:
        self.backlog = backlog
        self.coordinator = coordinator
        self.triage = triage
        self.scout = scout
        self.save = save
        self.announce = announce
        self.require_review = require_review

    # -- evidence ---------------------------------------------------------

    async def propose_from_event(self, event: Event) -> Improvement | None:
        proposal = self.scout.from_event(event)
        if proposal is None:
            return None
        item = self.backlog.add(proposal)
        if item.status in {
            ImprovementStatus.PROPOSED,
            ImprovementStatus.TRIAGED,
        }:
            self.triage.triage(item)
        await self.save()
        return item

    async def sync(self, candidates: Sequence[Candidate], present: set[str]) -> None:
        """Merge what the evolver can pick now into the backlog, retire what
        is no longer evidenced, and take one verification reading.

        ``present`` is every piece of evidence there is now -- including
        targets set aside after too many attempts, which are not
        ``candidates`` but are certainly not fixed.
        """
        present = present | {candidate.ref for candidate in candidates}
        for candidate in candidates:
            item = self.backlog.add(candidate.improvement())
            if item.status is ImprovementStatus.PROPOSED:
                self.triage.triage(item)
            elif item.status is ImprovementStatus.VERIFIED:
                # The problem it fixed is back: a regression, worked again.
                item.status = ImprovementStatus.READY
                item.rejection_reason = "regressed after verification"
            elif (
                item.status is ImprovementStatus.REJECTED
                and item.rejection_reason.startswith("evidence")
            ) or (
                item.status is ImprovementStatus.BLOCKED
                and item.rejection_reason == NOT_PICKABLE_NOW
            ):
                item.status = ImprovementStatus.READY
                item.rejection_reason = ""
        for item in list(self.backlog.items.values()):
            if item.source == RUNTIME_SOURCE or not item.source_ref:
                continue
            gone = item.source_ref not in present
            if item.status is ImprovementStatus.VERIFYING:
                status = self.coordinator.observe(item, 0.0 if gone else 1.0)
                await self._report(item, status)
            elif gone and item.status in {
                ImprovementStatus.READY,
                ImprovementStatus.TRIAGED,
                ImprovementStatus.ACTIVE,
            }:
                item.status = ImprovementStatus.REJECTED
                item.rejection_reason = "evidence no longer present"
                item.updated_at = now_utc()
        for item in self.backlog.items.values():
            if item.source == RUNTIME_SOURCE and item.status is ImprovementStatus.VERIFYING:
                started = item.verification_started_at or item.updated_at
                recurred = item.last_seen_at > started
                await self._report(item, self.coordinator.observe(item, 1.0 if recurred else 0.0))
        await self.save()

    # -- delegation -------------------------------------------------------

    def active(self) -> Improvement | None:
        return next(
            (
                item
                for item in self.backlog.items.values()
                if item.status is ImprovementStatus.ACTIVE
            ),
            None,
        )

    def choose(self) -> Improvement | None:
        """The improvement to work on next: one already active, else the
        best-scoring ready one whose dependencies are verified."""
        return self.active() or self.coordinator.activate_next()

    async def begin(
        self,
        improvement: Improvement,
        *,
        objective: str,
        generation: int,
        route: Callable[[WorkItem], str | None],
    ) -> WorkItem | None:
        """A bounded work item for one generation, awarded by ``route``
        (capability routing). A retry reuses the item and its budget."""
        work = next(
            (
                self.backlog.work_items[item_id]
                for item_id in reversed(improvement.work_item_ids)
                if item_id in self.backlog.work_items
                and self.backlog.work_items[item_id].status is WorkStatus.PENDING
            ),
            None,
        )
        if work is None:
            work = self.coordinator.create_work_item(
                improvement, objective, capabilities=[CODE_EDIT_CAPABILITY]
            )
            if work is None:
                await self.save()
                return None
        work.objective = objective
        work.inputs = {**work.inputs, "generation": generation}
        agent = route(work)
        if agent is None:
            work.status = WorkStatus.BLOCKED
            improvement.status = ImprovementStatus.BLOCKED
            improvement.rejection_reason = "no agent has the capability to implement it"
        else:
            work.assign(agent)
            work.status = WorkStatus.ACTIVE
        await self.save()
        return work

    async def record_validation(self, improvement_id: str, *, passed: bool) -> None:
        if item := self.backlog.items.get(improvement_id):
            self.coordinator.record_validation(item, passed=passed)
            await self.save()

    async def record_review(self, improvement_id: str, verdict: ReviewVerdict) -> None:
        if item := self.backlog.items.get(improvement_id):
            self.coordinator.record_review(item, verdict)
            await self.save()

    async def settle(self, outcome: Callable[[int], str | None], present: set[str]) -> None:
        """Close the work items whose generation has been decided.

        Read from the generation's recorded outcome rather than hooked into
        each way a generation can end, so none of them can be missed.
        """
        for work in list(self.backlog.work_items.values()):
            if work.status is not WorkStatus.ACTIVE or "generation" not in work.inputs:
                continue
            result = outcome(int(work.inputs["generation"]))
            if result is None:
                continue
            item = self.backlog.items.get(work.improvement_id or "")
            if result == EVOLUTION_OUTCOME_PROMOTED:
                work.status = WorkStatus.COMPLETED
                work.updated_at = now_utc()
                if item is None:
                    continue
                if item.source_ref and item.source_ref in present:
                    # Landed, and there is more of it (the next step of a
                    # backlog item): back in line, not yet measurable.
                    item.status = ImprovementStatus.READY
                    item.review_verdict = None
                    item.validation_passed = None
                elif not self.coordinator.begin_verification(
                    item, require_review=self.require_review
                ):
                    item.status = ImprovementStatus.READY
                item.updated_at = now_utc()
                continue
            work.fail(f"generation {work.inputs['generation']} {result}")
            work.inputs = {key: value for key, value in work.inputs.items() if key != "generation"}
            if item is None:
                continue
            item.review_verdict = None
            item.validation_passed = None
            if self.coordinator.refresh(item) is ImprovementStatus.NEEDS_HUMAN:
                await self._report(item, ImprovementStatus.NEEDS_HUMAN)
            else:
                item.status = ImprovementStatus.READY
        await self.save()

    async def _report(self, item: Improvement, status: ImprovementStatus) -> None:
        if status not in {
            ImprovementStatus.VERIFIED,
            ImprovementStatus.INEFFECTIVE,
            ImprovementStatus.NEEDS_HUMAN,
        }:
            return
        text = f"Improvement {item.id} '{item.title}' is {status.value}"
        if status is ImprovementStatus.NEEDS_HUMAN:
            text += f": {item.rejection_reason}"
        logger.info(text)
        if self.announce is not None:
            await self.announce(text)

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for item in self.backlog.items.values():
            counts[item.status.value] = counts.get(item.status.value, 0) + 1
        lines = [", ".join(f"{key}: {value}" for key, value in sorted(counts.items())) or "empty"]
        for item in sorted(self.backlog.items.values(), key=lambda entry: -entry.factors.score)[
            :10
        ]:
            lines.append(
                f"  {item.id} [{item.status.value}] score {item.factors.score:.2f} "
                f"{item.title}" + (f" -- {item.rejection_reason}" if item.rejection_reason else "")
            )
        return "\n".join(lines)
