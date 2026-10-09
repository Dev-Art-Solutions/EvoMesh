from __future__ import annotations

import time
from pathlib import Path

import pytest

from evomesh.singleton import AlreadyRunningError, SingletonLock


def test_a_second_lock_on_the_same_path_is_refused(tmp_path: Path) -> None:
    lock_path = tmp_path / "evomesh.lock"
    first = SingletonLock(lock_path)
    first.acquire()
    try:
        second = SingletonLock(lock_path)
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.release()


def test_releasing_the_first_lock_lets_a_second_one_acquire(tmp_path: Path) -> None:
    lock_path = tmp_path / "evomesh.lock"
    first = SingletonLock(lock_path)
    first.acquire()
    first.release()

    second = SingletonLock(lock_path)
    second.acquire()
    second.release()


def test_the_lock_path_and_its_parent_directory_are_created(tmp_path: Path) -> None:
    lock_path = tmp_path / "nested" / "evomesh.lock"
    lock = SingletonLock(lock_path)
    lock.acquire()
    try:
        assert lock_path.is_file()
    finally:
        lock.release()


def test_used_as_a_context_manager(tmp_path: Path) -> None:
    lock_path = tmp_path / "evomesh.lock"
    with SingletonLock(lock_path):
        other = SingletonLock(lock_path)
        with pytest.raises(AlreadyRunningError):
            other.acquire()

    # Released on exit -- a fresh lock can acquire it now.
    again = SingletonLock(lock_path)
    again.acquire()
    again.release()


def test_acquire_waits_then_succeeds_once_released(tmp_path):
    # A real lock is held and then released by a background thread; acquire()
    # with a timeout must retry instead of refusing immediately and succeed
    # once the holder lets go.
    holder = SingletonLock(tmp_path / "singleton.lock")
    holder.acquire()

    import threading

    release = threading.Event()

    def releaser():
        time.sleep(0.1)
        holder.release()
        release.set()

    thread = threading.Thread(target=releaser)
    thread.start()

    waiter = SingletonLock(tmp_path / "singleton.lock")
    waiter.acquire(wait_seconds=5)  # must not raise, must not hang
    waiter.release()
    thread.join()
    assert release.is_set()


def test_acquire_gives_up_after_timeout(tmp_path):
    # Held for the whole window: acquire() must give up with
    # AlreadyRunningError once wait_seconds has elapsed.
    holder = SingletonLock(tmp_path / "singleton.lock")
    holder.acquire()

    try:
        waiter = SingletonLock(tmp_path / "singleton.lock")
        with pytest.raises(AlreadyRunningError):
            waiter.acquire(wait_seconds=0.5)
    finally:
        holder.release()


def test_the_refusal_names_the_process_that_holds_the_lock(tmp_path: Path) -> None:
    """The holder is another process, as in real life: on Windows the lock
    byte cannot be read across processes, which is what sank generations
    1674-1676. The identity sits after it and is read from there."""
    import subprocess
    import sys

    path = tmp_path / "evomesh.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; from pathlib import Path; "
            "from evomesh.singleton import SingletonLock; "
            "lock = SingletonLock(Path(sys.argv[1])); lock.acquire(); print('held', flush=True); "
            "time.sleep(30)",
            str(path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"

        # Not holder.pid: a venv's python.exe is a launcher, and the lock is
        # held by the interpreter it starts -- a different pid.
        with pytest.raises(AlreadyRunningError, match=r"process \(pid \d+, started \d{4}-"):
            SingletonLock(path).acquire()
    finally:
        holder.kill()
        holder.wait()


def test_a_new_holder_replaces_the_old_identity(tmp_path: Path) -> None:
    from evomesh.singleton import read_holder

    path = tmp_path / "evomesh.lock"
    path.write_bytes(b"\0pid 99999, started long ago and much longer text than ours")

    with SingletonLock(path):
        held = read_holder(path)

    assert held.startswith("pid ") and "99999" not in held and "longer text" not in held
    assert path.read_bytes()[:1] == b"\0", "byte 0 stays the lock byte"
