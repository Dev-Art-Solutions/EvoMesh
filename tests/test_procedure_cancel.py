"""Cancellation while an operation is in flight (closure audit 9739188, R03):
the late receipt is kept, the cancellation is not lost, and nothing after the
cancelled step ever runs -- across a lost settlement, a restart, an
operation proven not to have applied, and one nobody can prove either way."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from evomesh.procedure_runtime import (
    CORE_ADAPTERS,
    AdapterContext,
    AdapterResult,
    ExecStatus,
    OpState,
    ProcedureExecution,
    ProcedureExecutor,
    ReconcileState,
)
from evomesh.procedures import AdapterContract, RetrySemantics, SideEffect
from tests.procedure_fixtures import ref
from tests.test_procedures import Host, promote, setup

_PATH = {"type": "string", "maxLength": 500}


class GatedWrite:
    """A real local write that signals once it has happened, then holds its
    reply until released: the window in which an operator cancels."""

    contract = AdapterContract(
        adapter_id="test.gated_write",
        contract_version=1,
        argument_schema={
            "type": "object",
            "properties": {"path": _PATH},
            "required": ["path"],
            "additionalProperties": False,
        },
        result_schema={
            "type": "object",
            "properties": {"written": _PATH},
            "required": ["written"],
            "additionalProperties": False,
        },
        required_capabilities=("artifact.write",),
        side_effect=SideEffect.LOCAL_WRITE,
        retry=RetrySemantics.NONE,
        reconcile_supported=True,
    )

    def __init__(self, root: Path) -> None:
        self.root = root
        self.applied = asyncio.Event()
        self.release = asyncio.Event()
        self.invocations = 0
        self.verdict: ReconcileState | None = None  # None: judge from the file

    async def invoke(self, context: AdapterContext, arguments: dict[str, Any]) -> AdapterResult:
        self.invocations += 1
        (self.root / arguments["path"]).write_text('{"gated": true}', encoding="utf-8")
        self.applied.set()
        await self.release.wait()
        return AdapterResult(
            True, {"written": arguments["path"]}, receipt={"operation": context.operation_key}
        )

    async def reconcile(
        self, context: AdapterContext, arguments: dict[str, Any]
    ) -> tuple[ReconcileState, dict[str, Any] | None]:
        if self.verdict is not None:
            return self.verdict, None
        if (self.root / arguments["path"]).is_file():
            return ReconcileState.APPLIED, {"written": arguments["path"]}
        return ReconcileState.NOT_APPLIED, None


GATED_THEN_SENTINEL: dict[str, Any] = {
    "schema_version": 1,
    "procedure_id": "gated_then_sentinel",
    "revision": 1,
    "name": "A gated write, then a write that must never follow a cancellation",
    "entry_step_id": "gated",
    "goal_kind": "gated_then_sentinel",
    "parameter_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    "output_schema": {
        "type": "object",
        "properties": {"artifact_id": _PATH},
        "required": ["artifact_id"],
        "additionalProperties": False,
    },
    "required_capabilities": ["artifact.write"],
    "steps": [
        {
            "id": "gated",
            "kind": "tool",
            "adapter": "test.gated_write",
            "contract_version": 1,
            "arguments": {"path": {"literal": "gated.json"}},
            "next": "sentinel",
        },
        {
            "id": "sentinel",
            "kind": "tool",
            "adapter": "core.json_write",
            "contract_version": 1,
            "arguments": {
                "path": {"literal": "sentinel.json"},
                "value": {"literal": {"ran": True}},
            },
            "next": "done",
        },
        {
            "id": "done",
            "kind": "complete",
            "result": {"artifact_id": ref("result", "sentinel", "artifact_id")},
        },
    ],
}


class Crash(Exception):
    pass


def _never(at: str, key: str) -> None:
    return None


def _crash_at(point: str):  # type: ignore[no-untyped-def]
    def fault(at: str, key: str) -> None:
        if at == point:
            raise Crash(at)

    return fault


async def _gated(
    tmp_path: Path, *, fault_at: str | None = None
) -> tuple[ProcedureExecutor, Host, GatedWrite, ProcedureExecution]:
    repository, registry, _, host = await setup(tmp_path)
    gate = GatedWrite(host.root)
    registry.catalog.add_adapter(gate.contract)
    await promote(registry, GATED_THEN_SENTINEL)
    executor = ProcedureExecutor(
        repository,
        registry,
        adapters=(*CORE_ADAPTERS, gate),
        fault=_crash_at(fault_at) if fault_at else _never,
    )
    match = registry.select("gated_then_sentinel", {}, host.caps)
    assert match.definition is not None and match.admission is not None
    execution = await executor.start(
        match.definition,
        match.admission,
        agent_id="agent-1",
        goal_id="g",
        occurrence_id="g#0",
        parameters={},
    )
    return executor, host, gate, execution


def _restarted(executor: ProcedureExecutor, gate: GatedWrite) -> ProcedureExecutor:
    """A new process over the same database."""
    return ProcedureExecutor(
        executor.repository, executor.registry, adapters=(*CORE_ADAPTERS, gate)
    )


async def _drain(executor: ProcedureExecutor, execution_id: str, host: Host) -> None:
    for _ in range(6):
        await executor.advance(execution_id, host)


async def _assert_cancelled_without_sentinel(
    executor: ProcedureExecutor, execution_id: str, host: Host
) -> list[Any]:
    _, execution = await executor.load(execution_id)
    assert execution.status is ExecStatus.CANCELLED, execution.status
    assert execution.cancel_requested
    assert execution.status_reason == "operator cancelled"
    operations = await executor.operations(execution_id)
    assert not any(op.step_id == "sentinel" for op in operations)
    assert not (host.root / "sentinel.json").exists()
    assert "sentinel" not in execution.completed_steps
    return operations


async def test_r03a_cancel_during_an_applied_effect_keeps_the_receipt(tmp_path: Path) -> None:
    executor, host, gate, execution = await _gated(tmp_path)

    running = asyncio.create_task(executor.advance(execution.execution_id, host))
    await gate.applied.wait()
    cancelled = await executor.request_cancel(execution.execution_id, "operator cancelled")
    assert cancelled.status is ExecStatus.CANCEL_REQUESTED
    gate.release.set()
    await running
    await _drain(executor, execution.execution_id, host)

    operations = await _assert_cancelled_without_sentinel(executor, execution.execution_id, host)
    gated = next(op for op in operations if op.step_id == "gated")
    assert gated.state is OpState.APPLIED
    assert gated.receipt == {"operation": gated.operation_key}, "the late receipt is kept"
    assert (host.root / "gated.json").is_file()
    assert gate.invocations == 1


async def test_r03a_a_lost_settlement_reconciled_later_stays_cancelled(tmp_path: Path) -> None:
    # The process dies after the effect, before settling it; the operator
    # cancels; the next process reconciles the pending operation.
    executor, host, gate, execution = await _gated(tmp_path, fault_at="after_effect")
    gate.release.set()
    try:
        await executor.advance(execution.execution_id, host)
    except Crash:
        pass
    await executor.request_cancel(execution.execution_id, "operator cancelled")

    again = _restarted(executor, gate)
    await _drain(again, execution.execution_id, host)

    operations = await _assert_cancelled_without_sentinel(again, execution.execution_id, host)
    gated = next(op for op in operations if op.step_id == "gated")
    assert gated.state is OpState.APPLIED, "reconciliation recorded what happened"
    assert gate.invocations == 1, "and did not do it again"


async def test_r03b_an_operation_proven_not_applied_is_not_retried(tmp_path: Path) -> None:
    executor, host, gate, execution = await _gated(tmp_path, fault_at="after_claim")
    try:
        await executor.advance(execution.execution_id, host)
    except Crash:
        pass
    await executor.request_cancel(execution.execution_id, "operator cancelled")

    again = _restarted(executor, gate)
    await _drain(again, execution.execution_id, host)

    operations = await _assert_cancelled_without_sentinel(again, execution.execution_id, host)
    gated = next(op for op in operations if op.step_id == "gated")
    assert gated.state is OpState.REJECTED and gated.error == "NOT_APPLIED"
    assert gate.invocations == 0, "cancelled, so the unapplied write is never resumed"
    assert not (host.root / "gated.json").exists()


async def test_r03c_an_unknown_effect_stays_gated_with_the_intent_intact(tmp_path: Path) -> None:
    executor, host, gate, execution = await _gated(tmp_path, fault_at="after_claim")
    try:
        await executor.advance(execution.execution_id, host)
    except Crash:
        pass
    await executor.request_cancel(execution.execution_id, "operator cancelled")
    gate.verdict = ReconcileState.UNKNOWN

    again = _restarted(executor, gate)
    await _drain(again, execution.execution_id, host)

    _, held = await again.load(execution.execution_id)
    assert held.status is ExecStatus.NEEDS_RECONCILIATION
    assert held.cancel_requested, "reconciliation did not erase the cancellation"
    # Nothing replaces it: the occurrence resumes this execution, not a new plan.
    definition = again.registry.definitions["gated_then_sentinel@1"]
    admission = again.registry.admissions["gated_then_sentinel@1"]
    resumed = await again.start(
        definition,
        admission,
        agent_id="agent-1",
        goal_id="g",
        occurrence_id="g#0",
        parameters={},
    )
    assert resumed.execution_id == execution.execution_id
    assert len(await again.repository.list_procedure_executions(occurrence_id="g#0")) == 1

    outcome = await again.resolve_reconciliation(
        execution.execution_id, host, actor="operator:test", decision="not_applied"
    )

    assert outcome.kind == "cancelled"
    await _drain(again, execution.execution_id, host)
    await _assert_cancelled_without_sentinel(again, execution.execution_id, host)
    assert gate.invocations == 0


async def test_r03d_a_restart_during_pending_cancellation_keeps_it(tmp_path: Path) -> None:
    executor, host, gate, execution = await _gated(tmp_path, fault_at="after_effect")
    gate.release.set()
    try:
        await executor.advance(execution.execution_id, host)
    except Crash:
        pass
    await executor.request_cancel(execution.execution_id, "operator cancelled")
    row = await executor.repository.load_procedure_execution(execution.execution_id)
    assert row is not None and json.loads(row[1])["cancel_requested"] is True

    # Two processes in turn, and an operator asking again in between.
    first = _restarted(executor, gate)
    await first.advance(execution.execution_id, host)
    await first.request_cancel(execution.execution_id, "operator cancelled")
    second = _restarted(executor, gate)
    await _drain(second, execution.execution_id, host)

    await _assert_cancelled_without_sentinel(second, execution.execution_id, host)
    assert gate.invocations == 1
