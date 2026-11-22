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
import re
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
TRUST_KEY = "manager_trust"
REPEATS_KEY = "manager_repeats"
MAX_TASKS = 100
OUTPUT_CHARS = 3500
APPROVAL_MINUTES = 30
ROSTER_CHARS = 4000
# message_human: enough to count down from ten or list a few items one by
# one; a loop past it is cut off rather than flooding the human's phone.
MESSAGES_PER_MINUTE = 20
MESSAGE_CHARS = 3500
# A repeat: something done again on a clock -- a message, or a question to
# an agent whose answer is passed on. The clock is code: a model takes
# seconds to answer, so "every second" can only be a loop, and a run never
# overlaps the one before it. Bounded so a forgotten one ends on its own.
REPEAT_MIN_SECONDS = 1.0
REPEAT_DEFAULT_RUNS = 600
REPEAT_MAX_RUNS = 3600
# A human's whole message, any case: stops everything the Manager runs for
# them -- repeats and handed-out work -- before any model reads it (rule 6:
# a word list, never a model, decides this).
STOP_WORDS = {"стоп", "спри", "спрете", "stop", "спри!", "стоп!", "stop!"}
# "спри 1", "stop #2", "спри номер 3": one item of the numbered list, by
# words alone (rule 6), before any model reads it.
STOP_ONE = re.compile(
    r"^(?:стоп|спри|спрете|stop)\s*(?:номер|задача|number|no\.?|#)?\s*#?(\d+)[.!]?$"
)
# "спри новините", "stop the crawl": a stop verb, then what to stop.
STOP_NAMED = re.compile(r"^(?:стоп|спри|спрете|stop)\b[\s,:-]*(.+)$")


def _stems(text: str) -> set[str]:
    """Rough word stems (the first five letters of words of four or more),
    enough for "новините" to find "новини" in any language. No list of
    filler words: a word that names nothing running matches nothing, and a
    match that is not unique asks instead of guessing."""
    return {word[:5] for word in re.findall(r"\w+", text.lower()) if len(word) >= 4}


# What an asked agent says when a repeated question has nothing new: that
# run sends nothing.
NOTHING_NEW = "NOTHING_NEW"

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

# Commands that change what agents do, never what they may do. These run
# without approval when the human's own message started the Manager's turn
# ("give NewsWatcher a goal") -- not when a repeat, a report or another
# agent's answer did. Everything that grants, revokes, installs, deletes or
# restarts stays behind approval whatever started it: that is how text from
# a web page could otherwise widen what an agent may do.
WORK: dict[str, set[str] | None] = {
    "/goal": {"add", "done", "drop", "pause", "resume", "notify", "pattern", "text"},
    "/agent": {"start", "stop", "mute", "unmute"},
    "/cycle": None,
    "/model": None,
    "/num-ctx": None,
}

# A human's whole reply to a waiting command, any case, decided by a word
# list (rule 6), never by a model.
YES_WORDS = {"да", "yes", "ok", "okay", "ок", "давай", "добре", "одобрявам", "approve", "y"}
NO_WORDS = {"не", "no", "недей", "откажи", "deny", "n"}


def classify(command: str) -> str:
    """'read', 'work', 'approve' or 'forbidden' for one console command line."""
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
    if name in WORK:
        actions = WORK[name]
        action = parts[1].lower() if len(parts) > 1 else ""
        return "work" if actions is None or action in actions else "approve"
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


