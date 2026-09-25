"""Typed procedure runtime: admission registry, core adapters, executor.

Architecture closure plan v2, sections 9-14. The executor advances exactly one
step per call and never picks a goal, runs a loop or calls a model except in
an explicit cognitive step. Every tool operation is journaled before dispatch
under a logical key; its settlement and the execution cursor move together in
one transaction; an operation whose effect is uncertain blocks the execution
until it is reconciled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from evomesh.cognitive_services import CognitiveServiceType, ModelInvocationReason
from evomesh.contracts import now_utc
from evomesh.coordination import WorkItem
from evomesh.harness_tools import ToolContext, ToolDenied, _permit, _resolve
from evomesh.procedures import (
    DEFAULT_LIMITS,
    OPAQUE_OBJECT,
    AdapterContract,
    AwaitStep,
    BindingError,
    BranchStep,
    Catalog,
    CheckContract,
    CognitiveStep,
    CompleteStep,
    DelegateStep,
    Issue,
    OutputContract,
    PredicateError,
    ProcedureDefinition,
    ProcedureError,
    ProcedureLimits,
    RetrySemantics,
    SideEffect,
    ToolStep,
    ValidateStep,
    canonical_json,
    digest_of,
    evaluate,
    resolve,
    schema_errors,
    validate_definition,
)
from evomesh.storage import SQLiteRepository

logger = logging.getLogger(__name__)

# Identifies this process's claims: a DISPATCHING operation with another
# epoch was claimed by a process that is gone.
PROCESS_EPOCH = uuid.uuid4().hex
# Executions some advance() in this process is inside right now, across every
# executor instance. One step at a time per execution: a second advancer gets
# "busy" instead of mistaking a live claim for a crashed one and recovering it.
_ADVANCING: set[str] = set()
RECEIPTS_DIR = ".evomesh-receipts"
MAX_JSON_READ_BYTES = 256 * 1024
RECONCILIATION_DECISIONS = frozenset({"recheck", "not_applied", "fail"})
# Failures that show the procedure itself is wrong, not its environment.
PROCEDURE_DEFECTS = frozenset({"VALIDATION_FAILED", "OUTPUT_SCHEMA_INVALID"})
# Beside the shipped definitions: the reviewed approvals, bound to digests.
ADMISSIONS_FILE = "admissions.json"


# -- admission -------------------------------------------------------------------


class AdmissionStatus(StrEnum):
    INVALID = "invalid"  # quarantined with its validation report
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    PROMOTED = "promoted"
    DEGRADED = "degraded"
    RETIRED = "retired"


class Admission(BaseModel):
    """Trusted metadata, stored apart from the immutable content."""

    procedure_id: str
    revision: int
    digest: str = ""
    status: AdmissionStatus
    source: str = "authored"
    owner: str = ""
    issues: list[dict[str, str]] = Field(default_factory=list)
    approved_by: str = ""
    approved_at: datetime | None = None
    approved_goal_kinds: list[str] = Field(default_factory=list)
    adapter_contracts: dict[str, int] = Field(default_factory=dict)
    reason: str = ""
    history: list[dict[str, str]] = Field(default_factory=list)
    raw: dict[str, Any] | None = None  # quarantined content of an invalid submission

    @property
    def key(self) -> str:
        return f"{self.procedure_id}@{self.revision}"

    def note(self, event: str, detail: str = "") -> None:
        self.history.append({"at": now_utc().isoformat(), "event": event, "detail": detail})


class Selection(StrEnum):
    MATCH = "match"
    NO_MATCH = "no_match"
    INCOMPATIBLE_PROCEDURE = "incompatible_procedure"
    INVALID_DEFINITION = "invalid_definition"
    PERMISSION_DENIED = "permission_denied"
    APPROVAL_REQUIRED = "approval_required"
    BUDGET_EXHAUSTED = "budget_exhausted"
    UNRESOLVED_EFFECT = "unresolved_effect"
    POSTCONDITION_FAILED = "postcondition_failed"


@dataclass
class MatchResult:
    selection: Selection
    definition: ProcedureDefinition | None = None
    admission: Admission | None = None
    reasons: list[str] = field(default_factory=list)


class RegistryError(ProcedureError):
    pass


def _read_shipped(
    directory: Path,
) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]], list[str]]:
    """Shipped definition files and the approval manifest, read as JSON."""
    shipped: list[tuple[str, dict[str, Any]]] = []
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    if not directory.is_dir():
        return shipped, entries, problems
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        if path.name == ADMISSIONS_FILE:
            items = raw if isinstance(raw, list) else []
            entries = [item for item in items if isinstance(item, dict)]
        elif isinstance(raw, dict):
            shipped.append((path.name, raw))
        else:
            problems.append(f"{path.name}: not a JSON object")
    return shipped, entries, problems


class ProcedureRegistry:
    """Definition revisions and their admission. Validates, looks up and
    explains; it never schedules."""

    def __init__(
        self,
        repository: SQLiteRepository,
        catalog: Catalog,
        limits: ProcedureLimits = DEFAULT_LIMITS,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.limits = limits
        self.definitions: dict[str, ProcedureDefinition] = {}
        self.admissions: dict[str, Admission] = {}
        self.enabled = True  # emergency disable stops *new* typed executions

    async def load(self) -> None:
        self.definitions.clear()
        self.admissions.clear()
        for payload in await self.repository.load_procedure_admissions():
            admission = Admission.model_validate_json(payload)
            self.admissions[admission.key] = admission
        for (
            procedure_id,
            revision,
            digest,
            content,
        ) in await self.repository.load_procedure_definitions():
            key = f"{procedure_id}@{revision}"
            report = validate_definition(json.loads(content), self.catalog, self.limits)
            definition = report.definition
            if definition is None or definition.digest() != digest or not report.ok:
                # Quarantine: visible, never executed, never deleted.
                admission = self.admissions.get(key) or Admission(
                    procedure_id=procedure_id, revision=revision, status=AdmissionStatus.INVALID
                )
                admission.status = AdmissionStatus.INVALID
                admission.issues = [issue.as_dict() for issue in report.issues] or [
                    {"code": "DIGEST_MISMATCH", "path": "$", "message": "stored digest differs"}
                ]
                admission.note("quarantined", "failed validation on load")
                self.admissions[key] = admission
                await self._save(admission)
                continue
            self.definitions[key] = definition

    async def register(
        self, raw: Mapping[str, Any], *, source: str = "authored", owner: str = ""
    ) -> Admission:
        report = validate_definition(raw, self.catalog, self.limits)
        definition = report.definition
        if definition is None or not report.ok:
            procedure_id = str(raw.get("procedure_id") or "unknown")
            revision = raw.get("revision")
            admission = Admission(
                procedure_id=procedure_id,
                revision=revision
                if isinstance(revision, int) and not isinstance(revision, bool)
                else 0,
                status=AdmissionStatus.INVALID,
                source=source,
                owner=owner,
                issues=[issue.as_dict() for issue in report.issues],
                raw=dict(raw),
            )
            admission.note("rejected", ", ".join(report.codes()))
            if admission.revision > 0 and admission.key not in self.definitions:
                self.admissions[admission.key] = admission
                await self._save(admission)
            return admission
        stored = await self.repository.insert_procedure_definition(
            definition.procedure_id,
            definition.revision,
            definition.digest(),
            definition.canonical(),
        )
        if not stored:
            raise RegistryError(
                "REVISION_CONFLICT",
                f"{definition.key} already exists with different content",
            )
        existing = self.admissions.get(definition.key)
        if existing is not None and existing.status is not AdmissionStatus.INVALID:
            self.definitions[definition.key] = definition
            return existing
        admission = Admission(
            procedure_id=definition.procedure_id,
            revision=definition.revision,
            digest=definition.digest(),
            status=AdmissionStatus.CANDIDATE if source == "learned" else AdmissionStatus.VALIDATED,
            source=source,
            owner=owner,
            adapter_contracts={
                step.adapter: step.contract_version
                for step in definition.steps
                if isinstance(step, ToolStep)
            },
        )
        admission.note("registered", source)
        self.definitions[definition.key] = definition
        self.admissions[definition.key] = admission
        await self._save(admission)
        return admission

    async def install(self, directory: Path) -> list[str]:
        """Register the shipped definitions under ``directory`` and apply
        its ``admissions.json``: a reviewed, checked-in approval bound to each
        definition's digest. An operator's later degrade/retire is kept --
        only a VALIDATED revision is promoted here, and a digest the manifest
        does not name stays unapproved. Returns one line per problem."""
        shipped, entries, problems = _read_shipped(directory)
        for name, raw in shipped:
            try:
                admission = await self.register(raw, source="shipped", owner="template")
            except RegistryError as exc:
                problems.append(f"{name}: {exc}")
                continue
            if admission.status is AdmissionStatus.INVALID:
                problems.append(f"{name}: {issues_text(admission.issues)}")
        for entry in entries:
            key = f"{entry.get('procedure_id')}@{entry.get('revision')}"
            admission = self.admissions.get(key)
            if admission is None or admission.status is not AdmissionStatus.VALIDATED:
                continue
            try:
                await self.approve(
                    key,
                    actor=str(entry.get("approved_by") or ""),
                    digest=str(entry.get("digest") or ""),
                    goal_kinds=list(entry.get("goal_kinds") or []) or None,
                )
            except RegistryError as exc:
                problems.append(f"{ADMISSIONS_FILE} {key}: {exc}")
        return problems

    async def mark_validated(self, key: str, report: str) -> Admission:
        admission = self._admission(key)
        if admission.status is not AdmissionStatus.CANDIDATE:
            raise RegistryError("NOT_A_CANDIDATE", f"{key} is {admission.status.value}")
        admission.status = AdmissionStatus.VALIDATED
        admission.note("validated", report)
        await self._save(admission)
        return admission

    async def approve(
        self, key: str, *, actor: str, digest: str, goal_kinds: list[str] | None = None
    ) -> Admission:
        """Trusted approval, bound to the exact digest. Only an operator or a
        template installation calls this; a definition cannot."""
        admission = self._admission(key)
        definition = self.definitions.get(key)
        if definition is None or admission.status is not AdmissionStatus.VALIDATED:
            raise RegistryError("NOT_VALIDATED", f"{key} is {admission.status.value}")
        if not actor or actor.startswith(("model", "agent:")):
            raise RegistryError("UNTRUSTED_APPROVER", "approval needs a trusted operator identity")
        if digest != definition.digest():
            raise RegistryError("DIGEST_MISMATCH", "approval names different content")
        report = validate_definition(definition, self.catalog, self.limits)
        if not report.ok:
            raise RegistryError("INVALID_DEFINITION", ", ".join(report.codes()))
        admission.status = AdmissionStatus.PROMOTED
        admission.approved_by = actor
        admission.approved_at = now_utc()
        admission.approved_goal_kinds = list(goal_kinds or [definition.goal_kind])
        admission.note("promoted", actor)
        await self._save(admission)
        return admission

    async def degrade(self, key: str, reason: str) -> Admission:
        admission = self._admission(key)
        admission.status = AdmissionStatus.DEGRADED
        admission.reason = reason
        admission.note("degraded", reason)
        await self._save(admission)
        return admission

    async def retire(self, key: str, reason: str) -> Admission:
        admission = self._admission(key)
        admission.status = AdmissionStatus.RETIRED
        admission.reason = reason
        admission.note("retired", reason)
        await self._save(admission)
        return admission

    def _admission(self, key: str) -> Admission:
        if key not in self.admissions:
            raise RegistryError("UNKNOWN_PROCEDURE", key)
        return self.admissions[key]

    async def _save(self, admission: Admission) -> None:
        await self.repository.save_procedure_admission(
            admission.procedure_id, admission.revision, admission.model_dump_json()
        )

    def select(
        self,
        goal_kind: str,
        parameters: Mapping[str, Any],
        capabilities: set[str],
        *,
        explicit: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> MatchResult:
        """The admitted procedure for this goal, or why there is none.
        Only NO_MATCH lets a caller fall back to legacy reasoning."""
        if not self.enabled:
            return MatchResult(Selection.NO_MATCH, reasons=["typed execution is disabled"])
        if explicit:
            admission = self.admissions.get(explicit)
            if admission is None:
                return MatchResult(Selection.INVALID_DEFINITION, reasons=[f"unknown {explicit}"])
            candidates = [admission]
        else:
            candidates = sorted(
                (
                    item
                    for item in self.admissions.values()
                    if item.status is AdmissionStatus.PROMOTED
                    and goal_kind in item.approved_goal_kinds
                ),
                key=lambda item: -item.revision,
            )
            if not candidates:
                return MatchResult(
                    Selection.NO_MATCH, reasons=[f"no admitted procedure for {goal_kind}"]
                )
        reasons: list[str] = []
        worst = Selection.INCOMPATIBLE_PROCEDURE
        for admission in candidates:
            definition = self.definitions.get(admission.key)
            if admission.status is AdmissionStatus.INVALID or definition is None:
                reasons.append(f"{admission.key}: invalid definition")
                worst = Selection.INVALID_DEFINITION
                continue
            if admission.status is not AdmissionStatus.PROMOTED:
                reasons.append(f"{admission.key}: {admission.status.value}, not promoted")
                worst = Selection.APPROVAL_REQUIRED
                continue
            if goal_kind not in admission.approved_goal_kinds:
                reasons.append(f"{admission.key}: not approved for {goal_kind}")
                continue
            changed = [
                adapter
                for adapter, version in admission.adapter_contracts.items()
                if (contract := self.catalog.adapters.get(adapter)) is None
                or contract.contract_version != version
            ]
            if changed:
                reasons.append(f"{admission.key}: adapter contract changed: {changed}")
                continue
            problems = schema_errors(dict(parameters), definition.parameter_schema, "parameters")
            if problems:
                reasons.append(f"{admission.key}: parameters: {'; '.join(problems[:3])}")
                continue
            missing = sorted(set(definition.required_capabilities) - capabilities)
            if missing:
                reasons.append(f"{admission.key}: agent lacks {missing}")
                worst = Selection.PERMISSION_DENIED
                continue
            scopes = {"goal": {"parameters": dict(parameters)}, "context": dict(context or {})}
            unmet = []
            for index, pred in enumerate(definition.preconditions):
                try:
                    if evaluate(pred, scopes) is not True:
                        unmet.append(index)
                except PredicateError:
                    unmet.append(index)
            if unmet:
                reasons.append(f"{admission.key}: preconditions {unmet} not satisfied")
                continue
            return MatchResult(Selection.MATCH, definition, admission, reasons)
        return MatchResult(worst, reasons=reasons)


# -- execution records ---------------------------------------------------------------------


class ExecStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({ExecStatus.COMPLETED, ExecStatus.FAILED, ExecStatus.CANCELLED})


class OpState(StrEnum):
    DISPATCHING = "dispatching"
    APPLIED = "applied"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    WAITING = "waiting"  # delegated child work not settled yet


class Budget(BaseModel):
    """One root envelope per goal occurrence. Retries and replacements
    inherit it; only a new occurrence gets a fresh one (plan 13.1)."""

    max_model_calls: int = 0
    max_tool_attempts: int = 16
    max_step_attempts: int = 64
    max_child_model_calls: int = 0
    max_delegation_depth: int = 3
    model_calls: int = 0
    tool_attempts: int = 0
    step_attempts: int = 0
    child_model_calls_reserved: int = 0

    def remaining(self) -> dict[str, int]:
        return {
            "model_calls": self.max_model_calls - self.model_calls,
            "tool_attempts": self.max_tool_attempts - self.tool_attempts,
            "step_attempts": self.max_step_attempts - self.step_attempts,
            "child_model_calls": self.max_child_model_calls - self.child_model_calls_reserved,
        }


class ProcedureExecution(BaseModel):
    execution_id: str
    agent_id: str
    goal_id: str
    occurrence_id: str
    work_item_id: str | None = None
    procedure_id: str
    revision: int
    digest: str
    path: str = "typed_authored"
    status: ExecStatus = ExecStatus.RUNNING
    status_reason: str = ""
    current_step_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    context_refs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    work: dict[str, Any] = Field(default_factory=dict)
    results: dict[str, Any] = Field(default_factory=dict)
    step_attempts: dict[str, int] = Field(default_factory=dict)
    completed_steps: list[str] = Field(default_factory=list)
    wait: dict[str, Any] | None = None
    next_eligible_at: datetime | None = None
    deadline_at: datetime
    budget: Budget = Field(default_factory=Budget)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    output: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL


class OperationRecord(BaseModel):
    operation_key: str
    execution_id: str
    occurrence_id: str
    step_id: str
    kind: str
    adapter: str = ""
    contract_version: int = 0
    argument_digest: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    state: OpState
    attempt: int = 1
    claim_epoch: str = PROCESS_EPOCH
    result: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    error: str = ""
    effect_certainty: str = "unknown"
    delivered: bool = False
    child: dict[str, Any] | None = None  # delegated work: status, result, executor
    started_at: datetime = Field(default_factory=now_utc)
    settled_at: datetime | None = None


StepKind = Literal[
    "advanced", "waiting", "failed", "needs_reconciliation", "completed", "cancelled", "busy"
]


@dataclass
class StepOutcome:
    kind: StepKind
    step_id: str = ""
    code: str = ""
    message: str = ""
    result: Any = None
    evidence: list[dict[str, Any]] = field(default_factory=list)


# -- adapters -----------------------------------------------------------------------------


class ReconcileState(StrEnum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


@dataclass
class AdapterResult:
    ok: bool
    result: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    code: str = ""
    message: str = ""
    retryable: bool = False
    denied: bool = False


@dataclass
class AdapterContext:
    agent_id: str
    tool_context: ToolContext
    blackboard: Any = None
    operation_key: str = ""


class DirectAction(Protocol):
    contract: AdapterContract

    async def invoke(self, context: AdapterContext, arguments: dict[str, Any]) -> AdapterResult: ...

    async def reconcile(
        self, context: AdapterContext, arguments: dict[str, Any]
    ) -> tuple[ReconcileState, dict[str, Any] | None]: ...


def _rel(context: ToolContext, target: Path) -> str:
    try:
        return "/".join(target.relative_to(context.root.resolve(strict=False)).parts)
    except ValueError:
        return str(target)


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_ANY_JSON: dict[str, Any] = OPAQUE_OBJECT


class JsonRead:
    contract = AdapterContract(
        adapter_id="core.json_read",
        contract_version=1,
        argument_schema=_object({"path": {"type": "string", "maxLength": 500}}, ["path"]),
        result_schema=_object(
            {
                "value": OPAQUE_OBJECT,
                "source_digest": {"type": "string", "maxLength": 100},
                "path": {"type": "string", "maxLength": 500},
                "keys": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 100},
                    "maxItems": 64,
                },
            },
            ["value", "source_digest", "path", "keys"],
        ),
        required_capabilities=("artifact.read",),
        side_effect=SideEffect.READ,
        retry=RetrySemantics.SAFE,
    )

    async def invoke(self, context: AdapterContext, arguments: dict[str, Any]) -> AdapterResult:
        try:
            target = _resolve(context.tool_context, arguments["path"])
            await _permit(context.tool_context, target, "read")
        except ToolDenied as exc:
            return AdapterResult(False, code="PERMISSION_DENIED", message=str(exc), denied=True)
        if not target.is_file():
            return AdapterResult(
                False, code="NOT_FOUND", message=f"{arguments['path']} does not exist"
            )
        data = await asyncio.to_thread(target.read_bytes)
        if len(data) > MAX_JSON_READ_BYTES:
            return AdapterResult(False, code="INPUT_TOO_LARGE", message=f"{len(data)} bytes")
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return AdapterResult(False, code="INVALID_JSON", message=str(exc))
        if not isinstance(value, dict):
            return AdapterResult(
                False, code="INVALID_JSON", message="the document is not an object"
            )
        return AdapterResult(
            True,
            {
                "value": value,
                "source_digest": digest_of(canonical_json(value)),
                "path": _rel(context.tool_context, target),
                "keys": sorted(str(key)[:100] for key in value)[:64],
            },
        )

    async def reconcile(
        self, context: AdapterContext, arguments: dict[str, Any]
    ) -> tuple[ReconcileState, dict[str, Any] | None]:
        return ReconcileState.NOT_APPLIED, None  # reading changes nothing: read again


class JsonWrite:
    """Canonical JSON to an approved destination under the agent's root, with
    a receipt keyed by the logical operation. An existing file is a conflict
    unless it is this operation's own completed write, or the unchanged output
    of an earlier write to the same path (a recurring occurrence replacing its
    own previous artifact). Ownership is what the path index recorded, never
    what the file claims about itself."""

    contract = AdapterContract(
        adapter_id="core.json_write",
        contract_version=1,
        argument_schema=_object(
            {
                "path": {"type": "string", "maxLength": 500},
                "value": OPAQUE_OBJECT,
            },
            ["path", "value"],
        ),
        result_schema=_object(
            {
                "artifact_id": {"type": "string", "maxLength": 500},
                "digest": {"type": "string", "maxLength": 100},
                "bytes": {"type": "integer", "minimum": 0},
            },
            ["artifact_id", "digest", "bytes"],
        ),
        required_capabilities=("artifact.write",),
        side_effect=SideEffect.LOCAL_WRITE,
        retry=RetrySemantics.NONE,
        reconcile_supported=True,
    )

    @staticmethod
    def _receipt_path(context: AdapterContext) -> Path:
        name = digest_of(context.operation_key)[:32] + ".json"
        return context.tool_context.root.resolve(strict=False) / RECEIPTS_DIR / name

    @staticmethod
    def _owner_path(context: AdapterContext, relative: str) -> Path:
        name = digest_of(relative)[:32] + ".json"
        return context.tool_context.root.resolve(strict=False) / RECEIPTS_DIR / "paths" / name

    def _owned(self, context: AdapterContext, relative: str, actual: str | None) -> bool:
        owner = self._owner_path(context, relative)
        if actual is None or not owner.is_file():
            return False
        try:
            record = json.loads(owner.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return record.get("path") == relative and record.get("digest") == actual

    async def invoke(self, context: AdapterContext, arguments: dict[str, Any]) -> AdapterResult:
        tool = context.tool_context
        if not tool.allow_write:
            return AdapterResult(
                False, code="PERMISSION_DENIED", message="writes are disabled", denied=True
            )
        try:
            target = _resolve(tool, arguments["path"])
            await _permit(tool, target, "write")
        except ToolDenied as exc:
            return AdapterResult(False, code="PERMISSION_DENIED", message=str(exc), denied=True)
        if RECEIPTS_DIR in target.parts:
            return AdapterResult(
                False, code="PERMISSION_DENIED", message="receipts are not writable", denied=True
            )
        content = canonical_json(arguments["value"]) + "\n"
        expected = digest_of(content)
        receipt_path = self._receipt_path(context)
        state, prior = await self.reconcile(context, arguments)
        if state is ReconcileState.APPLIED and prior is not None:
            return AdapterResult(True, prior, receipt={"reconciled": True})
        if state is ReconcileState.CONFLICT:
            return AdapterResult(
                False, code="DESTINATION_CONFLICT", message="destination already exists"
            )

        def write() -> None:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt = {
                "operation_key": context.operation_key,
                "path": _rel(tool, target),
                "digest": expected,
                "state": "prepared",
            }
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            # Bytes, not text: canonical LF on every platform, so the digest
            # recorded here is the digest of what is on disk.
            temporary.write_bytes(content.encode("utf-8"))
            os.replace(temporary, target)
            receipt["state"] = "applied"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            owner = self._owner_path(context, receipt["path"])
            owner.parent.mkdir(parents=True, exist_ok=True)
            owner.write_text(
                json.dumps(
                    {
                        "path": receipt["path"],
                        "digest": expected,
                        "operation_key": context.operation_key,
                    }
                ),
                encoding="utf-8",
            )

        await asyncio.to_thread(write)
        tool.tally.writes += 1
        result = {
            "artifact_id": _rel(tool, target),
            "digest": expected,
            "bytes": len(content.encode()),
        }
        return AdapterResult(True, result, receipt={"path": str(receipt_path), "digest": expected})

    async def reconcile(
        self, context: AdapterContext, arguments: dict[str, Any]
    ) -> tuple[ReconcileState, dict[str, Any] | None]:
        tool = context.tool_context
        try:
            target = _resolve(tool, arguments["path"])
        except ToolDenied:
            return ReconcileState.UNKNOWN, None
        content = canonical_json(arguments["value"]) + "\n"
        expected = digest_of(content)
        receipt_path = self._receipt_path(context)
        receipt = None
        if receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return ReconcileState.UNKNOWN, None
        exists = target.is_file()
        actual = digest_of(target.read_bytes()) if exists else None
        result = {
            "artifact_id": _rel(tool, target),
            "digest": expected,
            "bytes": len(content.encode()),
        }
        if receipt is not None and receipt.get("digest") != expected:
            return ReconcileState.CONFLICT, None
        if receipt is not None and exists and actual == expected:
            return ReconcileState.APPLIED, result
        if receipt is not None and not exists:
            return ReconcileState.NOT_APPLIED, None
        if receipt is None and not exists:
            return ReconcileState.NOT_APPLIED, None
        if receipt is None and self._owned(context, _rel(tool, target), actual):
            # Our own earlier artifact, unchanged since we wrote it.
            return ReconcileState.NOT_APPLIED, None
        # A file this operation has no receipt for, or one that changed since.
        return ReconcileState.CONFLICT, None


class JsonProject:
    contract = AdapterContract(
        adapter_id="core.json_project",
        contract_version=1,
        argument_schema=_object(
            {
                "value": OPAQUE_OBJECT,
                "fields": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 100},
                    "maxItems": 32,
                },
            },
            ["value", "fields"],
        ),
        result_schema=_object(
            {"value": OPAQUE_OBJECT},
            ["value"],
        ),
        required_capabilities=(),
        side_effect=SideEffect.PURE,
        retry=RetrySemantics.SAFE,
    )

    async def invoke(self, context: AdapterContext, arguments: dict[str, Any]) -> AdapterResult:
        value = arguments["value"]
        missing = [name for name in arguments["fields"] if name not in value]
        if missing:
            return AdapterResult(False, code="MISSING_FIELD", message=f"missing {missing}")
        return AdapterResult(True, {"value": {name: value[name] for name in arguments["fields"]}})

    async def reconcile(
        self, context: AdapterContext, arguments: dict[str, Any]
    ) -> tuple[ReconcileState, dict[str, Any] | None]:
        return ReconcileState.NOT_APPLIED, None


class FactRead:
    contract = AdapterContract(
        adapter_id="core.fact_read",
        contract_version=1,
        argument_schema=_object({"key": {"type": "string", "maxLength": 200}}, ["key"]),
        result_schema=_object(
            {
                "found": {"type": "boolean"},
                "value": {"type": "string", "maxLength": 4000},
                "source": {"type": "string", "maxLength": 200},
            },
            ["found", "value", "source"],
        ),
        required_capabilities=("blackboard.read",),
        side_effect=SideEffect.READ,
        retry=RetrySemantics.SAFE,
    )

    async def invoke(self, context: AdapterContext, arguments: dict[str, Any]) -> AdapterResult:
        board = context.blackboard
        fact = board.fact(arguments["key"]) if board is not None else None
        if fact is None:
            return AdapterResult(True, {"found": False, "value": "", "source": ""})
        return AdapterResult(
            True, {"found": True, "value": str(fact.value)[:4000], "source": fact.source}
        )

    async def reconcile(
        self, context: AdapterContext, arguments: dict[str, Any]
    ) -> tuple[ReconcileState, dict[str, Any] | None]:
        return ReconcileState.NOT_APPLIED, None


class FactPublish:
    contract = AdapterContract(
        adapter_id="core.fact_publish",
        contract_version=1,
        argument_schema=_object(
            {
                "key": {"type": "string", "maxLength": 200},
                "value": {"type": "string", "maxLength": 4000},
            },
            ["key", "value"],
        ),
        result_schema=_object({"key": {"type": "string", "maxLength": 200}}, ["key"]),
        required_capabilities=("blackboard.write",),
        side_effect=SideEffect.LOCAL_WRITE,
        retry=RetrySemantics.IDEMPOTENT,
    )

    async def invoke(self, context: AdapterContext, arguments: dict[str, Any]) -> AdapterResult:
        from evomesh.blackboard import WorldFact

        board = context.blackboard
        if board is None:
            return AdapterResult(False, code="UNAVAILABLE", message="no blackboard")
        board.publish_fact(
            WorldFact(key=arguments["key"], value=arguments["value"], source=context.agent_id)
        )
        return AdapterResult(True, {"key": arguments["key"]})

    async def reconcile(
        self, context: AdapterContext, arguments: dict[str, Any]
    ) -> tuple[ReconcileState, dict[str, Any] | None]:
        board = context.blackboard
        fact = board.fact(arguments["key"]) if board is not None else None
        if fact is not None and fact.value == arguments["value"]:
            return ReconcileState.APPLIED, {"key": arguments["key"]}
        return ReconcileState.NOT_APPLIED, None


CORE_ADAPTERS: tuple[DirectAction, ...] = (
    JsonRead(),
    JsonWrite(),
    JsonProject(),
    FactRead(),
    FactPublish(),
)


# -- trusted checks ------------------------------------------------------------------------


@dataclass
class CheckContext:
    execution: ProcedureExecution
    operations: list[OperationRecord]
    tool_context: ToolContext


@dataclass
class CheckOutcome:
    passed: bool
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


CheckFunction = Callable[[dict[str, Any], CheckContext], Awaitable[CheckOutcome]]


def _artifact_equals(
    context: CheckContext, artifact_id: str, expected_value: Any, extra: dict[str, Any]
) -> CheckOutcome:
    """The file holds exactly ``expected_value`` and this occurrence's journal
    holds the applied write receipt for it."""
    execution = context.execution
    expected = canonical_json(expected_value) + "\n"
    receipts = [
        op
        for op in context.operations
        if op.adapter == "core.json_write"
        and op.state is OpState.APPLIED
        and op.occurrence_id == execution.occurrence_id
        and (op.result or {}).get("artifact_id") == artifact_id
    ]
    if not receipts:
        return CheckOutcome(False, f"no applied write receipt for {artifact_id} in this occurrence")
    try:
        target = _resolve(context.tool_context, artifact_id)
    except ToolDenied as exc:
        return CheckOutcome(False, str(exc))
    if not target.is_file():
        return CheckOutcome(False, f"{artifact_id} does not exist")
    actual = target.read_text(encoding="utf-8")
    if actual != expected:
        return CheckOutcome(False, f"{artifact_id} does not match the expected content")
    return CheckOutcome(
        True,
        evidence={
            "artifact_id": artifact_id,
            "digest": digest_of(actual),
            "operation_key": receipts[-1].operation_key,
            **extra,
        },
    )


async def artifact_matches_source(arguments: dict[str, Any], context: CheckContext) -> CheckOutcome:
    """The artifact's content equals the value this occurrence's own source
    read captured, and this execution's journal holds the write receipt for
    it. Neither the writer's digest nor a file merely existing is trusted."""
    captured = context.execution.results.get(str(arguments.get("source_step")))
    if not isinstance(captured, dict) or "value" not in captured:
        return CheckOutcome(False, "the source read has no captured value")
    return _artifact_equals(
        context,
        str(arguments.get("artifact_id") or ""),
        captured["value"],
        {"source_digest": captured.get("source_digest", "")},
    )


