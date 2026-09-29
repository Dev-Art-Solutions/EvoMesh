import asyncio
import logging
import time

import pytest

from evomesh.loop_watchdog import LoopWatchdog


def _block_the_loop(seconds: float) -> None:
    time.sleep(seconds)  # the whole point: a synchronous call on the loop


async def test_a_blocked_loop_is_reported_with_the_blocking_stack(
    caplog: pytest.LogCaptureFixture,
) -> None:
    watchdog = LoopWatchdog(threshold=0.6)
    caplog.set_level(logging.WARNING, logger="evomesh.loop_watchdog")
    watchdog.start()
    try:
        await asyncio.sleep(1.2)
        _block_the_loop(2.5)
        await asyncio.sleep(1.5)
    finally:
        watchdog.stop()
    messages = [record.getMessage() for record in caplog.records]
    caught = [message for message in messages if "blocked for" in message and "so far" in message]
    assert len(caught) == 1
    assert "_block_the_loop" in caught[0]
    assert any("was blocked for" in message for message in messages)
    assert watchdog.stalls == 1
    assert watchdog.longest > 0.5


async def test_a_healthy_loop_says_nothing(caplog: pytest.LogCaptureFixture) -> None:
    watchdog = LoopWatchdog(threshold=0.6)
    caplog.set_level(logging.WARNING, logger="evomesh.loop_watchdog")
    watchdog.start()
    try:
        for _ in range(10):
            await asyncio.sleep(0.2)
    finally:
        watchdog.stop()
    assert watchdog.stalls == 0
    assert not caplog.records
