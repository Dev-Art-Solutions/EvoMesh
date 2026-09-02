from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def now_utc() -> datetime:
    return datetime.now(UTC)


def _short_id() -> str:
    return uuid4().hex[:8]


class AgentStatus(StrEnum):
    """Desired lifecycle state. Persisted with the definition.

    This answers "should this agent be running?", never "what is it doing right now?".
    The observed side of that question lives in ``AgentPhase``.
    """

    CANDIDATE = "candidate"
    ACTIVE = "active"
    STOPPED = "stopped"


class AgentPhase(StrEnum):
    """Observed runtime phase. Derived from a live loop, never persisted as truth."""

    OFFLINE = "offline"
    STARTING = "starting"
    IDLE = "idle"
    THINKING = "thinking"
    ACTING = "acting"
    # Blocked on a harness worker, which will certainly come back -- unlike
    # WAITING_HUMAN, which is blocked on a person who may not.
    AWAITING_HARNESS = "awaiting-harness"
    WAITING_HUMAN = "waiting-human"
    ERROR = "error"


class Autonomy(StrEnum):
    """How an agent spends a cycle tick."""

    CYCLIC = "cyclic"
    REACTIVE = "reactive"


class GoalStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"


OPEN_GOAL_STATUSES = frozenset({GoalStatus.PENDING, GoalStatus.ACTIVE, GoalStatus.BLOCKED})


class Goal(BaseModel):
    id: str = Field(default_factory=_short_id)
    description: str
    status: GoalStatus = GoalStatus.PENDING
    priority: int = 5
    attempts: int = 0
    max_attempts: int = 6
    recurring: bool = False
    notes: list[str] = Field(default_factory=list)
    last_error: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

    @property
    def is_open(self) -> bool:
        if self.status not in OPEN_GOAL_STATUSES:
            return False
        # attempts is a failure budget, not a cycle counter, so a standing goal
        # such as Guardian's health sweep stays open indefinitely.
        return self.recurring or self.attempts < self.max_attempts

    def note(self, text: str, *, keep: int = 8) -> None:
        cleaned = text.strip()
        if cleaned:
            self.notes = [*self.notes, cleaned][-keep:]
        self.updated_at = now_utc()


def belief_key(statement: str) -> str:
    """Derive a stable key from a free-form statement.

    Beliefs that arrive without a key are observations rather than measurements,
    so the key is just their opening words. Behaviors that perceive something
    structured (a provider's health, an agent's phase) pass an explicit key, and
    that is what lets the next percept revise the belief instead of stacking a
    near-duplicate next to it.
    """
    words = [part for part in "".join(
        character if character.isalnum() else " " for character in statement.lower()
    ).split()][:6]
    return ".".join(words) or "belief"


class Belief(BaseModel):
    """One thing the agent holds true, revisable by key."""

    key: str = ""
    statement: str
    source: str = "self"
    confidence: float = 1.0
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def _ensure_key(self) -> Belief:
        if not self.key:
            self.key = belief_key(self.statement)
        return self


class StepStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class PlanStep(BaseModel):
    description: str
    action: str = "think"
    status: StepStatus = StepStatus.PENDING
    result: str = ""

    def render(self) -> str:
        mark = {StepStatus.DONE: "x", StepStatus.FAILED: "!", StepStatus.PENDING: " "}
        return f"[{mark[self.status]}] {self.description}"


class IntentionStatus(StrEnum):
    ACTIVE = "active"
    ACHIEVED = "achieved"
    IMPOSSIBLE = "impossible"
    DROPPED = "dropped"


class Intention(BaseModel):
    """A goal the agent has committed to, plus the plan it is executing.

    Commitment is what separates an intention from a desire: once adopted, the
    agent keeps executing this plan across cycles and does not re-deliberate
    every tick. It reconsiders only when the plan runs out, the goal changes, or
    a belief the plan depends on is revised.
    """

    id: str = Field(default_factory=_short_id)
    goal_id: str
    plan: str = "ad-hoc"
    steps: list[PlanStep] = Field(default_factory=list)
    cursor: int = 0
    status: IntentionStatus = IntentionStatus.ACTIVE
    context_keys: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_step(cls, data: Any) -> Any:
        # Records written before plans existed carried a single free-text step.
        if isinstance(data, dict) and "step" in data and "steps" not in data:
            data = dict(data)
            step = str(data.pop("step", "")).strip()
            data["steps"] = [{"description": step}] if step else []
        return data

    @property
    def current(self) -> PlanStep | None:
        if self.status is not IntentionStatus.ACTIVE:
            return None
        return self.steps[self.cursor] if 0 <= self.cursor < len(self.steps) else None

    @property
    def exhausted(self) -> bool:
        return self.cursor >= len(self.steps)

    def advance(self, result: str, *, failed: bool = False) -> None:
        step = self.current
        if step is not None:
            step.status = StepStatus.FAILED if failed else StepStatus.DONE
            step.result = result[:300]
        self.cursor += 1
        self.updated_at = now_utc()

    def finish(self, status: IntentionStatus) -> None:
        self.status = status
        self.updated_at = now_utc()

    def render(self) -> str:
        return "\n".join(step.render() for step in self.steps) or "(no steps)"


@dataclass(frozen=True)
class BeliefChange:
    """What belief revision actually changed this cycle."""

    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self.added) | frozenset(self.updated)

    def __bool__(self) -> bool:
        return bool(self.added or self.updated)


