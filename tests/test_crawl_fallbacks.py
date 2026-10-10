"""The web-crawler's fallback chain: every fetch result is judged (a bot wall,
a 403 or an empty JavaScript shell is not the page), the next strategy is
tried until one gets real text, what worked is remembered per site, and a
start page with no links is crawled through its sitemap."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from evomesh.harness_tools import ToolContext, build_custom_tool
from evomesh.tools import MAX_TOOL_SECONDS, InvalidToolError, ToolDefinition, parse_tool

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_TOOLS = ROOT / "agent-templates" / "web-crawler" / "tools"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


crawl_site = _load(TEMPLATE_TOOLS / "crawl_site" / "scripts" / "crawl_site.py", "fb_crawl_site")
# The very module crawl_site imported, so a patched strategy is the one it calls.
webfetch = sys.modules["webfetch"]
fetch_page = _load(TEMPLATE_TOOLS / "fetch_page" / "scripts" / "fetch_page.py", "fb_fetch_page")

ARTICLE = (
    "<html><title>Story</title><body>" + "<p>Gold rose again today.</p>" * 20 + "</body></html>"
)
DATADOME = (
    "<html><head><title>g2.com</title></head><body><p id='cmsg'>Please enable JS and "
    "disable any ad blocker</p><script>var dd={'host':'geo.captcha-delivery.com'}</script>"
    "<script src='https://ct.captcha-delivery.com/c.js'></script></body></html>"
)
CLOUDFLARE = (
    "<html><title>Just a moment...</title><body><div id='cf-chl-widget'></div></body></html>"
)
JS_SHELL = (
    "<html><title>App</title><body><div id='root'></div>"
    "<script src='/app.js'></script></body></html>"
)


def _raw(body: str, status: int | None = 200, url: str = "https://site.test/") -> Any:
    return webfetch.Raw(body=body, url=url, status=status)


@pytest.mark.parametrize(
    ("raw", "verdict", "reason"),
    [
        (_raw(ARTICLE), "ok", ""),
        (_raw(DATADOME, 403), "blocked", "DataDome bot wall, HTTP 403"),
        (_raw(DATADOME, 200), "blocked", "DataDome bot wall, HTTP 200"),
        (_raw(CLOUDFLARE, 503), "blocked", "Cloudflare challenge, HTTP 503"),
        (_raw("<html><body>" + "<p>a real error page</p>" * 20 + "</body></html>", 500),
         "blocked", "HTTP 500"),
        (_raw("<p>Not here</p>", 404), "missing", "HTTP 404"),
        (_raw(JS_SHELL), "thin", "needs JavaScript"),
        (_raw(""), "thin", "no text"),
    ],
)
def test_every_result_is_judged_not_just_its_exit_code(raw: Any, verdict: str, reason: str) -> None:
    """Found live: Scrapling exits 0 on a 403 and saves the DataDome wall,
    and the crawler reported that wall as the site's content."""
    got, why, _ = webfetch.judge(raw, 150)

    assert got == verdict
    assert reason in why


def test_a_small_static_page_is_still_a_page() -> None:
    verdict, _, page = webfetch.judge(_raw("<html><title>Hi</title><p>Example Domain.</p>"), 150)

    assert verdict == "ok" and page.title == "Hi"


def test_a_captcha_through_the_reader_is_blocked_not_content() -> None:
    """Found live: r.jina.ai answers 200 with a warning and no content."""
    raw = webfetch.Raw(
        body="Title: g2.com\n\nURL Source: https://www.g2.com/\n\nWarning: This page maybe "
        "requiring CAPTCHA, please make sure you are authorized to access this page.\n\n"
        "Markdown Content:\n",
        url="https://www.g2.com/",
        status=200,
        kind="text",
    )

    verdict, reason, page = webfetch.judge(raw, 150)

    assert verdict == "blocked" and "CAPTCHA" in reason
    assert page.text == ""


def test_an_inline_svg_title_is_not_the_page_title() -> None:
    page = webfetch.Page()
    page.feed("<head><title>Real</title></head><body><svg><title>Icon</title></svg><p>x</p>")

    assert page.title == "Real"


def _fake(
    body: str, status: int | None = 200, calls: list[str] | None = None, name: str = ""
) -> Callable[..., Any]:
    def run(url: str, timeout: float) -> Any:
        if calls is not None:
            calls.append(name)
        return webfetch.Raw(body=body, url=url, status=status)

    return run


@pytest.fixture
def strategies(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Callable[..., Any]]]:
    table: dict[str, Callable[..., Any]] = {}
    monkeypatch.setattr(webfetch, "STRATEGIES", table)
    yield table


