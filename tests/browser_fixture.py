"""Run a tests/fixtures/browser page in real headless Chrome and read back the
JSON its runner script leaves in <pre id="results">.

``--dump-dom`` prints the DOM once the page settles, and
``--virtual-time-budget`` lets its timers (the delayed elements, the
infinite-scroll loads) run without real waiting. No extension, no network,
no CDP client: the page agent is loaded by the fixture's own <script> tag.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from evomesh.browser import find_chrome

FIXTURES = Path(__file__).parent / "fixtures" / "browser"


def run_fixture(name: str, *, budget_ms: int = 30000) -> dict[str, Any]:
    chrome = find_chrome()
    if chrome is None:
        raise FileNotFoundError("Chrome is not installed")
    with tempfile.TemporaryDirectory(prefix="evomesh-chrome-test-") as profile:
        completed = subprocess.run(
            [
                str(chrome),
                "--headless",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={profile}",
                f"--virtual-time-budget={budget_ms}",
                "--dump-dom",
                (FIXTURES / name).resolve().as_uri(),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
    match = re.search(r'<pre id="results">(.*?)</pre>', completed.stdout, re.S)
    if match is None:
        raise AssertionError(f"no results in the page: {completed.stderr[-2000:]}")
    return json.loads(html.unescape(match.group(1)))
