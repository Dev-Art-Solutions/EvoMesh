from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from evomesh.contracts import AgentDefinition, AgentStatus, Message
from evomesh.messaging import MessageBus
from evomesh.models import ModelProvider, ModelUnavailableError
from evomesh.storage import SQLiteRepository


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentDefinition] = {}

    def register(self, definition: AgentDefinition) -> None:
        if definition.id in self._agents:
            raise ValueError(f"Agent id already registered: {definition.id}")
        if any(item.name.lower() == definition.name.lower() for item in self._agents.values()):
            raise ValueError(f"Agent name already registered: {definition.name}")
        self._agents[definition.id] = definition

    def all(self) -> list[AgentDefinition]:
        return list(self._agents.values())

    def get(self, agent_id_or_name: str) -> AgentDefinition:
        if agent_id_or_name in self._agents:
            return self._agents[agent_id_or_name]
        for agent in self._agents.values():
            if agent.name.lower() == agent_id_or_name.lower():
                return agent
        raise KeyError(agent_id_or_name)


@dataclass
class AgentRuntime:
    definition: AgentDefinition
    provider: ModelProvider
    bus: MessageBus
    repository: SQLiteRepository
    on_response: Callable[[Message], None] | None = None
    _task: asyncio.Task[None] | None = field(default=None, init=False)

    async def start(self) -> None:
        self.definition.status = AgentStatus.ACTIVE
        await self.repository.save_agent(self.definition)
        self.bus.register(self.definition.id)
        self._task = asyncio.create_task(self._run(), name=f"agent:{self.definition.name}")

    async def stop(self) -> None:
        self.definition.status = AgentStatus.STOPPED
        await self.repository.save_agent(self.definition)
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            incoming = await self.bus.receive(self.definition.id)
            error = False
            try:
                response = await self.provider.generate(
                    incoming.content,
                    system=(
                        f"You are {self.definition.name}. Identity: {self.definition.identity}. "
                        f"Purpose: {self.definition.purpose}."
                    ),
                    model=self.definition.model_name,
                )
            except (ModelUnavailableError, RuntimeError) as exc:
                error = True
                response = (
                    f"Model error for {self.definition.provider}:"
                    f"{self.definition.model_name}: {exc}"
                )
            outgoing = Message(
                sender_id=self.definition.id,
                recipient_id=incoming.sender_id,
                conversation_id=incoming.conversation_id,
                correlation_id=incoming.id,
                content=response,
                metadata={"error": error},
            )
            await self.bus.send(outgoing)
            if self.on_response:
                self.on_response(outgoing)


SYSTEM_AGENTS = (
    ("architect", "Agent Architect", "Interview humans and create structured agent definitions."),
    ("guardian", "Guardian", "Validate definitions, permissions, and environment health."),
    ("evaluator", "Evaluator", "Run deterministic checks and scenario evaluations."),
    ("evolver", "Environment Evolver", "Create and validate isolated candidate generations."),
)


def system_agent_definitions(provider: str, model: str) -> list[AgentDefinition]:
    return [
        AgentDefinition(
            id=agent_id,
            name=name,
            type="system",
            created_by="bootstrap",
            identity=name,
            purpose=purpose,
            provider=provider,
            model_name=model,
            status=AgentStatus.ACTIVE,
        )
        for agent_id, name, purpose in SYSTEM_AGENTS
    ]
