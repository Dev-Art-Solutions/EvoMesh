from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from evomesh import cron
from evomesh.phase_label import phase_label


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
    RUNNABLE = "runnable"
    ACTIVE = "active"
    BLOCKED = "blocked"
    STALLED = "stalled"
    DONE = "done"
    # The target architecture calls this state "achieved". Keep DONE's wire
    # value so every agent definition already persisted by EvoMesh remains
    # readable and existing console/API clients do not need a flag day.
    ACHIEVED = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Finished goals a mind keeps for history; older ones are forgotten.
KEEP_CLOSED_GOALS = 30
CLOSED_GOAL_STATUSES = frozenset({GoalStatus.DONE, GoalStatus.FAILED, GoalStatus.CANCELLED})

OPEN_GOAL_STATUSES = frozenset(
    {GoalStatus.PENDING, GoalStatus.RUNNABLE, GoalStatus.ACTIVE, GoalStatus.BLOCKED}
)


class GoalConditionKind(StrEnum):
    BELIEF_EQUALS = "belief_equals"
    ARTIFACT_EXISTS = "artifact_exists"
    TOOL_RESULT = "tool_result"
    CHILD_GOALS_COMPLETE = "child_goals_complete"
    VALIDATOR_PASSES = "validator_passes"
    HUMAN_APPROVAL = "human_approval"


class GoalCondition(BaseModel):
    """A small, deterministic predicate over structured runtime evidence.

    The intentionally generic ``key``/``value`` pair keeps the first condition
    vocabulary compact: ``key`` names a belief, tool result, validator or human
    approval and ``value`` is the expected value. ``path`` is used only by the
    artifact predicate. More condition types can be added without changing Goal.
    """

    kind: GoalConditionKind
    key: str = ""
    value: Any = True
    path: str = ""


class GoalRetryPolicy(BaseModel):
    max_attempts: int | None = None
    backoff_seconds: float = 0.0
    backoff_multiplier: float = 1.0


class GoalUtility(BaseModel):
    expected_value: float = 0.0
    estimated_effort: float = 0.0
    risk: float = 0.0
    strategic_value: float = 0.0


class GoalEvidence(BaseModel):
    kind: str = "observation"
    reference: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)


class Goal(BaseModel):
    id: str = Field(default_factory=_short_id)
    description: str
    kind: str = "goal"
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: GoalStatus = GoalStatus.PENDING
    priority: int = 5
    utility: GoalUtility = Field(default_factory=GoalUtility)
    parent_goal_id: str | None = None
    child_goal_ids: list[str] = Field(default_factory=list)
    dependency_goal_ids: list[str] = Field(default_factory=list)
    owner_agent_id: str | None = None
    delegated_to_agent_id: str | None = None
    blocked_reason: str | None = None
    success_conditions: list[GoalCondition] = Field(default_factory=list)
    failure_conditions: list[GoalCondition] = Field(default_factory=list)
    deadline: datetime | None = None
    attempts: int = 0
    max_attempts: int = 6
    # How many times progress detection declared this goal stalled.
    stalls: int = 0
    # Which occurrence of a recurring goal this is; each completion starts
    # a new one, with its own operation keys and budget.
    occurrence: int = 0
    retry_policy: GoalRetryPolicy = Field(default_factory=GoalRetryPolicy)
    recurring: bool = False
    # How often this one goal is worth re-checking after it last finished,
    # independent of the agent's own cycle_seconds. An agent's cycle rate is
    # shared by everything it does -- messages, every other goal -- so
    # slowing it down to satisfy one goal that only needs hourly attention
    # would starve all the rest. This lets next_goal() skip a goal that is
    # not due yet instead, so the agent's own heartbeat never has to change.
    interval_seconds: int | None = None
    # A fixed schedule ("every day at 09:00", "every Monday") instead of a
    # fixed offset from last completion. Mutually exclusive with
    # interval_seconds in practice -- add_goal() only ever sets one -- and
    # takes priority if both are somehow set, since a wall-clock appointment
    # is a stronger statement than "some time after it last ran".
    cron: str | None = None
    next_attempt_at: datetime | None = None
    # Opt-in: a human who wants to be told what is happening on this specific
    # goal, not just able to ask -- both its progress (one line per step) and
    # when it finishes. Off by default, and toggled independently per goal
    # with /goal notify <agent> <id> [on|off]: an agent narrating every step
    # of every goal unprompted is noise, not a feature, until asked for.
    notify: bool = False
    # Optional. A regex a recurring goal's report is expected to match, line
    # by line, before it reaches a human -- a backstop for goals whose skill
    # asks the model for a strict format (e.g. news-impact-analysis's
    # "SYMBOL direction (confidence): headline -- why") and nothing else.
    # Prompting alone is not enforcement: a model can still narrate its own
    # bookkeeping ("appended this cycle's assessment to the scratch log as
    # the Nth entry") instead of the report line the skill asked for, and
    # that narration would otherwise reach the human verbatim. When set,
    # _apply() (agents.py) keeps only lines that match and announces nothing
    # if none do, rather than forwarding an unfiltered summary.
    report_pattern: str | None = None
    progress: float = 0.0
    evidence: list[GoalEvidence] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    last_error: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
    # Set once this goal closes DONE -- by default left None, since a
    # one-shot that never succeeds never finishes. agents.py records it
    # (see the DONE transition there) so the run's completion ledger,
    # built from this field, knows exactly when each goal ended.
    completed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        if self.status not in OPEN_GOAL_STATUSES:
            return False
        if self.next_attempt_at is not None and now_utc() < self.next_attempt_at:
            return False
        # attempts is a failure budget, not a cycle counter, so a standing goal
        # such as Guardian's health sweep stays open indefinitely.
        return self.recurring or self.attempts < self.attempt_limit

    @property
    def attempt_limit(self) -> int:
        return self.retry_policy.max_attempts or self.max_attempts

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
    # The harness job taking this step, if one is. Held on the step rather than
    # on the agent because the step is what the job is for: when the job
    # finishes, the step that asked for it is the one that consumes the answer.
    job: int = 0

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
    # A typed procedure execution this intention runs (closure plan 12.2);
    # None for the legacy textual plans.
    execution_id: str | None = None
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


