"""crawl_records: a listing's records, page by page, into one CSV -- found in
the JSON the page is drawn from, never in a menu it happens to embed."""

from __future__ import annotations

import csv
import html
import importlib.util
import json
import sys
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_TOOLS = ROOT / "agent-templates" / "web-crawler" / "tools"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


crawl_records = _load(
    TEMPLATE_TOOLS / "crawl_records" / "scripts" / "crawl_records.py", "cr_crawl_records"
)


def _project(number: int) -> dict[str, Any]:
    return {
        "id": number,
        "name": f"Project {number}",
        "blurb": "A thing — with Šejić's art",
        "goal": 1000.0,
        "deadline": 1795455199,
        "creator": {"id": 7, "name": "Maker"},
        "category": {"name": "Tabletop Games", "parent_name": "Games"},
        "location": {"displayable_name": "Örebro, Sweden"},
        "urls": {"web": {"project": f"/projects/maker/p{number}?ref=discovery"}},
        "tags": ["a", "b"],
    }


# Kickstarter's discover page, as seen 2026-11-01: the HTML embeds a menu as
# JSON (more entries than a page of projects), and ?format=json answers with
# the projects next to an aggregation and has_more.
MENU = json.dumps({"onOurRadar": [
    {"name": f"Tag {n}", "url": f"/discover/tags/t{n}", "id": n, "slug": f"t{n}"}
    for n in range(14)
]})
LISTING_HTML = (
    "<html><head><title>Discover</title></head><body><div id='root'></div>"
    f"<script type='application/json'>{MENU}</script>"
    "<p>Dice Throne Nexus</p></body></html>"
)


class Listing:
    """Three pages of four projects; page numbers past it are a 404."""

    def __init__(self, *, pages: int = 3, wrap: bool = False) -> None:
        self.seen: list[dict[str, list[str]]] = []
        listing = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's name
                parts = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parts.query)
                if parts.path == "/robots.txt":
                    return self._send(200, "User-agent: *\nDisallow: /private\n", "text/plain")
                if parts.path != "/discover":
                    return self._send(404, "<p>no</p>")
                if query.get("format") != ["json"]:
                    return self._send(200, LISTING_HTML)
                listing.seen.append(query)
                page = int(query.get("page", ["1"])[0])
                if page > pages:
                    return self._send(404, "<html><body><p>Not found</p></body></html>")
                body = json.dumps({
                    "projects": [_project(page * 10 + n) for n in range(4)],
                    "total_hits": 999,
                    "seed": 42,
                    "aggregations": {"categories": [
                        {"id": n, "count": 3, "name": f"c{n}"} for n in range(30)
                    ]},
                    "has_more": True,
                })
                if wrap:  # as headless Chrome dumps a JSON response
                    body = f"<html><head></head><body><pre>{html.escape(body)}</pre></body></html>"
                    return self._send(200, body)
                return self._send(200, body, "application/json")

            def _send(self, status: int, body: str, kind: str = "text/html") -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", f"{kind}; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Listing]:
    monkeypatch.chdir(tmp_path)
    site = Listing()
    yield site
    site.server.shutdown()


def _config(**overrides: Any) -> dict[str, Any]:
    return {
        **crawl_records.crawl_site.DEFAULTS,
        "delay_seconds": 0.0,
        "strategies": ["http"],
        "min_text_chars": 10,
        **overrides,
    }


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_the_records_are_the_list_with_the_most_cells_not_the_most_items() -> None:
    """Found live: by item count the 14-entry tag menu won, then by top-level
    keys the 174-entry category aggregation did -- over 12 projects."""
    body = json.dumps({
        "projects": [_project(n) for n in range(3)],
        "aggregations": {"categories": [{"id": n, "name": "c"} for n in range(20)]},
        "has_more": True, "seed": 9,
    })

    items, meta, where, _ = crawl_records.records_in(body)

    assert where == "projects" and len(items) == 3
    assert meta == {"has_more": True, "seed": 9}


def test_json_a_browser_shows_inside_pre_is_still_json() -> None:
    body = json.dumps({"items": [{"id": 1, "a": "x&y"}, {"id": 2, "a": "z"}]})
    shown = f"<html><head></head><body><pre>{html.escape(body)}</pre></body></html>"

    items, _, where, _ = crawl_records.records_in(shown)

    assert crawl_records.is_data(shown)
    assert where == "items" and items[0]["a"] == "x&y"


def test_cards_with_a_json_data_attribute_are_records() -> None:
    cards = "".join(
        f'<div data-project="{html.escape(json.dumps({"id": n, "name": f"P{n}"}))}"></div>'
        for n in range(3)
    )

    items, _, _, _ = crawl_records.records_in(f"<html><body>{cards}</body></html>")

    assert [item["name"] for item in items] == ["P0", "P1", "P2"]


def test_a_record_flattens_to_dotted_columns_with_its_link_and_readable_times() -> None:
    row = crawl_records.with_url(
        crawl_records.flatten(_project(1)), "https://www.kickstarter.com/discover?format=json"
    )

    assert row["creator.name"] == "Maker"
    assert row["location.displayable_name"] == "Örebro, Sweden"
    assert row["tags"] == "a; b"
    assert row["deadline_utc"] == "2026-11-23 17:33"
    assert row["url"] == "https://www.kickstarter.com/projects/maker/p1?ref=discovery"


