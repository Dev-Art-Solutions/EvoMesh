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