class PlanUsage(BaseModel):
    selected: int = 0
    succeeded: int = 0
    failed: int = 0


class MemoryEpisode(BaseModel):
    """Structured history, separate from revisable semantic beliefs."""

    kind: str
    summary: str
    goal_id: str = ""
    agent_id: str = ""
    outcome: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)


class ProcedureStatus(StrEnum):
    OBSERVED = "observed"
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    PROMOTED = "promoted"
    DEGRADED = "degraded"
    RETIRED = "retired"


class LearnedProcedure(BaseModel):
    """Reusable procedural knowledge retained after successful execution.

    ``trigger`` is the normalized goal description it was learned for, and
    ``goal_kind`` the goal kind: together they are what a later goal must
    match for the procedure to replace a planning call.
    """

    name: str
    trigger: str
    steps: list[str] = Field(default_factory=list)
    successes: int = 0
    failures: int = 0
    source_goal_id: str = ""
    pattern: str = ""
    goal_kind: str = ""
    parameter_schema: dict[str, str] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list)
    context_predicates: dict[str, Any] = Field(default_factory=dict)
    success_conditions: list[GoalCondition] = Field(default_factory=list)
    status: ProcedureStatus = ProcedureStatus.PROMOTED
    approved: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    uses: int = 0
    model_calls_saved: int = 0
    total_duration_seconds: float = 0.0
    validator_passes: int = 0
    validator_failures: int = 0
    last_used_at: datetime | None = None
    updated_at: datetime = Field(default_factory=now_utc)


class ExecutionTrace(BaseModel):
    """One finished execution of a plan, the raw material procedures are
    learned from. Persisted (bounded) in the agent's MindState, so repeated
    successes accumulate across restarts instead of resetting with them."""

    goal_type: str
    context_signature: str
    plan_name: str
    steps: list[str]
    tools: list[str] = Field(default_factory=list)
    agents: list[str] = Field(default_factory=list)
    goal_parameters: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    context_predicates: dict[str, Any] = Field(default_factory=dict)
    success_conditions: list[GoalCondition] = Field(default_factory=list)
    succeeded: bool
    model_calls: int = 0
    duration_seconds: float = 0.0
    validator_passed: bool | None = None
    created_at: datetime = Field(default_factory=now_utc)

    @property
    def pattern(self) -> str:
        material = "|".join((self.goal_type, self.context_signature, *self.steps))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


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


