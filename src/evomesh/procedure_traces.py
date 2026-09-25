"""Conservative learning for typed procedures (closure plan section 16).

Model-directed harness work can call two contract-backed tools, ``json_read``
and ``json_write``, that run the very adapters a typed procedure uses. Each
call is journaled against the goal occurrence it served. When that goal
completes on authoritative evidence (its own success conditions, re-checked
here, never the model's say-so), the occurrence becomes an OperationTrace.

A candidate procedure is built only from three or more distinct eligible
traces with the same operation sequence, and only through an operator's
approved binding map: equal-looking values are never taken as data lineage.
An argument the map does not bind is kept as a literal (an exact-scope
candidate) when every trace agrees, and refused when they do not. Candidates
enter the registry as CANDIDATE; a sandboxed replay can mark one VALIDATED,
and only a trusted operator promotes it.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from evomesh.contracts import FilesystemGrant, now_utc
from evomesh.harness_tools import Tool, ToolContext
from evomesh.procedure_runtime import (
    AdapterContext,
    AdmissionStatus,
    ExecStatus,
    JsonRead,
    JsonWrite,
    ProcedureExecutor,
    ProcedureRegistry,
    RegistryError,
    goal_evidence,
)
from evomesh.procedures import (
    Catalog,
    SideEffect,
    canonical_json,
    digest_of,
    validate_definition,
)
from evomesh.storage import SQLiteRepository

logger = logging.getLogger(__name__)

TRACE_VALUE_BYTES = 8192
MINIMUM_OCCURRENCES = 3
LEARNABLE_EFFECTS = frozenset({SideEffect.READ.value, SideEffect.LOCAL_WRITE.value})


class TraceStep(BaseModel):
    index: int
    operation_key: str
    adapter: str
    contract_version: int
    side_effect: str
    # None when the arguments were too large to keep: the trace then records
    # that it happened, and is not eligible to teach anything.
    arguments: dict[str, Any] | None = None
    argument_digest: str
    ok: bool
    code: str = ""
    result: dict[str, Any] | None = None
    result_digest: str = ""


class OperationTrace(BaseModel):
    trace_id: str
    occurrence_id: str
    goal_id: str
    goal_kind: str
    agent_id: str
    path: str = "model_directed"
    parameters: dict[str, Any] = Field(default_factory=dict)
    steps: list[TraceStep] = Field(default_factory=list)
    model_calls: int = 0
    completion_basis: list[str] = Field(default_factory=list)
    eligible: bool = False
    ineligible: list[str] = Field(default_factory=list)
    recorded_at: datetime = Field(default_factory=now_utc)

    def signature(self) -> tuple[tuple[str, int], ...]:
        return tuple((step.adapter, step.contract_version) for step in self.steps)


def _bounded(value: Any) -> Any | None:
    text = canonical_json(value)
    return value if len(text.encode("utf-8")) <= TRACE_VALUE_BYTES else None


class TraceRecorder:
    """Pending operations per goal occurrence, until the goal settles."""

    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository
        self.pending: dict[str, dict[str, Any]] = {}

    def next_index(self, occurrence: str) -> int:
        return len(self.pending.get(occurrence, {}).get("steps", []))

    def record(self, occurrence: str, goal_id: str, agent_id: str, step: TraceStep) -> None:
        entry = self.pending.setdefault(
            occurrence, {"goal_id": goal_id, "agent_id": agent_id, "steps": []}
        )
        entry["steps"].append(step)

    def discard(self, goal_id: str) -> None:
        for occurrence in [key for key, item in self.pending.items() if item["goal_id"] == goal_id]:
            del self.pending[occurrence]

    async def finalize(
        self,
        goal: Any,
        *,
        basis: list[str],
        model_calls: int,
    ) -> list[OperationTrace]:
        """Every pending occurrence of this goal becomes a stored trace; the
        repository's unique occurrence key makes a repeat a no-op."""
        traces: list[OperationTrace] = []
        for occurrence in [key for key, item in self.pending.items() if item["goal_id"] == goal.id]:
            entry = self.pending.pop(occurrence)
            steps: list[TraceStep] = entry["steps"]
            parameters = _bounded(dict(goal.parameters or {}))
            reasons = _ineligibility(goal, steps, basis, parameters)
            trace = OperationTrace(
                trace_id=uuid.uuid4().hex,
                occurrence_id=occurrence,
                goal_id=goal.id,
                goal_kind=str(goal.kind),
                agent_id=str(entry["agent_id"]),
                parameters=parameters or {},
                steps=steps,
                model_calls=model_calls,
                completion_basis=basis,
                eligible=not reasons,
                ineligible=reasons,
            )
            await self.repository.insert_procedure_trace(
                trace.trace_id, trace.goal_kind, occurrence, trace.model_dump_json()
            )
            traces.append(trace)
        return traces

    async def traces(self, goal_kind: str | None = None) -> list[OperationTrace]:
        rows = await self.repository.list_procedure_traces(goal_kind)
        return [OperationTrace.model_validate_json(row) for row in rows]


