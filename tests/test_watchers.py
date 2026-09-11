"""A watcher runs its command on its own clock and only speaks up when the
command has something to say -- never on the agent's own LLM cycle."""

from __future__ import annotations

import asyncio
import shlex
import sys

from evomesh.watchers import AgentWatcher

PYTHON = shlex.quote(sys.executable)


async def test_a_watcher_announces_only_nonempty_output() -> None:
    seen: list[str] = []
    announced = asyncio.Event()

    async def notify(text: str) -> None:
        seen.append(text)
        announced.set()

    script = "import sys; print('alert: equity below floor')"
    watcher = AgentWatcher(
        f"{PYTHON} -c \"{script}\"", interval_seconds=1, notify=notify
    )
    watcher.start()
    try:
        async with asyncio.timeout(2.0):
            await announced.wait()
    finally:
        await watcher.stop()

    assert seen[0] == "alert: equity below floor"


async def test_a_watcher_stays_silent_on_empty_output() -> None:
    seen: list[str] = []

    async def notify(text: str) -> None:
        seen.append(text)

    watcher = AgentWatcher(
        f'{PYTHON} -c "pass"', interval_seconds=1, notify=notify
    )
    watcher.start()
    try:
        await asyncio.sleep(0.3)
    finally:
        await watcher.stop()

    assert seen == []


async def test_stop_cancels_the_loop() -> None:
    async def notify(_: str) -> None:
        return None

    watcher = AgentWatcher(f'{PYTHON} -c "pass"', interval_seconds=1, notify=notify)
    watcher.start()
    assert watcher.running
    await watcher.stop()
    assert not watcher.running
