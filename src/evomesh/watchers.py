"""A deterministic watcher: a command run on its own short, fixed interval,
never on an agent's cognition cycle.

An agent's cycle_seconds is what a model reasons on -- 60s or more, because
every tick costs a whole model turn. Open orders and account equity move on a
scale of seconds; watching them on the model's own clock would mean an LLM
call every few seconds just to notice nothing changed. A watcher never touches
a model: it runs ``command`` on ``interval_seconds``, and only a non-empty
line of stdout becomes an announcement -- silence means nothing crossed a
threshold. What decides that is the command itself (a script bundled with a
tool or an agent template), not this loop.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path

from evomesh.processes import run_command

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 20.0


class AgentWatcher:
    def __init__(
        self,
        command: str,
        *,
        interval_seconds: float,
        notify: Callable[[str], Awaitable[None]],
        cwd: Path | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._argv = shlex.split(command, posix=True)
        self.interval_seconds = max(1.0, interval_seconds)
        self.notify = notify
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._argv and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._tick(), timeout=self.timeout_seconds)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                logger.warning("Watcher command timed out: %s", self._argv)
            except Exception:  # noqa: BLE001 - one bad tick must not end the watcher
                logger.exception("Watcher command failed: %s", self._argv)
            await asyncio.sleep(self.interval_seconds)

    async def _tick(self) -> None:
        result = await run_command(self._argv[0], *self._argv[1:], cwd=self.cwd)
        message = result.output.strip()
        if result.exit_code != 0:
            logger.warning("Watcher command exited %s: %s", result.exit_code, message[:500])
            return
        if message:
            await self.notify(message)
