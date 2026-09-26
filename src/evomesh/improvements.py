"""Evidence-backed improvement backlog and bounded coordination policy."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from evomesh.contracts import now_utc
from evomesh.coordination import WorkBudget, WorkItem, WorkStatus
from evomesh.events import Event, EventType

logger = logging.getLogger(__name__)

SOURCE_FILE = re.compile(r"src/evomesh/[A-Za-z0-9_]+\.py")

# Evidence kinds a candidate improvement can come from.
EVIDENCE_FAILING_TESTS = "failing_tests"
EVIDENCE_RUNTIME_FAULT = "runtime_fault"
EVIDENCE_HUMAN_BACKLOG = "human_backlog"
EVIDENCE_RUNTIME_EVENT = "runtime_event"
# The evolver's own fallbacks, once nothing evidenced is ready.
EVIDENCE_BACKLOG_EXHAUSTED = "backlog_exhausted"
EVIDENCE_CODEBASE_ANALYSIS = "codebase_analysis"
EVIDENCE_HUMAN_REQUEST = "human_request"
# Improvement.source of a proposal the scout made from a runtime event.
RUNTIME_SOURCE = "runtime"
# Improvement.source of something a job noticed outside its own objective.
DISCOVERY_SOURCE = "discovery"
# A discovery is worked once a second, independent job reports it too
# (or a human releases it): one model's aside is not evidence enough.
DISCOVERY_READY_OCCURRENCES = 2
# Sources that no file-level evidence backs: measured by recurrence.
RECURRENCE_SOURCES = frozenset({RUNTIME_SOURCE, DISCOVERY_SOURCE})

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
    # HTTP failures from a model endpoint (found live: a 404 for a model
    # Ollama does not have became a READY "improvement" for the evolver).
    "httpstatuserror",
    "not found for url",
    "/api/",
    "rate limit",
    "502 bad gateway",
    "503 service",
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


@dataclass(frozen=True)
class Observation:
    """One real reading by one observer (closure plan 15.3). ``covers`` is
    the evidence kinds it can speak to, ``eligible`` how many eligible
    requests or probes it actually processed; ``values`` optionally measures
    specific evidence refs (else: 0.0 when the evidence is gone, 1.0 when it
    is still present).

    ``targets`` is which evidence refs the reading actually exercised. None
    means every ref of the kinds it covers (a suite run re-runs every failing
    test). A set means only those: a log that shows the mesh ran, but not
    that the repaired path ran, is no evidence the fault is gone -- only
    that it came back, when it did."""

    observation_id: str
    observer_id: str
    covers: frozenset[str]
    eligible: int = 1
    healthy: bool = True
    values: Mapping[str, float] = field(default_factory=dict)
    targets: frozenset[str] | None = None


class VerificationPlan(BaseModel):
    metric: str
    baseline: float
    target: float
    direction: str = "at_most"
    minimum_observations: int = 1
    observations: list[float] = Field(default_factory=list)
    # One id per observation that counted: the same reading twice is one.
    observation_ids: list[str] = Field(default_factory=list)
    observers: list[str] = Field(default_factory=list)

    def record(self, observation_id: str, observer_id: str, value: float) -> bool:
        if observation_id in self.observation_ids:
            return False
        self.observation_ids.append(observation_id)
        self.observations.append(value)
        if observer_id not in self.observers:
            self.observers.append(observer_id)
        return True

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
    # The epic this improvement belongs to (Epic -> Improvement -> WorkItem).
    epic: str = ""
    # Its open stages, in order, when it is too big for one work item (a
    # backlog item with several steps): each becomes a WorkItem that depends
    # on the one before it. Empty for simple work, which stays one item.
    work_plan: list[str] = Field(default_factory=list)
    occurrences: int = 1
    last_seen_at: datetime = Field(default_factory=now_utc)
    verification_started_at: datetime | None = None
    rejection_reason: str = ""
    review_verdict: ReviewVerdict | None = None
    validation_passed: bool | None = None
    # The candidate revision each verdict judged: a verdict on one diff is
    # not carried over to another (closure plan 15.2).
    review_revision: str = ""
    validation_revision: str = ""
    # Why a VERIFYING item is not decided yet, in words a human can act on.
    inconclusive_reason: str = ""
    # A trusted operator's verification, when no observer can give one.
    verified_by: str = ""
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
        if error := str(event.payload.get("error") or ""):
            # A stall's own reason is only "same failure repeated N times";
            # the failure is what says whether it is the code's problem.
            reason = f"{reason}: {' '.join(error.split())[:200]}"
        agent = event.agent_id or event.source
        return Improvement(
            source_ref=f"{event.type.value}:{agent}:{' '.join(reason.lower().split())}",
            title=f"Prevent recurring {event.type.value.replace('_', ' ')}",
            problem=reason,
            evidence=[
                Evidence(
                    kind=EVIDENCE_RUNTIME_EVENT,
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
        elif (
            improvement.source == DISCOVERY_SOURCE
            and improvement.occurrences < DISCOVERY_READY_OCCURRENCES
        ):
            improvement.status = ImprovementStatus.TRIAGED
            improvement.rejection_reason = (
                "reported once; ready when another job reports it or a human releases it"
            )
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

    def record_review(
        self, improvement: Improvement, verdict: ReviewVerdict, revision: str = ""
    ) -> None:
        improvement.review_verdict = verdict
        improvement.review_revision = revision
        improvement.updated_at = now_utc()

    def refresh(self, improvement: Improvement) -> ImprovementStatus:
        work = [
            self.backlog.work_items[item_id]
            for item_id in improvement.work_item_ids
            if item_id in self.backlog.work_items
        ]
        if any(item.status in {WorkStatus.FAILED, WorkStatus.NEEDS_HUMAN} for item in work):
            improvement.status = ImprovementStatus.NEEDS_HUMAN
            improvement.rejection_reason = "a work item exhausted its attempt budget"
            improvement.updated_at = now_utc()
        return improvement.status

    def record_validation(
        self, improvement: Improvement, *, passed: bool, revision: str = ""
    ) -> None:
        improvement.validation_passed = passed
        improvement.validation_revision = revision
        if (
            revision
            and improvement.review_revision
            and improvement.review_revision != revision
        ):
            # The candidate changed since it was reviewed: that review is stale.
            improvement.review_verdict = None
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
        # A cancelled required item is not success; only one waived with a
        # recorded reason (its stage left the evidence) may be skipped.
        settled = all(
            item.status is WorkStatus.COMPLETED
            or (item.status is WorkStatus.CANCELLED and _waived(item))
            for item in work
        )
        same_revision = (
            not require_review
            or not improvement.review_revision
            or not improvement.validation_revision
            or improvement.review_revision == improvement.validation_revision
        )
        ready = (
            bool(work)
            and settled
            and any(item.status is WorkStatus.COMPLETED for item in work)
            and (not require_review or improvement.review_verdict is ReviewVerdict.COMPLETE)
            and improvement.validation_passed is True
            and same_revision
        )
        if ready:
            improvement.status = ImprovementStatus.VERIFYING
            improvement.verification_started_at = now_utc()
            improvement.updated_at = now_utc()
        return ready

    def observe(
        self, improvement: Improvement, observation: Observation | None, value: float
    ) -> ImprovementStatus:
        """Count one real reading toward verification, or say why it does
        not count. Never VERIFIED for want of a plan, an observer or data."""
        reason = _unusable(improvement, observation, present=value > 0)
        if reason or observation is None:
            improvement.inconclusive_reason = reason or "no observation"
            return improvement.status
        plan = improvement.verification
        assert plan is not None
        if not plan.record(observation.observation_id, observation.observer_id, value):
            return improvement.status  # the same reading, already counted
        verdict = plan.verdict()
        if verdict is None:
            improvement.inconclusive_reason = (
                f"{len(plan.observation_ids)} of {plan.minimum_observations} observations"
            )
        else:
            improvement.status = (
                ImprovementStatus.VERIFIED if verdict else ImprovementStatus.INEFFECTIVE
            )
            improvement.inconclusive_reason = ""
        improvement.updated_at = now_utc()
        return improvement.status

    def verify_by_operator(self, improvement: Improvement, actor: str, reason: str) -> None:
        """A human's verification, recorded as such -- for work no observer
        can measure, such as a feature checked against its acceptance."""
        if not actor or actor.startswith(("model", "agent:")):
            raise ValueError("verification needs a trusted operator identity")
        if improvement.status is not ImprovementStatus.VERIFYING:
            raise ValueError(f"{improvement.id} is {improvement.status.value}, not verifying")
        improvement.status = ImprovementStatus.VERIFIED
        improvement.verified_by = f"{actor}: {reason}"[:300]
        improvement.inconclusive_reason = ""
        improvement.updated_at = now_utc()


def _waived(work: WorkItem) -> bool:
    return any(entry.startswith("waived:") for entry in work.failure_history)


def _unusable(
    improvement: Improvement, observation: Observation | None, *, present: bool = False
) -> str:
    if improvement.verification is None:
        return "no verification plan: a human has to verify it"
    if observation is None:
        return f"no eligible observer for {improvement.source} evidence reported"
    if not observation.healthy:
        return f"observer {observation.observer_id} is unhealthy"
    if improvement.source not in observation.covers:
        return f"no eligible observer for {improvement.source} evidence"
    if observation.eligible <= 0:
        return f"observer {observation.observer_id} processed no eligible requests"
    if (
        observation.targets is not None
        and improvement.source_ref not in observation.targets
        and not present
    ):
        # Seeing the problem again is target-specific by definition; not
        # seeing it, from an observer that cannot show the path ran, is not.
        return (
            f"observer {observation.observer_id} cannot show {improvement.source_ref} "
            "was exercised; verify it by hand (/improvements verify <id> <reason>)"
        )
    return ""


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
    # Its open stages in order, when it is more than one piece of work.
    stages: tuple[str, ...] = ()

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
            work_plan=list(self.stages),
            success_criteria=[f"The {self.kind.replace('_', ' ')} evidence is gone after deploy."],
            verification=VerificationPlan(
                metric="evidence_present",
                baseline=1.0,
                target=0.0,
                minimum_observations=self.observations,
            ),
        )


EVOLUTION_OUTCOME_PROMOTED = "promoted"


class WorkOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class WorkState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # The executor cannot say: kept visible, never read as success.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExecutionScope:
    """What one piece of work may use: the routed assignee whose identity
    its jobs run under, the isolated workspace it may change, and the
    executor's own reference for what it opened there."""

    assignee: str
    workspace: str
    reference: str


