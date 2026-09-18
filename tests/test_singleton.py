from __future__ import annotations

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
