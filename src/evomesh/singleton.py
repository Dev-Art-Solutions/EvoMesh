"""Refuse to start a second EvoMesh against the same data.

Found live: the Windows Control Center launched a second `evomesh.exe`
without stopping the first (a relaunch that didn't check for one already
running). Both processes ran the full BDI/evolution loop against the same
git repo and generation counter at once -- only one of them could bind the
control port, so the other ran on, invisible to the Control Center, racing
candidate branches with its twin. Two hours of "no new evolution" traced
back to exactly this.

An OS-level advisory lock on a small file, held for the life of the
process, is the fix: a crash or kill releases it automatically (the OS
closes the handle), so there is no stale-PID-file cleanup to get wrong --
unlike a PID file, which a crash leaves behind looking like it's still held.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import BinaryIO


class AlreadyRunningError(RuntimeError):
    """Another EvoMesh process already holds the lock at this path."""


class SingletonLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: BinaryIO | None = None

    def acquire(self, wait_seconds: float = 0.0) -> None:
        if self._handle is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_bytes(b"\0")
        handle = open(self._path, "r+b")
        handle.seek(0)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                _lock_exclusive_nonblocking(handle)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise AlreadyRunningError(
                        f"another EvoMesh process already holds the lock at {self._path} "
                        "-- refusing to start a second instance against the same data"
                    ) from exc
                time.sleep(min(0.25, deadline - time.monotonic()))
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            _unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> SingletonLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _lock_exclusive_nonblocking(handle: BinaryIO) -> None:
    try:
        import msvcrt
    except ImportError:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock(handle: BinaryIO) -> None:
    try:
        import msvcrt
    except ImportError:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        # Already gone (e.g. the file was removed underneath us) -- the
        # handle close below still releases the OS-level lock either way.
        pass