def test_the_chain_moves_on_until_a_strategy_gets_real_text(
    strategies: dict[str, Callable[..., Any]],
) -> None:
    calls: list[str] = []
    strategies["plain"] = _fake(DATADOME, 403, calls, "plain")
    strategies["shell"] = _fake(JS_SHELL, 200, calls, "shell")
    strategies["browser"] = _fake(ARTICLE, 200, calls, "browser")
    strategies["never"] = _fake(ARTICLE, 200, calls, "never")
    memory: dict[str, str] = {}

    page = webfetch.fetch(
        "https://site.test/a", {"strategies": ["plain", "shell", "browser", "never"]}, memory=memory
    )

    assert page.ok and page.strategy == "browser" and page.title == "Story"
    assert calls == ["plain", "shell", "browser"], "the first page with real text wins"
    assert page.trail() == (
        "plain blocked (DataDome bot wall, HTTP 403) -> shell thin (0 chars, needs JavaScript) "
        "-> browser ok"
    )
    assert memory == {"site.test": "browser"}


def test_what_worked_on_a_site_is_tried_first_next_time(
    strategies: dict[str, Callable[..., Any]],
) -> None:
    calls: list[str] = []
    strategies["plain"] = _fake(DATADOME, 403, calls, "plain")
    strategies["browser"] = _fake(ARTICLE, 200, calls, "browser")

    page = webfetch.fetch(
        "https://site.test/b", {"strategies": ["plain", "browser"]}, memory={"site.test": "browser"}
    )

    assert page.ok and calls == ["browser"]


def test_a_copy_from_elsewhere_is_never_remembered_as_the_way_in(
    strategies: dict[str, Callable[..., Any]],
) -> None:
    """Found live: g2.com was remembered as "archive", so every later crawl
    would have skipped the live site for an old snapshot."""
    calls: list[str] = []
    strategies["plain"] = _fake(DATADOME, 403, calls, "plain")
    strategies["archive"] = _fake(ARTICLE, 200, calls, "archive")
    memory = {"old.test": "archive"}  # written before this rule existed

    config = {"strategies": ["plain", "archive"]}
    first = webfetch.fetch("https://site.test/", config, memory=memory)
    again = webfetch.fetch("https://old.test/", config, memory=memory)

    assert first.strategy == again.strategy == "archive"
    assert "site.test" not in memory
    assert calls == ["plain", "archive", "plain", "archive"], "the live site is tried first"


def test_saved_pages_are_capped_and_only_saved_pages_go(tmp_path: Path) -> None:
    crawls = tmp_path / "crawls"
    crawls.mkdir()
    for index in range(5):
        page = crawls / f"{index}.md"
        page.write_text("x", encoding="utf-8")
        os.utime(page, (1000 + index, 1000 + index))
    (crawls / "strategies.json").write_text("{}", encoding="utf-8")

    removed = webfetch.prune_saved(crawls, keep=2)

    assert removed == 3
    assert sorted(path.name for path in crawls.iterdir()) == ["3.md", "4.md", "strategies.json"]


def test_a_timed_out_fetcher_takes_everything_it_started_with_it(tmp_path: Path) -> None:
    """Found live: a timed-out headless Chrome left four of its processes
    running. The grandchild here writes a marker after 4 s; the command is
    timed out after 1 s, so the marker must never appear."""
    marker = tmp_path / "survived"
    grandchild = "import sys, time; time.sleep(4); open(sys.argv[1], 'w').write('x')"
    child = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}, sys.argv[1]]); "
        "time.sleep(30)"
    )

    with pytest.raises(ValueError, match="did not finish in time"):
        webfetch.run_tree([sys.executable, "-c", child, str(marker)], 1)
    time.sleep(5)

    assert not marker.exists()


def test_when_nothing_is_ok_the_fullest_partial_page_is_kept(
    strategies: dict[str, Callable[..., Any]],
) -> None:
    strategies["wall"] = _fake(DATADOME, 403)
    strategies["shell"] = _fake(JS_SHELL.replace("<body>", "<body><p>Loading app</p>"))

    page = webfetch.fetch("https://site.test/", {"strategies": ["wall", "shell"]})

    assert not page.ok and page.strategy == "shell" and page.text == "Loading app"


def test_a_missing_page_only_goes_on_to_the_archive(
    strategies: dict[str, Callable[..., Any]],
) -> None:
    calls: list[str] = []
    strategies["plain"] = _fake("<p>no</p>", 404, calls, "plain")
    strategies["browser"] = _fake(ARTICLE, 200, calls, "browser")
    strategies["archive"] = _fake(ARTICLE, 200, calls, "archive")

    page = webfetch.fetch("https://site.test/gone", {"strategies": ["plain", "browser", "archive"]})

    assert calls == ["plain", "archive"] and page.strategy == "archive"


