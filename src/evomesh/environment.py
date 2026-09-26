from __future__ import annotations

import asyncio
import functools
import logging
import re
import shutil
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from evomesh.agent_templates import AgentTemplateRegistry
from evomesh.agents import AgentRegistry, AgentRuntime, system_agent_definitions
from evomesh.bdi import BDIBehavior, ReflectiveBehavior
from evomesh.behaviors import EvolverBehavior, default_behaviors
from evomesh.blackboard import ArtifactRecord, Blackboard, WorldFact
from evomesh.cognition import AgentBehavior, CycleOutcome
from evomesh.cognitive_services import (
    CognitiveModelService,
    CognitiveServiceType,
    ModelInvocationReason,
)
from evomesh.config import Settings
from evomesh.contracts import (
    AgentDefinition,
    AgentPhase,
    AgentRuntimeState,
    AgentStatus,
    FilesystemGrant,
    Goal,
    GoalStatus,
    Message,
    now_utc,
)
from evomesh.coordination import (
    ASSISTANCE_CAPABILITY,
    DELEGATED_GOAL_KIND,
    CapabilityRegistry,
    ContractNet,
    Performative,
    WorkItem,
    WorkStatus,
    semantic_message,
)
from evomesh.events import Event, EventBus, EventType
from evomesh.evolution import CandidateWorkspace, EnvironmentEvolver
from evomesh.goal_manager import GoalManager, PreemptionPolicy
from evomesh.harness import HarnessResult, build_runner
from evomesh.harness_queue import (
    HarnessGateway,
    HarnessJob,
    HarnessQueue,
    HarnessWorker,
    JobStatus,
)
from evomesh.harness_session import HarnessSession, next_session_path
from evomesh.harness_tools import Tool, build_custom_tool, custom_tool_program
from evomesh.ideas import IDEAS_AGENT_ID, IDEAS_FILE_NAME, Idea, IdeaBook, IdeaScoutBehavior
from evomesh.improvements import (
    ImprovementBacklog,
    ImprovementControl,
    ImprovementCoordinator,
    ImprovementScout,
    ImprovementTriage,
)
from evomesh.mcp_client import McpManager
from evomesh.memory import AgentMemory, MemoryBudget, WorldContext
from evomesh.messaging import MessageBus
from evomesh.models import (
    AnthropicProvider,
    ModelProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
)
from evomesh.permissions import FilesystemPolicy
from evomesh.procedure_host import ProcedureLearning, build_procedure_service
from evomesh.procedure_runtime import occurrence_id
from evomesh.skills import MissingSkillError, PendingSkillWrite, SkillDefinition, SkillRegistry
from evomesh.storage import SQLiteRepository
from evomesh.tools import ToolRegistry as CustomToolRegistry
from evomesh.watchers import DEFAULT_TIMEOUT_SECONDS, AgentWatcher

logger = logging.getLogger(__name__)

# An unfinished assistance request older than this no longer blocks a new one.
ASSISTANCE_TTL_SECONDS = 3600.0
# Beliefs that are one agent's own conversation, not facts about the world.
PRIVATE_BELIEF_PREFIXES = ("inbox.",)
# Shared blackboard lines per section in every agent's world snapshot.
WORLD_BLACKBOARD_LINES = 6
# An open goal untouched this long is reported as stale in status.
STALE_GOAL_SECONDS = 3600.0


class HealthState(StrEnum):
    STARTING = "STARTING"
    READY = "READY"
    STOPPED = "STOPPED"


