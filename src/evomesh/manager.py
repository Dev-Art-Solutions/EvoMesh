"""The Manager: one agent a human talks to, that talks to all the others.

A human with nine agents had to know which one owns news, which one crawls,
and which console command does what. An agent with ``AgentDefinition.manager``
set is the single point of contact instead. It is an ordinary agent -- it
answers through its own harness job, in its own conversation and on its own
Telegram bot -- that is also given four tools:

- ``mesh_agents``: who exists, what each is for, and what it is doing now,
  which is how the Manager decides who owns a request.
- ``assign_task``: hand a piece of work to another agent as a goal on it. The
  Manager follows it (``ManagerDesk``) and tells the human when it finishes,
  fails or stalls, on its own bot. A quick question needs no task at all:
  the harness's ``ask_agent`` already asks another agent and waits.
- ``task_status``: the work it handed out, and what came of it.
- ``mesh_command``: a console command. A command that only reads runs at
  once. Anything that changes the mesh -- a grant, a goal edit, a restart --
  waits for a human's ``/manager approve <n>``, and some never run at all.

The approval is the boundary, and it is enforced here, in the mesh process:
the decision whether a command reads or writes is a table, never the
model's opinion, and ``/manager`` itself is unreachable from the tool, so the
Manager cannot approve its own requests or give itself more access.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel, Field

from evomesh.contracts import AgentDefinition, GoalStatus, Message
from evomesh.events import Event, EventType
from evomesh.goal_manager import GoalManager
from evomesh.harness_tools import Tool, ToolDenied

if TYPE_CHECKING:
    from evomesh.environment import Environment

logger = logging.getLogger(__name__)

STATE_KEY = "manager_tasks"
MAX_TASKS = 100
OUTPUT_CHARS = 3500
APPROVAL_MINUTES = 30
ROSTER_CHARS = 4000

# Never run from the tool, approved or not. /exit ends the mesh with nothing
# left to bring it back; /manager is the approval channel itself; the rest
# act on the console session the tool borrows, which nobody is looking at.
FORBIDDEN = {"/exit", "/manager", "/chat", "/confirm", "/cancel", "/attach"}

# Commands that only read, by command and (where it matters) sub-action.
# None: every form of the command reads. A set: only those first arguments
# do; anything else -- including a sub-action added later -- needs approval.
READ_ONLY: dict[str, set[str] | None] = {
    "/help": None,
    "/status": None,
    "/agents": None,
    "/skills": None,
    "/tools": None,
    "/agent-templates": None,
    "/models": None,
    "/beliefs": None,
    "/goals": None,
    "/intentions": None,
    "/notifications": None,
    "/memory": None,
    "/context": None,
    "/ideas": None,
    "/reports": None,
    "/skill": {"show"},
    "/tool": {"show"},
    "/agent-template": {"show"},
    "/idea": {"show"},
    "/capabilities": {"", "tools", "skills", "mcp", "grants", "check"},
    "/evolution": {"status"},
    "/harness": {"status"},
    "/learn": {"status", "review"},
    "/telegram": {"status"},
    "/browser": {"status", "pending"},
    "/mcp": {"servers", "check"},
    "/email": {"accounts"},
    "/wiki": {"search", "log"},
    "/typed": {"list", "show", "explain", "executions", "execution"},
}
# Read-only only in their bare form: with more arguments they change things.
READ_ONLY_BARE = {"/procedures": 2, "/improvements": 1, "/rules": 2}


def classify(command: str) -> str:
    """'read', 'approve' or 'forbidden' for one console command line."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return "forbidden"
    if not parts or not parts[0].startswith("/"):
        return "forbidden"
    name = parts[0].lower()
    if name in FORBIDDEN:
        return "forbidden"
    if name in READ_ONLY_BARE:
        return "read" if len(parts) <= READ_ONLY_BARE[name] else "approve"
    if name not in READ_ONLY:
        return "approve"
    actions = READ_ONLY[name]
    if actions is None:
        return "read"
    action = parts[1].lower() if len(parts) > 1 else ""
    if name == "/wiki" and action and action not in {"search", "lint", "log"}:
        return "read"  # /wiki <agent>: that agent's index
    return "read" if action in actions else "approve"


class TrackedTask(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:6])
    manager_id: str
    agent_id: str
    agent_name: str
    goal_id: str
    request: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: str = "running"  # running | done | failed | stalled | cancelled
    result: str = ""
    reported: bool = False


