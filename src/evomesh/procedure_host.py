"""The running mesh as a typed procedure's host (closure plan 12, 15).

ProcedureExecutor stays independent of Environment; this adapter is the one
place that knows how an agent's tool authority, model access, routing and
delivery are reached in a live mesh. Every authority decision reuses what the
harness already enforces: the agent's own FilesystemPolicy grants, never a
human-authority context (policy=None).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evomesh.cognitive_services import CognitiveServiceType, ModelInvocationReason
from evomesh.contracts import AgentDefinition, Goal
from evomesh.coordination import Performative, WorkItem, semantic_message
from evomesh.goal_manager import GoalEvaluationContext, GoalManager
from evomesh.harness_tools import Tool, ToolContext
from evomesh.permissions import PermissionDeniedError
from evomesh.procedure_runtime import (
    ExecStatus,
    ProcedureExecution,
    ProcedureExecutor,
    ProcedureRegistry,
    ProcedureService,
    RegistryError,
    core_catalog,
    issues_text,
    occurrence_id,
    typed_request,
)
from evomesh.procedure_traces import TraceRecorder, typed_harness_tools
from evomesh.procedures import validate_definition

if TYPE_CHECKING:
    from evomesh.environment import Environment

logger = logging.getLogger(__name__)

PATH_INPUT_KEYS = ("path", "source", "destination")


def _path_inputs(inputs: dict[str, Any]) -> list[str]:
    """Input values that name a file: the ones whose authority must travel
    with the requester, so delegation cannot launder a denied read."""
    return [
        str(value)
        for key, value in inputs.items()
        if isinstance(value, str) and value and (key in PATH_INPUT_KEYS or key.endswith("_path"))
    ]


class EnvironmentProcedureHost:
    def __init__(self, environment: Environment) -> None:
        self.environment = environment

    def capabilities(self, agent_id: str) -> set[str]:
        registry = self.environment.registry
        try:
            definition = registry.get(agent_id)
        except KeyError:
            return set()
        return set(definition.capabilities)

    def root_for(self, agent_id: str) -> Path:
        definition = self.environment.registry.get(agent_id)
        if definition.harness_root:
            return Path(definition.harness_root)
        return self.environment.default_harness_root(definition)

    def tool_context(self, agent_id: str) -> ToolContext:
        return ToolContext(
            root=self.root_for(agent_id),
            policy=self.environment.permissions,
            agent_id=agent_id,
            allow_write=self.environment.settings.harness.allow_write,
        )

    @property
    def blackboard(self) -> Any:
        return self.environment.blackboard

    async def think(
        self,
        agent_id: str,
        prompt: str,
        *,
        schema: dict[str, Any] | Any,
        service: str,
        reason: str,
        goal_id: str,
        task_id: str,
    ) -> str:
        environment = self.environment
        definition = environment.registry.get(agent_id)
        provider = environment.providers.get(definition.provider)
        if provider is None:
            raise RuntimeError(f"provider {definition.provider!r} is unavailable")
        try:
            service_type = CognitiveServiceType(service)
        except ValueError:
            service_type = CognitiveServiceType.OTHER
        try:
            invocation = ModelInvocationReason(reason)
        except ValueError:
            invocation = ModelInvocationReason.OTHER
        return await environment.cognition.generate(
            provider,
            prompt,
            service=service_type,
            reason=invocation,
            provider_name=definition.provider,
            agent_id=agent_id,
            goal_id=goal_id,
            task_id=task_id,
            system="You perform one typed reasoning step. Answer with JSON only.",
            model=definition.model_name,
            num_ctx=environment.resolve_num_ctx(definition.provider, definition.model_name),
            format=dict(schema),
        )

    async def _may_read(self, agent_id: str, root: Path, path: str) -> bool:
        target = Path(path)
        if not target.is_absolute():
            target = root / target
        try:
            await self.environment.permissions.require(agent_id, target, "read")
        except PermissionDeniedError:
            return False
        return True

    async def route(self, work: WorkItem, requester_id: str) -> tuple[str | None, str]:
        """Capability-eligible, running, outside the causation chain, and
        allowed to read every path the requester itself may read for this
        work. A requester without that read cannot delegate it away."""
        environment = self.environment
        requester_root = self.root_for(requester_id)
        paths = _path_inputs(work.inputs)
        for path in paths:
            if not await self._may_read(requester_id, requester_root, path):
                return None, f"the requester may not read {path}"
        excluded = {requester_id, *work.causation_chain}
        states = environment.runtime_states()
        bids = environment.contract_net.bids(
            work,
            states=states,
            active_work=list(environment.blackboard.work_items.values()),
            history=environment.blackboard.work_history(),
            exclude_agent_ids=excluded,
        )
        refused: list[str] = []
        for bid in bids:
            if bid.agent_id not in environment.runtimes:
                refused.append(f"{bid.agent_id}: not running")
                continue
            root = self.root_for(bid.agent_id)
            denied = [path for path in paths if not await self._may_read(bid.agent_id, root, path)]
            if denied:
                refused.append(f"{bid.agent_id}: may not read {denied[0]}")
                continue
            return bid.agent_id, "eligible"
        if not bids:
            return None, "no running peer offers " + ", ".join(work.required_capabilities)
        return None, "; ".join(refused)[:300]

    async def deliver(self, work: WorkItem, requester_id: str, assignee_id: str) -> None:
        environment = self.environment
        environment.blackboard.publish_work(work)
        await environment.bus.send(
            semantic_message(
                Performative.DELEGATE,
                sender_id=requester_id,
                recipient_id=assignee_id,
                task_id=work.id,
                goal_id=work.parent_goal_id,
                payload=work.model_dump(mode="json"),
                content=work.objective,
            )
        )
        await environment.repository.save_state("blackboard", environment.blackboard.dump())


def build_procedure_service(environment: Environment) -> ProcedureService:
    catalog = core_catalog()
    registry = ProcedureRegistry(environment.repository, catalog)
    registry.enabled = environment.settings.procedures.enabled
    executor = ProcedureExecutor(environment.repository, registry)
    return ProcedureService(registry, executor, EnvironmentProcedureHost(environment))


class ProcedureLearning:
    """Trace capture for one mesh: tools for agents' harness jobs, and the
    trace a verified occurrence leaves behind (closure plan 16.1)."""

    def __init__(self, environment: Environment) -> None:
        self.environment = environment
        self.recorder = TraceRecorder(environment.repository)
        self.task_ids: dict[str, set[str]] = {}

    def served(self, agent_id: str) -> tuple[str, str] | None:
        """The goal occurrence an agent's harness job is working for: the
        one its current intention serves. A typed execution needs no trace."""
        registry = self.environment.registry
        try:
            mind = registry.get(agent_id).mind
        except KeyError:
            return None
        intention = mind.current_intention()
        if intention is None or intention.execution_id or intention.plan.startswith("typed:"):
            return None
        goal = next((item for item in mind.goals if item.id == intention.goal_id), None)
        if goal is None or not goal.is_open:
            return None
        return goal.id, occurrence_id(goal)

    def tools_for(self, agent_id: str, *, task_id: str, allow_write: bool) -> tuple[Tool, ...]:
        if not self.environment.settings.procedures.collect_traces:
            return ()

        def served() -> tuple[str, str] | None:
            current = self.served(agent_id)
            if current is not None:
                self.task_ids.setdefault(current[1], set()).add(task_id)
            return current

        return typed_harness_tools(self.recorder, agent_id, served, allow_write=allow_write)

    async def goal_completed(self, agent: AgentDefinition, goal: Goal) -> list[str]:
        """Store a trace for each pending occurrence of ``goal``. Its basis is
        the goal's own success conditions, re-checked now against the files
        they name; a goal without any has no authoritative evidence."""
        pending = [
            key for key, item in self.recorder.pending.items() if item["goal_id"] == goal.id
        ]
        if not pending:
            return []
        manager = GoalManager(agent.mind)
        root = (
            Path(agent.harness_root)
            if agent.harness_root
            else self.environment.default_harness_root(agent)
        )
        context = GoalEvaluationContext(artifact_root=root)
        conditions = list(goal.success_conditions)
        met = [item for item in conditions if manager.condition_met(item, context)]
        basis = (
            [f"{item.kind.value}:{item.key or item.path}" for item in met]
            if conditions and len(met) == len(conditions)
            else []
        )
        tasks = set().union(*(self.task_ids.pop(key, set()) for key in pending))
        calls = sum(
            1
            for record in self.environment.cognition.metrics.records
            if record.agent_id == agent.id
            and (record.goal_id == goal.id or record.task_id in tasks)
        )
        traces = await self.recorder.finalize(goal, basis=basis, model_calls=calls)
        return [trace.trace_id for trace in traces]

    def goal_failed(self, goal_id: str) -> None:
        self.recorder.discard(goal_id)


def execution_label(execution: ProcedureExecution) -> str:
    return f"{execution.procedure_id}@{execution.revision} {execution.status.value}"


# -- operator controls (closure plan 21) -------------------------------------------

OPERATOR_USAGE = """Usage:
  /typed [list]                         admitted, candidate, degraded, retired revisions
  /typed show <procedure@revision>      definition, digest, admission history
  /typed validate <procedure@revision>  static validation only; nothing runs
  /typed approve <procedure@revision> <digest-prefix>  promote a validated revision
  /typed degrade|retire <procedure@revision> <reason>
  /typed explain <agent> <goal-id>      why a goal did or did not select a procedure
  /typed executions [agent]             open executions and why each is waiting
  /typed execution <execution-id>       one execution: step, budget, wait, receipts
  /typed cancel|pause|resume <execution-id>
  /typed reconcile <execution-id> recheck|not_applied|fail
  /typed disable|enable                 emergency stop for new typed executions"""

MINIMUM_DIGEST_PREFIX = 12


def execution_reason(execution: ProcedureExecution) -> str:
    """Why an execution is where it is, in the plan's own words -- never a
    bare "idle" for something that still owes an outcome (plan 21)."""
    status = execution.status
    if status is ExecStatus.WAITING and execution.wait:
        subject = execution.wait.get("subject")
        return {
            "work": "WAITING_FOR_CHILD",
            "evidence": "WAITING_FOR_EVIDENCE",
            "time": "WAITING_FOR_TIME",
        }.get(str(subject), "WAITING")
    if execution.next_eligible_at is not None and not execution.terminal:
        return "WAITING_RETRY"
    return {
        ExecStatus.PAUSED: "PAUSED",
        ExecStatus.NEEDS_RECONCILIATION: "NEEDS_RECONCILIATION",
        ExecStatus.CANCEL_REQUESTED: "CANCEL_REQUESTED",
        ExecStatus.FAILED: f"FAILED ({execution.status_reason})",
        ExecStatus.CANCELLED: "CANCELLED",
        ExecStatus.COMPLETED: "COMPLETED",
    }.get(status, "RUNNING")


class ProcedureOperator:
    """The operator's typed-procedure controls, as plain text in and out.

    Every decision here is deterministic and explained from structured facts
    -- no model call is spent explaining why a check failed. The actor is the
    human at the console; approval is bound to the digest they typed."""

    def __init__(self, environment: Environment, actor: str = "operator:console") -> None:
        self.environment = environment
        self.actor = actor

    @property
    def service(self) -> ProcedureService:
        return self.environment.procedures

    async def run(self, parts: list[str]) -> str:
        action = parts[1] if len(parts) > 1 else "list"
        args = parts[2:]
        try:
            if action == "list" and not args:
                return self.list()
            if action == "show" and len(args) == 1:
                return self.show(args[0])
            if action == "validate" and len(args) == 1:
                return self.validate(args[0])
            if action == "approve" and len(args) == 2:
                return await self.approve(args[0], args[1])
            if action in {"degrade", "retire"} and len(args) >= 2:
                return await self.revoke(action, args[0], " ".join(args[1:]))
            if action == "explain" and len(args) == 2:
                return self.explain(args[0], args[1])
            if action == "executions" and len(args) <= 1:
                return await self.executions(args[0] if args else None)
            if action == "execution" and len(args) == 1:
                return await self.execution(args[0])
            if action in {"cancel", "pause", "resume"} and len(args) == 1:
                return await self.control(action, args[0])
            if action == "reconcile" and len(args) == 2:
                return await self.reconcile(args[0], args[1])
            if action in {"disable", "enable"} and not args:
                self.service.registry.enabled = action == "enable"
                return (
                    "Typed execution is enabled."
                    if action == "enable"
                    else "Typed execution is disabled: no new typed execution starts; "
                    "running ones still settle, pause or wait for reconciliation."
                )
        except RegistryError as exc:
            return f"Refused: {exc.code}: {exc.message}"
        except KeyError as exc:
            return f"Unknown: {exc.args[0]}"
        return OPERATOR_USAGE

    def list(self) -> str:
        registry = self.service.registry
        rows = [
            f"{admission.key} {admission.status.value} source={admission.source}"
            f"{' by ' + admission.approved_by if admission.approved_by else ''}"
            f" kinds={','.join(admission.approved_goal_kinds) or '-'}"
            f"{' (' + admission.reason + ')' if admission.reason else ''}"
            for admission in sorted(registry.admissions.values(), key=lambda item: item.key)
        ]
        state = "enabled" if registry.enabled else "DISABLED"
        return "\n".join([f"Typed execution is {state}.", *rows]) if rows else (
            f"Typed execution is {state}. No typed procedures are registered."
        )

    def show(self, key: str) -> str:
        registry = self.service.registry
        admission = registry.admissions[key]
        definition = registry.definitions.get(key)
        lines = [
            f"{key}: {admission.status.value} (source {admission.source}, owner {admission.owner})",
            f"digest: {admission.digest or (definition.digest() if definition else '-')}",
        ]
        if definition is not None:
            lines.append(f"goal kind: {definition.goal_kind}; name: {definition.name}")
            lines.append(f"capabilities: {', '.join(definition.required_capabilities) or '-'}")
            lines.append(
                "steps: " + " -> ".join(f"{step.id}[{step.kind}]" for step in definition.steps)
            )
        if admission.issues:
            lines.append(f"issues: {issues_text(admission.issues)}")
        lines += [
            f"  {entry.get('at', '')[:19]} {entry.get('event')}: {entry.get('detail', '')}"
            for entry in admission.history[-10:]
        ]
        return "\n".join(lines)

    def validate(self, key: str) -> str:
        registry = self.service.registry
        definition = registry.definitions.get(key)
        if definition is None:
            raise KeyError(key)
        report = validate_definition(definition, registry.catalog, registry.limits)
        if report.ok:
            return (
                f"{key} is valid: capabilities {sorted(report.effective_capabilities)}, "
                f"effects {sorted(report.side_effects)}, "
                f"at most {report.max_model_calls} model call(s). Nothing was run."
            )
        return f"{key} is invalid: {issues_text(report.issues)}"

    async def approve(self, key: str, prefix: str) -> str:
        registry = self.service.registry
        definition = registry.definitions.get(key)
        if definition is None:
            raise KeyError(key)
        digest = definition.digest()
        if len(prefix) < MINIMUM_DIGEST_PREFIX or not digest.startswith(prefix):
            return (
                f"Refused: approval names content by digest; give at least "
                f"{MINIMUM_DIGEST_PREFIX} characters of {key}'s digest (see /typed show {key})."
            )
        admission = await registry.approve(key, actor=self.actor, digest=digest)
        return f"{key} is promoted by {admission.approved_by} for {admission.approved_goal_kinds}."

    async def revoke(self, action: str, key: str, reason: str) -> str:
        registry = self.service.registry
        admission = await (registry.degrade if action == "degrade" else registry.retire)(
            key, f"{self.actor}: {reason}"
        )
        return f"{key} is {admission.status.value}; running executions settle or are refused."

    def explain(self, agent_name: str, goal_id: str) -> str:
        agent = self.environment.registry.get(agent_name)
        goal = agent.mind.goal(goal_id)
        kind, parameters, explicit, _ = typed_request(goal)
        capabilities = self.service.host.capabilities(agent.id)
        match = self.service.registry.select(kind, parameters, capabilities, explicit=explicit)
        chosen = f" -> {match.admission.key}" if match.admission is not None else ""
        reasons = "; ".join(match.reasons) or "-"
        return (
            f"goal {goal.id} kind={kind or '-'} occurrence={occurrence_id(goal)}: "
            f"{match.selection.value}{chosen}\n  capabilities: {sorted(capabilities)}\n"
            f"  reasons: {reasons}"
        )

    async def executions(self, agent_name: str | None) -> str:
        repository = self.environment.repository
        rows = await repository.list_procedure_executions(open_only=True)
        wanted = self.environment.registry.get(agent_name).id if agent_name else None
        lines: list[str] = []
        for _, payload in rows:
            execution = ProcedureExecution.model_validate_json(payload)
            if wanted is not None and execution.agent_id != wanted:
                continue
            lines.append(
                f"{execution.execution_id} {execution.procedure_id}@{execution.revision} "
                f"agent={execution.agent_id} step={execution.current_step_id} "
                f"{execution_reason(execution)}"
            )
        return "\n".join(lines) or "No open typed executions."

    async def execution(self, execution_id: str) -> str:
        executor = self.service.executor
        _, execution = await executor.load(execution_id)
        operations = await self.environment.repository.list_procedure_operations(execution_id)
        budget = execution.budget
        lines = [
            f"{execution.execution_id}: {execution.procedure_id}@{execution.revision} "
            f"{execution_reason(execution)} ({execution.path})",
            f"goal {execution.goal_id} occurrence {execution.occurrence_id}; "
            f"step {execution.current_step_id}; done {execution.completed_steps}",
            f"budget: model {budget.model_calls}/{budget.max_model_calls}, "
            f"tools {budget.tool_attempts}/{budget.max_tool_attempts}, "
            f"steps {budget.step_attempts}/{budget.max_step_attempts}, "
            f"children {budget.child_model_calls_reserved}/{budget.max_child_model_calls}",
        ]
        if execution.wait:
            lines.append(f"waiting: {json.dumps(execution.wait, default=str)}")
        for state, payload in operations:
            record = json.loads(payload)
            lines.append(
                f"  {record.get('operation_key')} {state} {record.get('kind')} "
                f"{record.get('adapter') or ''} {record.get('error') or ''}".rstrip()
            )
        return "\n".join(lines)

    async def control(self, action: str, execution_id: str) -> str:
        executor = self.service.executor
        if action == "cancel":
            execution = await executor.request_cancel(execution_id, f"{self.actor} cancelled")
            await executor.advance(execution_id, self.service.host)
        else:
            execution = await executor.set_paused(execution_id, action == "pause")
        _, execution = await executor.load(execution_id)
        return f"{execution_id} is {execution_reason(execution)}."

    async def reconcile(self, execution_id: str, decision: str) -> str:
        outcome = await self.service.executor.resolve_reconciliation(
            execution_id, self.service.host, actor=self.actor, decision=decision
        )
        return f"{execution_id}: {outcome.kind} {outcome.code} {outcome.message}".rstrip()