class TelegramSettings(BaseModel):
    """A Telegram bot as a second console onto the mesh -- or, on an
    AgentDefinition, a private bot onto one agent only.

    ``token`` is the string BotFather hands back. ``allowed_chat_ids`` is the
    allow-list; leaving it empty and keeping ``adopt_first_chat`` on lets the
    first person who says /start claim the bot, which is the only way to learn
    a chat id without asking a human to go find it.
    """

    enabled: bool = False
    token: str = ""
    allowed_chat_ids: list[int] = Field(default_factory=list)
    adopt_first_chat: bool = True
    # Long-poll window. Telegram holds the request open this long when idle.
    poll_timeout_seconds: int = 30
    # Announce what the mesh does on its own -- promotions, restarts -- rather
    # than only answering when spoken to. On an agent's own bot this covers
    # only that agent's own goal progress, not the whole mesh's chatter.
    announcements: bool = True


class McpServerConfig(BaseModel):
    """One Model Context Protocol server this mesh (or one agent) may use as
    a tool source -- see mcp_client.py for the connection itself.

    ``name`` is both the merge key (Settings.mcp_servers + AgentDefinition.
    mcp_servers, merged by name, agent wins on a collision -- see
    Environment.active_mcp_tools) and the prefix every tool from this server
    gets: ``mcp__{name}__{tool}``.

    Exactly one of ``command`` (stdio -- spawned as a local subprocess,
    talked to over its stdin/stdout) or ``url`` (HTTP) should be set; empty
    ``command`` means HTTP. ``command``/``args``/``env``/``url``/``headers``
    must only ever come from a human-edited evomesh.yaml or an agent
    definition a human/console command wrote -- never from a harness job's
    own write tools, which is already true today because job filesystem
    grants are scoped to one candidate generation directory, never the live
    config or a persisted AgentDefinition row. An MCP server can execute
    arbitrary commands or reach arbitrary URLs on its own, same trust level
    as HarnessSettings.shell_allow -- this is not itself a sandbox.
    """

    name: str
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)


