"""``run_command``'s own timeout really kills the child.

The bug this guards against: a caller used to wrap ``run_command`` in
``asyncio.wait_for``, which only stops *waiting* on the worker thread -- the
blocking ``subprocess.run`` call still running inside it, and the process it
started, kept going for real. Found live: a python process from a hung
harness job was still alive more than two days after its supposed 60s
budget. ``subprocess.run(timeout=...)`` kills the child itself, on whichever
thread is actually running it, so there is nothing left to leak.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from evomesh.processes import run_command, without_virtual_env


async def test_a_timeout_reports_itself_and_does_not_hang() -> None:
    started = time.monotonic()

    result = await run_command(
        "python", "-c", "import time; time.sleep(30)", timeout_seconds=1.0
    )

    elapsed = time.monotonic() - started
    assert result.timed_out is True
    assert elapsed < 10  # nowhere near the child's real 30s sleep


async def test_a_command_that_finishes_in_time_is_not_marked_as_timed_out() -> None:
    result = await run_command("python", "-c", "print('hi')", timeout_seconds=10.0)

    assert result.timed_out is False
    assert result.exit_code == 0
    assert "hi" in result.output


async def test_a_timeout_kills_the_grandchildren_too(tmp_path: Path) -> None:
    """A timeout must reach everything the child started, on every platform.

    The grandchild writes a marker after 5s; the child is timed out after 2s.
    If the marker appears, the grandchild outlived the timeout. No pid probing:
    on Windows ``os.kill(pid, 0)`` *terminates* the process it was asked about.
    """
    marker = tmp_path / "grandchild-survived"
    grandchild = "import sys, time; time.sleep(5); open(sys.argv[1], 'w').write('x')"
    child = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}, sys.argv[1]]); "
        "time.sleep(30)"
    )

    result = await run_command(
        sys.executable, "-c", child, str(marker), timeout_seconds=2.0
    )

    assert result.timed_out is True
    await asyncio.sleep(5)
    assert not marker.exists()


def test_without_virtual_env_returns_the_environment_without_VIRTUAL_ENV() -> None:
    result = without_virtual_env()

    assert "VIRTUAL_ENV" not in result
    assert result == {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
