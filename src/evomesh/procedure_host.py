"""The running mesh as a typed procedure's host (closure plan 12, 15).

ProcedureExecutor stays independent of Environment; this adapter is the one
place that knows how an agent's tool authority, model access, routing and
delivery are reached in a live mesh. Every authority decision reuses what the
harness already enforces: the agent's own FilesystemPolicy grants, never a
human-authority context (policy=None).
"""

from __future__ import annotations

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
    ProcedureExecution,
    ProcedureExecutor,
    ProcedureRegistry,
    ProcedureService,
    core_catalog,
    occurrence_id,
)
from evomesh.procedure_traces import TraceRecorder, typed_harness_tools

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