async def artifact_matches_output(arguments: dict[str, Any], context: CheckContext) -> CheckOutcome:
    """The artifact holds exactly the output a cognitive step produced and
    the runtime validated in this occurrence -- not whatever the writer was
    handed, and not a model's claim that it wrote something."""
    execution = context.execution
    step_id = str(arguments.get("output_step") or "")
    validated = any(
        item.get("kind") == "cognitive" and item.get("step_id") == step_id
        for item in execution.evidence
    )
    if not validated or step_id not in execution.results:
        return CheckOutcome(False, f"{step_id} has no validated output in this occurrence")
    return _artifact_equals(
        context,
        str(arguments.get("artifact_id") or ""),
        execution.results[step_id],
        {"output_step": step_id, "model_generated": True},
    )


CORE_CHECKS: dict[str, tuple[CheckContract, CheckFunction]] = {
    "artifact_matches_source": (
        CheckContract(
            "artifact_matches_source",
            _object(
                {
                    "artifact_id": {"type": "string", "maxLength": 500},
                    "source_step": {"type": "string", "maxLength": 64},
                },
                ["artifact_id", "source_step"],
            ),
            "artifact equals the canonical value captured by this occurrence's source read",
        ),
        artifact_matches_source,
    ),
    "artifact_matches_output": (
        CheckContract(
            "artifact_matches_output",
            _object(
                {
                    "artifact_id": {"type": "string", "maxLength": 500},
                    "output_step": {"type": "string", "maxLength": 64},
                },
                ["artifact_id", "output_step"],
            ),
            "artifact equals the validated output of this occurrence's cognitive step",
        ),
        artifact_matches_output,
    ),
}


