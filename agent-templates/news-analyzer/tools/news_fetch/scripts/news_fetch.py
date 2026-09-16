"""Fetch recent headlines from a set of sources, stdlib only.

argv[1] = an optional JSON object overriding feeds/keywords/limit; falls
back to config.json beside the installed template (agent-templates/
news-watcher/config.json) when a field is not given.

RSS/Atom is tried first for any URL -- it needs no extra runtime dependency
and is far more reliable than scraping a page's HTML. finance.yahoo.com and
forexfactory.com have no public RSS for their news streams, so those two
hosts fall back to a small, site-specific regex extraction over the raw
(server-rendered) HTML instead -- not a general-purpose scraper, and not
expected to survive an unrelated site redesign. Any other host with no RSS
is a job for the harness's own scraping tool (Scrapling, see evomesh.yaml's
`scraping` settings) run by hand, not this script.

Every live fetch also feeds a small durable cache (.news_cache.jsonl beside
this template's AGENT.md, see CACHE_PATH) so a caller can look back over
more than one snapshot -- the live fetch above only ever returns what a
source has *right now*, and the previous headlines are gone the moment a
newer one pushes them off a feed. Pass {"from_cache": true} to read that
history back (optionally with "since_hours") instead of hitting the network
at all. Entries older than config.json's "cache_days" (default 3) are
pruned every time the cache is written.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

TOOL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = TOOL_DIR.parent.parent / "config.json"
CACHE_PATH = TOOL_DIR.parent.parent / "scripts" / ".news_cache.jsonl"

DEFAULT_FEEDS = [
    "https://finance.yahoo.com/",
    "https://www.forexfactory.com/news",
]
DEFAULT_LIMIT = 10
DEFAULT_CACHE_DAYS = 3
MAX_CACHE_ENTRIES = 5000
FETCH_TIMEOUT_SECONDS = 10

_YAHOO_FINANCE_HEADLINE = re.compile(
    r'<a[^>]+href="(https://finance\.yahoo\.com/[a-zA-Z0-9/_.\-]+)"[^>]{0,400}?>'
    r'.{0,200}?<h3[^>]*>([^<]{5,300})</h3>',
    re.S,
)
_FOREXFACTORY_HEADLINE = re.compile(r'href="(/news/(\d+)[a-z0-9\-]*)"[^>]*>([^<]{5,200})<')


def _load_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "EvoMesh-NewsWatcher/1.0"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        return response.read()


def _parse_feed(raw: bytes) -> list[dict[str, str]]:
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


def _scrape_yahoo_finance(html: str) -> list[dict[str, str]]:
    seen: set[str] = set()
    items: list[dict[str, str]] = []
    for link, title in _YAHOO_FINANCE_HEADLINE.findall(html):
        if link in seen:
            continue
        seen.add(link)
        items.append({"title": title.strip(), "link": link, "published": ""})
    return items


def _scrape_forexfactory(html: str) -> list[dict[str, str]]:
    seen: set[str] = set()
    items: list[dict[str, str]] = []
    for path, article_id, title in _FOREXFACTORY_HEADLINE.findall(html):
        if article_id in seen:
            continue
        seen.add(article_id)
        items.append({
            "title": title.strip(),
            "link": f"https://www.forexfactory.com{path}",
            "published": "",
        })
    return items


_HTML_SCRAPERS = {
    "finance.yahoo.com": _scrape_yahoo_finance,
    "forexfactory.com": _scrape_forexfactory,
    "www.forexfactory.com": _scrape_forexfactory,
}


def _parse_source(url: str, raw: bytes) -> list[dict[str, str]]:
    items = _parse_feed(raw)
    if items:
        return items
    scraper = _HTML_SCRAPERS.get(urllib.parse.urlparse(url).netloc.lower())
    if scraper is None:
        return []
    return scraper(raw.decode("utf-8", errors="replace"))


def _load_cache() -> list[dict]:
    if not CACHE_PATH.is_file():
        return []
    entries: list[dict] = []
    try:
        lines = CACHE_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("link") and entry.get("title"):
            entries.append(entry)
    return entries


def _append_cache(items: list[dict[str, str]], config: dict) -> None:
    """Merge freshly fetched items into the durable cache, stamping each
    genuinely new link with when it was first seen, then prune anything
    older than cache_days and rewrite the file. A no-op on any write error --
    the cache is a convenience, never the source of truth a live fetch is."""
    cache_days = config.get("cache_days")
    try:
        cache_days = float(cache_days) if cache_days is not None else DEFAULT_CACHE_DAYS
    except (TypeError, ValueError):
        cache_days = DEFAULT_CACHE_DAYS

    now = time.time()
    by_link: dict[str, dict] = {entry["link"]: entry for entry in _load_cache()}
    for item in items:
        link = item.get("link") or ""
        if not link or link in by_link:
            continue
        by_link[link] = {
            "title": item.get("title", ""),
            "link": link,
            "published": item.get("published", ""),
            "source": item.get("source", ""),
            "fetched_at": now,
        }

    cutoff = now - (cache_days * 86400)
    kept = [entry for entry in by_link.values() if entry.get("fetched_at", now) >= cutoff]
    kept.sort(key=lambda entry: entry.get("fetched_at", 0))
    kept = kept[-MAX_CACHE_ENTRIES:]

    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            "\n".join(json.dumps(entry) for entry in kept) + ("\n" if kept else ""),
            encoding="utf-8",
        )
    except OSError:
        pass


def _cached_items(keywords: list[str], since_hours: float | None) -> list[dict[str, str]]:
    entries = _load_cache()
    if since_hours is not None:
        cutoff = time.time() - (since_hours * 3600)
        entries = [entry for entry in entries if entry.get("fetched_at", 0) >= cutoff]
    entries.sort(key=lambda entry: entry.get("fetched_at", 0), reverse=True)
    if keywords:
        entries = [
            entry
            for entry in entries
            if any(keyword in entry.get("title", "").lower() for keyword in keywords)
        ]
    return [{"title": e["title"], "link": e["link"], "published": e.get("published", "")}
            for e in entries]


def fetch_and_cache(feeds: list[str], config: dict) -> list[dict[str, str]]:
    """Live-fetch every feed, tagging each item with its source, then merge
    the result into the durable cache before returning it. Shared with
    watch_news.py so the deterministic watcher's own polls also build up the
    same history a model can later query."""
    collected: list[dict[str, str]] = []
    for url in feeds:
        try:
            raw = _fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            continue
        for item in _parse_source(url, raw):
            item["source"] = url
            collected.append(item)
    _append_cache(collected, config)
    return collected


def main() -> int:
    config = _load_config()
    request: dict = {}
    if len(sys.argv) > 1 and sys.argv[1].strip():
        try:
            request = json.loads(sys.argv[1])
        except json.JSONDecodeError as exc:
            print(f"request is not valid JSON: {exc}")
            return 1

    raw_keywords = request.get("keywords") or config.get("keywords") or []
    keywords = [str(item).lower() for item in raw_keywords]
    limit = int(request.get("limit") or config.get("limit") or DEFAULT_LIMIT)

    if request.get("from_cache"):
        since_hours = request.get("since_hours")
        since_hours = float(since_hours) if since_hours is not None else None
        print(json.dumps(_cached_items(keywords, since_hours)[:limit], indent=2))
        return 0

    feeds = request.get("feeds") or config.get("feeds") or DEFAULT_FEEDS
    collected = fetch_and_cache(feeds, config)

    if keywords:
        collected = [
            item
            for item in collected
            if any(keyword in item["title"].lower() for keyword in keywords)
        ]

    result = [{"title": i["title"], "link": i["link"], "published": i["published"]}
              for i in collected]
    print(json.dumps(result[:limit], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
