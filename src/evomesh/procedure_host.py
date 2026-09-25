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
from evomesh.coordination import Performative, WorkItem, semantic_message
from evomesh.harness_tools import ToolContext
from evomesh.permissions import PermissionDeniedError
from evomesh.procedure_runtime import (
    ProcedureExecutor,
    ProcedureRegistry,
    ProcedureService,
    core_catalog,
)

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
    executor = ProcedureExecutor(environment.repository, registry)
    return ProcedureService(registry, executor, EnvironmentProcedureHost(environment))
