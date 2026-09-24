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
import shutil
import time

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


async def test_a_timeout_kills_the_whole_process_group() -> None:
    """Grandchildren survive their parent dying -- the group must be killed.

    ``subprocess.run`` only kills the direct child on timeout. If that child
    spawned a grandchild (here a shell backgrounding a sleeper), the
    grandchild would otherwise be orphaned and keep going. ``start_new_session``
    made the child its own group leader, so a timeout must reach the whole
    group via ``os.killpg``.
    """
    if shutil.which("sh") is None:  # pragma: no cover - platform guard
        return

    result = await run_command(
        "sh",
        "-c",
        "( sleep 30 ) & echo $! > /tmp/evomesh_pg_child.pid",
        timeout_seconds=1.0,
    )

    assert result.timed_out is True

    def _read_pid() -> int:
        with open("/tmp/evomesh_pg_child.pid", encoding="utf-8") as handle:
            return int(handle.read().strip())

    child_pid = await asyncio.to_thread(_read_pid)

    assert not _pid_is_running(child_pid)


def _pid_is_running(pid: int) -> bool:
    """True while a process with ``pid`` exists (best effort, no exceptions)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def test_without_virtual_env_returns_the_environment_without_VIRTUAL_ENV() -> None:
    result = without_virtual_env()

    assert "VIRTUAL_ENV" not in result
    assert result == {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
