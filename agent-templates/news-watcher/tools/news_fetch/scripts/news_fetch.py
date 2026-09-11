"""Fetch recent headlines from a set of RSS/Atom feeds, stdlib only.

argv[1] = an optional JSON object overriding feeds/keywords/limit; falls
back to config.json beside the installed template (agent-templates/
news-watcher/config.json) when a field is not given.

RSS is the primary source, deliberately -- it needs no extra runtime
dependency and is far more reliable than scraping a page's HTML. A source
with no RSS feed is a job for the harness's own scraping tool (Scrapling,
see evomesh.yaml's `scraping` settings) run by hand, not this script.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

TOOL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = TOOL_DIR.parent / "config.json"

DEFAULT_FEEDS = [
    "https://www.investing.com/rss/news_25.rss",
    "https://www.forexlive.com/feed/news",
]
DEFAULT_LIMIT = 10
FETCH_TIMEOUT_SECONDS = 10


def _load_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _fetch_feed(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "EvoMesh-NewsWatcher/1.0"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        return response.read()


def _parse_items(raw: bytes) -> list[dict[str, str]]:
    """RSS 2.0 <item> or Atom <entry> elements, namespace-agnostic."""
    items: list[dict[str, str]] = []
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return items
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        title = link = published = ""
        for child in element:
            child_tag = child.tag.rsplit("}", 1)[-1]
            if child_tag == "title":
                title = (child.text or "").strip()
            elif child_tag == "link":
                link = (child.get("href") or child.text or "").strip()
            elif child_tag in ("pubDate", "published", "updated"):
                published = (child.text or "").strip()
        if title:
            items.append({"title": title, "link": link, "published": published})
    return items


def main() -> int:
    config = _load_config()
    request: dict = {}
    if len(sys.argv) > 1 and sys.argv[1].strip():
        try:
            request = json.loads(sys.argv[1])
        except json.JSONDecodeError as exc:
            print(f"request is not valid JSON: {exc}")
            return 1

    feeds = request.get("feeds") or config.get("feeds") or DEFAULT_FEEDS
    keywords = [str(item).lower() for item in (request.get("keywords") or config.get("keywords") or [])]
    limit = int(request.get("limit") or config.get("limit") or DEFAULT_LIMIT)

    collected: list[dict[str, str]] = []
    for url in feeds:
        try:
            raw = _fetch_feed(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            continue
        collected.extend(_parse_items(raw))

    if keywords:
        collected = [
            item for item in collected if any(keyword in item["title"].lower() for keyword in keywords)
        ]

    print(json.dumps(collected[:limit], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
