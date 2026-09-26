"""Fetch recent headlines from a set of sources, stdlib only.

argv[1] = an optional JSON object overriding feeds/keywords/limit; falls
back to config.json (see _config_path()) when a field is not given.

RSS/Atom is tried first for any URL -- it needs no extra runtime dependency
and is far more reliable than scraping a page's HTML. finance.yahoo.com and
forexfactory.com have no public RSS for their news streams, so those two
hosts fall back to a small, site-specific regex extraction over the raw
(server-rendered) HTML instead -- not a general-purpose scraper, and not
expected to survive an unrelated site redesign.

config.json's "pages" are whole pages to report in full, not feeds:
wsj.com's stock news, say, which has no RSS and sits behind an anti-bot wall
(DataDome) that turns away urllib and even a headless browser. Those go
through the mesh's one crawling tool, Scrapling, as a real, headed Chrome --
the only mode wsj.com let through, found live 2026-09-27 -- placed off
screen by page_fetch.py so no window ever shows or takes focus. Their
headlines come from the page's own schema.org ItemList. A browser per fetch
is why a page is fetched at most every "page_minutes" (default 30); between
fetches its last result stands in.

Every live fetch also feeds a small durable cache (.news_cache.jsonl beside
whichever config.json this run resolved, see _cache_path()) so a caller can
look back over more than one snapshot -- the live fetch above only ever
returns what a source has *right now*, and the previous headlines are gone
the moment a newer one pushes them off a feed. Pass {"from_cache": true} to
read that history back (optionally with "since_hours") instead of hitting
the network at all. Entries older than config.json's "cache_days" (default
3) are pruned every time the cache is written.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree

# This one script file is reached two very different ways, and they disagree
# about where "beside this template's AGENT.md" even is:
#
# - The harness runs it as a subprocess custom tool (TOOL.md's `command:`),
#   with the *calling agent's own playground* as cwd (run_command in
#   harness_tools.py) -- and the tool itself is installed flattened into one
#   shared `tools/news_fetch/` in the registry root (ToolRegistry.install_
#   directory keys by tool name, so news-watcher's and news-analyzer's
#   bundled copies overwrite the same slot), so __file__ here no longer
#   points anywhere near either template. Found live: this sent every
#   model-driven `news_fetch` call reading a nonexistent
#   <repo_root>/config.json (so silently DEFAULT_FEEDS, never the
#   configured watchlist) and writing an orphaned, ever-growing
#   <repo_root>/scripts/.news_cache.jsonl nothing else ever read.
# - watch_news.py (the deterministic watcher) imports this module directly
#   from its own template's bundled copy instead, where __file__-relative
#   resolution already lands on the right config.json.
#
# Both invocations happen to share one cwd, though: the harness sets it to
# the agent's playground either way (see AgentWatcher's own `cwd=` in
# environment.py). A config.json living there -- the one place both paths
# agree on -- wins over the file-relative guess below, which stays only as
# the fallback for a freshly spawned agent that has not been given one yet.
TOOL_DIR = Path(__file__).resolve().parent.parent


def _config_path() -> Path:
    beside_playground = Path.cwd() / "config.json"
    if beside_playground.is_file():
        return beside_playground
    return TOOL_DIR.parent.parent / "config.json"


def _cache_path() -> Path:
    return _config_path().parent / "scripts" / ".news_cache.jsonl"


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
# Scrapling's CLI `extract` mode and flags for a "pages" entry, used only when
# Scrapling's own Python is not beside its executable (see _page_command):
# a real Chrome, headed -- and so a visible window. Headless and plain
# fetches got wsj.com's DataDome block page or a 401.
PAGE_FETCH_ARGS = ["stealthy-fetch", "--real-chrome", "--no-headless", "--wait", "6000",
                   "--timeout", "90000"]
PAGE_FETCH_TIMEOUT_SECONDS = 150
DEFAULT_PAGE_MINUTES = 30
_LD_JSON = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
# wsj.com ends every story's URL with its own id; a re-slugged headline keeps it.
_STORY_ID = re.compile(r"-([0-9a-f]{8})$")
_FOREXFACTORY_HEADLINE = re.compile(r'href="(/news/(\d+)[a-z0-9\-]*)"[^>]*>([^<]{5,200})<')


def _load_config() -> dict:
    config_path = _config_path()
    if not config_path.is_file():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
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


def _scraper(config: dict) -> list[str] | None:
    """Scrapling's command: EVOMESH_SCRAPER when the harness runs this as a
    tool, else config.json's "scraper", else the repo's own install -- the
    watcher runs with neither, from the agent's playground inside the repo."""
    value = os.environ.get("EVOMESH_SCRAPER", "").strip()
    value = value or str(config.get("scraper") or "").strip()
    if value:
        if value.startswith("["):
            return [str(part) for part in json.loads(value)]
        return [value] if Path(value).is_file() else shlex.split(value)
    for base in (Path.cwd(), Path(__file__).resolve().parent):
        for folder in (base, *base.parents):
            for candidate in (
                ".runtime/scrapling/Scripts/scrapling.exe",
                ".runtime/scrapling/bin/scrapling",
            ):
                if (folder / candidate).is_file():
                    return [str(folder / candidate)]
    return None


