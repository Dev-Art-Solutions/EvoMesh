"""Staged, reversible replacement of one capability directory.

A tool or skill bundle is a directory (``tools/<name>/``, ``skills/<name>/``)
that live harness jobs read and run from. Installing over one used to be
``rmtree`` then ``copytree``: a bundle that turned out invalid, a copy that
failed halfway, or a script file Windows held open left the old version
deleted and the new one partial -- a working tool replaced by a broken one,
with nothing to go back to.

Here the new bundle is copied into a staging directory first and validated
*there*; only a copy that parses is swapped in, by two directory renames on
the same volume, and the version it replaces is kept as ``previous/<kind>/
<name>`` so ``restore_previous`` can put it back. Every failure before the
swap leaves the installed directory untouched; a failure during it puts the
old one back before raising.

The work area lives under ``.runtime/capabilities`` of the registry's root:
ignored by git, and on the same volume as the target, which is what makes
``os.replace`` of a directory a rename rather than a copy.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

WORK_DIRNAME = Path(".runtime") / "capabilities"
_IGNORED = shutil.ignore_patterns("__pycache__", ".git", "*.pyc")


class BundleError(OSError):
    """The bundle could not be swapped in; the installed one is unchanged."""


def work_root(root: Path) -> Path:
    return root / WORK_DIRNAME


def previous_path(root: Path, kind: str, name: str) -> Path:
    return work_root(root) / "previous" / kind / name


def _discard(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def replace_directory[T](
    root: Path,
    kind: str,
    target: Path,
    fill: Callable[[Path], None],
    validate: Callable[[Path], T],
) -> T:
    """Build a new ``target`` in staging with ``fill``, check it with
    ``validate`` (which raises to refuse it), then swap it in.

    ``fill`` receives an empty staging directory; ``validate`` receives the
    filled one and returns whatever the caller wants back (a parsed
    definition). Blocking -- call it through ``asyncio.to_thread``.
    """
    stage = work_root(root) / "staging" / f"{target.name}-{uuid.uuid4().hex[:8]}"
    stage.parent.mkdir(parents=True, exist_ok=True)
    try:
        fill(stage)
        result = validate(stage)
    except BaseException:
        _discard(stage)
        raise
    previous = previous_path(root, kind, target.name)
    had_previous = target.exists()
    if had_previous:
        _discard(previous)
        if previous.exists():
            _discard(stage)
            raise BundleError(f"{previous}: the last kept version could not be cleared")
        previous.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(target, previous)
        except OSError as exc:
            _discard(stage)
            raise BundleError(
                f"{target}: could not be moved aside ({exc}); is one of its files in use? "
                "The installed version is unchanged."
            ) from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(stage, target)
    except OSError as exc:
        if had_previous:
            os.replace(previous, target)
        _discard(stage)
        raise BundleError(
            f"{target}: the new version could not be swapped in ({exc}); "
            "the installed version is unchanged."
        ) from exc
    return result


def copy_into(source: Path) -> Callable[[Path], None]:
    """A ``fill`` that copies ``source`` (a bundle directory) into staging."""

    def fill(stage: Path) -> None:
        shutil.copytree(source, stage, ignore=_IGNORED)

    return fill


def overlay_file(current: Path, filename: str, text: str) -> Callable[[Path], None]:
    """A ``fill`` for a single-file install over a bundle that may already
    have scripts beside it: the installed directory (if any) is copied, then
    ``filename`` is written over -- so replacing a TOOL.md never drops the
    scripts it runs."""

    def fill(stage: Path) -> None:
        if current.is_dir():
            shutil.copytree(current, stage, ignore=_IGNORED)
        else:
            stage.mkdir(parents=True)
        (stage / filename).write_text(text, encoding="utf-8")

    return fill


def restore_previous[T](
    root: Path, kind: str, target: Path, validate: Callable[[Path], T]
) -> T:
    """Swap the kept previous version back in. The version it replaces
    becomes the new previous one, so a rollback can itself be undone."""
    previous = previous_path(root, kind, target.name)
    if not previous.is_dir():
        raise BundleError(f"no previous version of {target.name} is kept")
    return replace_directory(root, kind, target, copy_into(previous), validate)