@dataclass
class WorkInspection:
    state: WorkState
    evidence: dict[str, Any] = field(default_factory=dict)


# Where a work item keeps its handle ("generation": from before the seam).
HANDLE_INPUTS = frozenset({"handle", "generation"})
# A durable handle is plain strings, stored on the work item itself, so a
# restart recovers it with the backlog and nothing is held in memory.
WorkHandle = dict[str, str]


class WorkExecutor(Protocol):
    """What carries out an improvement's work item (closure plan 18.2). The
    control plane decides what is worked and judges the result; an executor
    starts it, reports how it stands, and cancels it. The generation
    pipeline is one executor (evolution.GenerationExecutor), not an
    architectural dependency."""

    kind: str

    async def submit(self, work: WorkItem, scope: ExecutionScope) -> WorkHandle: ...

    def inspect(self, handle: Mapping[str, str]) -> WorkInspection: ...

    async def request_cancel(self, handle: Mapping[str, str]) -> WorkInspection: ...


def work_handle(work: WorkItem) -> Mapping[str, str] | None:
    """The handle a work item was submitted under; a generation number from
    before the seam existed reads as a generation handle."""
    handle = work.inputs.get("handle")
    if isinstance(handle, dict):
        return {str(key): str(value) for key, value in handle.items()}
    number = work.inputs.get("generation")
    if isinstance(number, int):
        return {"executor": "generation", "ref": str(number)}
    return None