@dataclass
class PendingCommand:
    number: int
    manager_id: str
    command: str
    reason: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def expired(self) -> bool:
        return datetime.now(UTC) - self.created_at > timedelta(minutes=APPROVAL_MINUTES)


class ManagerDesk:
    """The work Managers handed out, and the commands waiting on a human.
    Owned by Environment.manager; tasks persist, pending commands do not."""

    def __init__(self, environment: Environment) -> None:
        self.environment = environment
        self.tasks: list[TrackedTask] = []
        self.pending: dict[int, PendingCommand] = {}
        self._next = 1
        self._sweep_task: asyncio.Task[None] | None = None
        # (manager_id, prompt) -> that Manager's answer. Set by Environment to
        # its ask path; None in a bare desk, which then sends the raw report.
        self.report: Callable[[str, str], Awaitable[str]] | None = None
        self._checks: set[asyncio.Task[None]] = set()

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        stored = await self.environment.repository.load_state(STATE_KEY)
        if isinstance(stored, list):
            for item in stored:
                try:
                    self.tasks.append(TrackedTask.model_validate(item))
                except ValueError:
                    continue
        events = self.environment.events
        for kind in (EventType.GOAL_COMPLETED, EventType.TASK_FAILED, EventType.AGENT_STALLED):
            events.subscribe(kind, self._on_event)
        self._sweep_task = asyncio.create_task(self._sweep_loop(), name="manager-desk")

    async def stop(self) -> None:
        for checking in list(self._checks):
            checking.cancel()
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._sweep_task = None

    async def _save(self) -> None:
        self.tasks = self.tasks[-MAX_TASKS:]
        await self.environment.repository.save_state(
            STATE_KEY, [task.model_dump(mode="json") for task in self.tasks]
        )

    # -- handing out work -----------------------------------------------

    async def assign(self, manager: AgentDefinition, agent: str, task: str) -> TrackedTask:
        environment = self.environment
        try:
            target = environment.registry.get(agent)
        except KeyError as exc:
            raise ToolDenied(
                f"DENIED: no agent named {agent!r}. mesh_agents lists them."
            ) from exc
        if target.id == manager.id:
            raise ToolDenied("DENIED: that is you -- do the work yourself instead.")
        if target.type == "system":
            raise ToolDenied(
                f"DENIED: {target.name} is a system agent and takes no tasks. "
                "Its commands go through mesh_command."
            )
        text = " ".join(task.split())
        if not text:
            raise ToolDenied("DENIED: assign_task needs the task, in words.")
        # The goal's last summary is what comes back; found live, a crawl
        # ended on "compiled the two lists" with the headlines nowhere in it.
        goal = GoalManager(target.mind).create(
            f"{text} (Asked by {manager.name} for the human. Your final answer must be "
            "the result itself -- the list, the figures, the text -- not a description "
            "of what you did.)",
            priority=8,
        )
        target.touch()
        await environment.repository.save_agent(target)
        tracked = TrackedTask(
            manager_id=manager.id,
            agent_id=target.id,
            agent_name=target.name,
            goal_id=goal.id,
            request=text,
        )
        self.tasks.append(tracked)
        await self._save()
        runtime = environment.runtimes.get(target.id)
        if runtime is not None:
            runtime.wake()
        return tracked

    def tasks_for(self, manager_id: str) -> list[TrackedTask]:
        return [task for task in self.tasks if task.manager_id == manager_id]

    # -- following it ----------------------------------------------------

    async def _on_event(self, event: Event) -> None:
        for task in self.tasks:
            if task.status in ("running", "stalled") and task.goal_id == event.goal_id:
                summary = str(event.payload.get("summary") or "")
                if summary:
                    task.result = summary
                # Off the publisher's path: handlers run inside the finishing
                # agent's own cycle, and writing the report waits on a model.
                checking = asyncio.create_task(self.check(task), name=f"manager-{task.id}")
                self._checks.add(checking)
                checking.add_done_callback(self._checks.discard)

    async def settled(self) -> None:
        """Wait for the checks events started (tests, and an orderly stop)."""
        while self._checks:
            await asyncio.gather(*list(self._checks), return_exceptions=True)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                for task in list(self.tasks):
                    if task.status == "running":
                        await self.check(task)
            except Exception:  # noqa: BLE001 - the desk outlives one bad check
                logger.exception("manager desk sweep failed")

    async def check(self, task: TrackedTask) -> None:
        """Read the goal's state on its agent and report a change once."""
        if task.status not in ("running", "stalled"):
            return  # an earlier check already closed it
        try:
            agent = self.environment.registry.get(task.agent_id)
            goal = agent.mind.goal(task.goal_id)
        except KeyError:
            await self._close(task, "cancelled", "the agent or its goal no longer exists")
            return
        if goal.status is GoalStatus.DONE:
            await self._close(task, "done", task.result or goal.last_error or "")
        elif goal.status is GoalStatus.FAILED:
            await self._close(task, "failed", goal.last_error or goal.blocked_reason or "")
        elif goal.status is GoalStatus.CANCELLED:
            await self._close(task, "cancelled", goal.blocked_reason or "")
        elif goal.status is GoalStatus.STALLED and task.status == "running":
            task.status = "stalled"
            task.result = goal.last_error or "no progress"
            await self._save()
            await self._tell(
                task,
                f"⏸ {task.agent_name} is stuck on your task: {task.request}\n"
                f"{task.result[:600]}\nIt will try again on its own; "
                "say if I should drop it.",
            )

    async def _close(self, task: TrackedTask, status: str, result: str) -> None:
        task.status = status
        task.result = result
        await self._save()
        icon = {"done": "✅", "failed": "❌"}.get(status, "◻")
        verb = {"done": "finished", "failed": "could not finish"}.get(status, "stopped")
        body = await self._report(task, verb) or result.strip()[:1500] or "(it reported nothing)"
        await self._tell(task, f"{icon} {task.agent_name} {verb}: {task.request[:200]}\n\n{body}")

    async def _report(self, task: TrackedTask, verb: str) -> str:
        """The Manager writes the message itself: an agent's closing summary
        is often an account of the work rather than its result, and the
        Manager can ask that agent for the real thing first."""
        if self.report is None:
            return ""
        try:
            return (
                await self.report(
                    task.manager_id,
                    f"[task {task.id}] {task.agent_name} {verb} the work you gave it: "
                    f"{task.request}\nIts closing report:\n{task.result[:3000] or '(none)'}\n\n"
                    "Write the message the human gets now: the result itself, in their "
                    f"language. If the report only describes the result, ask_agent "
                    f"{task.agent_name} for the actual result first. If it failed, say why "
                    "and what you suggest.",
                )
            ).strip()
        except Exception:  # noqa: BLE001 - the raw report still goes out
            logger.exception("the manager could not write task %s's report", task.id)
            return ""

    async def _tell(self, task: TrackedTask, text: str) -> None:
        task.reported = True
        await self._save()
        await self.environment.announce_agent(task.manager_id, text)

    # -- commands --------------------------------------------------------

    async def run_command(self, manager: AgentDefinition, command: str) -> str:
        command = " ".join(command.split())
        kind = classify(command)
        if kind == "forbidden":
            raise ToolDenied(
                f"DENIED: {command.split(' ', 1)[0] or 'that'} cannot be run from here, "
                "approved or not. Tell the human to run it themselves."
            )
        if kind == "approve":
            number = self._next
            self._next += 1
            self.pending[number] = PendingCommand(number, manager.id, command, "changes the mesh")
            await self.environment.announce_agent(
                manager.id,
                f"🛑 {manager.name} wants to run: {command}\n"
                f"/manager approve {number}  or  /manager deny {number}",
            )
            return (
                f"confirmation_required: this command changes the mesh, so a human must "
                f"approve it with /manager approve {number}. They have been asked. Tell the "
                "human what you asked to run and why, then stop -- do not try another way."
            )
        return await self._execute(command)

    async def _execute(self, command: str) -> str:
        from evomesh.console import ConsoleChannel

        console = ConsoleChannel(self.environment)
        output = await console.route(command)
        if len(output) > OUTPUT_CHARS:
            output = output[:OUTPUT_CHARS] + f"\n[... {len(output) - OUTPUT_CHARS} chars cut]"
        return output or "(no output)"

    async def approve(self, number: int) -> str:
        pending = self.pending.pop(number, None)
        if pending is None:
            return f"No pending command {number}. /manager pending lists them."
        if pending.expired():
            return f"Command {number} expired ({APPROVAL_MINUTES} min); ask again."
        output = await self._execute(pending.command)
        # Into the Manager's inbox for its next answer, as a broadcast so it
        # is context, not a question it must reply to.
        await self.environment.send_message(
            Message(
                sender_id="mesh",
                recipient_id=pending.manager_id,
                content=f"The human approved and ran `{pending.command}`. Output:\n{output[:1500]}",
                metadata={"broadcast": True},
            )
        )
        return f"Ran {pending.command}\n{output}"

    def deny(self, number: int) -> str:
        pending = self.pending.pop(number, None)
        if pending is None:
            return f"No pending command {number}."
        return f"Denied: {pending.command}"

    # -- the roster ------------------------------------------------------

    def roster(self, exclude_id: str = "") -> str:
        environment = self.environment
        states = environment.runtime_states()
        lines: list[str] = []
        for agent in environment.registry.all():
            if agent.id == exclude_id:
                continue
            state = states.get(agent.id)
            phase = state.phase.value if state is not None else "offline"
            goals = [
                goal.description
                for goal in agent.mind.goals
                if goal.status
                in {GoalStatus.RUNNABLE, GoalStatus.ACTIVE, GoalStatus.PENDING, GoalStatus.STALLED}
            ][:3]
            parts = [
                f"## {agent.name} ({agent.type}, {agent.status.value}/{phase})",
                f"purpose: {' '.join(agent.purpose.split())[:240]}",
            ]
            if agent.skills:
                parts.append(f"skills: {', '.join(agent.skills)}")
            if agent.tools:
                parts.append(f"tools: {', '.join(agent.tools)}")
            if agent.browser is not None:
                parts.append(f"browser: {agent.browser.level}")
            if goals:
                parts.append("working on: " + " | ".join(g[:100] for g in goals))
            lines.append("\n".join(parts))
        text = "\n\n".join(lines)
        if len(text) > ROSTER_CHARS:
            text = text[:ROSTER_CHARS] + "\n[... cut]"
        return text


