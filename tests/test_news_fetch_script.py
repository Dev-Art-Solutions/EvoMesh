"""The NewsWatcher's news_fetch.py, a standalone script bundled with the
template (not part of the evomesh package), gets a small site-specific HTML
scrape for hosts with no RSS feed. Loaded by path since it isn't a package;
tested against synthetic fixtures shaped like the real markup, not live
network or real article text, so this stays deterministic in CI."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "agent-templates"
    / "news-watcher"
    / "tools"
    / "news_fetch"
    / "scripts"
    / "news_fetch.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("news_fetch_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


news_fetch = _load_module()

YAHOO_FIXTURE = """
<html><body>
<div class="story-item">
<a class="subtle-link" href="https://finance.yahoo.com/news/example-headline-one-120000.html"
   data-ylk="cpos:1;">
  <h3 class="clamp headline-a">Example Headline One</h3>
</a>
</div>
<div class="story-item">
<a class="subtle-link" href="https://finance.yahoo.com/news/example-headline-two-130000.html">
  <h3 class="clamp headline-b">Example Headline Two</h3>
</a>
</div>
</body></html>
"""

FOREXFACTORY_FIXTURE = """
<html><body>
<table>
<tr><td><a href="/news/1000001-example-article-a">Example Article A</a></td></tr>
<tr><td><a href="/news/1000001-example-article-a/hit">hit</a></td></tr>
<tr><td><a href="/news/1000002-example-article-b">Example Article B</a></td></tr>
</table>
</body></html>
"""


def test_yahoo_finance_scraper_extracts_title_and_link() -> None:
    items = news_fetch._scrape_yahoo_finance(YAHOO_FIXTURE)

    assert items == [
        {
            "title": "Example Headline One",
            "link": "https://finance.yahoo.com/news/example-headline-one-120000.html",
            "published": "",
        },
        {
            "title": "Example Headline Two",
            "link": "https://finance.yahoo.com/news/example-headline-two-130000.html",
            "published": "",
        },
    ]


def test_forexfactory_scraper_dedupes_by_article_id() -> None:
    items = news_fetch._scrape_forexfactory(FOREXFACTORY_FIXTURE)

    assert items == [
        {
            "title": "Example Article A",
            "link": "https://www.forexfactory.com/news/1000001-example-article-a",
            "published": "",
        },
        {
            "title": "Example Article B",
            "link": "https://www.forexfactory.com/news/1000002-example-article-b",
            "published": "",
        },
    ]


def test_parse_source_falls_back_to_the_scraper_for_a_known_host() -> None:
    items = news_fetch._parse_source(
        "https://finance.yahoo.com/", YAHOO_FIXTURE.encode("utf-8")
    )

    assert len(items) == 2
    assert items[0]["title"] == "Example Headline One"


def test_parse_source_returns_nothing_for_an_unknown_non_rss_host() -> None:
    items = news_fetch._parse_source(
        "https://example.com/news", b"<html><body>not rss</body></html>"
    )

    assert items == []


def test_config_path_prefers_one_beside_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness runs this script as a subprocess with the *calling
    agent's own playground* as cwd (see harness_tools.py's run_command), so
    a config.json living there -- one per agent -- is what a model-driven
    tool call actually needs, not a guess relative to wherever this
    particular installed copy of the script happens to sit on disk. Found
    live: every such call was silently reading a nonexistent
    <repo_root>/config.json instead, so it always ran on DEFAULT_FEEDS and
    never the agent's configured watchlist."""
    monkeypatch.chdir(tmp_path)
    playground_config = tmp_path / "config.json"
    playground_config.write_text(json.dumps({"feeds": ["https://example.com/rss"]}))

    assert news_fetch._config_path() == playground_config
    assert news_fetch._load_config() == {"feeds": ["https://example.com/rss"]}


def test_config_path_falls_back_to_the_template_when_cwd_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly spawned agent has no config.json of its own yet -- the
    template's own default (agent-templates/news-watcher/config.json here)
    is the right fallback, not an empty <repo_root>/config.json nobody
    ever writes."""
    monkeypatch.chdir(tmp_path)

    resolved = news_fetch._config_path()

    assert resolved == SCRIPT_PATH.parent.parent.parent.parent / "config.json"
    assert resolved.is_file()


def test_cache_path_sits_beside_whichever_config_was_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text("{}")

    assert news_fetch._cache_path() == tmp_path / "scripts" / ".news_cache.jsonl"


def test_parse_feed_still_handles_plain_rss() -> None:
    rss = (
        b"<?xml version='1.0'?><rss><channel>"
        b"<item><title>RSS Headline</title><link>https://example.com/a</link></item>"
        b"</channel></rss>"
    )

    items = news_fetch._parse_source("https://example.com/feed.rss", rss)

    assert items == [{"title": "RSS Headline", "link": "https://example.com/a", "published": ""}]


def test_feeds_are_fetched_in_parallel_and_a_dead_one_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sequentially, three slow feeds outlast the watcher's 20s kill; in
    parallel the whole poll takes about as long as the slowest one."""
    import time
    import urllib.error

    rss = (
        "<rss><channel><item><title>{0}</title><link>https://x/{0}</link></item>"
        "</channel></rss>"
    )

    def slow_fetch(url: str) -> bytes:
        time.sleep(0.5)
        if url.endswith("dead"):
            raise urllib.error.URLError("down")
        return rss.format(url.rsplit("/", 1)[-1]).encode()

    monkeypatch.setattr(news_fetch, "_fetch", slow_fetch)
    monkeypatch.setattr(news_fetch, "_append_cache", lambda items, config: None)

    started = time.monotonic()
    items = news_fetch.fetch_and_cache(
        ["https://a/one", "https://b/dead", "https://c/three"], {}
    )

    assert time.monotonic() - started < 1.2
    assert [item["title"] for item in items] == ["one", "three"]
    assert items[0]["source"] == "https://a/one"
