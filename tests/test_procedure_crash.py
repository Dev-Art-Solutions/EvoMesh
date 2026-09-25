"""Typed procedures across crashes (closure plan v2 AC-13; T36-T40): an
effect whose settlement was lost is reconciled, never blindly repeated, and
an effect nobody can prove stays explicit until an operator decides."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from evomesh.procedure_runtime import (
    CORE_ADAPTERS,
    AdapterContext,
    AdapterResult,
    ExecStatus,
    ProcedureExecutor,
    ProcedureRegistry,
    ReconcileState,
    RegistryError,
    core_catalog,
)
from evomesh.procedures import AdapterContract, RetrySemantics, SideEffect
from evomesh.storage import SQLiteRepository
from tests.procedure_fixtures import LOCAL_JSON_SNAPSHOT, ref
from tests.test_procedures import promote, run, setup, start_w1

ROOT = Path(__file__).resolve().parents[1]


class Crash(BaseException):
    """Stands in for the process dying at a fault point."""


def crash_at(point: str):  # type: ignore[no-untyped-def]
    def fault(at: str, key: str) -> None:
        if at == point:
            raise Crash(key)

    return fault


async def test_t36_crash_after_the_write_before_its_receipt_is_reconciled(
    tmp_path: Path,
) -> None:
    repository, registry, executor, host = await setup(tmp_path)
    await promote(registry, LOCAL_JSON_SNAPSHOT)
    (host.root / "in.json").write_text('{"a": 1}', encoding="utf-8")
    execution = await start_w1(registry, executor)
    await executor.advance(execution.execution_id, host)  # the read
    executor.fault = crash_at("after_effect")
    with pytest.raises(Crash):
        await executor.advance(execution.execution_id, host)
    target = host.root / "out" / "snap.json"
    written_at = target.stat().st_mtime_ns

    restarted = ProcedureExecutor(repository, registry)
    outcome = await run(restarted, execution.execution_id, host)

    assert outcome is not None and outcome.kind == "completed"
    assert target.stat().st_mtime_ns == written_at, "no second mutation"
    _, current = await restarted.load(execution.execution_id)
    assert current.completed_steps.count("write_snapshot") == 1


async def test_t36_crash_before_the_write_retries_it(tmp_path: Path) -> None:
    repository, registry, executor, host = await setup(tmp_path)
    await promote(registry, LOCAL_JSON_SNAPSHOT)
    (host.root / "in.json").write_text('{"a": 1}', encoding="utf-8")
    execution = await start_w1(registry, executor)
    await executor.advance(execution.execution_id, host)
    executor.fault = crash_at("after_claim")
    with pytest.raises(Crash):
        await executor.advance(execution.execution_id, host)
    assert not (host.root / "out" / "snap.json").exists()

    outcome = await run(ProcedureExecutor(repository, registry), execution.execution_id, host)

    assert outcome is not None and outcome.kind == "completed"
    assert (host.root / "out" / "snap.json").read_text(encoding="utf-8") == '{"a":1}\n'


async def test_a_process_that_keeps_crashing_runs_out_of_budget(tmp_path: Path) -> None:
    repository, registry, executor, host = await setup(tmp_path)
    await promote(registry, LOCAL_JSON_SNAPSHOT)
    (host.root / "in.json").write_text('{"a": 1}', encoding="utf-8")
    execution = await start_w1(registry, executor)
    await executor.advance(execution.execution_id, host)
    outcome = None
    for _ in range(200):
        crashing = ProcedureExecutor(repository, registry, fault=crash_at("after_claim"))
        try:
            outcome = await crashing.advance(execution.execution_id, host)
        except Crash:
            continue
        if outcome.kind == "failed":
            break

    assert outcome is not None and outcome.code == "BUDGET_EXHAUSTED"
    assert not (host.root / "out" / "snap.json").exists()


async def test_t39_restoring_an_older_ledger_does_not_repeat_the_write(tmp_path: Path) -> None:
    repository, registry, executor, host = await setup(tmp_path)
    await promote(registry, LOCAL_JSON_SNAPSHOT)
    (host.root / "in.json").write_text('{"a": 1}', encoding="utf-8")
    execution = await start_w1(registry, executor)
    await executor.advance(execution.execution_id, host)  # the read
    backup = tmp_path / "before-write.db"
    shutil.copy(tmp_path / "state.db", backup)
    outcome = await run(executor, execution.execution_id, host)
    assert outcome is not None and outcome.kind == "completed"
    target = host.root / "out" / "snap.json"
    written_at = target.stat().st_mtime_ns

    # The ledger goes back to before the write; the disk does not.
    shutil.copy(backup, tmp_path / "state.db")
    restored = SQLiteRepository(tmp_path / "state.db")
    await restored.initialize()
    registry = ProcedureRegistry(restored, core_catalog())
    await registry.load()
    replay = await run(ProcedureExecutor(restored, registry), execution.execution_id, host)

    assert replay is not None and replay.kind == "completed"
    assert target.stat().st_mtime_ns == written_at, "the receipt proved it applied"


# -- an effect nobody can prove ------------------------------------------------------


class Stamp:
    """A local write that keeps no receipt, so it cannot reconcile."""

    contract = AdapterContract(
        adapter_id="test.stamp",
        contract_version=1,
        argument_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "maxLength": 200}},
            "required": ["path"],
            "additionalProperties": False,
        },
        result_schema={
            "type": "object",
            "properties": {"stamped": {"type": "boolean"}},
            "required": ["stamped"],
            "additionalProperties": False,
        },
        required_capabilities=("artifact.write",),
        side_effect=SideEffect.LOCAL_WRITE,
        retry=RetrySemantics.NONE,
        reconcile_supported=False,
    )

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, context: AdapterContext, arguments: dict[str, Any]) -> AdapterResult:
        self.calls += 1
        target = context.tool_context.root / arguments["path"]
        target.write_text(str(self.calls), encoding="utf-8")
        return AdapterResult(True, {"stamped": True})

    async def reconcile(
        self, context: AdapterContext, arguments: dict[str, Any]
    ) -> tuple[ReconcileState, dict[str, Any] | None]:
        return ReconcileState.UNKNOWN, None


STAMPING = {
    "schema_version": 1,
    "procedure_id": "stamping",
    "revision": 1,
    "name": "Stamp a file",
    "entry_step_id": "stamp",
    "goal_kind": "stamping",
    "parameter_schema": {
        "type": "object",
        "properties": {"path": {"type": "string", "maxLength": 200}},
        "required": ["path"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {"stamped": {"type": "boolean"}},
        "required": ["stamped"],
        "additionalProperties": False,
    },
    "required_capabilities": ["artifact.write"],
    "steps": [
        {
            "id": "stamp",
            "kind": "tool",
            "adapter": "test.stamp",
            "contract_version": 1,
            "arguments": {"path": ref("goal", "parameters", "path")},
            "next": "done",
        },
        {
            "id": "done",
            "kind": "complete",
            "result": {"stamped": ref("result", "stamp", "stamped")},
        },
    ],
}


async def _stamping(tmp_path: Path):  # type: ignore[no-untyped-def]
    repository, _, _, host = await setup(tmp_path)
    catalog = core_catalog()
    stamp = Stamp()
    catalog.add_adapter(stamp.contract)
    registry = ProcedureRegistry(repository, catalog)
    await registry.load()
    await promote(registry, STAMPING)
    adapters = (*CORE_ADAPTERS, stamp)
    executor = ProcedureExecutor(repository, registry, adapters=adapters)
    match = registry.select("stamping", {"path": "s.txt"}, {"artifact.write"})
    assert match.definition is not None and match.admission is not None
    execution = await executor.start(
        match.definition,
        match.admission,
        agent_id="agent-1",
        goal_id="g",
        occurrence_id="g#0",
        parameters={"path": "s.txt"},
    )
    executor.fault = crash_at("after_effect")
    with pytest.raises(Crash):
        await executor.advance(execution.execution_id, host)
    restarted = ProcedureExecutor(repository, registry, adapters=adapters)
    return restarted, host, stamp, execution.execution_id


async def test_t40_an_unprovable_effect_waits_for_an_operator(tmp_path: Path) -> None:
    executor, host, stamp, execution_id = await _stamping(tmp_path)

    outcome = await executor.advance(execution_id, host)
    again = await executor.advance(execution_id, host)

    assert outcome.kind == "needs_reconciliation" and outcome.code == "UNRESOLVED_EFFECT"
    assert again.kind == "needs_reconciliation"
    assert stamp.calls == 1, "never repeated on its own"
    _, current = await executor.load(execution_id)
    assert current.status is ExecStatus.NEEDS_RECONCILIATION


async def test_t40_only_an_operator_resolves_it(tmp_path: Path) -> None:
    executor, host, stamp, execution_id = await _stamping(tmp_path)
    await executor.advance(execution_id, host)

    with pytest.raises(RegistryError):
        await executor.resolve_reconciliation(
            execution_id, host, actor="agent:helper", decision="not_applied"
        )
    await executor.resolve_reconciliation(
        execution_id, host, actor="operator:iliya", decision="not_applied"
    )
    outcome = await run(executor, execution_id, host)

    assert outcome is not None and outcome.kind == "completed"
    assert stamp.calls == 2, "retried once, on the operator's word"
    _, current = await executor.load(execution_id)
    assert any(item.get("kind") == "reconciliation" for item in current.evidence)


async def test_t40_an_operator_can_fail_it(tmp_path: Path) -> None:
    executor, host, stamp, execution_id = await _stamping(tmp_path)
    await executor.advance(execution_id, host)

    outcome = await executor.resolve_reconciliation(
        execution_id, host, actor="operator:iliya", decision="fail"
    )

    assert outcome.kind == "failed" and outcome.code == "RECONCILED_AS_FAILED"
    assert stamp.calls == 1


# -- T37: a real process dies mid-operation ----------------------------------------

CHILD = textwrap.dedent(
    """
    import asyncio, os, sys
    from pathlib import Path
    sys.path[:0] = [{src!r}, {root!r}]
    from evomesh.contracts import FilesystemGrant
    from evomesh.permissions import FilesystemPolicy
    from evomesh.procedure_runtime import ProcedureExecutor, ProcedureRegistry, core_catalog
    from evomesh.storage import SQLiteRepository
    from tests.procedure_fixtures import LOCAL_JSON_SNAPSHOT
    from tests.test_procedures import Host, promote, start_w1

    async def main():
        base = Path({base!r})
        repository = SQLiteRepository(base / "state.db")
        await repository.initialize()
        policy = FilesystemPolicy(repository)
        root = base / "playground"
        await policy.grant(FilesystemGrant(agent_id="agent-1", path=str(root), write=True))
        registry = ProcedureRegistry(repository, core_catalog())
        await registry.load()
        await promote(registry, LOCAL_JSON_SNAPSHOT)
        executor = ProcedureExecutor(repository, registry)
        host = Host(root, policy)
        execution = await start_w1(registry, executor)
        print(execution.execution_id, flush=True)
        await executor.advance(execution.execution_id, host)
        def die(point, key):
            if point == "after_effect":
                os._exit(3)
        executor.fault = die
        await executor.advance(execution.execution_id, host)

    asyncio.run(main())
    """
)


async def test_t37_a_killed_process_leaves_an_effect_that_is_reconciled(tmp_path: Path) -> None:
    base = tmp_path / "mesh"
    (base / "playground").mkdir(parents=True)
    (base / "playground" / "in.json").write_text('{"a": 1}', encoding="utf-8")
    script = tmp_path / "child.py"
    script.write_text(
        CHILD.format(src=str(ROOT / "src"), root=str(ROOT), base=str(base)), encoding="utf-8"
    )

    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert child.returncode == 3, child.stderr
    execution_id = child.stdout.strip().splitlines()[-1]
    target = base / "playground" / "out" / "snap.json"
    assert target.exists(), "the effect happened before the process died"
    written_at = target.stat().st_mtime_ns

    from evomesh.permissions import FilesystemPolicy
    from tests.test_procedures import Host

    repository = SQLiteRepository(base / "state.db")
    await repository.initialize()
    registry = ProcedureRegistry(repository, core_catalog())
    await registry.load()
    executor = ProcedureExecutor(repository, registry)
    host = Host(base / "playground", FilesystemPolicy(repository))
    outcome = await run(executor, execution_id, host)

    assert outcome is not None and outcome.kind == "completed"
    assert target.stat().st_mtime_ns == written_at, "no second mutation after the crash"
