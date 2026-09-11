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
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

TOOL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = TOOL_DIR.parent / "config.json"

DEFAULT_FEEDS = [
    "https://finance.yahoo.com/",
    "https://www.forexfactory.com/news",
]
DEFAULT_LIMIT = 10
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
    raw_keywords = request.get("keywords") or config.get("keywords") or []
    keywords = [str(item).lower() for item in raw_keywords]
    limit = int(request.get("limit") or config.get("limit") or DEFAULT_LIMIT)

    collected: list[dict[str, str]] = []
    for url in feeds:
        try:
            raw = _fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            continue
        collected.extend(_parse_source(url, raw))

    if keywords:
        collected = [
            item
            for item in collected
            if any(keyword in item["title"].lower() for keyword in keywords)
        ]

    print(json.dumps(collected[:limit], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