def _covering(observations: Sequence[Observation], source: str) -> Observation | None:
    """The healthy observation this pass that covers ``source`` evidence,
    else the first that covers it at all (so its fault can be reported)."""
    covering = [item for item in observations if source in item.covers]
    return next((item for item in covering if item.healthy), covering[0] if covering else None)


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

    async def propose_discovery(
        self, text: str, *, generation: int, job: int
    ) -> Improvement | None:
        """Scope creep, captured instead of acted on: a problem a job noticed
        outside its objective becomes a proposal, never an edit."""
        text = " ".join(text.split())
        if len(text) < 12:
            return None
        item = self.backlog.add(
            Improvement(
                title=text[:120],
                problem=text,
                evidence=[
                    Evidence(
                        kind=DISCOVERY_SOURCE,
                        reference=f"generation {generation} job {job}",
                        value=text,
                        source="harness",
                    )
                ],
                component=next(iter(SOURCE_FILE.findall(text)), "evomesh"),
                category=DISCOVERY_SOURCE,
                factors=PriorityFactors(confidence=0.5),
                source=DISCOVERY_SOURCE,
                created_by="harness job",
                source_ref=f"{DISCOVERY_SOURCE}:{text.lower()}",
                success_criteria=["No later job reports the same problem."],
                verification=VerificationPlan(
                    metric="re-reported", baseline=1.0, target=0.0, minimum_observations=3
                ),
            )
        )
        if item.status in {ImprovementStatus.PROPOSED, ImprovementStatus.TRIAGED}:
            self.triage.triage(item)
        await self.save()
        return item

    def set_dependency(self, improvement_id: str, depends_on: str) -> None:
        """``improvement_id`` waits until ``depends_on`` is VERIFIED; refused
        when either is unknown or the dependency would close a cycle."""
        items = self.backlog.items
        if improvement_id not in items or depends_on not in items:
            raise ValueError("both improvements must exist")
        seen: set[str] = set()
        pending = [depends_on]
        while pending:
            current = pending.pop()
            if current == improvement_id:
                raise ValueError("that dependency would make a cycle")
            if current not in seen:
                seen.add(current)
                pending.extend(items[current].dependencies if current in items else ())
        if depends_on not in items[improvement_id].dependencies:
            items[improvement_id].dependencies.append(depends_on)
            items[depends_on].related_items.append(improvement_id)

    async def sync(
        self,
        candidates: Sequence[Candidate],
        present: set[str],
        observations: Sequence[Observation] = (),
    ) -> None:
        """Merge what the evolver can pick now into the backlog, retire what
        is no longer evidenced, and count this pass's real observations.

        ``present`` is every piece of evidence there is now -- including
        targets set aside after too many attempts, which are not
        ``candidates`` but are certainly not fixed. A pass with no
        observation covering an item measures nothing for it.
        """
        present = present | {candidate.ref for candidate in candidates}
        for item in self.backlog.items.values():
            # Triage policy may have changed since a runtime proposal was
            # judged (a restart onto newer code): judge the waiting ones again.
            if item.source == RUNTIME_SOURCE and item.status in {
                ImprovementStatus.READY,
                ImprovementStatus.TRIAGED,
            }:
                self.triage.triage(item)
        for candidate in candidates:
            item = self.backlog.add(candidate.improvement())
            self._replan(item, candidate.stages)
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
            if item.source in RECURRENCE_SOURCES or not item.source_ref:
                continue
            gone = item.source_ref not in present
            if item.status is ImprovementStatus.VERIFYING:
                observation = _covering(observations, item.source)
                value = (
                    observation.values.get(item.source_ref, 0.0 if gone else 1.0)
                    if observation is not None
                    else 1.0
                )
                status = self.coordinator.observe(item, observation, value)
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
            if item.source in RECURRENCE_SOURCES and item.status is ImprovementStatus.VERIFYING:
                started = item.verification_started_at or item.updated_at
                recurred = item.last_seen_at > started
                await self._report(
                    item,
                    self.coordinator.observe(
                        item, _covering(observations, item.source), 1.0 if recurred else 0.0
                    ),
                )
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

    async def adopt(self, candidate: Candidate) -> Improvement | None:
        """Put work the evolver chose outside the ranked backlog -- a scout,
        a maintenance target, a human's own objective -- under the same
        lifecycle, budget and verification (B-009). ``None`` when that target
        already exhausted its budget and waits for a human."""
        item = self.backlog.add(candidate.improvement())
        if item.status is ImprovementStatus.NEEDS_HUMAN:
            return None
        active = self.active()
        if active is not None and active is not item:
            active.status = ImprovementStatus.READY
            active.updated_at = now_utc()
        item.status = ImprovementStatus.ACTIVE
        item.rejection_reason = ""
        item.updated_at = now_utc()
        await self.save()
        return item

    def choose(self) -> Improvement | None:
        """The improvement to work on next: one already active, else the
        best-scoring ready one whose dependencies are verified."""
        return self.active() or self.coordinator.activate_next()

    def _replan(self, improvement: Improvement, stages: tuple[str, ...]) -> None:
        """Keep the stage plan equal to the evidence: a stage that is no
        longer open (a human did it, or it landed) is not worked again."""
        if not stages and not improvement.work_plan:
            return
        improvement.work_plan = list(stages)
        for work in self._work_of(improvement):
            stage = work.inputs.get("stage")
            if (
                stage is not None
                and stage not in stages
                and work.status in {WorkStatus.PENDING, WorkStatus.BLOCKED}
            ):
                work.status = WorkStatus.CANCELLED
                work.failure_history.append(
                    "waived: the stage is no longer open in the evidence"
                )
                work.updated_at = now_utc()

    def _work_of(self, improvement: Improvement) -> list[WorkItem]:
        return [
            self.backlog.work_items[item_id]
            for item_id in improvement.work_item_ids
            if item_id in self.backlog.work_items
        ]

    def _plan_dag(self, improvement: Improvement) -> None:
        """One WorkItem per planned stage not yet represented, each
        depending on the one before it."""
        existing = {
            str(work.inputs.get("stage")): work
            for work in self._work_of(improvement)
            if work.inputs.get("stage") is not None and work.status is not WorkStatus.CANCELLED
        }
        previous: WorkItem | None = None
        for stage in improvement.work_plan:
            work = existing.get(stage)
            if work is None:
                work = WorkItem(
                    parent_goal_id=improvement.id,
                    improvement_id=improvement.id,
                    type="implementation",
                    objective=f"{improvement.title} [{stage}]",
                    required_capabilities=[CODE_EDIT_CAPABILITY],
                    success_conditions=list(improvement.success_criteria),
                    dependencies=[previous.id] if previous is not None else [],
                    inputs={"stage": stage},
                )
                self.backlog.work_items[work.id] = work
                improvement.work_item_ids.append(work.id)
            previous = work

    def ready_work(self, improvement: Improvement) -> WorkItem | None:
        """The first planned stage whose dependencies are done."""
        done = {WorkStatus.COMPLETED, WorkStatus.CANCELLED}
        for work in self._work_of(improvement):
            if work.status is not WorkStatus.PENDING:
                continue
            if all(
                dependency in self.backlog.work_items
                and self.backlog.work_items[dependency].status in done
                for dependency in work.dependencies
            ):
                return work
        return None

    async def begin(
        self,
        improvement: Improvement,
        *,
        objective: str,
        route: Callable[[WorkItem], str | None],
        executor: WorkExecutor,
        workspace: str,
        reference: str,
        stage: str | None = None,
    ) -> WorkItem | None:
        """A bounded work item, awarded by ``route`` (capability routing) and
        submitted to ``executor`` under the awarded agent's identity. A retry
        reuses the item and its budget. An improvement with a stage plan works
        the DAG: ``stage`` names the one this run does, else the first whose
        dependencies are done."""
        work: WorkItem | None = None
        if improvement.work_plan:
            self._plan_dag(improvement)
            work = next(
                (
                    item
                    for item in self._work_of(improvement)
                    if stage is not None
                    and item.inputs.get("stage") == stage
                    and item.status is WorkStatus.PENDING
                ),
                None,
            ) or self.ready_work(improvement)
        else:
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
        work.inputs = {
            key: value for key, value in work.inputs.items() if key not in HANDLE_INPUTS
        }
        agent = route(work)
        if agent is None:
            work.status = WorkStatus.BLOCKED
            improvement.status = ImprovementStatus.BLOCKED
            improvement.rejection_reason = "no agent has the capability to implement it"
        else:
            work.assign(agent)
            handle = await executor.submit(
                work, ExecutionScope(assignee=agent, workspace=workspace, reference=reference)
            )
            work.inputs = {**work.inputs, "handle": dict(handle)}
            work.status = WorkStatus.ACTIVE
        await self.save()
        return work

    async def cancel(
        self, improvement_id: str, executors: Mapping[str, WorkExecutor], reason: str
    ) -> list[WorkInspection]:
        """Stop an improvement's running work through its executor. A
        cancelled task is not success: the improvement waits, blocked."""
        item = self.backlog.items.get(improvement_id)
        if item is None:
            raise KeyError(improvement_id)
        results: list[WorkInspection] = []
        for work in self._work_of(item):
            if work.status is not WorkStatus.ACTIVE:
                continue
            handle = work_handle(work)
            executor = executors.get(handle["executor"]) if handle else None
            if handle is not None and executor is not None:
                results.append(await executor.request_cancel(handle))
            work.status = WorkStatus.CANCELLED
            work.failure_history.append(f"cancelled: {reason}")
            work.updated_at = now_utc()
        item.status = ImprovementStatus.BLOCKED
        item.rejection_reason = f"cancelled: {reason}"
        item.updated_at = now_utc()
        await self.save()
        return results

    async def record_validation(
        self, improvement_id: str, *, passed: bool, revision: str = ""
    ) -> None:
        if item := self.backlog.items.get(improvement_id):
            self.coordinator.record_validation(item, passed=passed, revision=revision)
            await self.save()

    async def record_review(
        self, improvement_id: str, verdict: ReviewVerdict, revision: str = ""
    ) -> None:
        if item := self.backlog.items.get(improvement_id):
            self.coordinator.record_review(item, verdict, revision)
            await self.save()

    async def verify(self, improvement_id: str, *, actor: str, reason: str) -> Improvement:
        item = self.backlog.items[improvement_id]
        self.coordinator.verify_by_operator(item, actor, reason)
        await self.save()
        await self._report(item, item.status)
        return item

    async def settle(
        self, executors: Mapping[str, WorkExecutor] | WorkExecutor, present: set[str]
    ) -> None:
        """Close the work items their executor has finished.

        Read from each executor's own record through the handle the work item
        carries, rather than hooked into each way a piece of work can end, so
        none of them can be missed -- and it survives a restart, because
        nothing here is held in memory.
        """
        by_kind = (
            dict(executors) if isinstance(executors, Mapping) else {executors.kind: executors}
        )
        for work in list(self.backlog.work_items.values()):
            if work.status is not WorkStatus.ACTIVE:
                continue
            handle = work_handle(work)
            executor = by_kind.get(handle["executor"]) if handle else None
            if handle is None or executor is None:
                continue
            inspection = executor.inspect(handle)
            if inspection.state in {WorkState.PENDING, WorkState.UNKNOWN}:
                continue
            item = self.backlog.items.get(work.improvement_id or "")
            if inspection.state is WorkState.COMPLETED:
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
            work.fail(f"{handle['executor']} {handle['ref']} {inspection.state.value}")
            work.inputs = {
                key: value for key, value in work.inputs.items() if key not in HANDLE_INPUTS
            }
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
        epics: dict[str, list[Improvement]] = {}
        for item in self.backlog.items.values():
            if item.epic:
                epics.setdefault(item.epic, []).append(item)
        for name, members in sorted(epics.items()):
            done = sum(item.status is ImprovementStatus.VERIFIED for item in members)
            lines.append(f"epic {name}: {done}/{len(members)} verified")
        for item in sorted(self.backlog.items.values(), key=lambda entry: -entry.factors.score)[
            :10
        ]:
            work = [
                self.backlog.work_items[wid]
                for wid in item.work_item_ids
                if wid in self.backlog.work_items
            ]
            active = [w for w in work if w.status is not WorkStatus.CANCELLED]
            completed = sum(1 for w in active if w.status is WorkStatus.COMPLETED)
            line = (
                f"  {item.id} [{item.status.value}] score {item.factors.score:.2f} "
                f"{item.title}"
                + (f" (after {', '.join(item.dependencies)})" if item.dependencies else "")
                + (f" -- {item.rejection_reason}" if item.rejection_reason else "")
                + (
                    f" -- not verified yet: {item.inconclusive_reason}"
                    if item.status is ImprovementStatus.VERIFYING and item.inconclusive_reason
                    else ""
                )
            )
            if work:
                line += f" work {completed}/{len(active)}"
            lines.append(line)
        return "\n".join(lines)