def test_an_html_listing_is_read_through_its_json_twin_page_by_page(listing: Listing) -> None:
    result = crawl_records.collect({"url": f"{listing.base}/discover?sort=magic"}, _config())
    saved, total, added, width = crawl_records.store(result, {"file": "ks"})

    assert result["source"].endswith("format=json")
    assert result["stopped"].startswith("the site has no page 4 (HTTP 404)")
    assert result["next"] is None
    assert [p[:2] for p in result["pages"]] == [(1, 4), (2, 4), (3, 4), (4, 0)]
    # The seed page 1 answered with keeps a shuffled order still across pages.
    assert all(query["seed"] == ["42"] for query in listing.seen[1:])
    assert (saved.name, total, added) == ("ks.csv", 12, 12)
    rows = _rows(saved)
    assert list(rows[0])[:4] == ["id", "name", "url", "blurb"]
    assert width == len(rows[0])
    assert rows[0]["url"] == f"{listing.base}/projects/maker/p10?ref=discovery"
    assert rows[0]["blurb"] == "A thing — with Šejić's art"


def test_a_long_listing_continues_into_the_same_file(listing: Listing) -> None:
    first = crawl_records.collect({"url": f"{listing.base}/discover", "max_pages": 2}, _config())
    crawl_records.store(first, {"file": "ks"})
    asked = {"url": f"{listing.base}/discover", "file": "ks", "max_pages": 2}
    text = crawl_records.render(first, asked, Path.cwd() / "x", 8, 8, 1)
    follow = json.loads(text.split("call crawl_records again with: ", 1)[1].splitlines()[0])

    follow["max_pages"] = 5
    second = crawl_records.collect(follow, _config())
    saved, total, added, _ = crawl_records.store(second, follow)
    again = crawl_records.store(second, follow)

    assert follow["start_page"] == 3 and follow["pin"] == {"seed": "42"}
    assert [p[0] for p in second["pages"]] == [3, 4]
    assert (total, added) == (12, 4)
    assert again[1:3] == (12, 0)
    assert len({row["id"] for row in _rows(saved)}) == 12


def test_a_listing_chrome_dumped_is_read_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    site = Listing(pages=1, wrap=True)
    try:
        result = crawl_records.collect({"url": f"{site.base}/discover?format=json"}, _config())
    finally:
        site.server.shutdown()

    assert len(result["records"]) == 4 and result["where"] == "projects"


def test_robots_txt_is_honoured(listing: Listing) -> None:
    result = crawl_records.collect({"url": f"{listing.base}/private/list"}, _config())

    assert result["records"] == [] and result["stopped"] == "robots.txt disallows this URL"


def test_a_page_with_no_records_says_so_and_points_at_crawl_site(listing: Listing) -> None:
    result = crawl_records.collect({"url": f"{listing.base}/nowhere"}, _config())
    text = crawl_records.render(result, {}, None, 0, 0, 0)

    assert text.startswith("No records found") and "crawl_site" in text


def test_the_installed_copy_matches_the_template() -> None:
    for name in ("TOOL.md", "scripts/crawl_records.py"):
        template = TEMPLATE_TOOLS / "crawl_records" / name
        installed = ROOT / "tools" / "crawl_records" / name
        assert installed.read_bytes() == template.read_bytes(), name


def test_a_page_without_records_mid_listing_is_tried_the_remaining_ways(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Found live: page 1 came through curl, page 2 met a wall there and the
    next "ok" was a text copy with no data -- the crawl stopped at page 2."""
    monkeypatch.chdir(tmp_path)
    webfetch = crawl_records.webfetch
    calls: list[tuple[str, int]] = []

    def listing(page: int) -> str:
        if page > 3:
            return ""
        return json.dumps({"projects": [_project(page * 10 + n) for n in range(4)]})

    def number(url: str) -> int:
        return int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["page"][0])

    def fast(url: str, timeout: float) -> Any:
        page = number(url)
        calls.append(("fast", page))
        if page == 2:  # walled for this one, this time
            return webfetch.Raw(body="<html><body>" + "<p>words</p>" * 30 + "</body></html>",
                                url=url, status=200)
        if not listing(page):
            return webfetch.Raw(body="<p>Not found</p>", url=url, status=404)
        return webfetch.Raw(body=listing(page), url=url, status=200)

    def slow(url: str, timeout: float) -> Any:
        calls.append(("slow", number(url)))
        return webfetch.Raw(body=listing(number(url)), url=url, status=200)

    def reader(url: str, timeout: float) -> Any:
        calls.append(("reader", number(url)))
        raise AssertionError("a Markdown copy is never a way to read records")

    monkeypatch.setattr(webfetch, "STRATEGIES", {"fast": fast, "reader": reader, "slow": slow})
    config = _config(strategies=["fast", "reader", "slow"], respect_robots=False)

    result = crawl_records.collect(
        {"url": "https://list.test/discover?format=json&page=1", "max_pages": 10}, config
    )

    assert len(result["records"]) == 12
    # What got page 2 is remembered for the site and tried first from then on.
    assert calls[:4] == [("fast", 1), ("fast", 2), ("slow", 2), ("slow", 3)]
    assert not any(name == "reader" for name, _ in calls)
    assert result["stopped"].startswith("the site has no page 4")