def test_a_private_address_never_goes_to_a_remote_copy(
    strategies: dict[str, Callable[..., Any]],
) -> None:
    calls: list[str] = []
    strategies["archive"] = _fake(ARTICLE, 200, calls, "archive")
    strategies["reader"] = _fake(ARTICLE, 200, calls, "reader")

    private = ("http://127.0.0.1:8080/", "http://localhost/a", "http://10.0.0.5/", "http://nas.local/")
    for url in private:
        page = webfetch.fetch(url, {"strategies": ["archive", "reader"]})
        assert not page.ok and {item.verdict for item in page.attempts} == {"skipped"}
    assert calls == []
    off = webfetch.fetch("https://site.test/", {"strategies": ["reader"], "allow_remote": False})
    assert not off.ok and calls == []


def test_a_strategy_that_cannot_run_here_is_skipped_and_errors_move_on(
    strategies: dict[str, Callable[..., Any]],
) -> None:
    def missing(url: str, timeout: float) -> Any:
        raise webfetch.Unavailable("not installed")

    def broken(url: str, timeout: float) -> Any:
        raise ValueError("connection reset")

    strategies["missing"] = missing
    strategies["broken"] = broken
    strategies["plain"] = _fake(ARTICLE)

    page = webfetch.fetch("https://site.test/", {"strategies": ["missing", "broken", "plain"]})

    assert page.ok and page.trail() == "broken error (connection reset) -> plain ok"
    assert page.attempts[0].reason == "not installed"


def test_the_deadline_stops_the_chain(strategies: dict[str, Callable[..., Any]]) -> None:
    calls: list[str] = []
    strategies["plain"] = _fake(ARTICLE, 200, calls, "plain")

    page = webfetch.fetch(
        "https://site.test/", {"strategies": ["plain"]}, deadline=time.monotonic() + 1
    )

    assert calls == [] and page.attempts[0].reason == "out of time"


def test_a_probe_tries_everything_and_names_the_best(
    strategies: dict[str, Callable[..., Any]],
) -> None:
    strategies["plain"] = _fake(DATADOME, 403)
    strategies["small"] = _fake("<html><title>S</title><p>" + "word " * 40 + "</p></html>")
    strategies["browser"] = _fake(ARTICLE)

    page = webfetch.fetch(
        "https://site.test/", {}, strategies=["plain", "small", "browser"], exhaustive=True
    )
    text = fetch_page.render_probe("https://site.test/", page)

    assert [item.verdict for item in page.attempts] == ["blocked", "ok", "ok"]
    assert page.strategy == "small", "the first good one is the page"
    assert "- plain: blocked -- DataDome bot wall, HTTP 403" in text
    assert 'Works: browser, small. Crawl with {"strategies": ["browser"]}' in text


