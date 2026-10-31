from __future__ import annotations

import os
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """No git command a test starts can walk up out of the test temp root.

    Found 2026-10-31 with GIT_TRACE_SETUP over the whole suite: a scratch
    folder with no .git of its own (under .pytest-tmp, inside this checkout)
    sent `git status`, `git worktree prune` and `git branch -D` to this
    project's own repository -- the one a live mesh may be using at that
    very moment. git stops looking for a repository at a ceiling directory,
    so a test's own `git init` still works and the checkout is out of reach.
    """
    basetemp = config.option.basetemp
    if basetemp:
        ceiling = str(Path(basetemp).resolve())
        current = os.environ.get("GIT_CEILING_DIRECTORIES", "")
        os.environ["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(
            part for part in (current, ceiling) if part
        )


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep relative default paths out of the checkout.

    Settings resolves relative paths only when it loads a config file, so a
    test that builds Settings directly keeps the default ``workspace`` and
    writes into whatever directory pytest was started from. That overwrites the
    world context of a mesh running from the same checkout, which then reports
    agents invented by the test suite.
    """
    run_dir = tmp_path / "cwd"
    run_dir.mkdir()
    monkeypatch.chdir(run_dir)
    return run_dir


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never launch the real Scrapling browser from a test. Found live
    2026-09-27: a test that spawns the real news-watcher template started its
    watcher, whose config fetches wsj.com through Scrapling's Chrome; the
    template's temp copy sits inside the checkout, so the walk up found the
    real install -- and the mesh's baseline suite, every few minutes, kept
    opening Chrome on the owner's desktop. Watchers inherit the environment."""
    monkeypatch.setenv("EVOMESH_NO_BROWSER", "1")


@pytest.fixture(scope="session")
def project_root() -> Path:
    from evomesh.codebase import project_root as _project_root

    return _project_root()
