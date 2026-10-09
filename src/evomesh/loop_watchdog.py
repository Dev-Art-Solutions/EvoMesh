"""Name whatever blocks the event loop, while it is blocking it.

Everything in the mesh -- every agent, the control port, Telegram -- shares
one asyncio loop. A synchronous call that runs for seconds on it stalls all
of them at once, and from outside that looks exactly like a hang: the
supervisor's ``/ping`` watchdog logged 32 unanswered pings in one night
(2026-09-29) with nothing in mesh.log to say why.

A heartbeat callback on the loop stamps the time every second; a daemon
thread checks the stamp. When the loop has not run for ``threshold``
seconds, the thread logs the loop thread's *current* stack -- the code that
is blocking it, caught in the act -- once per stall, and how long the stall
lasted when the loop comes back. A thread, not a task, for the same reason
the shutdown watchdog in __main__ is one: it has to run while the loop
cannot.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 1.0
# Where a stall is worth a stack. Well under the supervisor's 20 s /ping
# read timeout, well over an ordinary busy tick.
DEFAULT_THRESHOLD_SECONDS = 5.0
STACK_FRAMES = 14


class LoopWatchdog:
    def __init__(self, threshold: float = DEFAULT_THRESHOLD_SECONDS) -> None:
        self.threshold = threshold
        self.stalls = 0
        self.longest = 0.0
        self._beat = time.monotonic()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._handle: asyncio.TimerHandle | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Call from the loop's own thread, inside a running loop."""
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._beat = time.monotonic()
        self._schedule()
        self._thread = threading.Thread(target=self._watch, name="loop-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _schedule(self) -> None:
        if self._loop is None or self._stop.is_set():
            return
        self._beat = time.monotonic()
        self._handle = self._loop.call_later(HEARTBEAT_SECONDS, self._schedule)

    def stack(self) -> str:
        frame = sys._current_frames().get(self._loop_thread or -1)  # noqa: SLF001
        if frame is None:
            return "(loop thread has no frame)"
        return "".join(traceback.format_stack(frame)[-STACK_FRAMES:]).rstrip()

    def _watch(self) -> None:
        reported_since: float | None = None
        reported_stack = ""
        while not self._stop.wait(HEARTBEAT_SECONDS / 2):
            lag = time.monotonic() - self._beat - HEARTBEAT_SECONDS
            if lag >= self.threshold and reported_since is None:
                reported_since = self._beat
                # Kept for the recovery line: by then the loop runs something
                # else, and "blocked for 41.3s" alone means a search back
                # through the log for the onset line to learn what did it.
                reported_stack = self.stack()
                self.stalls += 1
                logger.warning(
                    "event loop blocked for %.1fs so far; it is running:\n%s",
                    lag,
                    reported_stack,
                )
            elif lag < self.threshold and reported_since is not None:
                total = max(0.0, self._beat - reported_since - HEARTBEAT_SECONDS)
                self.longest = max(self.longest, total)
                logger.warning(
                    "event loop was blocked for %.1fs; it was running:\n%s",
                    total,
                    reported_stack,
                )
                reported_since = None