def _ineligibility(
    goal: Any, steps: Sequence[TraceStep], basis: list[str], parameters: Any
) -> list[str]:
    from evomesh.coordination import DELEGATED_GOAL_KIND

    reasons: list[str] = []
    if goal.kind in {"goal", "", DELEGATED_GOAL_KIND}:
        reasons.append("no structured goal kind")
    if not goal.parameters or parameters is None:
        reasons.append("no bounded input contract")
    if not steps:
        reasons.append("no contract-backed operation")
    if not basis:
        reasons.append("no authoritative completion evidence")
    for step in steps:
        if not step.ok:
            reasons.append(
                f"operation {step.index} failed ({step.code}); a correction is not taught"
            )
        if step.arguments is None or step.result is None:
            reasons.append(f"operation {step.index} was too large to keep")
        if step.side_effect not in LEARNABLE_EFFECTS:
            reasons.append(f"operation {step.index} has effect {step.side_effect}")
    return reasons


# -- contract-backed tools for model-directed work ----------------------------------


def typed_harness_tools(
    recorder: TraceRecorder,
    agent_id: str,
    occurrence: Callable[[], tuple[str, str] | None],
    *,
    allow_write: bool,
) -> tuple[Tool, ...]:
    """``json_read``/``json_write`` for one agent's harness job, running the
    typed adapters under the job's own ToolContext (the agent's grants).
    ``occurrence`` names the (goal id, occurrence id) the job serves, if any."""

    async def call(adapter: Any, context: ToolContext, arguments: dict[str, Any]) -> str:
        served = occurrence()
        index = recorder.next_index(served[1]) if served else 0
        key = f"harness:{served[1] if served else uuid.uuid4().hex}:{index}"
        outcome = await adapter.invoke(
            AdapterContext(agent_id=agent_id, tool_context=context, operation_key=key), arguments
        )
        if served is not None:
            recorder.record(
                served[1],
                served[0],
                agent_id,
                TraceStep(
                    index=index,
                    operation_key=key,
                    adapter=adapter.contract.adapter_id,
                    contract_version=adapter.contract.contract_version,
                    side_effect=adapter.contract.side_effect.value,
                    arguments=_bounded(arguments),
                    argument_digest=digest_of(canonical_json(arguments)),
                    ok=outcome.ok,
                    code=outcome.code,
                    result=_bounded(outcome.result) if outcome.result is not None else None,
                    result_digest=digest_of(canonical_json(outcome.result or {})),
                ),
            )
        if not outcome.ok:
            return f"DENIED: {outcome.code}: {outcome.message}"
        return canonical_json(outcome.result)[:TRACE_VALUE_BYTES]

    async def read(context: ToolContext, args: dict[str, Any]) -> str:
        return await call(JsonRead(), context, {"path": str(args.get("path", ""))})

    async def write(context: ToolContext, args: dict[str, Any]) -> str:
        value = args.get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"value is not JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("value must be a JSON object")
        return await call(JsonWrite(), context, {"path": str(args.get("path", "")), "value": value})

    tools = [
        Tool(
            name="json_read",
            description=(
                "Read a JSON object file. Returns its value, digest and top-level keys. "
                "Prefer this over read for JSON data."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path."}},
                "required": ["path"],
            },
            run=read,
        )
    ]
    if allow_write:
        tools.append(
            Tool(
                name="json_write",
                description=(
                    "Write a JSON object to a new file as canonical JSON, with a receipt. "
                    "Refuses to overwrite a file it did not write itself."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path."},
                        "value": {"type": "object", "description": "The JSON object."},
                    },
                    "required": ["path", "value"],
                },
                run=write,
            )
        )
    return tuple(tools)


# -- candidate extraction (plan 16.3) ------------------------------------------------