class Environment:
    def __init__(
        self, settings: Settings, providers: dict[str, ModelProvider] | None = None
    ) -> None:
        self.settings = settings
        self.repository = SQLiteRepository(settings.data_path)
        self.registry = AgentRegistry()
        self.bus = MessageBus(self.repository)
        self.permissions = FilesystemPolicy(self.repository)
        self.skills = SkillRegistry(self.project_root)
        self.tools = CustomToolRegistry(self.project_root)
        self.mcp = McpManager(settings.mcp_servers)
        # learn_skill/patch_skill calls awaiting /learn approve|reject when
        # HarnessSettings.skill_write_approval is on. In-memory only, same
        # non-durability the harness queue's own jobs already accept.
        self.pending_skill_writes: dict[int, PendingSkillWrite] = {}
        self._next_pending_skill_write = 1
        self.agent_templates = AgentTemplateRegistry(self.project_root)
        self.providers = providers or self._build_providers()
        self.cognition = CognitiveModelService()
        self.events = EventBus()
        self.blackboard = Blackboard()
        self.capabilities = CapabilityRegistry()
        self.contract_net = ContractNet(self.capabilities)
        # Typed procedures (closure plan): selection, durable execution and
        # admission. Definitions ship under procedures/ and load at start().
        self.procedures = build_procedure_service(self)
        self.procedure_learning = ProcedureLearning(self)
        self.improvement_backlog = ImprovementBacklog()
        self.improvement_scout = ImprovementScout()
        self.improvement_triage = ImprovementTriage()
        self.improvement_coordinator = ImprovementCoordinator(self.improvement_backlog)
        self.improvements = ImprovementControl(
            self.improvement_backlog,
            self.improvement_coordinator,
            self.improvement_triage,
            self.improvement_scout,
            save=self._save_improvements,
            announce=self.announce,
            require_review=settings.evolution.review,
        )
        self.events.subscribe(EventType.GOAL_UNBLOCKED, self._wake_event_owner)
        self.events.subscribe(EventType.MESSAGE_RECEIVED, self._wake_event_owner)
        self.events.subscribe(EventType.TASK_COMPLETED, self._wake_event_owner)
        self.events.subscribe(EventType.GOAL_COMPLETED, self._unblock_goal_dependents)
        for event_type in EventType:
            self.events.subscribe(event_type, self.blackboard.publish_event)
        self.events.subscribe(EventType.AGENT_STALLED, self._capture_improvement)
        self.events.subscribe(EventType.TASK_FAILED, self._capture_improvement)
        self.events.subscribe(EventType.AGENT_STALLED, self._assist_stalled_agent)
        self.events.subscribe(EventType.BELIEF_CHANGED, self._publish_belief_fact)
        self.events.subscribe(EventType.GOAL_CREATED, self._start_delegated_work)
        self.events.subscribe(EventType.GOAL_COMPLETED, self._finish_delegated_work)
        self.events.subscribe(EventType.GOAL_COMPLETED, self._record_procedure_trace)
        self.events.subscribe(EventType.TASK_FAILED, self._drop_procedure_trace)
        self.events.subscribe(EventType.TASK_FAILED, self._fail_delegated_work)
        self.runtimes: dict[str, AgentRuntime] = {}
        self.harness_queue = HarnessQueue(settings.harness.max_queue)
        self.harness_workers: list[HarnessWorker] = []
        # What each finished job actually wrote, keyed by job number. Held here
        # rather than re-read from the JSONL: the record of a generation must not
        # depend on a file a human may have moved.
        self.harness_sessions: dict[int, list[dict[str, Any]]] = {}
        self.harness = HarnessGateway(self.harness_queue, self.harness_sessions)
        self.health_state = HealthState.STOPPED
        self.provider_health: tuple[bool, str] = (False, "not checked")
        self.world = WorldContext(settings.workspace_path)
        self.budget = settings.runtime.budget()
        self.behaviors: dict[str, AgentBehavior] = default_behaviors(
            settings.evolution.auto_validate,
            settings.evolution.max_repairs,
            settings.evolution.auto_promote,
            settings.evolution.auto_restart,
            settings.evolution.validate_seconds,
            settings.evolution.auto_plan,
            settings.harness.plan_max_steps,
            settings.harness.plan_max_seconds,
            settings.evolution.review,
            settings.evolution.review_max_steps,
            settings.evolution.review_max_seconds,
            settings.evolution.baseline_tests,
            settings.evolution.test_backlog,
            settings.evolution.scout_when_idle,
        )
        self.behaviors[IDEAS_AGENT_ID] = IdeaScoutBehavior(
            max_pending=settings.ideas.max_pending, enabled=settings.ideas.enabled
        )
        self.evolver = EnvironmentEvolver(
            CandidateWorkspace(
                self.project_root,
                settings.generation_path,
                # Never copy live state into a candidate generation.
                exclude=(settings.data_path, settings.workspace_path),
            ),
            self.repository,
            identity=settings.git.identity(),
            publish=settings.git.publish_policy(),
        )
        self.evolver.on_generation_landed = self._on_generation_landed
        self.evolver.on_lane_finished = self._wake_evolver
        # Only the Idea Scout adds to the improvement backlog (and a human, by
        # hand): what anything else thinks of goes to it as a proposal.
        self.ideas = IdeaBook(
            settings.workspace_path / IDEAS_FILE_NAME, self.project_root, settings.git.identity()
        )
        self.evolver.idea_sink = self._idea_from_scout
        self._idea_tasks: set[asyncio.Task[bool]] = set()
        # Set when a generation has landed in the tree this process is not
        # running. Whoever owns the process -- __main__, a test, a script --
        # decides what to do about it; the environment only raises the flag.
        self.restart_requested = asyncio.Event()
        self.restart_reason = ""
        # Channels that want to hear what the mesh did on its own, rather than
        # only what it was asked. Telegram registers one; the console does not,
        # because it is already printing the cycle summaries.
        self.notifiers: list[Callable[[str], Awaitable[None]]] = []
        # Same idea, scoped to one agent's own private Telegram bot -- a goal
        # finishing for agent X must not spill into agent Y's private chat,
        # so this is keyed by agent id rather than shared like notifiers above.
        self.agent_notifiers: dict[str, list[Callable[[str], Awaitable[None]]]] = {}
        # Channels that can map a reply or a reaction back to one idea (a
        # Telegram message id per idea, per chat). Every one of them gets every
        # idea: the shared chat and the Idea Scout's own bot alike, and a thumbs
        # up in either decides it.
        self.idea_notifiers: list[Callable[[int, str], Awaitable[None]]] = []
        # One TelegramChannel task per agent that declares its own bot, keyed
        # by agent id. Held here, not just in self.channels, because starting
        # or stopping an agent has to cancel exactly this task.
        self._agent_telegram_tasks: dict[str, asyncio.Task[None]] = {}
        # One deterministic watcher per agent that declares watch_command --
        # see watchers.py for why this is never the agent's own LLM cycle.
        self._agent_watchers: dict[str, AgentWatcher] = {}
        # The same announcements, kept for a channel with no push of its own:
        # the control port is request-response only (one client's /restart
        # must not leak into another's next reply), so the desktop Control
        # Center polls /notifications with a cursor instead of being pushed
        # to. Bounded so a mesh nobody is polling does not grow this forever.
        self.announcement_log: deque[tuple[int, datetime, str]] = deque(maxlen=200)
        self._next_announcement_id = 1
        # Long-lived channels the process owns, registered by whoever started
        # them. Held as plain objects rather than imported types: the console
        # only has to ask them about themselves, and importing the Telegram
        # channel here would make the environment depend on something that
        # already depends on it.
        self.channels: dict[str, Any] = {}
        # Offline states for agents that never started, so status is always
        # explainable instead of a stale "active" left behind by a previous run.
        self._offline: dict[str, AgentRuntimeState] = {}

    @property
    def project_root(self) -> Path:
        return self.settings.generation_path.parent

    # -- restarting into a landed generation ----------------------------

    def _wake_evolver(self) -> None:
        """A validation run finished: the agent driving the pipeline should
        consume the verdict now, not at the end of its cycle interval."""
        for runtime in self.runtimes.values():
            if isinstance(runtime.behavior, EvolverBehavior):
                runtime.wake()

    def _wake_event_owner(self, event: Event) -> None:
        """Wake only the agent whose structured event made work runnable."""
        if runtime := self.runtimes.get(event.agent_id):
            runtime.wake()

    async def _unblock_goal_dependents(self, event: Event) -> None:
        """Dispatch what GoalManager's refresh did for the dependants of a
        completed goal: one GOAL_UNBLOCKED per goal that became runnable."""
        for definition in self.registry.all():
            if not any(
                event.goal_id in goal.dependency_goal_ids for goal in definition.mind.goals
            ):
                continue
            for change in GoalManager(definition.mind).refresh():
                if (
                    change.before is GoalStatus.BLOCKED
                    and change.after is GoalStatus.RUNNABLE
                    and event.goal_id in change.goal.dependency_goal_ids
                ):
                    await self.events.publish(
                        Event(
                            EventType.GOAL_UNBLOCKED,
                            source="goal_manager",
                            agent_id=definition.id,
                            goal_id=change.goal.id,
                            payload={"dependency_goal_id": event.goal_id},
                        )
                    )

    async def _capture_improvement(self, event: Event) -> None:
        """Runtime evidence becomes a proposal; triage decides if it is work."""
        await self.improvements.propose_from_event(event)

    async def _save_improvements(self) -> None:
        await self.repository.save_state(
            "improvement_backlog_v2", self.improvement_backlog.dump()
        )

    async def _assist_stalled_agent(self, event: Event) -> None:
        """Delegate a diagnosis to a capable agent or broadcast a help request."""
        try:
            stalled = self.registry.get(event.agent_id)
        except KeyError:
            return
        origin: WorkItem | None = None
        stalled_goal = next(
            (goal for goal in stalled.mind.goals if goal.id == event.goal_id), None
        )
        if stalled_goal is not None:
            origin = self.blackboard.work_items.get(
                str(stalled_goal.parameters.get("work_item_id") or "")
            )
        chain = list(origin.causation_chain) if origin is not None else []
        reentered = stalled.id in chain
        if not reentered:
            chain.append(stalled.id)
        depth = (origin.delegation_depth + 1) if origin is not None else 1
        max_depth = origin.budget.max_delegation_depth if origin is not None else 3
        if origin is not None and (
            depth > max_depth or reentered
        ):
            origin.status = WorkStatus.NEEDS_HUMAN
            origin.failure_history.append("assistance causation loop or depth limit")
            origin.updated_at = now_utc()
            await self._save_blackboard()
            return
        now = now_utc()
        for work in self.blackboard.open_work():
            if work.type != "assistance" or work.inputs.get("stalled_agent_id") != stalled.id:
                continue
            if work.parent_goal_id != event.goal_id:
                continue
            if (now - work.created_at).total_seconds() < ASSISTANCE_TTL_SECONDS:
                # Help for this goal is already out; asking again only piles a
                # duplicate goal onto whoever accepted the first request.
                return
            # Nobody finished it in time: close it so this stall can ask again.
            work.status = WorkStatus.CANCELLED
            work.updated_at = now
        item = WorkItem(
            parent_goal_id=event.goal_id,
            type="assistance",
            requester_agent_id=stalled.id,
            objective=(
                f"Diagnose why {stalled.name} is stalled: "
                f"{event.payload.get('reason') or 'no progress'}"
            ),
            # The diagnosing capability, not the stalled agent's own set: no
            # other agent has all of those, so help only ever went out as an
            # unanswered broadcast.
            required_capabilities=[ASSISTANCE_CAPABILITY],
            inputs={"stalled_agent_id": stalled.id, "event": event.payload},
            expected_outputs=["diagnosis"],
            cause_work_item_id=origin.id if origin is not None else None,
            causation_chain=chain,
            delegation_depth=depth,
        )
        bid = self.contract_net.award(
            item,
            states=self.runtime_states(),
            active_work=list(self.blackboard.work_items.values()),
            history=self.blackboard.work_history(),
            exclude_agent_ids={stalled.id},
        )
        if bid is None and origin is not None:
            origin.status = WorkStatus.NEEDS_HUMAN
            origin.failure_history.append("no loop-free assistance route")
            origin.updated_at = now_utc()
            await self._save_blackboard()
            return
        self.blackboard.publish_work(item)
        message = semantic_message(
            Performative.DELEGATE if bid else Performative.HELP_REQUEST,
            sender_id=stalled.id,
            recipient_id=bid.agent_id if bid else None,
            task_id=item.id,
            goal_id=event.goal_id,
            payload=item.model_dump(mode="json"),
            content=item.objective,
        )
        if bid is None:
            message.metadata["broadcast"] = True
        await self.bus.send(message)
        await self._save_blackboard()

    # -- shared world state ---------------------------------------------

    def _reconcile_work_after_restart(self) -> int:
        """Delegated work that was assigned or active when the mesh stopped:
        still owned by an open delegated goal -> it carries on; otherwise it is
        closed explicitly instead of sitting ACTIVE forever or being redone.

        Only delegated work is judged here -- an improvement's work item is
        settled from its generation's recorded outcome by ImprovementControl.
        """
        open_goal_work = {
            str(goal.parameters.get("work_item_id"))
            for definition in self.registry.all()
            for goal in definition.mind.goals
            if goal.kind == DELEGATED_GOAL_KIND and goal.is_open
        }
        closed = 0
        for work in self.blackboard.open_work():
            if work.improvement_id is not None or work.status not in {
                WorkStatus.ASSIGNED,
                WorkStatus.ACTIVE,
            }:
                continue
            if work.id in open_goal_work:
                continue
            work.status = WorkStatus.CANCELLED
            work.failure_history.append("no live owner after restart")
            work.updated_at = now_utc()
            closed += 1
        return closed

    async def _save_blackboard(self) -> None:
        await self.repository.save_state("blackboard", self.blackboard.dump())

    async def _publish_belief_fact(self, event: Event) -> None:
        """A revised belief becomes a shared fact every agent can read,
        instead of something each has to be told in conversation."""
        key = str(event.payload.get("key") or "")
        if not key or key.startswith(PRIVATE_BELIEF_PREFIXES) or not self._has(event.agent_id):
            return
        definition = self.registry.get(event.agent_id)
        belief = definition.mind.belief(key)
        if belief is None:
            return
        self.blackboard.publish_fact(
            WorldFact(
                key=f"{definition.name}.{key}",
                value=belief.statement,
                source=definition.name,
                confidence=belief.confidence,
            )
        )
        await self._save_blackboard()

    def _delegated_goal(self, event: Event) -> tuple[AgentDefinition, Goal, WorkItem] | None:
        if not event.goal_id or not self._has(event.agent_id):
            return None
        definition = self.registry.get(event.agent_id)
        goal = next((item for item in definition.mind.goals if item.id == event.goal_id), None)
        if goal is None or goal.kind != DELEGATED_GOAL_KIND:
            return None
        work = self.blackboard.work_items.get(str(goal.parameters.get("work_item_id") or ""))
        if work is None:
            return None
        return definition, goal, work

    async def _start_delegated_work(self, event: Event) -> None:
        found = self._delegated_goal(event)
        if found is None:
            return
        definition, _, work = found
        work.assigned_agent_id = definition.id
        work.status = WorkStatus.ACTIVE
        work.updated_at = now_utc()
        await self._save_blackboard()

    async def _finish_delegated_work(self, event: Event) -> None:
        """A delegated goal is done: close its work item and hand the result
        back to whoever delegated it, as a structured RESULT."""
        self._close_assistance_for(event)
        found = self._delegated_goal(event)
        if found is None:
            await self._save_blackboard()
            return
        definition, goal, work = found
        summary = str(event.payload.get("summary") or goal.last_error or "done")
        await self._settle_typed_child(work.id, definition.id, "completed", goal, summary)
        work.status = WorkStatus.COMPLETED
        work.updated_at = now_utc()
        work.result_fact_key = f"work.{work.id}.result"
        work.result_artifact_keys = [
            str(key) for key in event.payload.get("artifact_keys", [])
        ]
        self.blackboard.publish_fact(
            WorldFact(key=work.result_fact_key, value=summary, source=definition.name)
        )
        requester = str(
            work.requester_agent_id or goal.parameters.get("requester_id") or ""
        )
        if requester:
            await self.bus.send(
                semantic_message(
                    Performative.RESULT,
                    sender_id=definition.id,
                    recipient_id=requester,
                    task_id=work.id,
                    goal_id=work.parent_goal_id,
                    payload={"summary": summary, "work_item_id": work.id},
                    content=f"{work.objective}: {summary}",
                )
            )
        await self.events.publish(
            Event(
                EventType.TASK_COMPLETED,
                source=definition.name,
                agent_id=requester,
                goal_id=work.parent_goal_id,
                payload={"work_item_id": work.id, "summary": summary},
            )
        )
        await self._save_blackboard()

    async def _record_procedure_trace(self, event: Event) -> None:
        if not event.goal_id or not self._has(event.agent_id):
            return
        definition = self.registry.get(event.agent_id)
        goal = next((item for item in definition.mind.goals if item.id == event.goal_id), None)
        if goal is not None:
            await self.procedure_learning.goal_completed(definition, goal)

    async def _drop_procedure_trace(self, event: Event) -> None:
        if not event.goal_id or not self._has(event.agent_id):
            return
        definition = self.registry.get(event.agent_id)
        goal = next((item for item in definition.mind.goals if item.id == event.goal_id), None)
        if goal is not None and goal.status is GoalStatus.FAILED:
            self.procedure_learning.goal_failed(goal.id)

    async def _settle_typed_child(
        self, work_id: str, executor_id: str, status: str, goal: Goal, summary: str
    ) -> None:
        """Record a child's outcome on the typed parent that created it (a
        no-op for work no procedure delegated), then wake the parent."""
        result: Any = summary
        evidence: list[dict[str, Any]] = []
        child = await self.procedures.executor.for_occurrence(occurrence_id(goal))
        if child is not None:
            # A typed child answers with its validated output and evidence,
            # not a prose summary.
            if child.output is not None:
                result = child.output
            evidence = [item for item in child.evidence if item.get("kind") == "validator"]
        settled = await self.procedures.executor.settle_child(
            work_id, status=status, executor_id=executor_id, result=result, evidence=evidence
        )
        if not settled:
            return
        parent = self.blackboard.work_items.get(work_id)
        requester = parent.requester_agent_id if parent is not None else None
        if requester and requester in self.runtimes:
            self.runtimes[requester].wake()

    def _close_assistance_for(self, event: Event) -> None:
        """The stalled goal finished after all: help for it is moot."""
        for work in self.blackboard.open_work():
            if (
                work.type == "assistance"
                and work.parent_goal_id == event.goal_id
                and work.inputs.get("stalled_agent_id") == event.agent_id
            ):
                work.status = WorkStatus.CANCELLED
                work.updated_at = now_utc()

    async def _fail_delegated_work(self, event: Event) -> None:
        found = self._delegated_goal(event)
        if found is None:
            return
        definition, goal, work = found
        if goal.status is not GoalStatus.FAILED:
            return
        reason = str(event.payload.get("reason") or "failed")
        await self._settle_typed_child(work.id, definition.id, "failed", goal, reason)
        work.fail(reason)
        requester = str(goal.parameters.get("requester_id") or "")
        if requester:
            await self.bus.send(
                semantic_message(
                    Performative.FAILURE,
                    sender_id=definition.id,
                    recipient_id=requester,
                    task_id=work.id,
                    goal_id=work.parent_goal_id,
                    payload={"reason": reason, "work_item_id": work.id},
                    content=f"{work.objective}: {reason}",
                )
            )
        await self._save_blackboard()

    def _on_generation_landed(self, number: int, commit: str) -> None:
        """A generation is now in the tree, and this process is not running it.

        Restarting is the whole point of applying a generation: until the
        process comes back up on the new commit, the mesh has evolved on disk
        and nowhere else. The flag is always raised so ``/evolution status``
        stays truthful even when the automatic restart is switched off.
        """
        self.restart_reason = (
            f"generation {number} landed as {commit[:8]}"
            f"{f' and was {self.evolver.last_publish}' if self.evolver.last_publish else ''}"
        )
        if not self.settings.evolution.auto_restart:
            logger.info("%s; auto_restart is off, so a human has to restart", self.restart_reason)
            return
        logger.info("%s; restarting into it", self.restart_reason)
        self.restart_requested.set()

    async def announce(self, text: str) -> None:
        """Tell every listening channel something the mesh did unprompted."""
        self.announcement_log.append((self._next_announcement_id, now_utc(), text))
        self._next_announcement_id += 1
        for notify in list(self.notifiers):
            try:
                await notify(text)
            except Exception:  # noqa: BLE001 - a broken channel never stops the mesh
                logger.exception("A notification channel failed")

    async def announce_idea(self, idea: Idea) -> None:
        """An idea, to every channel that can take a yes or no for it back.

        The Control Center polls the announcement log like any announcement;
        Telegram gets it through idea_notifiers so the message id can be tied
        to the idea (a thumbs-up or a reply on it approves that idea alone).
        """
        text = idea.chat_text()
        self.announcement_log.append((self._next_announcement_id, now_utc(), text))
        self._next_announcement_id += 1
        for send in list(self.idea_notifiers):
            try:
                await send(idea.number, text)
            except Exception:  # noqa: BLE001 - a broken channel never stops the mesh
                logger.exception("An idea channel failed")

    async def submit_idea(
        self,
        text: str = "",
        *,
        source: str,
        item: Any = None,
        direct: bool = False,
        sender_id: str | None = None,
    ) -> bool:
        """Hand an idea to the Idea Scout as a message (rule 2): raw ``text``
        to rewrite, or a finished ``item`` to vet. False when there is no
        Scout to hand it to."""
        if not self._has(IDEAS_AGENT_ID):
            return False
        payload: dict[str, Any] = {"source": source, "text": text, "direct": direct}
        if item is not None:
            payload["item"] = {
                "title": item.title,
                "detail": item.detail,
                "steps": [
                    {"path": step.path, "symbol": step.symbol, "change": step.change}
                    for step in item.steps
                ],
            }
        await self.bus.send(
            semantic_message(
                Performative.PROPOSE,
                sender_id=sender_id or source,
                recipient_id=IDEAS_AGENT_ID,
                payload=payload,
                content=text or (item.title if item is not None else ""),
            )
        )
        return True

    def _idea_from_scout(self, item: Any) -> bool:
        """The Evolver's scout found an item: the Scout files it, not the
        pipeline. Scheduled, because the pipeline stage that found it is
        synchronous."""
        if not self._has(IDEAS_AGENT_ID):
            return False
        task = asyncio.get_running_loop().create_task(
            self.submit_idea(item=item, source="Environment Evolver", sender_id="evolver")
        )
        self._idea_tasks.add(task)
        task.add_done_callback(self._idea_tasks.discard)
        return True

    def wake_ideas(self) -> None:
        """A human decided an idea: a review slot may have opened."""
        runtime = self.runtimes.get(IDEAS_AGENT_ID)
        if runtime is not None:
            runtime.wake()

    async def announce_agent(self, agent_id: str, text: str) -> None:
        """Like announce(), plus that one agent's own private bot, if any.

        A muted agent keeps running and keeps its goals -- muting silences
        what it says unprompted, not what it does. The mesh-wide channel is
        skipped too, not just the agent's private bot: a human who muted an
        agent because it spams does not want that same spam surfacing in the
        shared channel instead.
        """
        try:
            muted = self.registry.get(agent_id).muted
        except KeyError:
            muted = False
        if muted:
            logger.info("muted agent %s: %s", agent_id, text)
            return
        await self.announce(text)
        for notify in list(self.agent_notifiers.get(agent_id, [])):
            try:
                await notify(text)
            except Exception:  # noqa: BLE001 - a broken channel never stops the mesh
                logger.exception("A per-agent notification channel failed")

    async def start_agent_telegram(self, definition: AgentDefinition) -> None:
        """Start a private Telegram bot for one agent, if it has one configured.

        Deferred import: telegram.py imports Environment to talk to the mesh,
        so importing it back at module load time here would be circular.
        """
        settings = definition.telegram
        if settings is None or not settings.enabled or not settings.token.strip():
            return
        if definition.id in self._agent_telegram_tasks:
            return
        from evomesh.telegram import TelegramChannel

        channel = TelegramChannel(
            self, settings, locked_agent_id=definition.id, locked_agent_name=definition.name
        )
        self.channels[f"telegram:{definition.id}"] = channel
        self._agent_telegram_tasks[definition.id] = asyncio.create_task(
            channel.run(), name=f"telegram:{definition.slug}"
        )

    async def stop_agent_telegram(self, agent_id: str) -> None:
        channel = self.channels.pop(f"telegram:{agent_id}", None)
        if channel is not None:
            channel.stop()
        task = self._agent_telegram_tasks.pop(agent_id, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def start_agent_watcher(self, definition: AgentDefinition) -> None:
        if not definition.watch_command.strip() or definition.id in self._agent_watchers:
            return
        watcher = AgentWatcher(
            definition.watch_command,
            interval_seconds=definition.watch_interval_seconds or 5.0,
            timeout_seconds=definition.watch_timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
            notify=functools.partial(self.announce_agent, definition.id),
            cwd=self.default_harness_root(definition),
        )
        watcher.start()
        self._agent_watchers[definition.id] = watcher

    async def stop_agent_watcher(self, agent_id: str) -> None:
        watcher = self._agent_watchers.pop(agent_id, None)
        if watcher is not None:
            await watcher.stop()

    def _build_providers(self) -> dict[str, ModelProvider]:
        result: dict[str, ModelProvider] = {}
        for name, config in self.settings.models.providers.items():
            kind = config.kind or ("ollama" if name == "ollama" else "openai")
            if kind == "ollama":
                result[name] = OllamaProvider(
                    config.base_url, config.model, config.timeout_seconds, config.num_ctx
                )
            elif kind == "anthropic":
                result[name] = AnthropicProvider(
                    config.base_url,
                    config.model,
                    config.api_key,
                    config.timeout_seconds,
                    config.max_output_tokens,
                )
            else:
                result[name] = OpenAICompatibleProvider(
                    config.base_url, config.model, config.api_key, config.timeout_seconds
                )
        return result

    # -- lifecycle ------------------------------------------------------

    async def start(self, *, start_agent_loops: bool = False) -> None:
        self.health_state = HealthState.STARTING
        # This process is starting from whatever the tree holds right now, so a
        # restart owed by an earlier promotion has just been paid.
        self.evolver.workspace.supervisor.clear_restart_flag()
        # One-time catch-up for promote()'s own past leak (see sweep_applied's
        # docstring): every already-landed generation promote() left sitting
        # in the candidates dict before that fix existed gets its entry
        # cleared now, and prune_stale() reclaims the worktree/branch/
        # directory each one was holding open for nothing. A no-op on every
        # startup after the first.
        if self.evolver.workspace.supervisor.sweep_applied():
            await self.evolver.workspace.prune_stale()
        await self.repository.initialize()
        await self.procedures.registry.load()
        for problem in await self.procedures.registry.install(self.project_root / "procedures"):
            logger.warning("shipped procedure not admitted: %s", problem)
        restored = ImprovementBacklog.load(
            await self.repository.load_state("improvement_backlog_v2")
        )
        self.improvement_backlog.items = restored.items
        self.improvement_backlog.work_items = restored.work_items
        self.blackboard.load(await self.repository.load_state("blackboard"))
        await self.skills.load()
        await self.tools.load()
        await self.agent_templates.load()
        stored = await self.repository.load_agents()
        default_name = self.settings.models.default_provider
        provider_config = self.settings.models.providers.get(default_name)
        model = provider_config.model if provider_config else "local-model"
        system_models = {
            agent_id: (configuration.provider, configuration.model, configuration.num_ctx)
            for agent_id, configuration in self.settings.system_agents.items()
        }
        system_definitions = system_agent_definitions(default_name, model, system_models)
        seeded = {item.id: item for item in system_definitions}
        definitions = stored or system_definitions
        known_ids: set[str] = set()
        for definition in definitions:
            if definition.id in system_models:
                definition.provider, definition.model_name, definition.num_ctx = system_models[
                    definition.id
                ]
            self._reconcile(definition, seeded.get(definition.id))
            if definition.id not in known_ids:
                self.registry.register(definition)
                self.capabilities.register(definition)
                self.bus.register(definition.id)
                await self.repository.save_agent(definition)
                known_ids.add(definition.id)
        for system_definition in system_definitions:
            if system_definition.id not in known_ids:
                self.registry.register(system_definition)
                self.capabilities.register(system_definition)
                self.bus.register(system_definition.id)
                await self.repository.save_agent(system_definition)
        # After the registry is populated: ownership is read from agents' goals.
        if self._reconcile_work_after_restart():
            # Persisted now: found live, the reconciled state otherwise sat in
            # memory until something else happened to save the blackboard.
            await self._save_blackboard()
        self._apply_evolution_settings()
        provider = self.providers.get(default_name)
        if provider:
            self.provider_health = await provider.health()
        else:
            self.provider_health = (False, f"Provider '{default_name}' is not configured")
        self.evolver.provider = self.providers.get(default_name)
        self._start_harness_workers()
        if start_agent_loops:
            await self.start_all()
        await self.refresh_world()
        self.health_state = HealthState.READY

    def _reconcile(self, definition: AgentDefinition, seed: AgentDefinition | None) -> None:
        """Bring a persisted definition back to a state a boot can act on.

        A definition saved at shutdown carries status STOPPED, and older records
        predate goals entirely. Without this, system agents come back inert or,
        worse, come back labelled ACTIVE with no loop behind the label.
        """
        if seed is not None:
            definition.type = "system"
            definition.autonomy = seed.autonomy
            definition.purpose = definition.purpose or seed.purpose
            definition.identity = definition.identity or seed.identity
            if not definition.capabilities:
                definition.capabilities = list(seed.capabilities)
            if definition.status is not AgentStatus.ACTIVE:
                definition.status = AgentStatus.ACTIVE
            if not definition.mind.goals:
                for goal in seed.mind.goals:
                    definition.mind.goals.append(goal)
        elif definition.status is AgentStatus.STOPPED:
            # A human stopped it, or the last run ended. Either way it is not
            # running now, and start_all leaves it alone until asked.
            pass

    def _apply_evolution_settings(self) -> None:
        evolver = self.registry.get("evolver") if self._has("evolver") else None
        if evolver is None:
            return
        if not self.settings.evolution.autonomous:
            evolver.status = AgentStatus.STOPPED
        behavior = self.behaviors.get("evolver")
        if isinstance(behavior, EvolverBehavior) and self._has(IDEAS_AGENT_ID):
            # The Idea Scout does the scouting now, and its ideas wait for a
            # human; a scout generation would only race it.
            behavior.scout_when_idle = False
        objective = self.settings.evolution.objective
        if objective and not any(
            goal.description == objective for goal in evolver.mind.goals
        ):
            evolver.mind.add_goal(objective, priority=2)

    def _has(self, agent_id: str) -> bool:
        try:
            self.registry.get(agent_id)
        except KeyError:
            return False
        return True

    async def start_all(self) -> None:
        """Start every agent that is supposed to be running, and explain the rest."""
        self._offline.clear()
        for index, definition in enumerate(self.registry.all()):
            if definition.status is not AgentStatus.ACTIVE:
                self._mark_offline(definition, f"status is {definition.status}")
                continue
            if definition.provider not in self.providers:
                self._mark_offline(
                    definition, f"provider '{definition.provider}' is not configured"
                )
                continue
            try:
                await self.start_agent(definition.id, start_delay=index * self._stagger)
            except (RuntimeError, ValueError) as exc:
                self._mark_offline(definition, str(exc))

    @property
    def _stagger(self) -> float:
        return max(0.0, self.settings.runtime.stagger_seconds)

    def _mark_offline(self, definition: AgentDefinition, reason: str) -> None:
        logger.info("Agent %s is not running: %s", definition.name, reason)
        self._offline[definition.id] = AgentRuntimeState(
            agent_id=definition.id,
            name=definition.name,
            phase=AgentPhase.OFFLINE,
            goal=(goal.description if (goal := definition.mind.next_goal()) else None),
            last_error=reason,
        )

    async def stop(self) -> None:
        # Shutting the mesh down leaves every agent's desired status untouched,
        # so the next boot starts exactly what was running before.
        await self._stop_harness_workers()
        # After the workers, not before: no in-flight harness job should
        # still be mid call_tool() on an MCP connection this is about to
        # close (see mcp_client.py's own docstring on why this matters --
        # the same "actually terminate it" lesson as processes.py's timeout
        # fix, this time for MCP servers' own child processes/connections).
        await self.mcp.shutdown()
        await self.evolver.cancel_validation()
        for runtime in list(self.runtimes.values()):
            await runtime.stop(persist_status=False)
        self.runtimes.clear()
        for agent_id in list(self._agent_telegram_tasks):
            await self.stop_agent_telegram(agent_id)
        for agent_id in list(self._agent_watchers):
            await self.stop_agent_watcher(agent_id)
        self.health_state = HealthState.STOPPED

    # -- agents ---------------------------------------------------------

    async def register_agent(self, definition: AgentDefinition) -> None:
        self.registry.register(definition)
        self.capabilities.register(definition)
        self.bus.register(definition.id)
        await self.repository.save_agent(definition)
        if definition.type != "system":
            # A place to play, ready the moment the agent exists -- not a
            # harness grant yet (that stays an explicit capability, see
            # AgentDefinition.harness_root), just somewhere of its own to
            # land in the instant one is given without a human naming a path.
            await self.memory_for(definition).ensure_playground()

    def default_harness_root(self, definition: AgentDefinition) -> Path:
        """Where an agent's harness jobs run when nobody names a directory.

        A system agent (the Evolver, Guardian, ...) already works in the
        EvoMesh tree itself. An agent with a configured project_path (a real
        project the human pointed it at -- see AgentDefinition.project_path)
        works there instead of its own playground. Anything else gets that
        playground -- granting harness access with no path should never be
        how a fresh agent ends up editing this project's own source.
        """
        if definition.type == "system":
            return self.project_root
        if definition.project_path:
            return Path(definition.project_path)
        return self.memory_for(definition).playground_path

    def memory_for(self, definition: AgentDefinition) -> AgentMemory:
        return AgentMemory(self.settings.workspace_path, definition, self.budget_for(definition))

    def budget_for(self, definition: AgentDefinition) -> MemoryBudget:
        """This agent's own character budget -- see RuntimeSettings.
        budget_for_num_ctx for why this is not just ``self.budget`` for
        every agent regardless of which model it actually runs."""
        return self.settings.runtime.budget_for_num_ctx(self.num_ctx_for(definition))

    def cycle_seconds_for(self, definition: AgentDefinition) -> float:
        if definition.cycle_seconds:
            return float(definition.cycle_seconds)
        if definition.id == "evolver":
            return float(self.settings.evolution.cycle_seconds)
        return float(self.settings.runtime.cycle_seconds)

    def resolve_num_ctx(
        self, provider_name: str, model_name: str | None, override: int | None = None
    ) -> int | None:
        """The window a request against this provider/model should ask for.

        Checked in order: an explicit override (an agent's own setting) first,
        since it names the one agent that needs something different from its
        siblings; then the provider's per-model entry, for a provider serving
        more than one model with genuinely different needs; then the
        provider's own default. Absent all three, there is nowhere left to
        look and the provider's own default (unset) applies.
        """
        if override is not None:
            return override
        provider = self.settings.models.providers.get(provider_name)
        if provider is None:
            return None
        if model_name and model_name in provider.model_num_ctx:
            return provider.model_num_ctx[model_name]
        return provider.num_ctx

    def num_ctx_for(self, definition: AgentDefinition) -> int | None:
        return self.resolve_num_ctx(definition.provider, definition.model_name, definition.num_ctx)

    async def start_agent(self, agent_id: str, *, start_delay: float = 0.0) -> None:
        if agent_id in self.runtimes:
            return
        definition = self.registry.get(agent_id)
        provider = self.providers.get(definition.provider)
        if provider is None:
            raise RuntimeError(f"Provider '{definition.provider}' is unavailable")
        behavior = self.behaviors.get(definition.id, ReflectiveBehavior())
        if isinstance(behavior, BDIBehavior):
            behavior.reasoner.preemption = PreemptionPolicy(
                enabled=self.settings.runtime.preemption_enabled,
                minimum_score_delta=self.settings.runtime.preemption_minimum_score_delta,
                non_preemptible_goal_kinds=frozenset(
                    self.settings.runtime.non_preemptible_goal_kinds
                ),
                deadline_override_seconds=(
                    self.settings.runtime.preemption_deadline_override_seconds
                ),
            )
        runtime = AgentRuntime(
            definition=definition,
            provider=provider,
            bus=self.bus,
            repository=self.repository,
            memory=self.memory_for(definition),
            behavior=behavior,
            budget=self.budget_for(definition),
            cycle_seconds=self.cycle_seconds_for(definition),
            num_ctx=self.num_ctx_for(definition),
            cognitive=self.cognition,
            events=self.events,
            start_delay=start_delay,
            services=self._services,
            world_context=self._world_snapshot,
            announce=functools.partial(self.announce_agent, definition.id),
        )
        await runtime.start()
        self.runtimes[agent_id] = runtime
        self._offline.pop(agent_id, None)
        await self.start_agent_telegram(definition)
        self.start_agent_watcher(definition)

    async def stop_agent(self, agent_id: str, *, persist_status: bool = True) -> None:
        runtime = self.runtimes.pop(agent_id, None)
        if runtime:
            await runtime.stop(persist_status=persist_status)
            self._mark_offline(runtime.definition, "stopped by request")
        await self.stop_agent_telegram(agent_id)
        await self.stop_agent_watcher(agent_id)

    async def cycle_agent(self, agent_id_or_name: str) -> CycleOutcome:
        definition = self.registry.get(agent_id_or_name)
        runtime = self.runtimes.get(definition.id)
        if runtime is None:
            raise RuntimeError(f"Agent '{definition.name}' is not running")
        outcome = await runtime.run_cycle()
        await self.refresh_world()
        return outcome

    def runtime_states(self) -> dict[str, AgentRuntimeState]:
        states = {name: state for name, state in self._offline.items()}
        for agent_id, runtime in self.runtimes.items():
            states[agent_id] = runtime.state
        for definition in self.registry.all():
            states.setdefault(
                definition.id,
                AgentRuntimeState(agent_id=definition.id, name=definition.name),
            )
        # Derived, never stored: an agent with a job in flight is doing
        # something, and reporting it as `thinking` would be a label with no
        # loop behind it -- the exact failure the status/phase split exists to
        # prevent. An offline agent keeps its reason; a job cannot revive it.
        for job in self.harness_queue.open_jobs():
            state = states.get(job.agent_id)
            if state is not None and state.phase is not AgentPhase.OFFLINE:
                state.phase = AgentPhase.AWAITING_HARNESS
                state.last_outcome = job.describe()
        return states

    def stuck_agents(self) -> dict[str, float]:
        """Name -> seconds, for every agent whose cycle is stuck rather than slow.

        The control port answers /ping from its own coroutine regardless of
        whether any agent's cycle is making progress, so a supervisor that
        only checks the socket sees a mesh that has been frozen for twenty
        minutes as perfectly healthy. This is what lets it tell the two apart.
        """
        stuck: dict[str, float] = {}
        for runtime in self.runtimes.values():
            seconds = runtime.stuck_for()
            if seconds is not None:
                stuck[runtime.definition.name] = seconds
        return stuck

    # -- the harness worker ---------------------------------------------

    def _start_harness_workers(self) -> None:
        """No harness configured, no worker task. Rule: off means absent."""
        if not self.settings.harness.enabled or self.harness_workers:
            return
        for index in range(max(1, self.settings.harness.workers)):
            worker = HarnessWorker(
                self.harness_queue, self._run_harness_job, self._deliver_harness, lane="background"
            )
            worker.start(f"evomesh-harness-{index + 1}")
            self.harness_workers.append(worker)
        # A separate lane, not more of the same pool -- see
        # HarnessSettings.priority_workers for why a human's reactive
        # question needs a worker the background lane can never occupy.
        for index in range(max(0, self.settings.harness.priority_workers)):
            worker = HarnessWorker(
                self.harness_queue, self._run_harness_job, self._deliver_harness, lane="priority"
            )
            worker.start(f"evomesh-harness-priority-{index + 1}")
            self.harness_workers.append(worker)

    async def _stop_harness_workers(self) -> None:
        for worker in self.harness_workers:
            await worker.stop()
        self.harness_workers.clear()

    def submit_harness_job(
        self,
        objective: str,
        *,
        agent_id: str = "",
        root: Path | None = None,
        priority: bool = False,
    ) -> HarnessJob:
        """Queue a job and return its handle, which may be one already running."""
        if not self.settings.harness.enabled:
            raise RuntimeError("the harness is off; set harness.enabled in evomesh.yaml")
        return self.harness_queue.submit(
            objective,
            root or self.project_root,
            agent_id=agent_id,
            allow_write=self.settings.harness.allow_write,
            priority=priority,
        )

    def active_custom_tools(self) -> tuple[Tool, ...]:
        """Custom tools whose command is actually allow-listed.

        Filtered here, in ToolDefinition form, rather than after converting
        to a Tool: the same "an unusable tool in the schema is a tool a model
        will try" reasoning build_runner already applies to shell and fetch,
        just checked one program name earlier so a custom tool that runs an
        allowed program is the only kind that ever reaches the model.
        """
        allow = self.settings.harness.shell_programs()
        return tuple(
            build_custom_tool(definition, tool_dir=self.tools.root / definition.path.parent)
            for definition in self.tools.discover()
            if custom_tool_program(definition) in allow
        )

    def custom_tools_for(self, definition: AgentDefinition) -> tuple[Tool, ...]:
        """The allowed custom tools ``definition``'s harness jobs are offered:
        the ones it names, or -- when it names none at all -- every one for an
        ordinary agent and none for a system agent (see AgentDefinition.tools)."""
        tools = self.active_custom_tools()
        if definition.tools is not None:
            wanted = set(definition.tools)
            return tuple(tool for tool in tools if tool.name in wanted)
        return () if definition.type == "system" else tools

    async def active_mcp_tools(self, agent_id: str) -> tuple[Tool, ...]:
        """MCP-sourced tools for one agent's harness job: the mesh-wide
        default server list plus this agent's own AgentDefinition.
        mcp_servers, merged by name (see McpManager.resolve). Async, unlike
        active_custom_tools above, because tool discovery is a real
        list_tools() round trip against each connected server, not a
        filesystem scan."""
        definition = self.registry.get(agent_id) if agent_id and self._has(agent_id) else None
        overrides = definition.mcp_servers if definition is not None else []
        return await self.mcp.tools_for(overrides)

    def _make_delegate_work(self, sender_id: str) -> Callable[[str, str], Awaitable[str]]:
        """A harness job's ``delegate_work`` tool, bound to the agent it runs
        for: task-like requests become WorkItems routed by capability, never
        free-form text (B-008)."""

        async def delegate(capability: str, objective: str) -> str:
            sender = self.registry.get(sender_id)
            goal = sender.mind.next_goal()
            item = WorkItem(
                parent_goal_id=goal.id if goal is not None else "",
                requester_agent_id=sender.id,
                objective=objective,
                required_capabilities=[capability],
                expected_outputs=["result"],
            )
            bid = self.contract_net.award(
                item,
                states=self.runtime_states(),
                active_work=list(self.blackboard.work_items.values()),
                history=self.blackboard.work_history(),
                exclude_agent_ids={sender.id},
            )
            if bid is None:
                raise LookupError(f"no other agent has the capability {capability!r}")
            self.blackboard.publish_work(item)
            await self.bus.send(
                semantic_message(
                    Performative.DELEGATE,
                    sender_id=sender.id,
                    recipient_id=bid.agent_id,
                    task_id=item.id,
                    goal_id=item.parent_goal_id,
                    payload=item.model_dump(mode="json"),
                    content=item.objective,
                )
            )
            await self._save_blackboard()
            helper = self.registry.get(bid.agent_id).name
            return (
                f"delegated work item {item.id} to {helper}; its result will arrive "
                f"as a message and on the blackboard as work.{item.id}.result"
            )

        return delegate

    def _make_ask_agent(self, sender_id: str) -> Callable[[str, str], Awaitable[str]]:
        """A harness job's ``ask_agent`` tool, bound to the agent it runs for.

        Reaches AgentRuntime._handle's reactive path (the same one a human's
        `/chat <agent>` uses) rather than send_message() alone, so this is a
        real question-then-answer instead of a fire-and-forget inbox drop.
        Replies to a private, one-shot mailbox (never the sender's own
        agent_id) because that agent's ordinary _message_loop never stops
        listening on its own mailbox -- a bare reply-to-sender there would
        race this call for the very reply it is waiting on.
        """

        async def ask(agent: str, question: str) -> str:
            target = self.registry.get(agent)
            if target.id == sender_id:
                # This agent's own reactive handler is what would have to
                # answer -- and if this call is itself running inside that
                # same handler's lock (a harness job started from _handle or
                # run_cycle), asking itself would deadlock rather than just
                # look silly. Refused before the round-trip, not after a
                # timeout.
                raise ValueError("an agent cannot ask itself")
            reply_mailbox = f"ask:{uuid4()}"
            self.bus.register(reply_mailbox)
            try:
                await self.send_message(
                    Message(
                        sender_id=sender_id,
                        recipient_id=target.id,
                        content=question,
                        metadata={"reply_to": reply_mailbox},
                    )
                )
                reply = await self.bus.receive(reply_mailbox, wait_seconds=120)
                return reply.content
            finally:
                # One-shot: nothing else will ever address this mailbox
                # again, on either the answered or the timed-out path.
                self.bus.unregister(reply_mailbox)

        return ask

    _SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

    def _skill_authorship_check(self, name: str, agent_id: str) -> SkillDefinition | None:
        """Shared by learn_skill, patch_skill and approve_skill_write: the
        name is well-formed, and if a skill by that name already exists,
        this agent is the one who wrote it. Returns the existing definition
        (None if there is none) or raises ValueError -- never lets a name
        collision with a human-curated skill (news-triage, say) go through
        just because a model picked the same name.
        """
        if not self._SKILL_NAME.match(name):
            raise ValueError(
                f"'{name}' is not a valid skill name -- lowercase letters, "
                "digits and hyphens only, e.g. 'news-report-export'."
            )
        try:
            existing = self.skills.get(name)
        except MissingSkillError:
            return None
        if existing.created_by != f"agent:{agent_id}":
            raise ValueError(
                f"'{name}' already exists and this agent did not author it "
                f"(created_by={existing.created_by!r}) -- pick a different name."
            )
        return existing

    async def _commit_or_stage_skill_write(
        self, agent_id: str, *, kind: str, name: str, text: str, verb: str
    ) -> str:
        """The one place learn_skill and patch_skill actually reach disk --
        or don't, when HarnessSettings.skill_write_approval asks for a human
        to look first. ``text`` is already the exact SKILL.md content that
        would be written; staging changes nothing about it, so what a human
        approves with ``/learn approve <n>`` is exactly what lands.
        """
        if self.settings.harness.skill_write_approval:
            number = self._next_pending_skill_write
            self._next_pending_skill_write += 1
            summary = f"{verb} '{name}'"
            self.pending_skill_writes[number] = PendingSkillWrite(
                number=number, agent_id=agent_id, kind=kind, name=name, summary=summary, text=text
            )
            return (
                f"Staged as #{number} for human review ({summary}) -- not written yet. "
                "A human runs /learn approve <n> or /learn reject <n>."
            )
        definition = await self.skills.install(text, created_by=f"agent:{agent_id}")
        return f"{verb} '{definition.name}': {definition.description} ({definition.path})"

    def _make_learn_skill(self, agent_id: str) -> Callable[[str, str, str], Awaitable[str]]:
        """A harness job's ``learn_skill`` tool, bound to the agent it runs
        for -- see AgentDefinition.can_learn_skills and harness_tools.
        tool_learn_skill for what gates this being wired in at all.
        """

        async def learn(name: str, description: str, body: str) -> str:
            slug = name.strip().lower()
            existing = self._skill_authorship_check(slug, agent_id)
            text = f"---\nname: {slug}\ndescription: {description}\n---\n\n{body}\n"
            verb = "Updated" if existing is not None else "Learned"
            return await self._commit_or_stage_skill_write(
                agent_id, kind="learn", name=slug, text=text, verb=verb
            )

        return learn

    def _make_patch_skill(self, agent_id: str) -> Callable[[str, str, str], Awaitable[str]]:
        """A harness job's ``patch_skill`` tool -- a targeted, unique-match
        edit of a skill this same agent already wrote, the same anchor
        contract the harness's own ``edit`` tool uses on an ordinary file,
        so a small fix costs one short call instead of re-sending the whole
        body through learn_skill.
        """

        async def patch(name: str, old_text: str, new_text: str) -> str:
            slug = name.strip().lower()
            existing = self._skill_authorship_check(slug, agent_id)
            if existing is None:
                raise ValueError(f"'{slug}' does not exist yet -- use learn_skill to create it.")
            if not old_text:
                raise ValueError("patch_skill needs 'old_text', the exact text to replace.")
            raw = await asyncio.to_thread(
                (self.skills.root / existing.path).read_text, encoding="utf-8"
            )
            count = raw.count(old_text)
            if count == 0:
                raise ValueError(
                    f"that text is not in '{slug}' -- it may have changed since you last read it."
                )
            if count > 1:
                raise ValueError(
                    f"'{old_text[:60]}...' appears {count} times in '{slug}' -- include more "
                    "surrounding text so it matches exactly once."
                )
            text = raw.replace(old_text, new_text, 1)
            return await self._commit_or_stage_skill_write(
                agent_id, kind="patch", name=slug, text=text, verb="Patched"
            )

        return patch

    async def approve_skill_write(self, number: int) -> SkillDefinition:
        """Commit a pending learn_skill/patch_skill exactly as staged --
        console.py's `/learn approve <n>`. Re-runs the authorship check at
        commit time, not just at staging time: the skill landscape may have
        moved since (a human could have written a same-named skill in the
        meantime), so approving is not a bypass of the same rule a live
        write already enforces.
        """
        pending = self.pending_skill_writes.get(number)
        if pending is None:
            raise MissingSkillError(f"no pending skill write numbered {number}")
        self._skill_authorship_check(pending.name, pending.agent_id)
        definition = await self.skills.install(pending.text, created_by=f"agent:{pending.agent_id}")
        del self.pending_skill_writes[number]
        return definition

    def reject_skill_write(self, number: int) -> PendingSkillWrite:
        """Discard a pending write without ever touching disk --
        console.py's `/learn reject <n>`."""
        pending = self.pending_skill_writes.pop(number, None)
        if pending is None:
            raise MissingSkillError(f"no pending skill write numbered {number}")
        return pending

    async def _run_harness_job(self, job: HarnessJob) -> HarnessResult:
        settings = self.settings.harness
        provider_name = self.settings.models.default_provider
        model: str | None = None
        num_ctx_override: int | None = None
        self_check_command = settings.self_check_command
        self_check_max_attempts = settings.self_check_max_attempts
        can_learn_skills = False
        custom_tools = self.active_custom_tools()
        if job.agent_id and self._has(job.agent_id):
            definition = self.registry.get(job.agent_id)
            custom_tools = self.custom_tools_for(definition)
            provider_name, model = definition.provider, definition.model_name
            num_ctx_override = definition.num_ctx
            can_learn_skills = definition.can_learn_skills
            if definition.self_check_command is not None:
                self_check_command = definition.self_check_command
            if definition.self_check_max_attempts is not None:
                self_check_max_attempts = definition.self_check_max_attempts
        provider = self.providers.get(provider_name)
        if provider is None:
            raise RuntimeError(f"Provider '{provider_name}' is not configured")
        session = HarnessSession(next_session_path(settings.session_path))
        job_num_ctx = self.resolve_num_ctx(provider_name, model, num_ctx_override)
        # A reading-only job gets read, grep and ls and nothing that acts.
        reading = job.reading_only
        acting = bool(job.agent_id) and not reading
        runner = build_runner(
            provider,
            job.root,
            cognitive=self.cognition,
            cognitive_agent_id=job.agent_id,
            cognitive_task_id=f"harness:{job.number}",
            session=session,
            limits=settings.limits(),
            model=model,
            max_steps=job.max_steps if job.max_steps is not None else settings.max_steps,
            max_seconds=job.max_seconds if job.max_seconds is not None else settings.max_seconds,
            transcript_chars=settings.transcript_chars_for_num_ctx(job_num_ctx),
            shell_allow=frozenset() if reading else settings.shell_programs(),
            shell_seconds=settings.shell_seconds,
            read_only=not job.allow_write or reading,
            allow_write=job.allow_write and not reading,
            write_prefix=job.write_prefix,
            num_ctx=job_num_ctx,
            scraping_executable=(
                self.settings.scraping.executable
                if self.settings.scraping.enabled and not reading
                else ""
            ),
            scraping_timeout=self.settings.scraping.timeout_seconds,
            ask_agent=self._make_ask_agent(job.agent_id) if acting else None,
            delegate_work=self._make_delegate_work(job.agent_id) if acting else None,
            learn_skill=(
                self._make_learn_skill(job.agent_id) if acting and can_learn_skills else None
            ),
            patch_skill=(
                self._make_patch_skill(job.agent_id) if acting and can_learn_skills else None
            ),
            skills_root=self.skills.root,
            custom_tools=()
            if reading
            else custom_tools
            + await self.active_mcp_tools(job.agent_id or "")
            + (
                self.procedure_learning.tools_for(
                    job.agent_id, task_id=f"harness:{job.number}", allow_write=job.allow_write
                )
                if job.agent_id and self._has(job.agent_id)
                else ()
            ),
            self_check_command=self_check_command,
            self_check_max_attempts=self_check_max_attempts,
            structured_fallback=settings.structured_fallback,
            stop=lambda: job.detail if job.status is JobStatus.CANCELLED else "",
        )
        # An agent's job runs under that agent's grants, so the harness is the
        # loudest user of the permission policy rather than a way around it.
        #
        # Which is why the environment has to grant the root it just handed out.
        # A candidate generation is a directory the mesh created *for this agent
        # to work in*, and without a grant every tool is denied -- the first real
        # generation through the harness spent four steps discovering that. The
        # grant is scoped to that one disposable copy, is visible in `/grant`
        # like any other, and dies with the directory.
        if job.agent_id:
            await self.permissions.grant(
                FilesystemGrant(
                    agent_id=job.agent_id,
                    path=str(job.root),
                    read=True,
                    write=job.allow_write,
                )
            )
            runner.context.policy = self.permissions
            runner.context.agent_id = job.agent_id
        # Every skill is a file under skills/, inside the job root the same as
        # any other -- this line is the only thing that makes one reachable at
        # all: naming it and where to read it, so the model decides whether to
        # spend a step on it, rather than the description being pinned to
        # every job whether it turns out relevant or not.
        catalog = self.skills.render_catalog() if job.catalog else ""
        task = f"{catalog}\n\n{job.objective}" if catalog else job.objective
        try:
            return await runner.run(task)
        finally:
            # Kept even when the job failed: what it managed to change before
            # it broke is the part a human has to look at.
            self.harness_sessions[job.number] = list(session.entries)

    async def _deliver_harness(self, job: HarnessJob) -> None:
        """A finished job is an ordinary inbound message, not a callback.

        It lands in the mailbox, gets the audit record every message gets, and
        wakes the loop the agent already has -- so no behavior has to know that
        a worker exists.

        A job a behavior polls for itself (notify=False: a pipeline stage, a
        plan step) is not delivered, but its agent's cycle is still woken, so
        the poll that consumes it runs now rather than a cycle_seconds later.
        """
        if job.agent_id and (runtime := self.runtimes.get(job.agent_id)) is not None:
            runtime.wake()
        await self._publish_job_artifacts(job)
        if not job.agent_id or not job.notify:
            return
        if job.result is not None:
            said = job.result.answer or job.result.detail
            body = f"Harness job {job.number} {job.result.outcome}: {said}"
        else:
            body = f"Harness job {job.number} did not finish: {job.detail}"
        await self.send_message(
            Message(sender_id="harness", recipient_id=job.agent_id, content=body)
        )

    async def _publish_job_artifacts(self, job: HarnessJob) -> None:
        """What a job wrote is a shared artifact, found by key instead of
        re-described in a message."""
        changes = self.harness.changes(job)
        if not changes:
            return
        owner = (
            self.registry.get(job.agent_id).name
            if job.agent_id and self._has(job.agent_id)
            else "console"
        )
        for change in changes:
            path = str(change.get("path") or "")
            if not path:
                continue
            self.blackboard.publish_artifact(
                ArtifactRecord(
                    key=f"{owner}:{path}",
                    path=str(job.root / path),
                    source=owner,
                    metadata={"job": job.number, "kind": change.get("kind")},
                )
            )
        await self._save_blackboard()

    def _services(self) -> dict[str, object]:
        return {
            "environment": self,
            "evolver": self.evolver,
            "skills": self.skills,
            "tools": self.tools,
            "mcp": self.mcp,
            "permissions": self.permissions,
            "repository": self.repository,
            "runtime_states": self.runtime_states(),
            "provider_health": self.provider_health,
            "registry": self.registry,
            "harness": self.harness if self.settings.harness.enabled else None,
            "events": self.events,
            "blackboard": self.blackboard,
            "capabilities": self.capabilities,
            "contract_net": self.contract_net,
            "improvements": self.improvements,
            "procedures": self.procedures,
            "ideas": self.ideas,
        }

    async def configure_agent_model(
        self, agent_id_or_name: str, provider_name: str, model_name: str
    ) -> AgentDefinition:
        if provider_name not in self.providers:
            raise ValueError(f"Provider '{provider_name}' is not configured")
        definition = self.registry.get(agent_id_or_name)
        was_running = definition.id in self.runtimes
        if was_running:
            # A model swap is not a request to disable the agent.
            await self.stop_agent(definition.id, persist_status=False)
        definition.provider = provider_name
        definition.model_name = model_name
        definition.touch()
        await self.repository.save_agent(definition)
        if was_running:
            definition.status = AgentStatus.ACTIVE
            await self.start_agent(definition.id)
        return definition

    async def configure_agent_num_ctx(
        self, agent_id_or_name: str, num_ctx: int | None
    ) -> AgentDefinition:
        """Set or clear one agent's context-window override.

        None (not zero) means "stop overriding" -- the agent goes back to
        whatever its provider's own num_ctx resolves to, the same as it would
        for an agent that never had an override at all.
        """
        definition = self.registry.get(agent_id_or_name)
        was_running = definition.id in self.runtimes
        if was_running:
            await self.stop_agent(definition.id, persist_status=False)
        definition.num_ctx = num_ctx
        definition.touch()
        await self.repository.save_agent(definition)
        if was_running:
            definition.status = AgentStatus.ACTIVE
            await self.start_agent(definition.id)
        return definition

    async def configure_agent_muted(self, agent_id_or_name: str, muted: bool) -> AgentDefinition:
        """Toggle whether this agent's unprompted announcements are silenced.

        No stop/restart needed, unlike a model or context-window change: this
        touches nothing the running loop reads, only what announce_agent()
        does with what it produces.
        """
        definition = self.registry.get(agent_id_or_name)
        definition.muted = muted
        definition.touch()
        await self.repository.save_agent(definition)
        return definition

    async def delete_agent(
        self, agent_id_or_name: str, *, wipe_workspace: bool = False
    ) -> AgentDefinition:
        """Remove an agent for good: stopped, unregistered, its grants gone,
        and -- only if asked -- its memory.md/context.md/playground with it.

        A system agent (Architect, Guardian, Evaluator, the Evolver itself)
        is structural to the mesh rather than something a human spawned, so
        deleting one is refused rather than silently taking down a piece of
        the mesh's own machinery; stop it instead if it needs to be quiet.
        `wipe_workspace` defaults to false: the registry entry going away is
        already the irreversible half of this, and a directory left behind
        is a mistake a human can still recover from, not one they are forced
        to accept up front.
        """
        definition = self.registry.get(agent_id_or_name)
        if definition.type == "system":
            raise ValueError(
                f"'{definition.name}' is a core agent and cannot be deleted; stop it instead."
            )
        await self.stop_agent(definition.id, persist_status=False)
        await self.permissions.revoke_all(definition.id)
        self.registry.unregister(definition.id)
        self.capabilities.unregister(definition.id)
        await self.repository.delete_agent(definition.id)
        if wipe_workspace:
            directory = self.memory_for(definition).directory
            await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)
        return definition

    async def available_models(self, provider_name: str) -> list[str]:
        provider = self.providers.get(provider_name)
        if provider is None:
            raise ValueError(f"Provider '{provider_name}' is not configured")
        return await provider.list_models()

    # -- world ----------------------------------------------------------

    def _world_snapshot(self) -> str:
        lines = [
            f"Environment: {self.settings.environment_name}",
            f"Provider: {self.settings.models.default_provider} "
            f"({'ready' if self.provider_health[0] else self.provider_health[1]})",
        ]
        for state in self.runtime_states().values():
            goal = f" goal: {state.goal}" if state.goal else ""
            lines.append(f"- {state.name} [{state.phase}]{goal}")
        shared = self.blackboard.projection(limit=WORLD_BLACKBOARD_LINES)
        for section in ("Facts", "Artifacts", "Work items"):
            if shared[section] != "none":
                lines.append(f"Shared {section.lower()}:")
                lines.append(shared[section])
        return "\n".join(lines)

    async def refresh_world(self) -> None:
        states = self.runtime_states()
        roster = "\n".join(
            f"- {state.name}: {state.phase}"
            + (f", goal: {state.goal}" if state.goal else "")
            + (f", last: {state.last_outcome}" if state.last_outcome else "")
            for state in states.values()
        )
        pipeline = await self.evolver.pipeline_state()
        await self.world.write(
            {
                "Environment": (
                    f"name: {self.settings.environment_name}\n"
                    f"generation: {self.evolver.workspace.supervisor.metadata()['active']}\n"
                    f"provider: {self.settings.models.default_provider} "
                    f"({'ready' if self.provider_health[0] else self.provider_health[1]})"
                ),
                "Agents": roster,
                "Evolution": f"stage: {pipeline.get('stage', 'plan')}",
                **{
                    f"Shared {name.lower()}": text
                    for name, text in self.blackboard.projection().items()
                },
            }
        )

    # -- passthroughs ---------------------------------------------------

    async def send_message(self, message: Message) -> None:
        await self.bus.send(message)

    async def grant_access(self, grant: FilesystemGrant) -> None:
        await self.permissions.grant(grant)

    async def revoke_access(self, agent_id: str, path: str) -> None:
        await self.permissions.revoke(agent_id, path)

    async def request_model_inference(
        self,
        prompt: str,
        *,
        provider_name: str | None = None,
        model_name: str | None = None,
        system: str = "",
    ) -> str:
        name = provider_name or self.settings.models.default_provider
        provider = self.providers.get(name)
        if provider is None:
            raise RuntimeError(f"Provider '{name}' is unavailable")
        num_ctx = self.resolve_num_ctx(name, model_name)
        return await self.cognition.generate(
            provider,
            prompt,
            service=CognitiveServiceType.DIRECT_INFERENCE,
            reason=ModelInvocationReason.EXPLICIT_INFERENCE_REQUEST,
            provider_name=name,
            system=system,
            model=model_name,
            num_ctx=num_ctx,
        )

    def status(self) -> dict[str, object]:
        states = self.runtime_states()
        return {
            "environment": self.settings.environment_name,
            "generation": self.evolver.workspace.supervisor.metadata()["active"],
            "status": self.health_state,
            "agents": len(self.registry.all()),
            "running": len(self.runtimes),
            "cycles": sum(state.cycles for state in states.values()),
            "provider": self.settings.models.default_provider,
            "provider_ready": self.provider_health[0],
            "provider_message": self.provider_health[1],
            "cognition": self.cognition.metrics.snapshot(),
            "events": len(self.events.history),
            "goals_completed_without_model": sum(
                event.type is EventType.GOAL_COMPLETED
                and event.payload.get("model_calls") == 0
                for event in self.events.history
            ),
            "blackboard": {
                "facts": len(self.blackboard.facts),
                "artifacts": len(self.blackboard.artifacts),
                "work_items": len(self.blackboard.work_items),
            },
            "improvements": {
                "total": len(self.improvement_backlog.items),
                **Counter(item.status.value for item in self.improvement_backlog.items.values()),
                "work_items": Counter(
                    work.status.value for work in self.improvement_backlog.work_items.values()
                ),
            },
            # Open goals nobody has touched for an hour: work that is
            # neither progressing nor failing loudly enough to be a stall.
            "stale_goals": {
                definition.name: count
                for definition in self.registry.all()
                for count in [
                    len(
                        GoalManager(definition.mind).stalled_goals(
                            timedelta(seconds=STALE_GOAL_SECONDS)
                        )
                    )
                ]
                if count
            },
        }
