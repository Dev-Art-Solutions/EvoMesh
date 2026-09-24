"""Work the harness does for an agent, so the agent does not stop to do it.

A tool loop takes minutes. A cycle has to stay a tick, or rule 7 -- one stage
per cycle -- becomes a sentence nobody can keep. So an agent submits a job and
carries on: it keeps cycling, keeps answering, and simply commits to nothing new
while a job of its own is open.

The worker holds no policy. The submitter names the root, whether the job may
write, and on whose behalf it runs; the worker takes jobs and runs them. Anything
the worker could decide for itself is something two callers would later disagree
about.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from evomesh.harness import HarnessResult

logger = logging.getLogger(__name__)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"


@dataclass
class HarnessJob:
    number: int
    objective: str
    root: Path
    # Empty means the human at the console. An agent id makes the job run under
    # that agent's filesystem grants and sends the result to its mailbox.
    agent_id: str = ""
    allow_write: bool = False
    # Narrower than the job root -- see ToolContext.write_prefix in
    # harness_tools.py for why this exists and what it refuses.
    write_prefix: str | None = None
    # None means "use harness.max_steps/max_seconds from config". Set by a
    # caller that knows this job is a small, pre-scoped write (a plan stage,
    # a decomposed leaf) and wants it to fail fast rather than wander for as
    # long as an open-ended mutation is allowed to.
    max_steps: int | None = None
    max_seconds: float | None = None
    # What to call this job in a status line. Optional: an objective that is one
    # sentence needs no label, and one that is a page needs one.
    label: str = ""
    # A human waiting on an actual reply -- a reactive chat message, not an
    # agent's own background plan step or the Evolver's pipeline -- goes to
    # the front of whatever is still *queued* the moment it arrives (see
    # HarnessQueue's own priority ordering below). It cannot preempt a job
    # already running: the single worker this project's target hardware
    # usually has finishes what it started, this only decides what it picks
    # up next.
    priority: bool = False
    status: JobStatus = JobStatus.QUEUED
    steps: int = 0
    # Whether a finished job should land in its agent's inbox as a message.
    # True for a job nobody else is watching for (the console, a genuine
    # human question that outran its synchronous wait). False for a job a
    # behavior already consumes itself by polling `harness.job(job.number)`
    # every cycle (a plan step, an evolution pipeline stage) -- delivering
    # those too turned the agent's own routine work into a fresh inbound
    # "message" every cycle, which respond() then tried to answer as if a
    # human had asked it something, which produced another delivered
    # message, forever: an agent (NewsAnalyzer, live) stuck answering its
    # own last answer in an ever-growing loop that never touched news again.
    notify: bool = True
    result: HarnessResult | None = None
    detail: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    @property
    def open(self) -> bool:
        return self.status in (JobStatus.QUEUED, JobStatus.RUNNING)

    @property
    def title(self) -> str:
        """One line, because the objective is no longer one line.

        Since the Evolver started asking through the harness, an objective
        carries the project map and the standing rules -- a page of text. A
        status line that prints it is a status line nobody can read.

        The submitter may say what the job is; failing that, the ``OBJECTIVE:``
        line is looked for, and only then the first line -- which for a repair
        job is the map's own header, which is how this was noticed.
        """
        chosen = self.label
        if not chosen:
            for line in self.objective.splitlines():
                if line.startswith("OBJECTIVE: "):
                    chosen = line[len("OBJECTIVE: ") :].strip()
                    break
        if not chosen:
            chosen = next(
                (line.strip() for line in self.objective.splitlines() if line.strip()), ""
            )
        return chosen[:100] + ("..." if len(chosen) > 100 else "")

    def describe(self) -> str:
        who = self.agent_id or "console"
        if self.status is JobStatus.RUNNING:
            return f"job {self.number} [{who}] running: {self.title}"
        if self.status is JobStatus.DONE and self.result is not None:
            return (
                f"job {self.number} [{who}] {self.result.outcome} -- {self.result.summary()}"
            )
        if self.status is JobStatus.CANCELLED:
            return f"job {self.number} [{who}] cancelled: {self.detail}"
        flag = " (priority)" if self.priority else ""
        return f"job {self.number} [{who}] queued{flag}: {self.title}"


class QueueFull(RuntimeError):
    pass


# What the environment hands the queue: run this job, give me its result. Kept
# as a callable so the queue never learns about providers, settings or agents.
Runner = Callable[[HarnessJob], Awaitable[HarnessResult]]
Delivery = Callable[[HarnessJob], Awaitable[None]]


class HarnessQueue:
    """FIFO of jobs, and at most one open job per agent.

    The per-agent limit is not tidiness. A behavior that submits once per cycle
    would otherwise fill the queue with the same objective while the first copy
    is still running, and every copy would edit the same files.
    """

    def __init__(self, max_queue: int = 8, retain_finished: int = 200) -> None:
        self.max_queue = max_queue
        self.retain_finished = retain_finished
        self.jobs: dict[int, HarnessJob] = {}
        # Two separate lines, not one queue read in priority order: the old
        # single PriorityQueue only changed *order*, so a human's reactive
        # question (priority=True, see bdi.py's respond()) still had to wait
        # out whatever background job (an Evolver stage, another agent's own
        # plan step) a single worker had already started -- up to
        # harness.max_seconds, minutes on a slow model. A worker reading only
        # `_priority_waiting` never touches that backlog, so a dedicated one
        # (harness.priority_workers) is free the instant a human asks
        # something, no matter how long the background lane is running.
        self._priority_waiting: asyncio.Queue[int] = asyncio.Queue()
        self._background_waiting: asyncio.Queue[int] = asyncio.Queue()
        # `jobs` is process memory, not a database -- nothing here ever stops
        # running on its own, so an unpruned dict has no ceiling (the same
        # class of bug this project has already fixed for generation
        # worktrees, filesystem grants, and mesh.log). A queue this small
        # loses no functionality: every real caller either polls the job it
        # just submitted until it finishes and consumes the result in that
        # same stretch (a plan step, an evolution pipeline stage, a reactive
        # question's synchronous wait), or asks `recent()` for a short,
        # bounded status listing -- nothing holds a *finished* job's number
        # across an arbitrarily long stretch of the mesh's own uptime. An
        # *open* job is never pruned regardless of age or count.
        self._next = 1

    def _prune_finished(self) -> None:
        finished = sorted(
            (job for job in self.jobs.values() if not job.open),
            key=lambda job: job.number,
            reverse=True,
        )
        for job in finished[self.retain_finished :]:
            del self.jobs[job.number]

    def open_job_for(self, agent_id: str) -> HarnessJob | None:
        if not agent_id:
            return None
        return next(
            (job for job in self.jobs.values() if job.agent_id == agent_id and job.open),
            None,
        )

    def submit(
        self,
        objective: str,
        root: Path,
        *,
        agent_id: str = "",
        allow_write: bool = False,
        write_prefix: str | None = None,
        label: str = "",
        max_steps: int | None = None,
        max_seconds: float | None = None,
        notify: bool = True,
        priority: bool = False,
    ) -> HarnessJob:
        existing = self.open_job_for(agent_id)
        if existing is not None:
            # Not an error: the caller asked for work it already has running,
            # and the honest answer is the handle it was given the first time.
            return existing
        queued = sum(1 for job in self.jobs.values() if job.status is JobStatus.QUEUED)
        if queued >= self.max_queue:
            raise QueueFull(f"the harness queue already holds {queued} waiting jobs")
        job = HarnessJob(
            number=self._next,
            objective=objective,
            root=root,
            agent_id=agent_id,
            allow_write=allow_write,
            write_prefix=write_prefix,
            label=label,
            max_steps=max_steps,
            max_seconds=max_seconds,
            notify=notify,
            priority=priority,
        )
        self._next += 1
        self.jobs[job.number] = job
        (self._priority_waiting if priority else self._background_waiting).put_nowait(job.number)
        self._prune_finished()
        return job

    async def take(self, *, lane: str = "any") -> HarnessJob:
        """Pull the next queued job.

        ``lane="priority"`` and ``lane="background"`` read only their own
        line -- a dedicated priority worker never sees, and is never
        delayed by, whatever the background lane is doing. ``"any"`` (the
        default, and the only choice with a single worker) drains priority
        first but falls through to background rather than sit idle.
        """
        if lane == "priority":
            return await self._take_from(self._priority_waiting)
        if lane == "background":
            return await self._take_from(self._background_waiting)
        if not self._priority_waiting.empty():
            job = await self._take_from(self._priority_waiting)
            if job is not None:
                return job
        priority_get = asyncio.ensure_future(self._priority_waiting.get())
        background_get = asyncio.ensure_future(self._background_waiting.get())
        try:
            done, pending = await asyncio.wait(
                {priority_get, background_get}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if priority_get in done:
                number = priority_get.result()
                if background_get in done:
                    # Both ready at once -- priority wins, but the
                    # background number drawn in the same instant must go
                    # back, or that job is silently dropped from the queue.
                    self._background_waiting.put_nowait(background_get.result())
            else:
                number = background_get.result()
        finally:
            for task in (priority_get, background_get):
                if not task.done():
                    task.cancel()
        job = self.jobs.get(number)
        if job is None or job.status is not JobStatus.QUEUED:
            return await self.take(lane=lane)
        job.status = JobStatus.RUNNING
        return job

    async def _take_from(self, waiting: asyncio.Queue[int]) -> HarnessJob:
        while True:
            number = await waiting.get()
            job = self.jobs.get(number)
            if job is not None and job.status is JobStatus.QUEUED:
                job.status = JobStatus.RUNNING
                return job

    def finish(self, job: HarnessJob, result: HarnessResult) -> None:
        job.result = result
        job.steps = result.steps
        job.status = JobStatus.DONE
        job.finished_at = datetime.now(UTC)

    def cancel(self, job: HarnessJob, detail: str) -> None:
        job.status = JobStatus.CANCELLED
        job.detail = detail
        job.finished_at = datetime.now(UTC)

    def open_jobs(self) -> list[HarnessJob]:
        return [job for job in self.jobs.values() if job.open]

    def recent(self, limit: int = 5) -> list[HarnessJob]:
        return sorted(self.jobs.values(), key=lambda job: job.number, reverse=True)[:limit]


class HarnessGateway:
    """What a behavior is allowed to do with the harness: ask, and look.

    A behavior never reaches the worker, the provider or the session writer --
    it submits an objective and reads what came back. Keeping the surface this
    narrow is what stops the queue becoming a second way into the filesystem.
    """

    def __init__(self, queue: HarnessQueue, sessions: dict[int, list[dict[str, Any]]]) -> None:
        self.queue = queue
        self.sessions = sessions

    def submit(
        self,
        objective: str,
        *,
        agent_id: str,
        root: Path,
        label: str = "",
        write_prefix: str | None = None,
        max_steps: int | None = None,
        max_seconds: float | None = None,
        notify: bool = True,
        priority: bool = False,
        allow_write: bool = True,
    ) -> HarnessJob:
        return self.queue.submit(
            objective,
            root,
            agent_id=agent_id,
            allow_write=allow_write,
            write_prefix=write_prefix,
            priority=priority,
            label=label,
            max_steps=max_steps,
            max_seconds=max_seconds,
            notify=notify,
        )

    def job(self, number: int) -> HarnessJob | None:
        return self.queue.jobs.get(number)

    def open_job_for(self, agent_id: str) -> HarnessJob | None:
        """The job this agent already has in flight, if any.

        A behavior that re-reaches its own step every cycle must find the job it
        submitted rather than queue another copy of it.
        """
        return self.queue.open_job_for(agent_id)

    def changes(self, job: HarnessJob) -> list[dict[str, Any]]:
        """Every edit and write the job actually applied, with its diff."""
        return [
            entry
            for entry in self.sessions.get(job.number, [])
            if entry.get("kind") in ("edit", "write")
        ]


class HarnessWorker:
    """One tool loop at a time, taking whatever its lane hands it.

    ``lane="background"`` is what the original single worker always was:
    the Evolver's pipeline, an agent's own plan step, anything with no
    human waiting on it. ``lane="priority"`` only ever sees a reactive
    question (bdi.py's respond(), priority=True) -- it is a separate
    worker precisely so that lane is never behind a background job that
    is already minutes into harness.max_seconds.
    """

    def __init__(
        self, queue: HarnessQueue, run: Runner, deliver: Delivery, *, lane: str = "any"
    ) -> None:
        self.queue = queue
        self.run = run
        self.deliver = deliver
        self.lane = lane
        self.task: asyncio.Task[None] | None = None

    def start(self, name: str) -> None:
        self.task = asyncio.create_task(self._loop(), name=name)

    async def _loop(self) -> None:
        while True:
            job = await self.queue.take(lane=self.lane)
            try:
                result = await self.run(job)
                self.queue.finish(job, result)
            except asyncio.CancelledError:
                # A mesh stopping mid-job reports it rather than leaving the
                # submitter waiting on a result that is never coming.
                self.queue.cancel(job, "the mesh stopped while this job was running")
                await self._deliver(job)
                raise
            except Exception as exc:  # noqa: BLE001 - one bad job never kills the worker
                logger.exception("harness job %s failed", job.number)
                self.queue.cancel(job, f"{type(exc).__name__}: {exc}")
            await self._deliver(job)

    async def _deliver(self, job: HarnessJob) -> None:
        try:
            await self.deliver(job)
        except Exception:  # noqa: BLE001 - a broken mailbox never kills the worker
            logger.exception("could not deliver harness job %s", job.number)

    async def stop(self) -> None:
        if self.task is None:
            return
        self.task.cancel()
        try:
            await self.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self.task = None
