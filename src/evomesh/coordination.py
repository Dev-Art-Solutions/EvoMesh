"""Structured multi-agent work, capability routing, and semantic messages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from evomesh.contracts import AgentDefinition, AgentRuntimeState, Message, now_utc

# The goal kind an accepted DELEGATE becomes on the receiving agent.
DELEGATED_GOAL_KIND = "delegated_work"
# The capability a stalled agent's diagnosis is delegated to.
ASSISTANCE_CAPABILITY = "health.verify"


class Performative(StrEnum):
    INFORM = "inform"
    QUERY = "query"
    REQUEST = "request"
    DELEGATE = "delegate"
    PROPOSE = "propose"
    ACCEPT = "accept"
    REJECT = "reject"
    RESULT = "result"
    FAILURE = "failure"
    HELP_REQUEST = "help_request"


class WorkStatus(StrEnum):
    PENDING = "pending"
    BLOCKED = "blocked"
    OFFERED = "offered"
    ASSIGNED = "assigned"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkBudget(BaseModel):
    max_attempts: int = 3
    max_model_calls: int | None = None
    max_seconds: float | None = None


class WorkItem(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:10])
    parent_goal_id: str
    improvement_id: str | None = None
    type: str = "task"
    objective: str
    required_capabilities: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected_outputs: list[str] = Field(default_factory=list)
    success_conditions: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    assigned_agent_id: str | None = None
    status: WorkStatus = WorkStatus.PENDING
    attempts: int = 0
    failure_history: list[str] = Field(default_factory=list)
    budget: WorkBudget = Field(default_factory=WorkBudget)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

    def assign(self, agent_id: str) -> None:
        self.assigned_agent_id = agent_id
        self.status = WorkStatus.ASSIGNED
        self.updated_at = now_utc()

    def fail(self, reason: str) -> None:
        self.attempts += 1
        self.failure_history.append(reason)
        self.status = (
            WorkStatus.FAILED
            if self.attempts >= self.budget.max_attempts
            else WorkStatus.PENDING
        )
        if self.status is WorkStatus.PENDING:
            self.assigned_agent_id = None
        self.updated_at = now_utc()


def semantic_message(
    performative: Performative,
    *,
    sender_id: str,
    recipient_id: str | None,
    task_id: str = "",
    goal_id: str = "",
    payload: dict[str, Any] | None = None,
    content: str = "",
) -> Message:
    return Message(
        sender_id=sender_id,
        recipient_id=recipient_id,
        type="acl",
        content=content,
        performative=performative.value,
        task_id=task_id or None,
        goal_id=goal_id or None,
        payload=dict(payload or {}),
    )


class CapabilityRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentDefinition] = {}

    def register(self, agent: AgentDefinition) -> None:
        self._agents[agent.id] = agent

    def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    def candidates(self, required: list[str]) -> list[AgentDefinition]:
        needed = set(required)
        return sorted(
            (
                agent
                for agent in self._agents.values()
                if needed.issubset(set(agent.capabilities))
            ),
            key=lambda agent: (agent.name.lower(), agent.id),
        )


@dataclass(frozen=True)
class Bid:
    agent_id: str
    capability_match: float
    load: int
    success_rate: float
    score: float


class ContractNet:
    """Deterministic capability + load + history selection policy."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self.registry = registry

    def bids(
        self,
        item: WorkItem,
        *,
        states: dict[str, AgentRuntimeState] | None = None,
        active_work: list[WorkItem] | None = None,
        history: dict[str, tuple[int, int]] | None = None,
        exclude_agent_ids: set[str] | None = None,
    ) -> list[Bid]:
        states = states or {}
        active_work = active_work or []
        history = history or {}
        exclude_agent_ids = exclude_agent_ids or set()
        required = set(item.required_capabilities)
        bids: list[Bid] = []
        for agent in self.registry.candidates(item.required_capabilities):
            if agent.id in exclude_agent_ids:
                continue
            offered = set(agent.capabilities)
            match = len(required & offered) / len(required) if required else 1.0
            load = sum(
                work.assigned_agent_id == agent.id
                and work.status in {WorkStatus.ASSIGNED, WorkStatus.ACTIVE}
                for work in active_work
            )
            successes, failures = history.get(agent.id, (0, 0))
            success_rate = successes / (successes + failures) if successes + failures else 0.5
            availability = 0.0 if states.get(agent.id) is None else 0.1
            score = match * 0.65 + success_rate * 0.25 + availability - load * 0.1
            bids.append(Bid(agent.id, match, load, success_rate, score))
        return sorted(bids, key=lambda bid: (-bid.score, bid.agent_id))

    def award(self, item: WorkItem, **kwargs: Any) -> Bid | None:
        bids = self.bids(item, **kwargs)
        if not bids:
            return None
        item.assign(bids[0].agent_id)
        return bids[0]
