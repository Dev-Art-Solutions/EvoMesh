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
import re
import sys
from pathlib import Path
from types import ModuleType

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
NEWS_FETCH_PATH = TEMPLATE_DIR / "tools" / "news_fetch" / "scripts" / "news_fetch.py"
STATE_PATH = TEMPLATE_DIR / "scripts" / ".watch_state.json"
MAX_REMEMBERED_LINKS = 2000


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
    pages = [str(url) for url in (config.get("pages") or [])]
    whole_feeds = news_fetch.whole_feeds_of(config)
    if not keywords and not pages and not whole_feeds:
        # Nothing configured to watch for -- stay silent rather than
        # announcing every single headline.
        return 0

    feeds = config.get("feeds") or news_fetch.DEFAULT_FEEDS
    state = _load_state()
    # Ordered, oldest first, so trimming forgets the oldest -- a set trimmed
    # arbitrarily and let a page's still-listed stories be reported again.
    seen: dict[str, None] = dict.fromkeys(str(key) for key in state["seen"])

    collected = news_fetch.fetch_and_cache(
        news_fetch.with_whole_feeds(feeds, whole_feeds), config, pages
    )

    # A page or a whole feed (WSJ's markets RSS, say) is reported whole: every
    # headline on it not already reported. Other feeds only when a keyword matches.
    whole = {*pages, *whole_feeds}
    patterns = [keyword_pattern(keyword) for keyword in keywords]
    matches = [
        item
        for item in collected
        if item.get("source") in whole or any(pattern.search(item["title"]) for pattern in patterns)
    ]
    # Once each: a story is its link (or its site's story id) and its
    # headline, so one story from two sources, or twice on one page, is one.
    lines: list[str] = []
    for item in matches:
        keys = [item["link"], news_fetch.story_key(item["link"]), item["title"].casefold()]
        if not any(key in seen for key in keys):
            lines.append(f"{item['title']} ({item['link']})")
        for key in keys:  # still listed: newest again, so it is the last forgotten
            seen.pop(key, None)
            seen[key] = None

    if lines:
        print("\n".join(lines))
    state["seen"] = list(seen)[-MAX_REMEMBERED_LINKS:]
    try:
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass
    return 0


def keyword_pattern(keyword: str) -> re.Pattern[str]:
    """A keyword as a whole word or phrase, case-insensitive. Found live
    2026-09-26: as a plain substring, "ETH" matched "Ethiopia", "AI" matched
    "said" and "oil" matched "turmoil", so the watcher reported war news as a
    crypto signal."""
    return re.compile(rf"(?<![0-9A-Za-z]){re.escape(keyword)}(?![0-9A-Za-z])", re.IGNORECASE)


if __name__ == "__main__":
    raise SystemExit(main())
