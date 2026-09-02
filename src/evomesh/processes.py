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


async def run_command(
    program: str, *arguments: str, cwd: Path | None = None
) -> CommandResult:
    """Run one command to completion, with stderr folded into stdout."""

    def call() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603 - the caller supplies the program
            [program, *arguments],
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    completed = await asyncio.to_thread(call)
    return CommandResult(
        exit_code=completed.returncode,
        output=(completed.stdout or b"").decode(errors="replace"),
    )
