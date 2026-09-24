from __future__ import annotations

import asyncio
from pathlib import Path

from evomesh.config import Settings
from evomesh.console import ConsoleChannel
from evomesh.control import wait_for_console_or_shutdown
from evomesh.environment import Environment
from evomesh.models import MockProvider


async def test_shutdown_when_console_task_wins_sets_shutdown_event() -> None:
    """When the console task finishes first, the shutdown event is signalled."""
    settings = Settings(data_path=Path(":memory:") / "data.db")
    environment = Environment(settings, {"mock": MockProvider()})
    console = ConsoleChannel(environment)
    console.running = False  # run() will exit immediately on its first input

    shutdown = asyncio.Event()

    await wait_for_console_or_shutdown(console, shutdown)

    assert shutdown.is_set()