class MindState(BaseModel):
    beliefs: list[Belief] = Field(default_factory=list)
    goals: list[Goal] = Field(default_factory=list)
    intentions: list[Intention] = Field(default_factory=list)
    plan_statistics: dict[str, PlanUsage] = Field(default_factory=dict)
    episodes: list[MemoryEpisode] = Field(default_factory=list)
    procedures: dict[str, LearnedProcedure] = Field(default_factory=dict)
    execution_traces: list[ExecutionTrace] = Field(default_factory=list)

    def open_goals(self) -> list[Goal]:
        # Imported lazily to keep the persisted contracts independent from the
        # service that operates on them. GoalManager is now the single place
        # that knows whether dependencies make an otherwise-open goal runnable.
        from evomesh.goal_manager import GoalManager

        return GoalManager(self).runnable_goals()

    def next_goal(self) -> Goal | None:
        return next(iter(self.open_goals()), None)

    def goal(self, goal_id: str) -> Goal:
        for goal in self.goals:
            if goal.id == goal_id:
                return goal
        raise KeyError(goal_id)

    def add_goal(
        self,
        description: str,
        *,
        priority: int = 5,
        recurring: bool = False,
        interval_seconds: int | None = None,
        cron_expression: str | None = None,
        notify: bool = False,
        report_pattern: str | None = None,
        kind: str = "goal",
        parameters: dict[str, Any] | None = None,
        parent_goal_id: str | None = None,
        dependency_goal_ids: Sequence[str] = (),
        success_conditions: Sequence[GoalCondition] = (),
        failure_conditions: Sequence[GoalCondition] = (),
        deadline: datetime | None = None,
        owner_agent_id: str | None = None,
    ) -> Goal:
        parent = self.goal(parent_goal_id) if parent_goal_id is not None else None
        known = {goal.id for goal in self.goals}
        missing = [item for item in dependency_goal_ids if item not in known]
        if missing:
            # Every creation path, not only GoalManager.create, refuses a
            # dependency on nothing (B-006). A new goal has no dependants yet,
            # so it cannot close a cycle; a missing reference is the only way
            # it can break the graph.
            raise ValueError(f"goal depends on unknown goal(s): {', '.join(missing)}")
        goal = Goal(
            description=description.strip(),
            kind=kind,
            parameters=dict(parameters or {}),
            priority=priority,
            parent_goal_id=parent_goal_id,
            dependency_goal_ids=list(dependency_goal_ids),
            success_conditions=list(success_conditions),
            failure_conditions=list(failure_conditions),
            deadline=deadline,
            owner_agent_id=owner_agent_id,
            recurring=recurring,
            interval_seconds=interval_seconds,
            cron=cron_expression,
            notify=notify,
            report_pattern=report_pattern,
        )
        if cron_expression:
            if not cron.is_valid_cron_expression(cron_expression):
                raise ValueError(f"invalid cron expression: {cron_expression!r}")
            # A cron goal is an appointment, not a "do this now" -- unlike
            # interval_seconds, which is silent until the goal has run once,
            # this must not fire the moment it is created.
            goal.next_attempt_at = cron.next_after(cron_expression, now_utc())
        self.goals.append(goal)
        if parent is not None:
            if goal.id not in parent.child_goal_ids:
                parent.child_goal_ids.append(goal.id)
        self._prune_closed_goals()
        return goal

    def _prune_closed_goals(self, keep: int = KEEP_CLOSED_GOALS) -> None:
        """Forget the oldest finished goals beyond ``keep``.

        Found live: the Guardian held hundreds of done "Investigate why ..."
        goals, persisted with the agent and scanned on every cycle. A closed
        goal an open one still points at (dependency, parent, child) stays.
        """
        closed = [goal for goal in self.goals if goal.status in CLOSED_GOAL_STATUSES]
        if len(closed) <= keep:
            return
        referenced = {
            related
            for goal in self.goals
            if goal.status not in CLOSED_GOAL_STATUSES
            for related in (*goal.dependency_goal_ids, *goal.child_goal_ids, goal.parent_goal_id)
            if related
        }
        forget = {goal.id for goal in closed[:-keep] if goal.id not in referenced}
        self.goals = [goal for goal in self.goals if goal.id not in forget]

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

    def record_plan_selected(self, name: str) -> None:
        usage = self.plan_statistics.setdefault(name, PlanUsage())
        usage.selected += 1

    def record_plan_outcome(self, name: str, *, success: bool) -> None:
        usage = self.plan_statistics.setdefault(name, PlanUsage())
        if success:
            usage.succeeded += 1
        else:
            usage.failed += 1

    # -- episodic and procedural memory --------------------------------

    def record_episode(self, episode: MemoryEpisode, *, keep: int = 100) -> None:
        self.episodes = [*self.episodes, episode][-keep:]

    def remember_procedure(self, procedure: LearnedProcedure) -> None:
        self.procedures[procedure.name] = procedure

    def record_trace(self, trace: ExecutionTrace, *, keep: int = 200) -> None:
        self.execution_traces = [*self.execution_traces, trace][-keep:]


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
    capabilities: list[str] = Field(default_factory=list)
    # Forward-chaining rules this agent runs every cycle before deliberation,
    # in rules.rule_from_config's shape. Plain data here so the contract does
    # not depend on the engine; a malformed one is refused where it is set.
    rules: list[dict[str, Any]] = Field(default_factory=list)
    # The custom tools (tools/<name>/TOOL.md) this agent's harness jobs are
    # offered. None is the old behavior -- every allowed custom tool -- kept
    # for an agent that never said, except a system agent, which gets none.
    # Found 2026-09-24: every job, the Evolver's included, was offered all
    # seven (mt5_signal, which places a trade, among them): 5870 characters
    # of schema on every turn of a code job, and seven more ways for a small
    # model to pick the wrong tool. A template sets this from its `tools:`.
    tools: list[str] | None = None
    permissions: list[str] = Field(default_factory=list)
    memory_enabled: bool = True
    memory_strategy: str = "persistent"
    autonomy: Autonomy = Autonomy.CYCLIC
    # Where this agent may run harness jobs. Empty means it may not: the harness
    # is a capability an agent is given, like a filesystem grant, rather than
    # something every agent has because the mesh has it.
    harness_root: str = ""
    # A second, separate grant on top of harness_root: whether this agent's
    # own harness jobs (reactive chat reply or a cyclic plan step alike) get
    # the learn_skill tool -- see harness_tools.tool_learn_skill. False by
    # default on purpose: the harness's write tool is confined to the job
    # root, but a skill this agent authors lands in the mesh-wide skills/
    # directory every agent reads from, so it is its own deliberate grant
    # rather than something harness_root implies. See console.py's
    # `/learn grant <agent>` / `/learn revoke <agent>`.
    can_learn_skills: bool = False
    # Optional. An absolute path to a real project this agent should work in --
    # a standalone repo the human already has, not somewhere under this
    # mesh's own workspace/agents/<slug>/ tree (that tree is what
    # Settings.workspace_path and "workspace" mean everywhere else in this
    # codebase -- deliberately a different name here to not collide with
    # that). Empty (the default) is unchanged from before this field
    # existed: the agent gets only its own mesh-managed playground. Set, it
    # becomes what Environment.default_harness_root hands out instead of
    # that playground -- memory.md/context.md still live in the playground
    # either way, only where the agent's own *work* happens moves. See
    # console.py's `/agent project <agent> <path>|clear`.
    project_path: str = ""
    cycle_seconds: int | None = None
    # A private Telegram bot for this agent alone -- its own BotFather token,
    # separate from the mesh-wide bot in Settings.telegram. None means this
    # agent is only reachable through the mesh-wide bot (or the console).
    telegram: TelegramSettings | None = None
    # MCP servers this agent uses as a tool source, on top of the mesh-wide
    # default list in Settings.mcp_servers -- merged by McpServerConfig.name,
    # this agent's own entry winning on a name collision (see
    # Environment.active_mcp_tools). Empty (the default) means exactly the
    # mesh-wide list, same shape as skills/permissions above: an explicit,
    # per-agent grant list, not a mesh-wide switch. See McpServerConfig's own
    # docstring for why its fields must never be settable from inside a
    # harness job.
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)
    # A command run on its own short, fixed interval, outside this agent's
    # cognition cycle entirely -- for state that moves on a scale of seconds
    # (open orders, account equity) where an LLM turn every few seconds would
    # mean a model call just to notice nothing changed. Empty means no watcher.
    # A non-empty line of the command's stdout becomes an announcement to this
    # agent's own channels; silence means nothing crossed a threshold.
    watch_command: str = ""
    watch_interval_seconds: float | None = None
    # How long one run of watch_command may take before it is killed; None
    # defers to watchers.DEFAULT_TIMEOUT_SECONDS.
    watch_timeout_seconds: float | None = None
    # Overrides the provider's num_ctx for this one agent. None defers to
    # ProviderSettings.num_ctx for whatever provider/model this agent runs.
    num_ctx: int | None = None
    # Overrides HarnessSettings.self_check_command/self_check_max_attempts for
    # this one agent's own harness jobs. None defers to the mesh-wide setting;
    # "" (empty, distinct from None) explicitly turns self-check off for this
    # agent even when the mesh-wide setting is on. Exists because a coding
    # agent (Coder, MT5 Coder, ...) working in its own real project needs
    # that project's own lint/type/test command, not whatever one other
    # project the mesh-wide setting happens to be pointed at -- see
    # console.py's `/agent self-check <agent> <command>|clear`.
    self_check_command: str | None = None
    self_check_max_attempts: int | None = None
    # Silences this agent's unprompted announcements (mesh-wide and its own
    # private bot) without touching whether it runs or what any goal's own
    # `notify` flag says -- a human who still wants the agent working, just
    # not narrating, mutes the agent rather than editing every goal.
    muted: bool = False
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
        parts = [f"phase={phase_label(self.phase)}", f"cycles={self.cycles}"]
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
    reply_to: str | None = None
    type: str = "text"
    content: str
    performative: str | None = None
    goal_id: str | None = None
    task_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)
    expires_at: datetime | None = None


class FilesystemGrant(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    agent_id: str
    path: str
    read: bool = True
    write: bool = False


class SkillDefinition(BaseModel):
    """A description, not a capability. What `read`ing ``path`` teaches an
    agent to do with the tools it already has -- never a new tool of its own.

    A skill that needed ``entrypoint``, ``inputs`` and ``outputs`` was a tool
    wearing a skill's name: the earlier shape of this class, and the mistake
    it existed to make. Everything a skill can *do* already exists as a
    harness tool or a shell program; a skill only ever adds procedure.
    """

    name: str
    description: str
    # Relative to the repository root -- skills/<name>/SKILL.md by
    # convention. Plain Markdown on purpose (the same reasoning as
    # memory.md and context.md, rule 18): a human reads or edits it directly,
    # and an agent reads it with the same `read` tool it reads any file with.
    path: Path
    created_by: str = "system"
