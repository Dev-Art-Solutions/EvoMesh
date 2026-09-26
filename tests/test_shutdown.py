"""Stopping the mesh is bounded (found live 2026-09-26: a /restart sat for
fourteen minutes, nothing logged after "Restarting", every agent still at
work). A hanging step is named and left behind, children are killed, and a
watchdog makes sure the process exits with the code the launcher expects."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from evomesh import __main__ as entry
from evomesh import environment as environment_module
from evomesh.environment import Environment, shutdown_step
from evomesh.models import MockProvider
from evomesh.processes import (  # pyright: ignore[reportPrivateUsage]
    _RUNNING,
    kill_running,
    run_command,
)
from tests.test_bdi import settings_for


async def test_a_hanging_step_is_named_and_left_behind(caplog: pytest.LogCaptureFixture) -> None:
    started = time.monotonic()

    await shutdown_step("waiting forever", asyncio.sleep(3600), seconds=0.1)

    assert time.monotonic() - started < 5
    assert "waiting forever took longer" in caplog.text


async def test_the_environment_stops_even_if_one_agent_will_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start(start_agent_loops=True)
    runtime = environment.runtimes["guardian"]

    async def never(*, persist_status: bool = True) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(runtime, "stop", never)
    monkeypatch.setattr(environment_module, "SHUTDOWN_STEP_SECONDS", 0.2)
    started = time.monotonic()

    await environment.stop()

    assert time.monotonic() - started < 10
    assert environment.runtimes == {}
    assert environment.health_state.value == "STOPPED"


async def test_a_child_still_running_is_killed_at_shutdown() -> None:
    run = asyncio.create_task(
        run_command(sys.executable, "-c", "import time; time.sleep(60)", timeout_seconds=120)
    )
    for _ in range(100):
        if _RUNNING:
            break
        await asyncio.sleep(0.05)
    assert _RUNNING, "the child is registered while it runs"
    started = time.monotonic()

    assert kill_running() >= 1
    result = await asyncio.wait_for(run, 30)

    assert time.monotonic() - started < 30, "the waiting thread returned at once"
    assert result.exit_code != 0
    assert not _RUNNING


async def test_the_watchdog_exits_with_the_restart_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    environment.restart_requested.set()
    exited: list[int] = []
    monkeypatch.setattr(entry, "SHUTDOWN_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(entry.os, "_exit", exited.append)

    entry._start_shutdown_watchdog(environment)  # pyright: ignore[reportPrivateUsage]
    for _ in range(100):
        if exited:
            break
        await asyncio.sleep(0.05)

    assert exited == [entry.RESTART_EXIT_CODE]