class BindingMap(BaseModel):
    """An operator's statement of where each argument comes from, per goal
    kind: ``"1.value": {"result": [0, "value"]}`` or ``"0.path": {"param":
    "source"}``. The runtime checks it against every trace; it never guesses."""

    goal_kind: str
    approved_by: str
    bindings: dict[str, dict[str, Any]] = Field(default_factory=dict)


class CandidateReport(BaseModel):
    ok: bool
    code: str = ""
    reasons: list[str] = Field(default_factory=list)
    definition: dict[str, Any] | None = None
    source_traces: list[str] = Field(default_factory=list)
    restrictions: list[str] = Field(default_factory=list)
    exact_scope: bool = False
    expected_cost: dict[str, float] = Field(default_factory=dict)


def _refused(code: str, *reasons: str, traces: Sequence[OperationTrace] = ()) -> CandidateReport:
    return CandidateReport(
        ok=False,
        code=code,
        reasons=list(reasons)[:20],
        source_traces=[trace.trace_id for trace in traces],
    )


def _lookup(source: dict[str, Any], trace: OperationTrace) -> tuple[bool, Any]:
    if "param" in source:
        name = str(source["param"])
        return name in trace.parameters, trace.parameters.get(name)
    if "result" in source:
        path = list(source["result"])
        index, fields = int(path[0]), path[1:]
        if index >= len(trace.steps) or trace.steps[index].result is None:
            return False, None
        node: Any = trace.steps[index].result
        for field in fields:
            if not isinstance(node, dict) or field not in node:
                return False, None
            node = node[field]
        return True, node
    return False, None


