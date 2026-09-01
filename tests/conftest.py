from __future__ import annotations

from pathlib import Path

import pytest


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