def _cited_ids_within_inputs(result: Any, inputs: Mapping[str, Any]) -> list[str]:
    """Every evidence id the model cites must be one it was given."""
    given: set[str] = set()

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("id"), str):
                given.add(node["id"])
            for item in node.values():
                collect(item)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(dict(inputs))
    cited = result.get("evidence_ids", []) if isinstance(result, dict) else []
    return [f"cites unknown evidence id {item!r}" for item in cited if item not in given]


CORE_OUTPUTS: tuple[OutputContract, ...] = (
    OutputContract(
        "report_comparison_v1",
        _object(
            {
                "summary": {"type": "string", "maxLength": 1200},
                "evidence_ids": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 64},
                    "maxItems": 20,
                },
            },
            ["summary", "evidence_ids"],
        ),
        _cited_ids_within_inputs,
    ),
    OutputContract(
        "local_json_inspection_v1",
        _object(
            {
                "artifact_id": {"type": "string", "maxLength": 500},
                "keys": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 100},
                    "maxItems": 64,
                },
                "digest": {"type": "string", "maxLength": 100},
            },
            ["artifact_id", "keys", "digest"],
        ),
    ),
    OutputContract(
        "authorized_json_inspection_v1",
        _object(
            {
                "artifact_id": {"type": "string", "maxLength": 500},
                "keys": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 100},
                    "maxItems": 64,
                },
                "digest": {"type": "string", "maxLength": 100},
            },
            ["artifact_id", "keys", "digest"],
        ),
    ),
)


