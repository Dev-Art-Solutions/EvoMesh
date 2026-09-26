"""The web-crawler template: crawl a real (local) site politely, schedule its
own recurring tasks through the mesh's control port, and deliver results
only to endpoints a human configured."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from evomesh.config import Settings
from evomesh.contracts import GoalStatus
from evomesh.control import ControlServer
from evomesh.environment import Environment
from evomesh.harness_tools import ToolContext, build_custom_tool
from evomesh.models import MockProvider
from evomesh.tools import ToolDefinition

TEMPLATE = Path(__file__).resolve().parents[1] / "agent-templates" / "web-crawler"


def _script(tool: str) -> ModuleType:
    path = TEMPLATE / "tools" / tool / "scripts" / f"{tool}.py"
    spec = importlib.util.spec_from_file_location(f"crawler_{tool}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


crawl_site = _script("crawl_site")
crawl_schedule = _script("crawl_schedule")
send_results = _script("send_results")

PAGES = {
    "/robots.txt": ("text/plain", "User-agent: *\nDisallow: /private\n"),
    "/": (
        "text/html",
        "<html><head><title>Home</title><script>var x = 'gold';</script></head><body>"
        "<p>Welcome to the shop.</p><a href='/a'>A</a> <a href='/private/x'>secret</a>"
        " <a href='https://elsewhere.invalid/'>away</a></body></html>",
    ),
    "/a": (
        "text/html",
        "<html><title>A</title><body><p>Gold price rises today.</p>"
        "<p>Nothing golden here.</p><a href='/b#top'>B</a></body></html>",
    ),
    "/b": ("text/html", "<html><title>B</title><body><p>Silver is flat.</p></body></html>"),
    "/private/x": ("text/html", "<p>Gold hidden</p>"),
}


class Site:
    """A real HTTP server on localhost, serving PAGES and recording POSTs."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the http.server contract
                kind, body = PAGES.get(self.path, ("text/html", ""))
                if self.path not in PAGES:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", f"{kind}; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                site.posts.append(
                    {
                        "path": self.path,
                        "token": self.headers.get("X-Token"),
                        "body": json.loads(self.rfile.read(length)),
                    }
                )
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    # The Scrapling CLI's shape, played by a small script: CI has no Scrapling.
    fake = Path(__file__).with_name("fake_scrapling.py")
    monkeypatch.setenv("EVOMESH_SCRAPER", json.dumps([sys.executable, str(fake)]))
    server = Site()
    yield server
    server.close()


REAL_SCRAPLING = Path(__file__).resolve().parents[1] / ".runtime/scrapling/Scripts/scrapling.exe"


