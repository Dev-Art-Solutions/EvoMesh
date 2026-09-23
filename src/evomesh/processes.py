"""Run an external command without leaving an asyncio transport behind.

Every subprocess in this project used ``asyncio.create_subprocess_exec``, which
is the obvious choice and is wrong on Windows for the one thing that matters
here. The proactor loop's subprocess transport is finalised by the garbage
collector, which happens *after* the loop that owns it has closed -- so its
``__del__`` raises ``ValueError: I/O operation on closed pipe`` into the
unraisable hook, and pytest attributes that to whichever test happens to be
running at the time.

That is not cosmetic in this codebase. Candidate validation *is* the test suite,
so roughly one candidate in three failed for a reason it had not caused, the
pipeline read that as a verdict, and the Evolver spent its repair budget trying
to fix a warning about a pipe. Rule 9 draws a line between a candidate that
failed and a run the host broke; this was the host breaking runs while wearing
the candidate's name.

A blocking ``subprocess.run`` on a worker thread has no transport to finalise.
The commands here are short-lived or genuinely long-running and blocking -- git
plumbing, ``uv run pytest`` -- so a thread is the honest shape for them anyway.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str
    timed_out: bool = False


async def run_command(
    program: str,
    *arguments: str,
    cwd: Path | None = None,
    timeout_seconds: float | None = None,
) -> CommandResult:
    """Run one command to completion, with stderr folded into stdout.

    Named ``timeout_seconds``, not ``timeout`` -- ruff's ASYNC109 reads a
    plain ``timeout`` parameter on an async function as "should have used
    ``asyncio.timeout()`` instead", which is backwards here: asyncio-level
    cancellation is exactly what this replaces (see below), so the rule's
    suggested fix would reintroduce the bug.

    The value is enforced by ``subprocess.run`` itself, not by a caller
    wrapping this coroutine in ``asyncio.wait_for``. That used to be the
    shape here, and it was wrong the same way the module docstring's
    transport bug was wrong: cancelling the *await* only stops this side
    from waiting on the worker thread, it does not reach into the thread
    and stop the blocking ``subprocess.run`` call still running inside it --
    so a "timed out" child kept running for real. Found live: a python
    process from a job's hung ``<<'EOF'`` heredoc read (see harness_tools.py)
    still alive more than two days after its supposed 60s ``shell_seconds``
    budget, holding no lock and doing nothing anyone could see from the
    mesh log. ``subprocess.run(timeout=...)`` kills the child itself (via
    ``Popen.kill()``) before raising, on whichever thread is actually
    running it, so the process is really gone either way.
    """

    def call() -> tuple[int, bytes, bool]:
        try:
            completed = subprocess.run(  # noqa: S603 - the caller supplies the program
                [program, *arguments],
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
                check=False,
            )
            return completed.returncode, completed.stdout or b"", False
        except subprocess.TimeoutExpired as exc:
            return 124, exc.output or b"", True

    exit_code, output, timed_out = await asyncio.to_thread(call)
    return CommandResult(
        exit_code=exit_code,
        output=output.decode(errors="replace"),
        timed_out=timed_out,
    )
