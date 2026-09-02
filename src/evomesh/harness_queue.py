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
    # What to call this job in a status line. Optional: an objective that is one
    # sentence needs no label, and one that is a page needs one.
    label: str = ""
    status: JobStatus = JobStatus.QUEUED
    steps: int = 0
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
        return f"job {self.number} [{who}] queued: {self.title}"


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

    def __init__(self, max_queue: int = 8) -> None:
        self.max_queue = max_queue
        self.jobs: dict[int, HarnessJob] = {}
        self._waiting: asyncio.Queue[int] = asyncio.Queue()
        self._next = 1

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
        label: str = "",
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
            label=label,
        )
        self._next += 1
        self.jobs[job.number] = job
        self._waiting.put_nowait(job.number)
        return job

    async def take(self) -> HarnessJob:
        while True:
            number = await self._waiting.get()
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
        self, objective: str, *, agent_id: str, root: Path, label: str = ""
    ) -> HarnessJob:
        return self.queue.submit(
            objective, root, agent_id=agent_id, allow_write=True, label=label
        )

    def job(self, number: int) -> HarnessJob | None:
        return self.queue.jobs.get(number)

    def changes(self, job: HarnessJob) -> list[dict[str, Any]]:
        """Every edit and write the job actually applied, with its diff."""
        return [
            entry
            for entry in self.sessions.get(job.number, [])
            if entry.get("kind") in ("edit", "write")
        ]


class HarnessWorker:
    """One tool loop at a time, taking whatever the queue hands it."""

    def __init__(self, queue: HarnessQueue, run: Runner, deliver: Delivery) -> None:
        self.queue = queue
        self.run = run
        self.deliver = deliver
        self.task: asyncio.Task[None] | None = None

    def start(self, name: str) -> None:
        self.task = asyncio.create_task(self._loop(), name=name)

    async def _loop(self) -> None:
        while True:
            job = await self.queue.take()
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