def _parameter_schema(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        return {"type": "string", "maxLength": 500}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    return None


def extract_candidate(
    traces: Sequence[OperationTrace],
    binding_map: BindingMap | None,
    catalog: Catalog,
    *,
    revision: int = 1,
    minimum: int = MINIMUM_OCCURRENCES,
) -> CandidateReport:
    eligible = [trace for trace in traces if trace.eligible]
    unique: dict[str, OperationTrace] = {}
    for trace in eligible:
        unique.setdefault(trace.occurrence_id, trace)  # a replayed trace counts once
    if not unique:
        return _refused("INSUFFICIENT_EVIDENCE", "no eligible trace")
    cohorts = Counter(trace.signature() for trace in unique.values())
    signature, _ = cohorts.most_common(1)[0]
    cohort = [trace for trace in unique.values() if trace.signature() == signature]
    kinds = {trace.goal_kind for trace in cohort}
    if len(kinds) != 1:
        return _refused("NOT_COMPILABLE", f"mixed goal kinds {sorted(kinds)}", traces=cohort)
    goal_kind = kinds.pop()
    if len(cohort) < minimum:
        return _refused(
            "INSUFFICIENT_EVIDENCE",
            f"{len(cohort)} distinct verified occurrences; {minimum} required",
            traces=cohort,
        )
    if binding_map is not None:
        if binding_map.goal_kind != goal_kind:
            return _refused("NOT_COMPILABLE", "the binding map is for another goal kind")
        if not binding_map.approved_by or binding_map.approved_by.startswith(("model", "agent:")):
            return _refused("NOT_COMPILABLE", "the binding map is not operator-approved")
    bindings = binding_map.bindings if binding_map is not None else {}
    first = cohort[0]
    step_ids = [f"step_{index + 1}" for index in range(len(first.steps))]
    steps: list[dict[str, Any]] = []
    restrictions: list[str] = []
    parameters: dict[str, dict[str, Any]] = {}
    for index, template in enumerate(first.steps):
        names = set(template.arguments or {})
        if any(set(trace.steps[index].arguments or {}) != names for trace in cohort):
            return _refused(
                "NOT_COMPILABLE", f"operation {index} changes its arguments", traces=cohort
            )
        arguments: dict[str, Any] = {}
        for name in sorted(names):
            key = f"{index}.{name}"
            observed = [(trace.steps[index].arguments or {})[name] for trace in cohort]
            source = bindings.get(key)
            if source is not None:
                if "result" in source and int(list(source["result"])[0]) >= index:
                    return _refused("NOT_COMPILABLE", f"{key} is bound to a later result")
                for trace, value in zip(cohort, observed, strict=True):
                    found, expected = _lookup(source, trace)
                    if not found or canonical_json(expected) != canonical_json(value):
                        return _refused(
                            "NOT_COMPILABLE",
                            f"the binding map says {key} comes from {source}, "
                            f"but trace {trace.trace_id} disagrees",
                            traces=cohort,
                        )
                if "param" in source:
                    name_ = str(source["param"])
                    schema = _parameter_schema(cohort[0].parameters.get(name_))
                    if schema is None:
                        return _refused("NOT_COMPILABLE", f"parameter {name_} is not a scalar")
                    parameters[name_] = schema
                    arguments[name] = {"ref": {"scope": "goal", "path": ["parameters", name_]}}
                else:
                    path = list(source["result"])
                    arguments[name] = {
                        "ref": {"scope": "result", "path": [step_ids[int(path[0])], *path[1:]]}
                    }
                continue
            if len({canonical_json(value) for value in observed}) != 1:
                return _refused(
                    "NOT_COMPILABLE",
                    f"{key} varies across occurrences and has no recorded provenance",
                    traces=cohort,
                )
            arguments[name] = {"literal": observed[0]}
            restrictions.append(f"{key} is fixed to the observed value")
        steps.append(
            {
                "id": step_ids[index],
                "kind": "tool",
                "adapter": template.adapter,
                "contract_version": template.contract_version,
                "arguments": arguments,
                "next": step_ids[index + 1] if index + 1 < len(first.steps) else "check",
            }
        )
    output_schema, done, check = _terminal(first, steps, step_ids, bindings)
    if check is not None:
        steps.append(check)
    else:
        steps[-1]["next"] = "done"
    steps.append(done)
    definition = {
        "schema_version": 1,
        "procedure_id": f"learned_{goal_kind}",
        "revision": revision,
        "name": f"Learned: {goal_kind}",
        "description": (
            f"Compiled from {len(cohort)} verified model-directed occurrences; "
            f"restrictions: {'; '.join(restrictions) or 'none'}"
        )[:1000],
        "entry_step_id": step_ids[0],
        "goal_kind": goal_kind,
        "parameter_schema": {
            "type": "object",
            "properties": parameters,
            "required": sorted(parameters),
            "additionalProperties": False,
        },
        "output_schema": output_schema,
        # What the observed contracts need -- declared, so the validator can
        # check it covers the effective set rather than trusting the author.
        "required_capabilities": sorted(
            {
                capability
                for step in first.steps
                if (contract := catalog.adapters.get(step.adapter)) is not None
                for capability in contract.required_capabilities
            }
        ),
        "steps": steps,
    }
    report = validate_definition(definition, catalog)
    if not report.ok or report.definition is None:
        return _refused(
            "NOT_COMPILABLE",
            *(f"{issue.code} at {issue.path}" for issue in report.issues),
            traces=cohort,
        )
    observed_calls = sum(trace.model_calls for trace in cohort) / len(cohort)
    return CandidateReport(
        ok=True,
        definition=definition,
        source_traces=[trace.trace_id for trace in cohort],
        restrictions=restrictions,
        exact_scope=bool(restrictions),
        expected_cost={"model_calls_observed": observed_calls, "model_calls_candidate": 0.0},
    )


def _result_ref(step_id: str, field: str) -> dict[str, Any]:
    return {"ref": {"scope": "result", "path": [step_id, field]}}


def _terminal(
    trace: OperationTrace,
    steps: list[dict[str, Any]],
    step_ids: list[str],
    bindings: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Output schema, completion step, and a trusted check when the map
    proves a written value is a read value: the learned procedure keeps a
    validator instead of trusting the output it happened to produce."""
    writes = [index for index, step in enumerate(trace.steps) if step.adapter == "core.json_write"]
    if not writes:
        digest = _result_ref(step_ids[len(steps) - 1], "source_digest")
        schema = {
            "type": "object",
            "properties": {"digest": {"type": "string", "maxLength": 100}},
            "required": ["digest"],
            "additionalProperties": False,
        }
        done = {"id": "done", "kind": "complete", "result": {"digest": digest}}
        return schema, done, None
    last = writes[-1]
    schema = {
        "type": "object",
        "properties": {"artifact_id": {"type": "string", "maxLength": 500}},
        "required": ["artifact_id"],
        "additionalProperties": False,
    }
    artifact = _result_ref(step_ids[last], "artifact_id")
    done = {"id": "done", "kind": "complete", "result": {"artifact_id": artifact}}
    path = list(bindings.get(f"{last}.value", {}).get("result", []))
    copies_a_read = (
        len(path) == 2
        and path[1] == "value"
        and trace.steps[int(path[0])].adapter == "core.json_read"
    )
    if not copies_a_read:
        return schema, done, None
    check = {
        "id": "check",
        "kind": "validate",
        "check": "artifact_matches_source",
        "arguments": {
            "artifact_id": artifact,
            "source_step": {"literal": step_ids[int(path[0])]},
        },
        "next": "done",
    }
    return schema, done, check


# -- sandboxed replay (plan 16.4) ----------------------------------------------------


class ReplayReport(BaseModel):
    key: str
    completed: bool
    status: str
    code: str = ""
    validators: dict[str, bool] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    held_out: bool = False
    note: str = ""

    def passed(self) -> bool:
        return self.completed and all(self.validators.values())


class _ReplayHost:
    def __init__(self, root: Path, policy: Any, agent_id: str, capabilities: set[str]) -> None:
        self.root = root
        self.policy = policy
        self.agent_id = agent_id
        self.caps = capabilities
        self.blackboard = None

    def capabilities(self, agent_id: str) -> set[str]:
        return self.caps

    def tool_context(self, agent_id: str) -> ToolContext:
        return ToolContext(
            root=self.root, policy=self.policy, agent_id=agent_id, allow_write=True
        )

    async def think(self, agent_id: str, prompt: str, **kwargs: Any) -> str:
        raise RuntimeError("a replay makes no model call")

    async def route(self, work: Any, requester_id: str) -> tuple[str | None, str]:
        return None, "a replay does not delegate"

    async def deliver(self, work: Any, requester_id: str, assignee_id: str) -> None:
        raise RuntimeError("a replay does not delegate")


async def replay(
    registry: ProcedureRegistry,
    executor: ProcedureExecutor,
    policy: Any,
    key: str,
    *,
    fixture: Path,
    parameters: dict[str, Any],
    training: Sequence[OperationTrace] = (),
) -> ReplayReport:
    """Run a candidate once in a disposable copy of ``fixture``, under a
    temporary grant scoped to that copy. Nothing outside it can change."""
    definition = registry.definitions.get(key)
    admission = registry.admissions.get(key)
    if definition is None or admission is None:
        raise RegistryError("UNKNOWN_PROCEDURE", key)
    held_out = all(trace.parameters != parameters for trace in training)
    sandbox = Path(tempfile.mkdtemp(prefix="evomesh-replay-"))
    agent_id = f"replay-{uuid.uuid4().hex[:8]}"
    grant = FilesystemGrant(agent_id=agent_id, path=str(sandbox), read=True, write=True)
    try:
        shutil.copytree(fixture, sandbox, dirs_exist_ok=True)
        await policy.grant(grant)
        host = _ReplayHost(sandbox, policy, agent_id, set(definition.required_capabilities))
        execution = await executor.start(
            definition,
            admission,
            agent_id=agent_id,
            goal_id=f"replay:{key}",
            occurrence_id=f"replay:{uuid.uuid4().hex}",
            parameters=parameters,
        )
        outcome = None
        for _ in range(len(definition.steps) * 3 + 3):
            outcome = await executor.advance(execution.execution_id, host)
            if outcome.kind in {"completed", "failed", "cancelled", "needs_reconciliation"}:
                break
        _, final = await executor.load(execution.execution_id)
        validators, _ = goal_evidence(final)
        return ReplayReport(
            key=key,
            completed=final.status is ExecStatus.COMPLETED,
            status=final.status.value,
            code=outcome.code if outcome is not None else "",
            validators=validators,
            output=final.output,
            held_out=held_out,
        )
    finally:
        await policy.revoke(agent_id, str(sandbox))
        shutil.rmtree(sandbox, ignore_errors=True)


async def validate_candidate(
    registry: ProcedureRegistry, key: str, reports: Sequence[ReplayReport]
) -> None:
    """CANDIDATE -> VALIDATED on replay evidence that includes a held-out
    input. Promotion stays a separate, trusted operator action."""
    admission = registry.admissions.get(key)
    if admission is None or admission.status is not AdmissionStatus.CANDIDATE:
        raise RegistryError("NOT_A_CANDIDATE", key)
    failed = [report for report in reports if not report.passed()]
    if not reports or failed:
        raise RegistryError("REPLAY_FAILED", "; ".join(f"{r.status} {r.code}" for r in failed))
    if not any(report.held_out for report in reports):
        raise RegistryError("NO_HELD_OUT_REPLAY", "replay on an input no training trace used")
    await registry.mark_validated(
        key,
        json.dumps([report.model_dump(mode="json") for report in reports])[:4000],
    )
