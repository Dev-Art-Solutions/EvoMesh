"""The NewsWatcher's deterministic keyword watcher.

Run on its own interval by evomesh.watchers.AgentWatcher -- never on the
agent's own cognition cycle, and never reporting "still checking" progress
the way a BDI goal would. Reuses the same fetch/scrape logic as the
news_fetch tool (imported by file path, since neither is an installed
package), filters by the keywords configured in config.json, and prints one
line per headline it has not already reported. Silence means either nothing
new matched, or no keywords are configured to match against at all.

Every poll also feeds news_fetch's own durable cache (see CACHE_PATH there)
via fetch_and_cache -- this watcher's dedup state (below) only remembers
which links it already announced, not the headlines themselves, so the
cache is what lets a later on-demand news_fetch call ("show me the last
day") see history that never matched a keyword at all.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
NEWS_FETCH_PATH = TEMPLATE_DIR / "tools" / "news_fetch" / "scripts" / "news_fetch.py"
STATE_PATH = TEMPLATE_DIR / "scripts" / ".watch_state.json"
MAX_REMEMBERED_LINKS = 500


def _load_news_fetch() -> ModuleType:
    spec = importlib.util.spec_from_file_location("news_fetch_module", NEWS_FETCH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_state() -> dict:
    if not STATE_PATH.is_file():
        return {"seen": []}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"seen": []}
    if not isinstance(data.get("seen"), list):
        data["seen"] = []
    return data


def main() -> int:
    # A watcher's stdout is a pipe captured by run_command, but on Windows a
    # pipe's default encoding is still the system codepage, not UTF-8 -- so a
    # non-ASCII headline (Cyrillic, say) raised UnicodeEncodeError out of the
    # print() below and the whole tick was logged as a bare "exited 1" with no
    # readable cause.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    news_fetch = _load_news_fetch()
    config = news_fetch._load_config()
    keywords = [str(item).lower() for item in (config.get("keywords") or [])]
    if not keywords:
        # Nothing configured to watch for -- stay silent rather than
        # announcing every single headline.
        return 0

    feeds = config.get("feeds") or news_fetch.DEFAULT_FEEDS
    state = _load_state()
    seen: set[str] = set(state["seen"])

    collected = news_fetch.fetch_and_cache(feeds, config)

    matches = [
        item for item in collected if any(keyword in item["title"].lower() for keyword in keywords)
    ]
    new_matches = [item for item in matches if item["link"] not in seen]

    if new_matches:
        print("\n".join(f"{item['title']} ({item['link']})" for item in new_matches))

    for item in matches:
        seen.add(item["link"])
    state["seen"] = list(seen)[-MAX_REMEMBERED_LINKS:]
    try:
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