def core_catalog() -> Catalog:
    catalog = Catalog(
        cognitive_services=frozenset(item.value for item in CognitiveServiceType),
        cognitive_reasons=frozenset(item.value for item in ModelInvocationReason),
    )
    for adapter in CORE_ADAPTERS:
        catalog.add_adapter(adapter.contract)
    for contract, _function in CORE_CHECKS.values():
        catalog.add_check(contract)
    for output in CORE_OUTPUTS:
        catalog.add_output(output)
    return catalog


# -- the executor -----------------------------------------------------------------------------


class ProcedureHost(Protocol):
    """What an execution needs from the running mesh, per agent."""

    def capabilities(self, agent_id: str) -> set[str]: ...

    def tool_context(self, agent_id: str) -> ToolContext: ...

    @property
    def blackboard(self) -> Any: ...

    async def think(
        self,
        agent_id: str,
        prompt: str,
        *,
        schema: Mapping[str, Any],
        service: str,
        reason: str,
        goal_id: str,
        task_id: str,
    ) -> str: ...

    async def route(self, work: WorkItem, requester_id: str) -> tuple[str | None, str]: ...

    async def deliver(self, work: WorkItem, requester_id: str, assignee_id: str) -> None: ...


FaultHook = Callable[[str, str], None]


def _no_fault(point: str, operation_key: str) -> None:
    return None


