"""Fetch one page through Scrapling as a real Chrome that never shows.

On Windows news_fetch runs this on a desktop of its own that is never
displayed (see _run_hidden there); the off-screen position below is what
keeps the window out of sight anywhere else. A failure is written to
<output>.error, since a hidden run has no console to read.

Run by news_fetch.py with Scrapling's own Python (the venv beside its
executable): argv = <url> <output.html>. wsj.com's DataDome turns away every
headless mode (a 401) and only lets a headed real Chrome through -- which,
from the CLI, is a window popping up over whatever a human is doing. The CLI
cannot pass browser flags, and Scrapling merges extra_flags through a set, so
its own --window-position=0,0 could win either way. This replaces that one
default instead: the browser is still headed (what DataDome checks), only
placed off screen, never focused. Found live 2026-09-27: the window sat at
(-32768,-32768) for the whole fetch and the page came back 200.
"""

from __future__ import annotations

import importlib
import sys

OFF_SCREEN = "--window-position=-32000,-32000"


def _off_screen(flags: tuple[str, ...]) -> tuple[str, ...]:
    kept = tuple(flag for flag in flags if flag != "--start-maximized")
    return tuple(OFF_SCREEN if flag.startswith("--window-position") else flag for flag in kept)


def main() -> int:
    url, output = sys.argv[1], sys.argv[2]
    try:
        return _fetch(url, output)
    except Exception as exc:  # noqa: BLE001 - reported to news_fetch, not raised
        with open(f"{output}.error", "w", encoding="utf-8") as handle:
            handle.write(f"{type(exc).__name__}: {exc}")
        return 1


def _fetch(url: str, output: str) -> int:
    base = importlib.import_module("scrapling.engines._browsers._base")
    for name in ("DEFAULT_ARGS", "STEALTH_ARGS"):
        setattr(base, name, _off_screen(tuple(getattr(base, name))))
    if not any(flag == OFF_SCREEN for flag in base.DEFAULT_ARGS + base.STEALTH_ARGS):
        base.STEALTH_ARGS = (*base.STEALTH_ARGS, OFF_SCREEN)

    fetchers = importlib.import_module("scrapling.fetchers")
    page = fetchers.StealthyFetcher.fetch(
        url, headless=False, real_chrome=True, wait=6000, timeout=90000
    )
    if page.status != 200:
        with open(f"{output}.error", "w", encoding="utf-8") as handle:
            handle.write(f"HTTP {page.status}")
        return 1
    with open(output, "w", encoding="utf-8") as handle:
        handle.write(page.html_content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