def build_manager_tools(desk: ManagerDesk, definition: AgentDefinition) -> tuple[Tool, ...]:
    """The four tools, for a Manager only; none for anyone else."""
    if not definition.manager:
        return ()

    async def agents(context: Any, args: dict[str, Any]) -> str:
        del context, args
        return desk.roster(exclude_id=definition.id) or "no other agents"

    async def assign(context: Any, args: dict[str, Any]) -> str:
        del context
        task = await desk.assign(
            definition, str(args.get("agent") or ""), str(args.get("task") or "")
        )
        return (
            f"task {task.id} is now a goal on {task.agent_name}. You will be told when it "
            "finishes, fails or stalls, and you tell the human; task_status shows it any time."
        )

    async def status(context: Any, args: dict[str, Any]) -> str:
        del context, args
        tasks = desk.tasks_for(definition.id)[-10:]
        if not tasks:
            return "no tasks handed out yet"
        return "\n".join(
            f"{task.id} [{task.status}] {task.agent_name}: {task.request[:120]}"
            + (f"\n  -> {' '.join(task.result.split())[:300]}" if task.result else "")
            for task in reversed(tasks)
        )

    async def command(context: Any, args: dict[str, Any]) -> str:
        del context
        return await desk.run_command(definition, str(args.get("command") or ""))

    return (
        Tool(
            name="mesh_agents",
            description="Every other agent: what it is for, its skills and tools, what it "
            "is doing now. Read it to decide who owns a request.",
            parameters={"type": "object", "properties": {}},
            run=agents,
        ),
        Tool(
            name="assign_task",
            description="Hand longer work (a crawl, a report, a recurring check) to the "
            "agent that owns it. It becomes a goal on that agent; you are told when it "
            "ends. For a quick question use ask_agent instead.",
            parameters={
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "agent name"},
                    "task": {"type": "string", "description": "the work, in full"},
                },
                "required": ["agent", "task"],
            },
            run=assign,
        ),
        Tool(
            name="task_status",
            description="The tasks you handed out and what came of them.",
            parameters={"type": "object", "properties": {}},
            run=status,
        ),
        Tool(
            name="mesh_command",
            description="Run one EvoMesh console command, e.g. \"/agents\" or "
            "\"/goals Crawler\". Read-only commands run now; one that changes the mesh "
            "waits for the human's approval.",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            run=command,
        ),
    )