class ProcedureExecutor:
    def __init__(
        self,
        repository: SQLiteRepository,
        registry: ProcedureRegistry,
        *,
        adapters: tuple[DirectAction, ...] = CORE_ADAPTERS,
        checks: Mapping[str, tuple[CheckContract, CheckFunction]] = CORE_CHECKS,
        limits: ProcedureLimits = DEFAULT_LIMITS,
        clock: Callable[[], datetime] = now_utc,
        fault: FaultHook = _no_fault,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.adapters = {adapter.contract.adapter_id: adapter for adapter in adapters}
        self.checks = dict(checks)
        self.limits = limits
        self.clock = clock
        self.fault = fault
        self._in_flight: set[str] = set()

    # -- lifecycle -------------------------------------------------------------

    async def start(
        self,
        definition: ProcedureDefinition,
        admission: Admission,
        *,
        agent_id: str,
        goal_id: str,
        occurrence_id: str,
        parameters: dict[str, Any],
        context: dict[str, Any] | None = None,
        context_refs: dict[str, dict[str, Any]] | None = None,
        work: dict[str, Any] | None = None,
        work_item_id: str | None = None,
        budget: Budget | None = None,
    ) -> ProcedureExecution:
        """A new execution, or the open one this occurrence already has
        (resume before reselect; plan 12.2)."""
        open_ones = await self.repository.list_procedure_executions(
            occurrence_id=occurrence_id, open_only=True
        )
        if open_ones:
            return ProcedureExecution.model_validate_json(open_ones[0][1])
        previous = await self.repository.list_procedure_executions(occurrence_id=occurrence_id)
        envelope = budget or Budget(
            max_model_calls=min(
                self.limits.max_root_model_calls,
                sum(
                    step.max_model_calls + step.repair_calls
                    for step in definition.steps
                    if isinstance(step, CognitiveStep)
                ),
            ),
            max_tool_attempts=self.limits.max_total_step_attempts,
            max_step_attempts=self.limits.max_total_step_attempts,
            max_child_model_calls=sum(
                step.budget.max_model_calls
                for step in definition.steps
                if isinstance(step, DelegateStep)
            ),
            max_delegation_depth=self.limits.max_delegation_depth,
        )
        if previous:
            # A replacement inherits what the occurrence already spent.
            spent = ProcedureExecution.model_validate_json(previous[-1][1]).budget
            envelope.model_calls = spent.model_calls
            envelope.tool_attempts = spent.tool_attempts
            envelope.step_attempts = spent.step_attempts
            envelope.child_model_calls_reserved = spent.child_model_calls_reserved
        execution = ProcedureExecution(
            execution_id=uuid.uuid4().hex,
            agent_id=agent_id,
            goal_id=goal_id,
            occurrence_id=occurrence_id,
            work_item_id=work_item_id,
            procedure_id=definition.procedure_id,
            revision=definition.revision,
            digest=definition.digest(),
            path="typed_learned" if admission.source == "learned" else "typed_authored",
            current_step_id=definition.entry_step_id,
            parameters=parameters,
            context=dict(context or {}),
            context_refs=dict(context_refs or {}),
            work=dict(work or {}),
            deadline_at=self.clock()
            + timedelta(seconds=self.limits.default_execution_deadline_seconds),
            budget=envelope,
        )
        created = await self.repository.create_procedure_execution(
            execution.execution_id,
            occurrence_id,
            agent_id,
            execution.status.value,
            execution.model_dump_json(),
        )
        if not created:
            open_ones = await self.repository.list_procedure_executions(
                occurrence_id=occurrence_id, open_only=True
            )
            return ProcedureExecution.model_validate_json(open_ones[0][1])
        return execution

    async def load(self, execution_id: str) -> tuple[int, ProcedureExecution]:
        row = await self.repository.load_procedure_execution(execution_id)
        if row is None:
            raise ProcedureError("UNKNOWN_EXECUTION", execution_id)
        return row[0], ProcedureExecution.model_validate_json(row[1])

    async def operations(self, execution_id: str) -> list[OperationRecord]:
        return [
            OperationRecord.model_validate_json(payload)
            for _state, payload in await self.repository.list_procedure_operations(execution_id)
        ]

    async def open_for(self, occurrence_id: str) -> ProcedureExecution | None:
        rows = await self.repository.list_procedure_executions(
            occurrence_id=occurrence_id, open_only=True
        )
        return ProcedureExecution.model_validate_json(rows[0][1]) if rows else None

    async def request_cancel(self, execution_id: str, reason: str) -> ProcedureExecution:
        """Stop admitting new operations now; an in-flight one is accounted
        for before the execution becomes CANCELLED."""
        for _ in range(5):
            version, execution = await self.load(execution_id)
            if execution.terminal:
                return execution
            in_flight = [
                op
                for op in await self.operations(execution_id)
                if op.state in {OpState.DISPATCHING, OpState.UNKNOWN}
            ]
            execution.status = ExecStatus.CANCEL_REQUESTED if in_flight else ExecStatus.CANCELLED
            execution.status_reason = reason
            if await self._commit(version, execution):
                return execution
        raise ProcedureError("CONTENDED", "could not record the cancellation")

    async def set_paused(self, execution_id: str, paused: bool) -> ProcedureExecution:
        version, execution = await self.load(execution_id)
        if execution.terminal:
            return execution
        if paused and execution.status in {ExecStatus.RUNNING, ExecStatus.WAITING}:
            execution.status = ExecStatus.PAUSED
        elif not paused and execution.status is ExecStatus.PAUSED:
            execution.status = ExecStatus.RUNNING
        await self._commit(version, execution)
        return execution

    async def _commit(
        self,
        version: int,
        execution: ProcedureExecution,
        operations: tuple[tuple[OperationRecord, str | None], ...] = (),
    ) -> bool:
        execution.updated_at = self.clock()
        return await self.repository.commit_procedure_transition(
            execution.execution_id,
            version,
            execution.status.value,
            execution.model_dump_json(),
            [
                (op.operation_key, op.state.value, op.model_dump_json(), expected)
                for op, expected in operations
            ],
        )

    # -- one step ---------------------------------------------------------------

    async def advance(self, execution_id: str, host: ProcedureHost) -> StepOutcome:
        if execution_id in _ADVANCING:  # checked and taken with no await between
            return StepOutcome("busy", "", "IN_FLIGHT", "another advance holds this execution")
        _ADVANCING.add(execution_id)
        try:
            return await self._advance(execution_id, host)
        finally:
            _ADVANCING.discard(execution_id)

    async def _advance(self, execution_id: str, host: ProcedureHost) -> StepOutcome:
        version, execution = await self.load(execution_id)
        definition = self.registry.definitions.get(f"{execution.procedure_id}@{execution.revision}")
        if execution.terminal:
            return self._terminal_outcome(execution)
        if definition is None or definition.digest() != execution.digest:
            return await self._fail(
                version, execution, "DEFINITION_UNAVAILABLE", "pinned revision is gone"
            )
        step = definition.step(execution.current_step_id)
        if execution.status is ExecStatus.PAUSED:
            return StepOutcome("waiting", step.id, "PAUSED", "paused")
        pending = await self._pending_operation(execution, step.id)
        if pending is not None and pending.operation_key in self._in_flight:
            return StepOutcome("busy", step.id, "IN_FLIGHT", "another advance holds this step")
        if execution.status is ExecStatus.CANCEL_REQUESTED and pending is None:
            execution.status = ExecStatus.CANCELLED
            await self._commit(version, execution)
            return StepOutcome("cancelled", step.id, message=execution.status_reason)
        if pending is not None and pending.state in {OpState.DISPATCHING, OpState.UNKNOWN}:
            return await self._recover(version, execution, definition, step, pending, host)
        if execution.status is ExecStatus.CANCEL_REQUESTED:
            execution.status = ExecStatus.CANCELLED
            await self._commit(version, execution)
            return StepOutcome("cancelled", step.id, message=execution.status_reason)
        now = self.clock()
        if now >= execution.deadline_at:
            return await self._fail(
                version, execution, "DEADLINE_EXCEEDED", "the execution deadline passed"
            )
        if execution.next_eligible_at is not None and now < execution.next_eligible_at:
            return StepOutcome(
                "waiting", step.id, "WAITING_RETRY", f"retry at {execution.next_eligible_at}"
            )
        registry_admission = self.registry.admissions.get(
            f"{execution.procedure_id}@{execution.revision}"
        )
        if registry_admission is None or registry_admission.status in {
            AdmissionStatus.RETIRED,
            AdmissionStatus.INVALID,
        }:
            return await self._fail(
                version, execution, "ADMISSION_REVOKED", "the revision was revoked"
            )
        if execution.budget.step_attempts >= execution.budget.max_step_attempts:
            return await self._fail(
                version, execution, "BUDGET_EXHAUSTED", "step attempts exhausted"
            )
        try:
            if isinstance(step, ToolStep):
                return await self._tool(version, execution, definition, step, host)
            if isinstance(step, CognitiveStep):
                return await self._cognitive(version, execution, step, host)
            if isinstance(step, BranchStep):
                return await self._branch(version, execution, step)
            if isinstance(step, ValidateStep):
                return await self._validate(version, execution, step, host)
            if isinstance(step, DelegateStep):
                return await self._delegate(version, execution, step, host)
            if isinstance(step, AwaitStep):
                return await self._await(version, execution, step, host)
            if isinstance(step, CompleteStep):
                return await self._complete(version, execution, definition, step)
        except BindingError as exc:
            return await self._fail(version, execution, exc.code, exc.message)
        raise ProcedureError("UNKNOWN_STEP", step.id)

    # -- helpers -----------------------------------------------------------------

    def _scopes(
        self, execution: ProcedureExecution, definition: ProcedureDefinition | None = None
    ) -> dict[str, Any]:
        return {
            "goal": {"parameters": execution.parameters},
            "result": execution.results,
            "context": execution.context,
            "work": execution.work,
            "constants": dict(definition.constants) if definition is not None else {},
        }

    def _definition(self, execution: ProcedureExecution) -> ProcedureDefinition:
        return self.registry.definitions[f"{execution.procedure_id}@{execution.revision}"]

    @staticmethod
    def operation_key(execution: ProcedureExecution, step_id: str) -> str:
        return f"{execution.occurrence_id}:{execution.execution_id}:{step_id}"

    async def _pending_operation(
        self, execution: ProcedureExecution, step_id: str
    ) -> OperationRecord | None:
        row = await self.repository.load_procedure_operation(self.operation_key(execution, step_id))
        return OperationRecord.model_validate_json(row[1]) if row else None

    def _terminal_outcome(self, execution: ProcedureExecution) -> StepOutcome:
        if execution.status is ExecStatus.COMPLETED:
            return StepOutcome(
                "completed",
                execution.current_step_id,
                result=execution.output,
                evidence=execution.evidence,
            )
        if execution.status is ExecStatus.CANCELLED:
            return StepOutcome(
                "cancelled", execution.current_step_id, message=execution.status_reason
            )
        return StepOutcome(
            "failed", execution.current_step_id, execution.status_reason, execution.status_reason
        )

    async def _fail(
        self, version: int, execution: ProcedureExecution, code: str, message: str
    ) -> StepOutcome:
        execution.status = ExecStatus.FAILED
        execution.status_reason = code
        execution.evidence.append(
            {
                "kind": "failure",
                "code": code,
                "message": message[:500],
                "step_id": execution.current_step_id,
            }
        )
        await self._commit(version, execution)
        await self._degrade_on_defect(execution, code, message)
        return StepOutcome("failed", execution.current_step_id, code, message)

    async def _degrade_on_defect(
        self, execution: ProcedureExecution, code: str, message: str
    ) -> None:
        """A failure the procedure itself caused stops its selection (plan
        16.5); an environmental one (a denied path, a busy peer) does not."""
        if code not in PROCEDURE_DEFECTS:
            return
        key = f"{execution.procedure_id}@{execution.revision}"
        admission = self.registry.admissions.get(key)
        if admission is not None and admission.status is AdmissionStatus.PROMOTED:
            await self.registry.degrade(key, f"{code} in {execution.execution_id}: {message}"[:300])

    def _advance_to(
        self, execution: ProcedureExecution, step_id: str, target: str, result: Any
    ) -> None:
        if result is not None:
            execution.results[step_id] = result
        execution.completed_steps.append(step_id)
        execution.current_step_id = target
        execution.status = ExecStatus.RUNNING
        execution.wait = None
        execution.next_eligible_at = None

    def _adapter_context(
        self, execution: ProcedureExecution, host: ProcedureHost, key: str
    ) -> AdapterContext:
        return AdapterContext(
            execution.agent_id, host.tool_context(execution.agent_id), host.blackboard, key
        )

    # -- tool --------------------------------------------------------------------

    async def _tool(
        self,
        version: int,
        execution: ProcedureExecution,
        definition: ProcedureDefinition,
        step: ToolStep,
        host: ProcedureHost,
    ) -> StepOutcome:
        adapter = self.adapters.get(step.adapter)
        if adapter is None or adapter.contract.contract_version != step.contract_version:
            return await self._fail(
                version, execution, "CONTRACT_CHANGED", f"{step.adapter} changed"
            )
        missing = sorted(
            set(adapter.contract.required_capabilities) - host.capabilities(execution.agent_id)
        )
        if missing:
            return await self._fail(version, execution, "PERMISSION_DENIED", f"lacks {missing}")
        scopes = self._scopes(execution, definition)
        for pred in definition.preconditions:
            try:
                if evaluate(pred, scopes) is not True:
                    return await self._fail(
                        version, execution, "PRECONDITION_FAILED", "a precondition no longer holds"
                    )
            except PredicateError as exc:
                return await self._fail(version, execution, exc.code, exc.message)
        arguments = resolve(step.arguments, scopes)
        problems = schema_errors(arguments, adapter.contract.argument_schema, "arguments")
        if problems:
            return await self._fail(
                version, execution, "INVALID_ARGUMENTS", "; ".join(problems[:3])
            )
        if execution.budget.tool_attempts >= execution.budget.max_tool_attempts:
            return await self._fail(
                version, execution, "BUDGET_EXHAUSTED", "tool attempts exhausted"
            )
        key = self.operation_key(execution, step.id)
        argument_digest = digest_of(canonical_json(arguments))
        prior = await self._pending_operation(execution, step.id)
        if prior is not None and prior.argument_digest != argument_digest:
            return await self._fail(
                version, execution, "OPERATION_CONFLICT", "same operation with different arguments"
            )
        attempts = execution.step_attempts.get(step.id, 0)
        if attempts >= step.max_attempts:
            return await self._fail(version, execution, "STEP_ATTEMPTS_EXHAUSTED", step.id)
        record = OperationRecord(
            operation_key=key,
            execution_id=execution.execution_id,
            occurrence_id=execution.occurrence_id,
            step_id=step.id,
            kind="tool",
            adapter=step.adapter,
            contract_version=step.contract_version,
            argument_digest=argument_digest,
            arguments=arguments,
            state=OpState.DISPATCHING,
            attempt=attempts + 1,
        )
        execution.step_attempts[step.id] = attempts + 1
        execution.budget.tool_attempts += 1
        execution.budget.step_attempts += 1
        expected = None if prior is None else prior.state.value
        if not await self._commit(version, execution, ((record, expected),)):
            return StepOutcome("busy", step.id, "CLAIM_LOST", "another advance claimed this step")
        version += 1
        self.fault("after_claim", key)
        return await self._dispatch(version, execution, definition, step, adapter, record, host)

    async def _dispatch(
        self,
        version: int,
        execution: ProcedureExecution,
        definition: ProcedureDefinition,
        step: ToolStep,
        adapter: DirectAction,
        record: OperationRecord,
        host: ProcedureHost,
    ) -> StepOutcome:
        context = self._adapter_context(execution, host, record.operation_key)
        self._in_flight.add(record.operation_key)
        try:
            try:
                outcome = await asyncio.wait_for(
                    adapter.invoke(context, record.arguments), adapter.contract.timeout_seconds
                )
            except TimeoutError:
                outcome = None
            self.fault("after_effect", record.operation_key)
        finally:
            self._in_flight.discard(record.operation_key)
        record.settled_at = self.clock()
        if outcome is None:
            if adapter.contract.side_effect in {SideEffect.PURE, SideEffect.READ}:
                record.state = OpState.REJECTED
                record.error = "TIMEOUT"
                return await self._retry_or_fail(
                    version, execution, step, record, "TIMEOUT", "timed out"
                )
            record.state = OpState.UNKNOWN
            record.error = "TIMEOUT"
            execution.status = ExecStatus.NEEDS_RECONCILIATION
            execution.status_reason = f"{record.operation_key}: outcome unknown after timeout"
            await self._commit(version, execution, ((record, OpState.DISPATCHING.value),))
            return StepOutcome(
                "needs_reconciliation", step.id, "UNKNOWN_EFFECT", execution.status_reason
            )
        if not outcome.ok:
            record.state = OpState.REJECTED
            record.error = outcome.code
            record.effect_certainty = "not_applied"
            if outcome.denied or not outcome.retryable:
                execution.status = ExecStatus.FAILED
                execution.status_reason = outcome.code
                execution.evidence.append(
                    {
                        "kind": "failure",
                        "code": outcome.code,
                        "message": outcome.message[:500],
                        "step_id": step.id,
                    }
                )
                await self._commit(version, execution, ((record, OpState.DISPATCHING.value),))
                return StepOutcome("failed", step.id, outcome.code, outcome.message)
            return await self._retry_or_fail(
                version, execution, step, record, outcome.code, outcome.message
            )
        return await self._settle_success(
            version,
            execution,
            definition,
            step,
            adapter,
            record,
            outcome.result or {},
            outcome.receipt,
        )

    async def _settle_success(
        self,
        version: int,
        execution: ProcedureExecution,
        definition: ProcedureDefinition,
        step: ToolStep,
        adapter: DirectAction,
        record: OperationRecord,
        result: dict[str, Any],
        receipt: dict[str, Any] | None,
    ) -> StepOutcome:
        record.state = OpState.APPLIED
        record.result = result
        record.receipt = receipt
        record.effect_certainty = "applied"
        record.settled_at = self.clock()
        problems = schema_errors(result, adapter.contract.result_schema, "result")
        if problems:
            execution.status = ExecStatus.FAILED
            execution.status_reason = "RESULT_SCHEMA_INVALID"
            await self._commit(version, execution, ((record, "*"),))
            return StepOutcome("failed", step.id, "RESULT_SCHEMA_INVALID", "; ".join(problems[:3]))
        self._advance_to(execution, step.id, step.next, result)
        execution.evidence.append(
            {
                "kind": "receipt",
                "step_id": step.id,
                "operation_key": record.operation_key,
                "adapter": step.adapter,
                "occurrence_id": execution.occurrence_id,
            }
        )
        if not await self._commit(version, execution, ((record, "*"),)):
            return StepOutcome("busy", step.id, "CLAIM_LOST", "settlement lost its version")
        return StepOutcome("advanced", step.id, result=result)

    async def _retry_or_fail(
        self,
        version: int,
        execution: ProcedureExecution,
        step: ToolStep,
        record: OperationRecord,
        code: str,
        message: str,
    ) -> StepOutcome:
        if execution.step_attempts.get(step.id, 0) < step.max_attempts:
            execution.next_eligible_at = self.clock() + timedelta(
                seconds=2 ** execution.step_attempts.get(step.id, 0)
            )
            execution.status = ExecStatus.WAITING
            execution.wait = {"reason": "retry", "code": code}
            # The record goes back to "retry": same key, same frozen arguments.
            await self._commit(version, execution, ((record, "*"),))
            return StepOutcome("waiting", step.id, "WAITING_RETRY", message)
        execution.status = ExecStatus.FAILED
        execution.status_reason = code
        await self._commit(version, execution, ((record, "*"),))
        return StepOutcome("failed", step.id, code, message)

    async def _recover(
        self,
        version: int,
        execution: ProcedureExecution,
        definition: ProcedureDefinition,
        step: Any,
        record: OperationRecord,
        host: ProcedureHost,
    ) -> StepOutcome:
        """An operation claimed by a process that is gone, or whose outcome is
        unknown: never blindly repeat a mutation."""
        if not isinstance(step, ToolStep):
            # Cognitive calls change nothing outside; a lost one is re-issued
            # (and charged) by the normal path once the record is settled.
            record.state = OpState.REJECTED
            record.error = "LOST_IN_FLIGHT"
            await self._commit(version, execution, ((record, "*"),))
            return StepOutcome(
                "waiting", step.id, "RECOVERED", "lost cognitive call will be re-issued"
            )
        adapter = self.adapters.get(step.adapter)
        if adapter is None:
            return await self._fail(version, execution, "CONTRACT_CHANGED", step.adapter)
        contract = adapter.contract
        if contract.retry is RetrySemantics.SAFE:
            record.state = OpState.REJECTED
            record.error = "LOST_IN_FLIGHT"
            execution.status = ExecStatus.RUNNING
            self._release_attempt(execution, step.id)
            await self._commit(version, execution, ((record, "*"),))
            return StepOutcome("waiting", step.id, "RECOVERED", "safe operation will be repeated")
        if not contract.reconcile_supported and contract.retry is not RetrySemantics.IDEMPOTENT:
            execution.status = ExecStatus.NEEDS_RECONCILIATION
            execution.status_reason = f"{record.operation_key}: cannot prove whether it applied"
            record.state = OpState.UNKNOWN
            await self._commit(version, execution, ((record, "*"),))
            return StepOutcome(
                "needs_reconciliation", step.id, "UNRESOLVED_EFFECT", execution.status_reason
            )
        state, result = await adapter.reconcile(
            self._adapter_context(execution, host, record.operation_key), record.arguments
        )
        if state is ReconcileState.APPLIED and result is not None:
            execution.status = ExecStatus.RUNNING
            return await self._settle_success(
                version, execution, definition, step, adapter, record, result, {"reconciled": True}
            )
        if state is ReconcileState.NOT_APPLIED:
            record.state = OpState.REJECTED
            record.error = "NOT_APPLIED"
            execution.status = ExecStatus.RUNNING
            self._release_attempt(execution, step.id)
            await self._commit(version, execution, ((record, "*"),))
            return StepOutcome(
                "waiting", step.id, "RECOVERED", "the operation did not apply; it may be retried"
            )
        execution.status = ExecStatus.NEEDS_RECONCILIATION
        execution.status_reason = f"{record.operation_key}: reconciliation says {state.value}"
        record.state = OpState.UNKNOWN
        await self._commit(version, execution, ((record, "*"),))
        return StepOutcome(
            "needs_reconciliation", step.id, "UNRESOLVED_EFFECT", execution.status_reason
        )

    @staticmethod
    def _release_attempt(execution: ProcedureExecution, step_id: str) -> None:
        """An attempt proven not to have applied gives back its per-step
        allowance (plan 13.2). The root counters keep it: a process that
        keeps crashing still runs out of budget."""
        attempts = execution.step_attempts.get(step_id, 0)
        execution.step_attempts[step_id] = max(0, attempts - 1)

    async def resolve_reconciliation(
        self, execution_id: str, host: ProcedureHost, *, actor: str, decision: str
    ) -> StepOutcome:
        """Operator path for an effect nobody could prove either way:
        "recheck" re-runs the adapter's reconciliation; "not_applied" is the
        operator's word that it did not happen, so the step may be retried;
        "fail" ends the execution. Only a trusted operator decides."""
        if decision not in RECONCILIATION_DECISIONS:
            raise RegistryError("UNKNOWN_DECISION", decision)
        if not actor or actor.startswith(("model", "agent:")):
            raise RegistryError("UNTRUSTED_APPROVER", "reconciliation needs an operator identity")
        version, execution = await self.load(execution_id)
        definition = self._definition(execution)
        step = definition.step(execution.current_step_id)
        record = await self._pending_operation(execution, step.id)
        if execution.status is not ExecStatus.NEEDS_RECONCILIATION or record is None:
            return StepOutcome("failed", step.id, "NOT_RECONCILING", "nothing to reconcile")
        execution.evidence.append(
            {
                "kind": "reconciliation",
                "actor": actor,
                "decision": decision,
                "operation_key": record.operation_key,
            }
        )
        if decision == "fail":
            execution.status = ExecStatus.FAILED
            execution.status_reason = "RECONCILED_AS_FAILED"
            record.state = OpState.REJECTED
            await self._commit(version, execution, ((record, "*"),))
            return StepOutcome("failed", step.id, "RECONCILED_AS_FAILED", actor)
        if decision == "not_applied":
            record.state = OpState.REJECTED
            record.error = "NOT_APPLIED_BY_OPERATOR"
            execution.status = ExecStatus.RUNNING
            execution.status_reason = ""
            self._release_attempt(execution, step.id)
            await self._commit(version, execution, ((record, "*"),))
            return StepOutcome("waiting", step.id, "RECOVERED", f"{actor}: not applied")
        return await self._recover(version, execution, definition, step, record, host)

    # -- cognitive -------------------------------------------------------------------

    async def _cognitive(
        self, version: int, execution: ProcedureExecution, step: CognitiveStep, host: ProcedureHost
    ) -> StepOutcome:
        output = self.registry.catalog.outputs.get(step.output_schema)
        if output is None:
            return await self._fail(version, execution, "UNKNOWN_OUTPUT_SCHEMA", step.output_schema)
        inputs = resolve(step.inputs, self._scopes(execution))
        prompt = (
            f"{step.instruction}\n\nINPUTS (JSON, the only data you may use):\n"
            f"{canonical_json(inputs)}\n\nReply with exactly one JSON object and nothing else, "
            f"matching this schema:\n{canonical_json(output.schema)}"
        )
        guard = self.limits.max_inline_result_bytes
        if len(prompt) > guard:
            return await self._fail(
                version,
                execution,
                "CONTEXT_BUDGET_EXCEEDED",
                f"{len(prompt)} chars of mandatory input",
            )
        attempts = execution.step_attempts.get(step.id, 0)
        allowed = step.max_model_calls + step.repair_calls
        feedback = ""
        while attempts < allowed:
            if execution.budget.model_calls >= execution.budget.max_model_calls:
                return await self._fail(
                    version, execution, "BUDGET_EXHAUSTED", "model calls exhausted"
                )
            attempts += 1
            execution.step_attempts[step.id] = attempts
            execution.budget.model_calls += 1
            execution.budget.step_attempts += 1
            key = f"{self.operation_key(execution, step.id)}#{attempts}"
            record = OperationRecord(
                operation_key=key,
                execution_id=execution.execution_id,
                occurrence_id=execution.occurrence_id,
                step_id=step.id,
                kind="cognitive",
                argument_digest=digest_of(prompt),
                state=OpState.DISPATCHING,
                attempt=attempts,
            )
            if not await self._commit(version, execution, ((record, None),)):
                return StepOutcome(
                    "busy", step.id, "CLAIM_LOST", "another advance claimed this step"
                )
            version += 1
            try:
                text = await host.think(
                    execution.agent_id,
                    prompt + feedback,
                    schema=output.schema,
                    service=step.service,
                    reason=step.reason,
                    goal_id=execution.goal_id,
                    task_id=f"{execution.execution_id}:{step.id}",
                )
            except Exception as exc:  # noqa: BLE001 - provider failures are outcomes here
                record.state = OpState.REJECTED
                record.error = f"{type(exc).__name__}: {exc}"[:300]
                await self._commit(version, execution, ((record, "*"),))
                version += 1
                return await self._fail(version, execution, "MODEL_UNAVAILABLE", record.error)
            record.state = OpState.APPLIED
            record.settled_at = self.clock()
            parsed, problems = self._parse_output(text, output, inputs)
            record.result = {"valid": not problems}
            if not problems:
                self._advance_to(execution, step.id, step.next, parsed)
                execution.evidence.append(
                    {
                        "kind": "cognitive",
                        "step_id": step.id,
                        "calls": attempts,
                        "schema": step.output_schema,
                        "model_generated": True,
                    }
                )
                await self._commit(version, execution, ((record, "*"),))
                return StepOutcome("advanced", step.id, result=parsed)
            record.error = "; ".join(problems[:3])
            await self._commit(version, execution, ((record, "*"),))
            version += 1
            if problems[0].startswith("UNEXPECTED_TOOL_CALL"):
                return await self._fail(version, execution, "UNEXPECTED_TOOL_CALL", record.error)
            feedback = (
                f"\n\nYour previous reply was rejected: {record.error}. "
                "Reply again with only the JSON object."
            )
        return await self._fail(
            version, execution, "COGNITIVE_OUTPUT_INVALID", "output failed its schema"
        )

    @staticmethod
    def _parse_output(
        text: str, output: OutputContract, inputs: Mapping[str, Any]
    ) -> tuple[Any, list[str]]:
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match is None:
            return None, ["no JSON object in the reply"]
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            return None, [f"invalid JSON: {exc}"]
        if isinstance(value, dict) and ({"tool_calls", "tool", "function_call"} & set(value)):
            return None, ["UNEXPECTED_TOOL_CALL: a cognitive step may not call tools"]
        problems = schema_errors(value, output.schema, "output")
        if not problems and output.semantic_check is not None:
            problems = output.semantic_check(value, inputs)
        return value, problems

    # -- branch / validate / complete ---------------------------------------------------

    async def _branch(
        self, version: int, execution: ProcedureExecution, step: BranchStep
    ) -> StepOutcome:
        try:
            value = evaluate(step.predicate, self._scopes(execution, self._definition(execution)))
        except PredicateError as exc:
            return await self._fail(version, execution, exc.code, exc.message)
        if value is None:
            return await self._fail(
                version, execution, "PREDICATE_UNRESOLVED", "the branch predicate is unknown"
            )
        execution.budget.step_attempts += 1
        self._advance_to(
            execution,
            step.id,
            step.then if value else step.else_,
            {"taken": "then" if value else "else"},
        )
        await self._commit(version, execution)
        return StepOutcome("advanced", step.id, result={"taken": "then" if value else "else"})

    async def _validate(
        self, version: int, execution: ProcedureExecution, step: ValidateStep, host: ProcedureHost
    ) -> StepOutcome:
        entry = self.checks.get(step.check)
        if entry is None:
            return await self._fail(version, execution, "UNKNOWN_CHECK", step.check)
        contract, function = entry
        arguments = resolve(step.arguments, self._scopes(execution, self._definition(execution)))
        problems = schema_errors(arguments, contract.argument_schema, "arguments")
        if problems:
            return await self._fail(
                version, execution, "INVALID_ARGUMENTS", "; ".join(problems[:3])
            )
        outcome = await function(
            arguments,
            CheckContext(
                execution,
                await self.operations(execution.execution_id),
                host.tool_context(execution.agent_id),
            ),
        )
        execution.budget.step_attempts += 1
        evidence = {
            "kind": "validator",
            "check": step.check,
            "passed": outcome.passed,
            "occurrence_id": execution.occurrence_id,
            "execution_id": execution.execution_id,
            "step_id": step.id,
            "reason": outcome.reason,
            **outcome.evidence,
        }
        execution.evidence.append(evidence)
        if not outcome.passed:
            execution.status = ExecStatus.FAILED
            execution.status_reason = "VALIDATION_FAILED"
            await self._commit(version, execution)
            await self._degrade_on_defect(execution, "VALIDATION_FAILED", outcome.reason)
            return StepOutcome(
                "failed", step.id, "VALIDATION_FAILED", outcome.reason, evidence=[evidence]
            )
        self._advance_to(execution, step.id, step.next, {"passed": True, **outcome.evidence})
        await self._commit(version, execution)
        return StepOutcome("advanced", step.id, result=evidence, evidence=[evidence])

    async def _complete(
        self,
        version: int,
        execution: ProcedureExecution,
        definition: ProcedureDefinition,
        step: CompleteStep,
    ) -> StepOutcome:
        result = resolve(step.result, self._scopes(execution, definition))
        problems = schema_errors(result, definition.output_schema, "output")
        if problems:
            return await self._fail(
                version, execution, "OUTPUT_SCHEMA_INVALID", "; ".join(problems[:3])
            )
        execution.output = result
        execution.completed_steps.append(step.id)
        execution.status = ExecStatus.COMPLETED
        execution.status_reason = "graph completed"
        await self._commit(version, execution)
        return StepOutcome("completed", step.id, result=result, evidence=execution.evidence)

    # -- delegation and waits -----------------------------------------------------------

    @staticmethod
    def child_work_id(execution: ProcedureExecution, step_id: str) -> str:
        return "w" + digest_of(f"{execution.execution_id}:{step_id}")[:15]

    async def _delegate(
        self, version: int, execution: ProcedureExecution, step: DelegateStep, host: ProcedureHost
    ) -> StepOutcome:
        work_id = self.child_work_id(execution, step.id)
        key = f"delegate:{work_id}"
        existing = await self.repository.load_procedure_operation(key)
        if existing is not None:
            record = OperationRecord.model_validate_json(existing[1])
            self._advance_to(execution, step.id, step.next, {"work_item_id": work_id})
            await self._commit(version, execution)
            if not record.delivered and record.child and record.child.get("assignee"):
                await self._deliver(record, host, execution)
            return StepOutcome("advanced", step.id, result={"work_item_id": work_id})
        chain = list(execution.work.get("causation_chain", []))
        depth = int(execution.work.get("delegation_depth", 0)) + 1
        if depth > execution.budget.max_delegation_depth:
            return await self._fail(version, execution, "DELEGATION_DEPTH_EXCEEDED", str(depth))
        if (
            execution.budget.child_model_calls_reserved + step.budget.max_model_calls
            > execution.budget.max_child_model_calls
        ):
            return await self._fail(
                version, execution, "BUDGET_EXHAUSTED", "child model calls exceed the root envelope"
            )
        inputs = resolve(step.inputs, self._scopes(execution))
        work = WorkItem(
            id=work_id,
            parent_goal_id=execution.goal_id,
            requester_agent_id=execution.agent_id,
            type=step.work_kind,
            objective=step.objective,
            required_capabilities=list(step.required_capabilities),
            inputs=inputs,
            expected_outputs=[step.output_schema],
            success_conditions=[step.success_contract],
            deadline=self.clock() + timedelta(seconds=step.budget.deadline_seconds),
            causation_chain=[*chain, execution.agent_id],
            delegation_depth=depth,
        )
        work.budget.max_attempts = step.budget.max_attempts
        work.budget.max_model_calls = step.budget.max_model_calls
        assignee, reason = await host.route(work, execution.agent_id)
        if assignee is None:
            return await self._fail(version, execution, "NO_ELIGIBLE_PEER", reason)
        work.assign(assignee)
        record = OperationRecord(
            operation_key=key,
            execution_id=execution.execution_id,
            occurrence_id=execution.occurrence_id,
            step_id=step.id,
            kind="delegate",
            argument_digest=digest_of(canonical_json(inputs)),
            arguments={"work": work.model_dump(mode="json")},
            state=OpState.WAITING,
            child={"status": "assigned", "assignee": assignee},
        )
        execution.budget.child_model_calls_reserved += step.budget.max_model_calls
        execution.budget.step_attempts += 1
        self._advance_to(execution, step.id, step.next, {"work_item_id": work_id})
        # Child work, the parent's cursor and the delivery intent: one commit.
        if not await self._commit(version, execution, ((record, None),)):
            return StepOutcome("busy", step.id, "CLAIM_LOST", "another advance created this child")
        await self._deliver(record, host, execution)
        return StepOutcome("advanced", step.id, result={"work_item_id": work_id})

    async def _deliver(
        self, record: OperationRecord, host: ProcedureHost, execution: ProcedureExecution
    ) -> None:
        work = WorkItem.model_validate(record.arguments["work"])
        await host.deliver(work, execution.agent_id, str((record.child or {}).get("assignee")))
        record.delivered = True
        await self.repository.update_procedure_operation(
            record.operation_key, record.state.value, record.state.value, record.model_dump_json()
        )

    async def open_executions(self, agent_id: str) -> list[ProcedureExecution]:
        rows = await self.repository.list_procedure_executions(open_only=True)
        executions = [ProcedureExecution.model_validate_json(row[1]) for row in rows]
        return [item for item in executions if item.agent_id == agent_id]

    async def for_occurrence(self, occurrence: str) -> ProcedureExecution | None:
        """The latest execution for one goal occurrence, open or not."""
        rows = await self.repository.list_procedure_executions(occurrence_id=occurrence)
        if not rows:
            return None
        return ProcedureExecution.model_validate_json(rows[-1][1])

    async def settle_child(
        self,
        work_id: str,
        *,
        status: str,
        executor_id: str,
        result: Any = None,
        evidence: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Record a child's authoritative outcome. Mismatched executors and
        repeated settlements are refused; the parent re-reads this on wake."""
        key = f"delegate:{work_id}"
        row = await self.repository.load_procedure_operation(key)
        if row is None:
            return False
        record = OperationRecord.model_validate_json(row[1])
        if record.state is not OpState.WAITING:
            return False  # settled once already
        child = dict(record.child or {})
        if child.get("assignee") != executor_id:
            logger.warning(
                "ignoring a result for %s from %s (assigned %s)",
                work_id,
                executor_id,
                child.get("assignee"),
            )
            return False
        child.update(
            {
                "status": status,
                "result": result,
                "evidence": evidence or [],
                "executor": executor_id,
            }
        )
        record.child = child
        record.state = OpState.APPLIED if status == "completed" else OpState.REJECTED
        record.settled_at = self.clock()
        return await self.repository.update_procedure_operation(
            key, OpState.WAITING.value, record.state.value, record.model_dump_json()
        )

    async def _await(
        self, version: int, execution: ProcedureExecution, step: AwaitStep, host: ProcedureHost
    ) -> StepOutcome:
        now = self.clock()
        if execution.wait is None or execution.wait.get("step_id") != step.id:
            execution.wait = {"step_id": step.id, "subject": step.subject, "since": now.isoformat()}
            execution.status = ExecStatus.WAITING
            if not await self._commit(version, execution):
                return StepOutcome("busy", step.id, "CLAIM_LOST", "wait intent lost its version")
            version += 1
        since = datetime.fromisoformat(str(execution.wait["since"]))
        timed_out = now >= since + timedelta(seconds=step.timeout_seconds)
        if step.subject == "time":
            if not timed_out:
                return StepOutcome("waiting", step.id, "WAITING_TIME", "waiting for time")
            self._advance_to(
                execution, step.id, step.next, {"waited_seconds": step.timeout_seconds}
            )
            await self._commit(version, execution)
            return StepOutcome("advanced", step.id)
        reference = resolve(step.reference, self._scopes(execution))
        if step.subject == "evidence":
            fact = host.blackboard.fact(str(reference)) if host.blackboard is not None else None
            # Anything published during this execution counts, even before the
            # wait began: the wait re-reads durable state, it does not depend
            # on having been subscribed when the event happened.
            if fact is not None and fact.created_at >= execution.created_at:
                self._advance_to(
                    execution, step.id, step.next, {"value": fact.value, "source": fact.source}
                )
                await self._commit(version, execution)
                return StepOutcome("advanced", step.id)
            if timed_out:
                return await self._fail(version, execution, "AWAIT_TIMEOUT", str(reference))
            return StepOutcome("waiting", step.id, "WAITING_EVIDENCE", str(reference))
        row = await self.repository.load_procedure_operation(f"delegate:{reference}")
        if row is None:
            return await self._fail(version, execution, "UNKNOWN_WORK", str(reference))
        record = OperationRecord.model_validate_json(row[1])
        if not record.delivered and (record.child or {}).get("assignee"):
            await self._deliver(record, host, execution)
        child = record.child or {}
        if record.state is OpState.WAITING:
            if timed_out:
                return await self._fail(version, execution, "AWAIT_TIMEOUT", str(reference))
            return StepOutcome("waiting", step.id, "WAITING_FOR_CHILD", str(reference))
        if child.get("status") != "completed":
            return await self._fail(
                version, execution, "CHILD_NOT_COMPLETED", str(child.get("status"))
            )
        result = child.get("result")
        if step.output_schema is not None:
            output = self.registry.catalog.outputs[step.output_schema]
            problems = schema_errors(result, output.schema, "child")
            if problems:
                return await self._fail(
                    version, execution, "CHILD_RESULT_INVALID", "; ".join(problems[:3])
                )
        self._advance_to(execution, step.id, step.next, result)
        execution.evidence.append(
            {
                "kind": "child",
                "step_id": step.id,
                "work_item_id": reference,
                "executor": child.get("executor"),
                "occurrence_id": execution.occurrence_id,
            }
        )
        await self._commit(version, execution)
        return StepOutcome("advanced", step.id, result=result)


def goal_evidence(execution: ProcedureExecution) -> tuple[dict[str, bool], dict[str, Any]]:
    """Validator and tool evidence for GoalManager, only for this occurrence."""
    validators: dict[str, bool] = {}
    tools: dict[str, Any] = {}
    for item in execution.evidence:
        if item.get("occurrence_id") != execution.occurrence_id:
            continue
        if item.get("kind") == "validator":
            validators[str(item["check"])] = bool(item.get("passed"))
        elif item.get("kind") == "receipt":
            tools[str(item["step_id"])] = "applied"
    return validators, tools


def issues_text(issues: list[Issue] | list[dict[str, str]]) -> str:
    parts = [
        f"{item['code']} at {item['path']}"
        if isinstance(item, dict)
        else f"{item.code} at {item.path}"
        for item in issues
    ]
    return "; ".join(parts[:10])


# -- the facade BDI uses ------------------------------------------------------------------

RESERVED_PARAMETERS = frozenset({"procedure"})


def occurrence_id(goal: Any) -> str:
    return f"{goal.id}#{getattr(goal, 'occurrence', 0)}"


def typed_request(goal: Any) -> tuple[str, dict[str, Any], str | None, dict[str, Any]]:
    """(goal kind, parameters, explicit binding, work contract) for a goal.
    A delegated goal is typed by the work kind it carries."""
    from evomesh.coordination import DELEGATED_GOAL_KIND

    parameters = dict(goal.parameters or {})
    if goal.kind == DELEGATED_GOAL_KIND:
        work = {
            "work_item_id": parameters.get("work_item_id"),
            "causation_chain": list(parameters.get("causation_chain") or []),
            "delegation_depth": int(parameters.get("delegation_depth") or 0),
            "requester_id": parameters.get("requester_id"),
        }
        inputs = parameters.get("inputs")
        return str(parameters.get("work_type") or ""), dict(inputs or {}), None, work
    explicit = parameters.get("procedure")
    return (
        goal.kind,
        {key: value for key, value in parameters.items() if key not in RESERVED_PARAMETERS},
        str(explicit) if explicit else None,
        {},
    )


class ProcedureService:
    """Selection, start/resume and one-step advance for the BDI reasoner."""

    def __init__(
        self, registry: ProcedureRegistry, executor: ProcedureExecutor, host: ProcedureHost
    ) -> None:
        self.registry = registry
        self.executor = executor
        self.host = host

    def _context(
        self, definition: ProcedureDefinition, agent: Any
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        values: dict[str, Any] = {}
        refs: dict[str, dict[str, Any]] = {}
        for dependency in definition.context_dependencies:
            if dependency.source == "belief":
                belief = agent.mind.belief(dependency.key)
                if belief is not None:
                    values[dependency.name] = belief.statement
                    refs[dependency.name] = {
                        "source": "belief",
                        "key": dependency.key,
                        "at": belief.updated_at.isoformat(),
                    }
            else:
                board = self.host.blackboard
                fact = board.fact(dependency.key) if board is not None else None
                if fact is not None:
                    values[dependency.name] = fact.value
                    refs[dependency.name] = {
                        "source": "fact",
                        "key": dependency.key,
                        "at": fact.created_at.isoformat(),
                    }
        return values, refs

    async def begin(
        self, goal: Any, agent: Any
    ) -> tuple[ProcedureExecution | None, MatchResult | None]:
        occurrence = occurrence_id(goal)
        existing = await self.executor.open_for(occurrence)
        if existing is not None:
            return existing, None  # resume before reselect
        kind, parameters, explicit, work = typed_request(goal)
        if not kind:
            return None, None
        match = self.registry.select(
            kind, parameters, self.host.capabilities(agent.id), explicit=explicit
        )
        if (
            match.selection is not Selection.MATCH
            or match.definition is None
            or match.admission is None
        ):
            return None, match
        values, refs = self._context(match.definition, agent)
        execution = await self.executor.start(
            match.definition,
            match.admission,
            agent_id=agent.id,
            goal_id=goal.id,
            occurrence_id=occurrence,
            parameters=parameters,
            context=values,
            context_refs=refs,
            work=work,
            work_item_id=work.get("work_item_id"),
        )
        return execution, match

    async def advance(self, execution_id: str) -> StepOutcome:
        return await self.executor.advance(execution_id, self.host)

    async def reap(self, agent: Any) -> list[str]:
        """Cancel this agent's open executions whose goal occurrence is gone:
        the goal was cancelled, finished some other way or moved on. A
        cancellation is settled through the executor, so an in-flight
        operation is accounted for rather than orphaned."""
        reaped: list[str] = []
        goals = {goal.id: goal for goal in agent.mind.goals}
        for execution in await self.executor.open_executions(agent.id):
            goal = goals.get(execution.goal_id)
            if goal is not None and goal.is_open and occurrence_id(goal) == execution.occurrence_id:
                continue
            await self.executor.request_cancel(execution.execution_id, "the goal closed")
            await self.executor.advance(execution.execution_id, self.host)
            reaped.append(execution.execution_id)
        return reaped

    async def cancel(self, execution_id: str, reason: str) -> ProcedureExecution:
        return await self.executor.request_cancel(execution_id, reason)

    async def execution(self, execution_id: str) -> ProcedureExecution:
        return (await self.executor.load(execution_id))[1]

    async def label(self, execution_id: str, label: str) -> None:
        version, execution = await self.executor.load(execution_id)
        execution.evidence.append(
            {"kind": "label", "label": label, "occurrence_id": execution.occurrence_id}
        )
        await self.executor._commit(version, execution)  # pyright: ignore[reportPrivateUsage]