@pytest.mark.skipif(not REAL_SCRAPLING.is_file(), reason="Scrapling is not installed here")
def test_the_real_fetcher_crawls_the_site(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVOMESH_SCRAPER", str(REAL_SCRAPLING))
    config = {**crawl_site.DEFAULTS, "delay_seconds": 0.0}

    result = crawl_site.crawl({"url": f"{site.base}/", "focus": ["gold"]}, config)

    assert [page["url"] for page in result["pages"]][:2] == [f"{site.base}/", f"{site.base}/a"]
    assert any("robots" in item for item in result["skipped"])
    assert result["pages"][1]["matches"][0].startswith("Gold price rises today.")


def test_without_a_fetcher_the_crawl_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVOMESH_SCRAPER", "")

    with pytest.raises(crawl_site.NoFetcher):
        crawl_site.crawl({"url": "https://example.com/"}, dict(crawl_site.DEFAULTS))


def test_a_crawl_stays_on_the_site_honours_robots_and_finds_the_focus(site: Site) -> None:
    config = {**crawl_site.DEFAULTS, "delay_seconds": 0.0}

    result = crawl_site.crawl({"url": f"{site.base}/", "focus": ["gold"]}, config)

    urls = [page["url"] for page in result["pages"]]
    assert urls == [f"{site.base}/", f"{site.base}/a", f"{site.base}/b"]
    assert any("/private/x" in item and "robots" in item for item in result["skipped"])
    home, first = result["pages"][0], result["pages"][1]
    assert "var x" not in home["text"], "scripts are not page text"
    assert home["matches"] == []
    assert len(first["matches"]) == 1, "one match: whole words, not 'golden'"
    assert first["matches"][0].startswith("Gold price rises today.")


def test_the_answer_fits_the_harness_and_shows_matches_first(
    site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found live: as indented JSON the page text was one 4000-character line
    before the matches, the harness cut it, and the model saw no content."""
    from evomesh.harness_tools import ToolLimits, _clip  # pyright: ignore[reportPrivateUsage]

    monkeypatch.chdir(tmp_path)
    config = {**crawl_site.DEFAULTS, "delay_seconds": 0.0}
    result = crawl_site.crawl({"url": f"{site.base}/", "focus": ["gold"]}, config)
    result["pages"][0]["text"] = "long navigation text " * 2000  # a real page's bulk
    saved = crawl_site.save_full(result, {"url": site.base}).relative_to(tmp_path).as_posix()

    text = crawl_site.render(result, saved)
    shown = _clip(text, ToolLimits(), unit="lines")

    assert len(text) <= crawl_site.OUTPUT_BUDGET
    assert "- Gold price rises today." in shown, "the match survives the harness's clip"
    assert shown.index("Matches:") < shown.index("Excerpt:")
    assert saved.startswith("crawls/") and saved in shown
    assert "long navigation text" in (tmp_path / saved).read_text(encoding="utf-8")


def test_max_pages_and_follow_bound_the_crawl(site: Site) -> None:
    config = {**crawl_site.DEFAULTS, "delay_seconds": 0.0}

    only_one = crawl_site.crawl({"url": f"{site.base}/", "max_pages": 1}, config)
    followed = crawl_site.crawl({"url": f"{site.base}/", "follow": ["/a"]}, config)

    assert len(only_one["pages"]) == 1 and "max_pages=1" in only_one["note"]
    assert [page["url"] for page in followed["pages"]] == [f"{site.base}/", f"{site.base}/a"]


def test_results_go_only_to_configured_endpoints_and_are_always_kept(
    site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    hook = {"url": f"{site.base}/hook", "headers": {"X-Token": "t"}}
    (tmp_path / "config.json").write_text(json.dumps({"endpoints": {"hook": hook}}), "utf-8")

    reply = send_results.run(
        {
            "task": "Gold news",
            "results": [{"title": "Gold price rises today."}],
            "to": ["hook", f"{site.base}/steal"],
        }
    )

    assert "hook: HTTP 204" in reply
    assert "refused" in reply, "a raw URL is never a destination"
    assert [post["path"] for post in site.posts] == ["/hook"]
    assert site.posts[0]["token"] == "t"
    assert site.posts[0]["body"]["results"] == [{"title": "Gold price rises today."}]
    saved = list((tmp_path / "results").glob("*-gold-news.json"))
    assert len(saved) == 1


async def test_custom_tools_are_told_which_agent_is_calling(tmp_path: Path) -> None:
    definition = ToolDefinition(
        name="whoami",
        description="prints the calling agent",
        command="python -c \"import os; print(os.environ['EVOMESH_AGENT_ID'])\"",
        path=tmp_path / "TOOL.md",
    )
    tool = build_custom_tool(definition)
    context = ToolContext(root=tmp_path, agent_id="crawler-7", shell_allow=frozenset({"python"}))

    output = await tool.run(context, {})

    assert "crawler-7" in output


async def test_the_crawler_schedules_its_own_tasks_through_the_control_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    await environment.agent_templates.install_directory(TEMPLATE)
    crawler = await environment.agent_templates.instantiate(environment, "web-crawler")
    control = ControlServer(environment, asyncio.Event(), "127.0.0.1", 0)
    await control.start()
    assert control._server is not None  # pyright: ignore[reportPrivateUsage]
    port = control._server.sockets[0].getsockname()[1]  # pyright: ignore[reportPrivateUsage]
    config = {**crawl_schedule.DEFAULTS, "control_port": port}

    def call(request: dict[str, Any]) -> str:
        return crawl_schedule.run(request, crawler.id, config)

    added = await asyncio.to_thread(
        call,
        {"action": "add", "task": "Crawl https://example.com/jobs for Python roles", "every": "2h"},
    )
    too_often = await asyncio.to_thread(
        call, {"action": "add", "task": "Crawl https://example.com every minute", "every": "1m"}
    )
    listed = await asyncio.to_thread(call, {"action": "list"})

    assert "Added goal" in added
    assert "Refused" in too_often
    task = next(goal for goal in crawler.mind.goals if goal.description.startswith("[crawl]"))
    assert task.interval_seconds == 7200 and task.recurring and task.notify
    assert task.id in listed

    removed = await asyncio.to_thread(call, {"action": "remove", "goal_id": task.id})
    assert task.status in {GoalStatus.CANCELLED, GoalStatus.FAILED, GoalStatus.DONE}, removed
    await control.stop()
    await environment.stop()


async def test_the_template_spawns_with_its_tools_and_skill(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    await environment.agent_templates.install_directory(TEMPLATE)

    crawler = await environment.agent_templates.instantiate(environment, "web-crawler")

    assert crawler.identity == "Crawler"
    assert crawler.harness_root != ""
    assert {"crawl_site", "crawl_schedule", "send_results"} <= {
        tool.name for tool in environment.tools.discover()
    }
    assert "web-crawling" in {skill.name for skill in environment.skills.discover()}
    await environment.stop()