class SpaSite:
    """A start page with no links -- its menu is JavaScript -- and a sitemap."""

    def __init__(self) -> None:
        pages: dict[str, tuple[int, str]] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                status, body = pages.get(self.path, (404, "<p>missing</p>"))
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        pages.update(
            {
                "/robots.txt": (200, f"User-agent: *\nSitemap: {self.base}/map.xml\n"),
                "/": (200, "<html><title>Shop</title><p>Welcome to the shop.</p></html>"),
                "/map.xml": (
                    200,
                    f"<urlset><url><loc>{self.base}/gold</loc></url>"
                    f"<url><loc>https://elsewhere.test/x</loc></url></urlset>",
                ),
                "/gold": (200, "<html><title>Gold</title><p>Gold is 2400.</p></html>"),
            }
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def spa(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[SpaSite]:
    monkeypatch.setenv("EVOMESH_SCRAPER", "")
    monkeypatch.chdir(tmp_path)
    site = SpaSite()
    yield site
    site.server.shutdown()


def _config(**overrides: Any) -> dict[str, Any]:
    return {
        **crawl_site.DEFAULTS,
        "delay_seconds": 0.0,
        "strategies": ["http"],
        "min_text_chars": 10,
        **overrides,
    }


def test_a_start_page_without_links_is_crawled_through_its_sitemap(
    spa: SpaSite, tmp_path: Path
) -> None:
    result = crawl_site.crawl({"url": f"{spa.base}/", "focus": ["gold"]}, _config())

    assert [page["url"] for page in result["pages"]] == [f"{spa.base}/", f"{spa.base}/gold"]
    assert result["discovered"] == "1 link(s) from the site's sitemap"
    assert result["pages"][1]["matches"] == ["Gold is 2400."]
    rendered = crawl_site.render(result)
    assert "Found through: 1 link(s) from the site's sitemap" in rendered
    assert f"{spa.base}/gold [via http]" in rendered
    memory = json.loads((tmp_path / "crawls" / "strategies.json").read_text(encoding="utf-8"))
    assert memory == {spa.base.removeprefix("http://"): "http"}


def test_feed_discovery_reads_only_real_feeds_and_their_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guessed /feed that 404s with an HTML page used to give up its
    stylesheet link as a page to crawl; an Atom feed its rel="self" link."""
    responses = {
        "https://s.test/feed": webfetch.Raw(
            body='<html><head><link rel="stylesheet" href="https://s.test/site.css"></head>'
            "<body>Not found</body></html>",
            status=404,
        ),
        "https://s.test/rss": webfetch.Raw(
            body='<html><link rel="icon" href="https://s.test/favicon.ico"></html>', status=200
        ),
        "https://s.test/atom.xml": webfetch.Raw(
            body='<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
            '<link rel="self" href="https://s.test/atom.xml"/>'
            '<link rel="hub" href="https://hub.test/"/>'
            '<entry><link rel="alternate" href="https://s.test/post-1"/></entry>'
            '<entry><link href="https://s.test/post-2"/></entry></feed>',
            status=200,
        ),
        "https://s.test/rss.xml": webfetch.Raw(
            body="<rss><channel><item><link>https://s.test/news-1</link></item></channel></rss>",
            status=200,
        ),
    }

    def fake_get(url: str, timeout: float, **_: Any) -> Any:
        return responses[url]

    monkeypatch.setattr(webfetch, "http_get", fake_get)

    links = webfetch.feed_links(list(responses), deadline=None)

    assert links == ["https://s.test/post-1", "https://s.test/post-2", "https://s.test/news-1"]


def test_a_crawl_that_reads_nothing_says_what_to_try_next(spa: SpaSite) -> None:
    result = crawl_site.crawl({"url": f"{spa.base}/nowhere", "discover": False}, _config())

    text = crawl_site.render(result)

    assert result["pages"] == []
    assert "http missing (HTTP 404)" in text
    assert '"probe": true' in text


def test_the_request_can_name_the_strategy_a_probe_found(
    spa: SpaSite, strategies: dict[str, Callable[..., Any]]
) -> None:
    calls: list[str] = []
    strategies["http"] = _fake(DATADOME, 403, calls, "http")
    strategies["stealth"] = _fake(ARTICLE, 200, calls, "stealth")

    result = crawl_site.crawl(
        {"url": f"{spa.base}/", "strategies": ["stealth"], "max_pages": 1},
        _config(strategies=["http", "stealth"]),
    )

    assert calls == ["stealth"] and result["pages"][0]["via"] == "stealth"


def test_a_tool_can_ask_for_more_time_than_shell_seconds(tmp_path: Path) -> None:
    path = tmp_path / "TOOL.md"
    text = "---\nname: slow\ndescription: d\ncommand: python x.py\ntimeout_seconds: {}\n---\n"

    assert parse_tool(path, text.format(210)).timeout_seconds == 210
    assert parse_tool(path, text.format(99999)).timeout_seconds == MAX_TOOL_SECONDS
    plain = parse_tool(path, "---\nname: a\ndescription: d\ncommand: c\n---\n")
    assert plain.timeout_seconds is None
    with pytest.raises(InvalidToolError):
        parse_tool(path, text.format("soon"))


async def test_the_tool_runs_with_its_own_timeout_and_is_told_it(tmp_path: Path) -> None:
    definition = ToolDefinition(
        name="budget",
        description="prints its time budget",
        command="python -c \"import os; print(os.environ['EVOMESH_TOOL_TIMEOUT'])\"",
        path=tmp_path / "TOOL.md",
        timeout_seconds=210,
    )
    context = ToolContext(root=tmp_path, shell_allow=frozenset({"python"}), shell_seconds=60)

    output = await build_custom_tool(definition).run(context, {})

    assert "210" in output


def test_the_crawl_budget_fits_inside_the_tool_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVOMESH_TOOL_TIMEOUT", "60")
    assert crawl_site.time_budget({"time_budget_seconds": 150}) == 45
    monkeypatch.setenv("EVOMESH_TOOL_TIMEOUT", "")
    assert crawl_site.time_budget({"time_budget_seconds": 150}) == 150


def test_a_copy_from_elsewhere_is_said_first() -> None:
    """Found live: the model answered from a three-day-old Wayback copy of
    g2.com and never said so -- the note came after the fetch trail."""
    note = "archived copy from 2026-10-06 (web.archive.org), not the live page"
    page = {"url": "https://g2.test/", "title": "CRM", "text": "Salesforce", "via": "archive"}

    text = crawl_site.render(
        {"pages": [{**page, "note": note}], "skipped": [], "errors": [], "fetch": []}
    )

    assert text.startswith(f"IMPORTANT, tell the human: {note}.")