class MindState(BaseModel):
    beliefs: list[Belief] = Field(default_factory=list)
    goals: list[Goal] = Field(default_factory=list)
    intentions: list[Intention] = Field(default_factory=list)

    def open_goals(self) -> list[Goal]:
        return sorted(
            (goal for goal in self.goals if goal.is_open),
            key=lambda goal: (goal.priority, goal.created_at),
        )

    def next_goal(self) -> Goal | None:
        return next(iter(self.open_goals()), None)

    def goal(self, goal_id: str) -> Goal:
        for goal in self.goals:
            if goal.id == goal_id:
                return goal
        raise KeyError(goal_id)

    def add_goal(
        self, description: str, *, priority: int = 5, recurring: bool = False
    ) -> Goal:
        goal = Goal(description=description.strip(), priority=priority, recurring=recurring)
        self.goals.append(goal)
        return goal

    # -- beliefs --------------------------------------------------------

    def belief(self, key: str) -> Belief | None:
        return next((item for item in self.beliefs if item.key == key), None)

    def believes(self, key: str, statement: str) -> bool:
        held = self.belief(key)
        return held is not None and held.statement == statement

    def revise(self, percepts: Sequence[Belief], *, keep: int = 40) -> BeliefChange:
        """The belief revision function: fold percepts into the belief base.

        A percept whose key is already held replaces it rather than piling up
        beside it, which is the whole point of keying beliefs. Which keys moved
        is returned, because that is what decides whether a committed intention
        is still worth keeping.
        """
        added: list[str] = []
        updated: list[str] = []
        for percept in percepts:
            if not percept.statement.strip():
                continue
            held = self.belief(percept.key)
            if held is None:
                self.beliefs.append(percept)
                added.append(percept.key)
            elif held.statement != percept.statement:
                held.statement = percept.statement
                held.source = percept.source
                held.confidence = percept.confidence
                held.updated_at = now_utc()
                updated.append(percept.key)
            else:
                held.updated_at = now_utc()
        if len(self.beliefs) > keep:
            # Drop the least recently confirmed beliefs, not the oldest ones: a
            # fact that keeps being re-perceived is still current.
            self.beliefs = sorted(self.beliefs, key=lambda item: item.updated_at)[-keep:]
        return BeliefChange(tuple(added), tuple(updated))

    def remember(self, statement: str, source: str = "self", *, keep: int = 40) -> None:
        cleaned = statement.strip()
        if cleaned:
            self.revise([Belief(statement=cleaned, source=source)], keep=keep)

    def forget(self, key: str) -> bool:
        before = len(self.beliefs)
        self.beliefs = [item for item in self.beliefs if item.key != key]
        return len(self.beliefs) != before

    # -- intentions -----------------------------------------------------

    def current_intention(self) -> Intention | None:
        return next(
            (item for item in self.intentions if item.status is IntentionStatus.ACTIVE),
            None,
        )

    def commit(
        self,
        goal_id: str,
        steps: Sequence[str],
        *,
        plan: str = "ad-hoc",
        action: str = "think",
        context_keys: Sequence[str] = (),
        keep: int = 12,
    ) -> Intention:
        """Adopt one plan for one goal. Any previous commitment is dropped."""
        for item in self.intentions:
            if item.status is IntentionStatus.ACTIVE:
                item.finish(IntentionStatus.DROPPED)
        intention = Intention(
            goal_id=goal_id,
            plan=plan,
            steps=[PlanStep(description=text, action=action) for text in steps if text.strip()],
            context_keys=list(context_keys),
        )
        self.intentions = [*self.intentions, intention][-keep:]
        return intention

    def intend(self, goal_id: str, step: str, *, keep: int = 12) -> Intention:
        """Commit to a single ad-hoc step. Kept for callers that have no plan."""
        return self.commit(goal_id, [step], keep=keep)


class AgentDefinition(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    type: str = "agent"
    generation: int = 1
    created_by: str = "human"
    parent_agent_id: str | None = None
    identity: str = ""
    purpose: str
    provider: str = "ollama"
    model_name: str = "qwen3"
    mind: MindState = Field(default_factory=MindState)
    skills: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    memory_enabled: bool = True
    memory_strategy: str = "persistent"
    autonomy: Autonomy = Autonomy.CYCLIC
    cycle_seconds: int | None = None
    status: AgentStatus = AgentStatus.CANDIDATE
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

    @property
    def slug(self) -> str:
        cleaned = "".join(
            character if character.isalnum() else "-" for character in self.name.lower()
        )
        return "-".join(part for part in cleaned.split("-") if part) or self.id[:8]

    def touch(self) -> None:
        self.updated_at = now_utc()


class AgentRuntimeState(BaseModel):
    """What an agent is actually doing. Rebuilt on every boot, never trusted from disk."""

    agent_id: str
    name: str = ""
    phase: AgentPhase = AgentPhase.OFFLINE
    cycles: int = 0
    goal: str | None = None
    last_outcome: str = ""
    last_error: str | None = None
    last_cycle_at: datetime | None = None

    def describe(self) -> str:
        parts = [f"phase={self.phase}", f"cycles={self.cycles}"]
        if self.goal:
            parts.append(f"goal={self.goal}")
        if self.last_error:
            parts.append(f"error={self.last_error}")
        return " ".join(parts)


class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    sender_id: str
    recipient_id: str | None
    conversation_id: str = Field(default_factory=lambda: str(uuid4()))
    correlation_id: str | None = None
    type: str = "text"
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)


class FilesystemGrant(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    agent_id: str
    path: str
    read: bool = True
    write: bool = False


class SkillDefinition(BaseModel):
    name: str
    version: str = "1.0.0"
    generation: int = 1
    description: str
    entrypoint: str
    inputs: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    required_permissions: list[str] = Field(default_factory=list)
    required_dependencies: list[str] = Field(default_factory=list)
    created_by: str = "system"
    parent_generation: int = 1
    status: str = "active"