def _page_command(scraper: list[str], url: str, output: Path, config: dict) -> list[str]:
    """How to fetch a page: Scrapling's own Python running page_fetch.py
    (headed but off screen), when Scrapling is an installed executable with
    its venv's Python beside it; its CLI otherwise, or when config.json sets
    "page_fetch_args" explicitly."""
    if len(scraper) == 1 and os.environ.get("EVOMESH_NO_BROWSER"):
        # The real install, under the test suite (tests/conftest.py): no
        # Chrome on the desktop of whoever runs it.
        raise OSError("real browser fetches are off (EVOMESH_NO_BROWSER)")
    if len(scraper) == 1 and not config.get("page_fetch_args"):
        executable = Path(scraper[0])
        for name in ("python.exe", "python"):
            python = executable.with_name(name)
            if executable.stem.lower() == "scrapling" and python.is_file():
                helper = Path(__file__).resolve().with_name("page_fetch.py")
                return [str(python), str(helper), url, str(output)]
    mode, *flags = config.get("page_fetch_args") or PAGE_FETCH_ARGS
    return [*scraper, "extract", mode, url, str(output), *flags]


def _fetch_page(url: str, config: dict) -> str:
    """One page's HTML through Scrapling. Raises OSError when it fails."""
    scraper = _scraper(config)
    if scraper is None:
        raise OSError("Scrapling is not installed (scripts/install-scrapling.ps1)")
    with tempfile.TemporaryDirectory(prefix="evomesh-news-") as scratch:
        output = Path(scratch) / "page.html"
        try:
            run = subprocess.run(  # noqa: S603 - the mesh's own configured fetcher
                _page_command(scraper, url, output, config),
                capture_output=True,
                timeout=PAGE_FETCH_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OSError(f"{url}: the fetch did not finish in time") from exc
        if run.returncode != 0 or not output.is_file():
            detail = (run.stdout or run.stderr or b"").decode("utf-8", errors="replace")
            raise OSError(f"{url}: {detail.strip()[-300:] or f'exit {run.returncode}'}")
        return output.read_text(encoding="utf-8", errors="replace")


def story_key(link: str) -> str:
    """What makes two links the same story: the link without query or
    fragment, or a site's own story id when it has one."""
    parsed = urllib.parse.urlparse(link)
    path = parsed.path.rstrip("/")
    match = _STORY_ID.search(path)
    host = parsed.netloc.lower()
    return f"{host}#{match.group(1)}" if match else f"{host}{path}"


def _item_list_elements(node: object):
    if isinstance(node, dict):
        if str(node.get("@type", "")).lower() == "itemlist":
            for element in node.get("itemListElement") or []:
                if isinstance(element, dict):
                    yield element
            return
        for value in node.values():
            yield from _item_list_elements(value)
    elif isinstance(node, list):
        for value in node:
            yield from _item_list_elements(value)


def parse_page(html: str) -> list[dict[str, str]]:
    """Every headline in the page's schema.org ItemList, in page order and
    once each: a story listed twice (by link or by headline) is one item."""
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for block in _LD_JSON.findall(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for element in _item_list_elements(data):
            inner = element.get("item") if isinstance(element.get("item"), dict) else {}
            title = " ".join(str(element.get("name") or inner.get("name") or "").split())
            link = str(element.get("url") or inner.get("url") or "").strip()
            if not title or not link.startswith("http"):
                continue
            parsed = urllib.parse.urlparse(link)._replace(query="", fragment="")
            link = urllib.parse.urlunparse(parsed)
            keys = {story_key(link), title.casefold()}
            if keys & seen:
                continue
            seen |= keys
            published = str(element.get("datePublished") or inner.get("datePublished") or "")
            items.append({"title": title, "link": link, "published": published})
    return items


def _page_state_path() -> Path:
    return _cache_path().with_name(".news_pages.json")


def _load_page_state() -> dict:
    try:
        data = json.loads(_page_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def fetch_pages(pages: list[str], config: dict, *, force: bool = False) -> list[dict[str, str]]:
    """Each page's headlines: fetched live at most every page_minutes, the
    last result otherwise -- or when a live fetch fails."""
    try:
        minutes = float(config.get("page_minutes") or DEFAULT_PAGE_MINUTES)
    except (TypeError, ValueError):
        minutes = DEFAULT_PAGE_MINUTES
    state = _load_page_state()
    now = time.time()
    collected: list[dict[str, str]] = []
    for url in pages:
        entry = state.get(url) if isinstance(state.get(url), dict) else {}
        items = entry.get("items") or []
        if force or now - float(entry.get("at") or 0) >= minutes * 60:
            try:
                items = parse_page(_fetch_page(url, config))
                state[url] = {"at": now, "items": items}
            except OSError as exc:
                print(f"page fetch failed: {exc}", file=sys.stderr)
        for item in items:
            collected.append({**item, "source": url})
    try:
        _page_state_path().parent.mkdir(parents=True, exist_ok=True)
        _page_state_path().write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass
    return collected


def _load_cache() -> list[dict]:
    cache_path = _cache_path()
    if not cache_path.is_file():
        return []
    entries: list[dict] = []
    try:
        lines = cache_path.read_text(encoding="utf-8").splitlines()
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
        cache_path = _cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
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


def fetch_and_cache(
    feeds: list[str], config: dict, pages: list[str] | None = None
) -> list[dict[str, str]]:
    """Live-fetch every feed (and every page, see fetch_pages), tagging each
    item with its source, then merge the result into the durable cache before
    returning it. Shared with watch_news.py so the deterministic watcher's own
    polls also build up the same history a model can later query."""
    # In parallel, not one after another: sequentially, three feeds at
    # FETCH_TIMEOUT_SECONDS each could take 30s, and the watcher running this
    # kills its command at 20s -- found live 2026-09-25, a slow feed turned
    # whole ticks into "Watcher command timed out" with nothing reported.
    def fetch(url: str) -> bytes | None:
        try:
            return _fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            return None

    collected: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(feeds) + 1))) as pool:
        # The page's browser runs beside the feeds, not after them.
        paged = pool.submit(fetch_pages, pages or [], config)
        for url, raw in zip(feeds, pool.map(fetch, feeds), strict=True):
            if raw is None:
                continue
            for item in _parse_source(url, raw):
                item["source"] = url
                collected.append(item)
        collected.extend(paged.result())
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
    pages = request.get("pages") or config.get("pages") or []
    collected = fetch_and_cache(feeds, config, pages)

    if keywords:
        # A page is reported whole; keywords only narrow the feeds.
        collected = [
            item
            for item in collected
            if item.get("source") in pages
            or any(keyword in item["title"].lower() for keyword in keywords)
        ]

    result = [{"title": i["title"], "link": i["link"], "published": i["published"]}
              for i in collected]
    print(json.dumps(result[:limit], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
