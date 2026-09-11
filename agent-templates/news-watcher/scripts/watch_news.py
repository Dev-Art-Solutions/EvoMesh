"""The NewsWatcher's deterministic keyword watcher.

Run on its own interval by evomesh.watchers.AgentWatcher -- never on the
agent's own cognition cycle, and never reporting "still checking" progress
the way a BDI goal would. Reuses the same fetch/scrape logic as the
news_fetch tool (imported by file path, since neither is an installed
package), filters by the keywords configured in config.json, and prints one
line per headline it has not already reported. Silence means either nothing
new matched, or no keywords are configured to match against at all.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.error
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

    collected: list[dict[str, str]] = []
    for url in feeds:
        try:
            raw = news_fetch._fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            continue
        collected.extend(news_fetch._parse_source(url, raw))

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