@dataclass
class Repeat:
    number: int
    description: str
    every_seconds: float
    runs: int = 0
    task: asyncio.Task[None] | None = None
    # What restarts it after a mesh restart: the arguments, with the counter
    # and the runs left as they stood after its last run.
    spec: dict[str, Any] = field(default_factory=dict)
    stopped: bool = False


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
        # manager_id -> True while a human's own message is being answered: the
        # only time a "work" command runs without approval. Set by the console.
        self.human_turn: dict[str, bool] = {}
        # /manager trust on: what changes access runs too when the human asked
        # -- the human's own choice, off by default, kept across restarts.
        self.trusted = False
        self._stopping = False
        self._saving = asyncio.Lock()
        # manager_id -> what each number in its last numbered list meant.
        self._listed: dict[str, list[tuple[str, str]]] = {}
        # manager_id -> its running repeats. Persisted (REPEATS_KEY): the mesh
        # restarts itself whenever a generation lands, and "every 30 seconds"
        # must not end quietly then.
        self.repeats: dict[str, list[Repeat]] = {}
        self._repeat_ids = 1

    # -- lifecycle -------------------------------------------------------

    async def set_trusted(self, trusted: bool) -> None:
        self.trusted = trusted
        await self.environment.repository.save_state(TRUST_KEY, {"trusted": trusted})

    async def start(self) -> None:
        trust = await self.environment.repository.load_state(TRUST_KEY)
        self.trusted = bool(isinstance(trust, dict) and trust.get("trusted"))
        saved = await self.environment.repository.load_state(REPEATS_KEY)
        for spec in saved if isinstance(saved, list) else []:
            try:
                self.start_repeat(
                    str(spec["manager_id"]),
                    float(spec["every_seconds"]),
                    message=str(spec.get("message") or ""),
                    agent=str(spec.get("agent") or ""),
                    question=str(spec.get("question") or ""),
                    start=int(spec.get("start", 1)),
                    step=int(spec.get("step", 1)),
                    runs=int(spec.get("runs", REPEAT_DEFAULT_RUNS)),
                )
            except (KeyError, TypeError, ValueError, ToolDenied):
                logger.warning("a saved repeat could not be restarted: %s", spec)
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
        # A stopping mesh pauses its repeats; they are saved and start again.
        self._stopping = True
        for checking in list(self._checks):
            checking.cancel()
        for items in self.repeats.values():
            for item in items:
                if item.task is not None:
                    item.task.cancel()
        self.repeats.clear()
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

    # -- repeats and stopping ----------------------------------------------

    def start_repeat(
        self,
        manager_id: str,
        every_seconds: float,
        *,
        message: str = "",
        agent: str = "",
        question: str = "",
        start: int = 1,
        step: int = 1,
        runs: int = REPEAT_DEFAULT_RUNS,
    ) -> Repeat:
        """Send ``message`` (with {n} counting from ``start`` by ``step``), or
        ask ``agent`` ``question`` and pass the answer on, every
        ``every_seconds`` -- counted from when the last run finished."""
        if every_seconds < REPEAT_MIN_SECONDS:
            raise ToolDenied(f"DENIED: every_seconds must be at least {REPEAT_MIN_SECONDS:g}.")
        if bool(message.strip()) == bool(question.strip()):
            raise ToolDenied("DENIED: give either a message or a question, not both or neither.")
        if message:
            try:
                message.format(n=start)
            except (KeyError, IndexError, ValueError) as exc:
                raise ToolDenied(f"DENIED: a message may use only {{n}}: {exc}") from exc
        target = manager_id
        if question:
            try:
                target = self.environment.registry.get(agent or manager_id).id
            except KeyError as exc:
                raise ToolDenied(f"DENIED: no agent named {agent!r}.") from exc
            if self.report is None:
                raise ToolDenied("DENIED: asking is not available in this mesh.")
        runs = max(1, min(int(runs), REPEAT_MAX_RUNS))
        repeat = Repeat(
            number=self._repeat_ids,
            description=message or f"{agent or 'me'}: {question}",
            every_seconds=every_seconds,
            spec={
                "manager_id": manager_id,
                "every_seconds": every_seconds,
                "message": message,
                "agent": target if question else "",
                "question": question,
                "start": start,
                "step": step,
                "runs": runs,
            },
        )
        self._repeat_ids += 1

        async def run() -> None:
            n = start
            for left in range(runs, 0, -1):
                repeat.spec.update(start=n, runs=left)
                await self._save_repeats()
                if message:
                    text = message.format(n=n)
                else:
                    assert self.report is not None
                    try:
                        text = await self.report(
                            target,
                            f"{question}\n\nThis is asked again every {every_seconds:g}s. "
                            "Give only what is new since your last answer to it; if "
                            f"nothing is, reply exactly {NOTHING_NEW}.",
                        )
                    except Exception as exc:  # noqa: BLE001 - one failed run is reported
                        text = f"({repeat.description[:80]}: {exc})"
                    if NOTHING_NEW in text.upper().replace(" ", "_"):
                        # "Every 30 seconds" must not mean a "no news" every 30 seconds.
                        repeat.runs += 1
                        await asyncio.sleep(every_seconds)
                        continue
                await self.environment.tell_human(manager_id, text[:MESSAGE_CHARS])
                repeat.runs += 1
                n += step
                await asyncio.sleep(every_seconds)

        repeat.task = asyncio.create_task(run(), name=f"manager-repeat-{repeat.number}")
        running = self.repeats.setdefault(manager_id, [])
        running.append(repeat)

        def finished(_: asyncio.Task[None]) -> None:
            if repeat in running:
                running.remove(repeat)
            self._persist_soon()

        repeat.task.add_done_callback(finished)
        self._persist_soon()
        return repeat

    async def _save_repeats(self) -> None:
        # One write at a time, the list read inside: a run's own save landing
        # after a stop's would bring the stopped repeat back at the next start.
        async with self._saving:
            specs = [
                item.spec
                for items in self.repeats.values()
                for item in items
                if item.task is not None and not item.task.done() and not item.stopped
            ]
            await self.environment.repository.save_state(REPEATS_KEY, specs)

    def _persist_soon(self) -> None:
        if self._stopping:
            return  # a mesh stopping keeps its repeats for the next start
        try:
            saving = asyncio.get_running_loop().create_task(self._save_repeats())
        except RuntimeError:
            return
        self._checks.add(saving)
        saving.add_done_callback(self._checks.discard)

    def listing(self, manager_id: str) -> list[str]:
        """Everything this Manager runs for the human, numbered 1..n -- the
        numbers "stop 1" refers to -- then what finished lately, unnumbered."""
        entries: list[tuple[str, str]] = []
        lines: list[str] = []
        for item in self.repeats.get(manager_id, []):
            entries.append(("repeat", str(item.number)))
            lines.append(
                f"{len(entries)}. every {item.every_seconds:g}s ({item.runs} so far): "
                f"{item.description[:120]}"
            )
        mine = self.tasks_for(manager_id)
        for task in reversed(mine):
            if task.status in ("running", "stalled"):
                entries.append(("task", task.id))
                lines.append(
                    f"{len(entries)}. {task.agent_name} [{task.status}]: {task.request[:120]}"
                )
        self._listed[manager_id] = entries
        if not lines:
            lines.append("Nothing is running.")
        done = [task for task in reversed(mine) if task.status not in ("running", "stalled")]
        if done:
            lines.append("Finished lately:")
            lines += [
                f"- {task.agent_name} [{task.status}]: {task.request[:100]}"
                + (f" -> {' '.join(task.result.split())[:160]}" if task.result else "")
                for task in done[:5]
            ]
        return lines

    async def stop_listed(self, manager_id: str, number: int) -> str:
        if manager_id not in self._listed:
            self.listing(manager_id)
        entries = self._listed.get(manager_id, [])
        if not 1 <= number <= len(entries):
            return f"There is no {number} in the list; ask for the list again."
        kind, key = entries[number - 1]
        if kind == "repeat":
            if self.stop_repeats(manager_id, int(key)):
                return f"⏹ Stopped {number}."
            return f"{number} had already ended."
        for task in self.tasks_for(manager_id):
            if task.id == key and task.status in ("running", "stalled"):
                await self.stop_task(task)
                return f"⏹ Stopped {number} ({task.agent_name})."
        return f"{number} had already ended."

    def describe_repeats(self, manager_id: str | None = None) -> list[str]:
        owners = [manager_id] if manager_id is not None else list(self.repeats)
        return [
            f"repeat #{item.number} every {item.every_seconds:g}s, {item.runs} run(s): "
            f"{item.description[:120]}"
            for owner in owners
            for item in self.repeats.get(owner, [])
        ]

    def stop_repeats(self, manager_id: str, number: int | None = None) -> int:
        running = self.repeats.get(manager_id, [])
        chosen = [item for item in running if number is None or item.number == number]
        for item in chosen:
            item.stopped = True
            if item.task is not None:
                item.task.cancel()
            running.remove(item)
        if chosen:
            self._persist_soon()
        return len(chosen)

    async def stop_task(self, task: TrackedTask) -> None:
        """Take handed-out work back: drop the goal on its agent."""
        try:
            agent = self.environment.registry.get(task.agent_id)
            goal = agent.mind.goal(task.goal_id)
        except KeyError:
            pass
        else:
            if goal.status not in (GoalStatus.DONE, GoalStatus.FAILED, GoalStatus.CANCELLED):
                GoalManager(agent.mind).cancel(goal, "stopped by the human")
                agent.touch()
                await self.environment.repository.save_agent(agent)
        task.status = "cancelled"
        task.result = "stopped by the human"
        await self._save()

    async def stop_everything(self, manager_id: str) -> str:
        """Every repeat and every running handed-out task of this Manager."""
        repeats = self.stop_repeats(manager_id)
        open_tasks = [
            task
            for task in self.tasks_for(manager_id)
            if task.status in ("running", "stalled")
        ]
        for task in open_tasks:
            await self.stop_task(task)
        if not repeats and not open_tasks:
            return ""
        parts = []
        if repeats:
            parts.append(f"{repeats} repeating")
        if open_tasks:
            names = ", ".join(sorted({task.agent_name for task in open_tasks}))
            parts.append(f"{len(open_tasks)} task(s) on {names}")
        return "⏹ Stopped: " + "; ".join(parts) + "."

    async def stop_on_word(self, manager_id: str, text: str) -> str | None:
        """Called before a human's message reaches a Manager: a stop word,
        with anything running, stops it all at once -- a model would take
        seconds to read it, and a repeat would not wait for that."""
        said = " ".join(text.lower().split())
        if one := STOP_ONE.match(said):
            return await self.stop_listed(manager_id, int(one.group(1)))
        if said in STOP_WORDS:
            return await self.stop_everything(manager_id) or None
        if named := STOP_NAMED.match(said):
            return await self._stop_named(manager_id, named.group(1))
        return None

    async def _stop_named(self, manager_id: str, words: str) -> str | None:
        """ "спри новините": the one running thing those words name. Several:
        the list, to pick by number. None: no match, so a model decides --
        found live, "stop sending me the news" went to the model, which could
        not see its repeats and stopped nothing."""
        wanted = _stems(words)
        if not wanted:
            return None
        lines = self.listing(manager_id)
        entries = self._listed.get(manager_id, [])
        texts: list[str] = []
        for kind, key in entries:
            if kind == "repeat":
                item = next(
                    (r for r in self.repeats.get(manager_id, []) if str(r.number) == key), None
                )
                texts.append(item.description if item else "")
            else:
                task = next((t for t in self.tasks if t.id == key), None)
                texts.append(f"{task.agent_name} {task.request}" if task else "")
        hits = [index for index, text in enumerate(texts, 1) if wanted & _stems(text)]
        if len(hits) == 1:
            return await self.stop_listed(manager_id, hits[0])
        if len(hits) > 1:
            numbered = [line for line in lines if line[:1].isdigit()]
            return "Which one? Say \"stop <number>\":\n" + "\n".join(numbered)
        return None

    # -- commands --------------------------------------------------------

    async def run_command(self, manager: AgentDefinition, command: str) -> str:
        command = " ".join(command.split())
        kind = classify(command)
        if kind == "forbidden":
            raise ToolDenied(
                f"DENIED: {command.split(' ', 1)[0] or 'that'} cannot be run from here, "
                "approved or not. Tell the human to run it themselves."
            )
        human_asked = self.human_turn.get(manager.id, False)
        if human_asked and (kind == "work" or (kind == "approve" and self.trusted)):
            return await self._execute(command)
        if kind in ("approve", "work"):
            number = self._next
            self._next += 1
            self.pending[number] = PendingCommand(number, manager.id, command, "changes the mesh")
            await self.environment.announce_agent(
                manager.id,
                f"🛑 {manager.name} wants to run: {command}\n"
                f"Reply \"yes\" or \"no\" (or /manager approve|deny {number})",
            )
            return (
                f"confirmation_required: this command changes the mesh, so the human must "
                f"approve it: they reply \"yes\" (or /manager approve {number}). They have been "
                "asked. Tell them what you asked to run and why, then stop -- do not try "
                "another way."
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

    async def answer_pending(self, manager_id: str, text: str) -> str | None:
        """A human's bare "yes"/"no" with commands waiting: approve or deny
        them all, before any model reads it. None when it is neither."""
        word = " ".join(text.lower().split()).strip(".!")
        waiting = sorted(
            (item for item in self.pending.values()
             if item.manager_id == manager_id and not item.expired()),
            key=lambda item: item.number,
        )
        if not waiting or word not in YES_WORDS | NO_WORDS:
            return None
        if word in NO_WORDS:
            return "\n".join(self.deny(item.number) for item in waiting)
        return "\n\n".join([await self.approve(item.number) for item in waiting])

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


def coordinates(definition: AgentDefinition) -> bool:
    """Whether an agent has the coordination tools (AgentDefinition.coordination)."""
    if definition.type == "system":
        return False
    if definition.coordination is not None:
        return definition.coordination
    return definition.manager


def build_manager_tools(desk: ManagerDesk, definition: AgentDefinition) -> tuple[Tool, ...]:
    """The coordination tools for an agent that has them, plus mesh_command for
    a manager; nothing for anyone else."""
    if not coordinates(definition) and not definition.manager:
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
        # One numbered list -- the numbers the human says "stop 2" to.
        return "\n".join(desk.listing(definition.id))

    async def command(context: Any, args: dict[str, Any]) -> str:
        del context
        return await desk.run_command(definition, str(args.get("command") or ""))

    def _number(args: dict[str, Any], key: str, default: float) -> float:
        value = args.get(key)
        try:
            return float(value) if value is not None and value != "" else default
        except (TypeError, ValueError) as exc:
            raise ToolDenied(f"DENIED: {key} must be a number") from exc

    async def repeat(context: Any, args: dict[str, Any]) -> str:
        del context
        started = desk.start_repeat(
            definition.id,
            _number(args, "every_seconds", 0),
            message=str(args.get("message") or ""),
            agent=str(args.get("agent") or ""),
            question=str(args.get("question") or ""),
            start=int(_number(args, "start", 1)),
            step=int(_number(args, "step", 1)),
            runs=int(_number(args, "max_runs", REPEAT_DEFAULT_RUNS)),
        )
        return (
            f"repeat #{started.number} running every {started.every_seconds:g}s. It ends "
            "when the human says stop, or with stop(repeat)."
        )

    async def stop(context: Any, args: dict[str, Any]) -> str:
        del context
        what = str(args.get("what") or "all").strip().lower()
        if what == "all":
            return await desk.stop_everything(definition.id) or "nothing was running"
        if what == "number":
            return await desk.stop_listed(definition.id, int(_number(args, "number", 0)))
        if what.startswith("repeat"):
            number = int(_number(args, "number", 0))
            return f"stopped {desk.stop_repeats(definition.id, number)} repeat(s)"
        task_id = str(args.get("task") or "")
        for task in desk.tasks_for(definition.id):
            if task.id == task_id and task.status in ("running", "stalled"):
                await desk.stop_task(task)
                return f"stopped task {task.id} on {task.agent_name}"
        raise ToolDenied(f"DENIED: no running task {task_id!r}; task_status lists them.")

    sent: list[float] = []

    async def message(context: Any, args: dict[str, Any]) -> str:
        del context
        text = str(args.get("text") or "").strip()
        if not text:
            raise ToolDenied("DENIED: message_human needs the text.")
        now = asyncio.get_running_loop().time()
        sent[:] = [at for at in sent if now - at < 60]
        if len(sent) >= MESSAGES_PER_MINUTE:
            raise ToolDenied(
                f"DENIED: {MESSAGES_PER_MINUTE} messages a minute is the limit; put the rest "
                "in your answer."
            )
        sent.append(now)
        await desk.environment.tell_human(definition.id, text[:MESSAGE_CHARS])
        return "sent"

    coordination = (
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
            description="Everything you run for the human, numbered (repeats and handed-out "
            "tasks), and what finished lately. Show it as is when they ask for their tasks: "
            'they stop one by its number ("stop 2").',
            parameters={"type": "object", "properties": {}},
            run=status,
        ),
        Tool(
            name="message_human",
            description="Send the human a separate message right now, before your answer: "
            "when they ask for things one message at a time, or progress on long work. "
            "Your final answer still goes out last, so do not repeat what you sent.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            run=message,
        ),
        Tool(
            name="repeat",
            description="Do something again on a clock until the human says stop: send a "
            'message ("{n}" counts) or ask an agent a question and pass its answer on '
            '(no agent: you answer it yourself). E.g. every_seconds 1, message "{n}"; or '
            'every_seconds 600, agent NewsWatcher, question "latest headlines".',
            parameters={
                "type": "object",
                "properties": {
                    "every_seconds": {"type": "number", "description": "at least 1"},
                    "message": {"type": "string"},
                    "agent": {"type": "string"},
                    "question": {"type": "string"},
                    "start": {"type": "integer"},
                    "step": {"type": "integer"},
                    "max_runs": {"type": "integer", "description": "safety limit"},
                },
                "required": ["every_seconds"],
            },
            run=repeat,
        ),
        Tool(
            name="stop",
            description='Stop what you run for the human. what: "number" with the number '
            'from task_status\'s list ("stop 2"), "all", or "repeat"/"task" by id.',
            parameters={
                "type": "object",
                "properties": {
                    "what": {"type": "string", "enum": ["number", "all", "repeat", "task"]},
                    "number": {"type": "integer"},
                    "task": {"type": "string"},
                },
            },
            run=stop,
        ),
    )
    commands = Tool(
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
    )
    # The console's commands are the manager's alone; the rest is anyone's
    # who was given coordination.
    return (
        (coordination if coordinates(definition) else ())
        + ((commands,) if definition.manager else ())
    )

