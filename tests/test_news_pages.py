"""NewsWatcher's "pages": a whole page (wsj.com's stocks, which has no RSS
and turns away anything but a real browser) fetched through Scrapling,
every headline on it reported once -- no matter how a story recurs. The
Scrapling stand-in here writes a fixture where the real one would write the
page, so the subprocess path runs without a browser."""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "agent-templates" / "news-watcher"
PAGE = "https://www.wsj.com/finance/stocks?page=1"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


news_fetch = _load(
    "news_fetch_pages", TEMPLATE_DIR / "tools" / "news_fetch" / "scripts" / "news_fetch.py"
)
watch_news = _load("watch_news_pages", TEMPLATE_DIR / "scripts" / "watch_news.py")

FAKE_SCRAPLING = """
import pathlib, sys
_extract, mode, url, output, *flags = sys.argv[1:]
here = pathlib.Path(__file__).parent
with open(here / "calls.log", "a", encoding="utf-8") as log:
    log.write(" ".join([mode, url, *flags]) + "\\n")
pathlib.Path(output).write_text((here / "page.html").read_text(encoding="utf-8"), encoding="utf-8")
"""


def _page(*items: tuple[str, str]) -> str:
    """A page shaped like wsj.com's: its schema.org ItemList (lowercase
    "itemList", as WSJ writes it) inside the page's one ld+json block."""
    data = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "mainEntity": {
            "@type": "itemList",
            "itemListElement": [
                {"@type": "ListItem", "name": name, "position": index, "url": url}
                for index, (name, url) in enumerate(items, start=1)
            ],
        },
    }
    return (
        '<html><head><script type="application/ld+json">'
        f"{json.dumps(data)}</script></head><body>Most Popular</body></html>"
    )


@pytest.fixture
def playground(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The agent's playground as the watcher sees it: its cwd, holding the
    config.json that wins, with Scrapling faked and every feed offline."""
    scraper = tmp_path / "fake"
    scraper.mkdir()
    (scraper / "scrapling.py").write_text(FAKE_SCRAPLING, encoding="utf-8")
    command = [sys.executable, str(scraper / "scrapling.py")]
    monkeypatch.setenv("EVOMESH_SCRAPER", json.dumps(command))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps({"feeds": ["https://feed.invalid/rss"], "pages": [PAGE], "page_minutes": 30}),
        encoding="utf-8",
    )

    def offline(url: str) -> bytes:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(news_fetch, "_fetch", offline)
    monkeypatch.setattr(watch_news, "_load_news_fetch", lambda: news_fetch)
    monkeypatch.setattr(watch_news, "STATE_PATH", tmp_path / "watch_state.json")
    return scraper


def _serve(playground: Path, html: str) -> None:
    (playground / "page.html").write_text(html, encoding="utf-8")


def _calls(playground: Path) -> list[str]:
    log = playground / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []


def test_the_whole_page_is_parsed_once_each_in_order() -> None:
    items = news_fetch.parse_page(
        _page(
            ("Stocks Rise", "https://www.wsj.com/finance/stocks/stocks-rise-de1cba89?mod=hp"),
            ("Oil Tumbles", "https://www.wsj.com/finance/stocks/oil-tumbles-c1af7222"),
            ("Stocks Rise", "https://www.wsj.com/finance/stocks/stocks-rise-again-0badf00d"),
            ("Stocks Rise Again", "https://www.wsj.com/finance/stocks/re-slugged-de1cba89"),
        )
    )

    assert [item["title"] for item in items] == ["Stocks Rise", "Oil Tumbles"]
    assert items[0]["link"] == "https://www.wsj.com/finance/stocks/stocks-rise-de1cba89"


def test_scrapling_s_own_python_fetches_the_page_off_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scrapling installed as a venv: its Python runs page_fetch.py, a
    headed Chrome placed off screen -- no window ever pops up."""
    monkeypatch.delenv("EVOMESH_NO_BROWSER")
    (tmp_path / "scrapling.exe").write_bytes(b"")
    (tmp_path / "python.exe").write_bytes(b"")
    output = tmp_path / "page.html"

    command = news_fetch._page_command([str(tmp_path / "scrapling.exe")], PAGE, output, {})

    assert command[0] == str(tmp_path / "python.exe")
    assert Path(command[1]).name == "page_fetch.py" and Path(command[1]).is_file()
    assert command[2:] == [PAGE, str(output)]


def test_the_test_suite_never_launches_the_real_browser(tmp_path: Path) -> None:
    """conftest sets EVOMESH_NO_BROWSER: the real install is refused."""
    (tmp_path / "scrapling.exe").write_bytes(b"")

    with pytest.raises(OSError, match="EVOMESH_NO_BROWSER"):
        news_fetch._page_command([str(tmp_path / "scrapling.exe")], PAGE, tmp_path / "o", {})


def test_page_fetch_keeps_the_browser_headed_but_off_screen() -> None:
    scripts = TEMPLATE_DIR / "tools" / "news_fetch" / "scripts"
    page_fetch = _load("page_fetch_script", scripts / "page_fetch.py")

    flags = page_fetch._off_screen(("--mute-audio", "--start-maximized", "--window-position=0,0"))

    assert flags == ("--mute-audio", "--window-position=-32000,-32000")


def test_without_its_python_a_page_goes_through_scrapling_s_cli(playground: Path) -> None:
    _serve(playground, _page(("Stocks Rise", "https://www.wsj.com/a/stocks-rise-de1cba89")))

    items = news_fetch.fetch_pages([PAGE], news_fetch._load_config())

    assert [item["title"] for item in items] == ["Stocks Rise"]
    assert items[0]["source"] == PAGE
    (call,) = _calls(playground)
    assert call.startswith(f"stealthy-fetch {PAGE} ")
    assert "--real-chrome" in call and "--no-headless" in call


def test_a_page_is_fetched_at_most_every_page_minutes(playground: Path) -> None:
    _serve(playground, _page(("Stocks Rise", "https://www.wsj.com/a/stocks-rise-de1cba89")))
    config = news_fetch._load_config()

    first = news_fetch.fetch_pages([PAGE], config)
    again = news_fetch.fetch_pages([PAGE], config)

    assert again == first, "between fetches, the last result stands in"
    assert len(_calls(playground)) == 1
    news_fetch.fetch_pages([PAGE], config, force=True)
    assert len(_calls(playground)) == 2


def test_the_watcher_reports_the_whole_page_then_only_what_is_new(
    playground: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _serve(
        playground,
        _page(
            ("Stocks Rise", "https://www.wsj.com/a/stocks-rise-de1cba89"),
            ("Oil Tumbles", "https://www.wsj.com/a/oil-tumbles-c1af7222"),
        ),
    )
    assert watch_news.main() == 0
    first = capsys.readouterr().out.splitlines()
    assert first == [
        "Stocks Rise (https://www.wsj.com/a/stocks-rise-de1cba89)",
        "Oil Tumbles (https://www.wsj.com/a/oil-tumbles-c1af7222)",
    ], "no keywords are needed: a page is reported whole"

    # Due again: one new story, one old story re-slugged, one re-headlined.
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    config["page_minutes"] = 0.0001
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    _serve(
        playground,
        _page(
            ("Stocks Rise, Then Fall", "https://www.wsj.com/a/stocks-rise-then-fall-de1cba89"),
            ("Oil Tumbles", "https://www.wsj.com/a/oil-story-moved-11112222"),
            ("Fed Holds Rates", "https://www.wsj.com/a/fed-holds-rates-5cbde123"),
        ),
    )
    assert watch_news.main() == 0

    assert capsys.readouterr().out.splitlines() == [
        "Fed Holds Rates (https://www.wsj.com/a/fed-holds-rates-5cbde123)"
    ]
    assert len(_calls(playground)) == 2
    assert watch_news.main() == 0
    assert capsys.readouterr().out == "", "nothing new, nothing said"


def test_a_failed_page_fetch_is_silent_and_keeps_the_last_result(
    playground: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(playground, _page(("Stocks Rise", "https://www.wsj.com/a/stocks-rise-de1cba89")))
    news_fetch.fetch_pages([PAGE], news_fetch._load_config())
    (playground / "page.html").unlink()  # the stand-in now fails

    items = news_fetch.fetch_pages([PAGE], news_fetch._load_config(), force=True)

    assert [item["title"] for item in items] == ["Stocks Rise"]
    assert "page fetch failed" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "win32", reason="the hidden desktop is Windows-only")
def test_on_windows_the_page_fetch_runs_on_a_hidden_desktop(
    playground: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off screen still left a taskbar button: the whole fetch runs on a
    desktop that is never displayed instead."""
    ran: list[list[str]] = []

    def hidden(argv: list[str], timeout: float) -> int:
        ran.append(argv)
        Path(argv[-1]).write_text(
            _page(("Stocks Rise", "https://www.wsj.com/a/stocks-rise-de1cba89")), encoding="utf-8"
        )
        return 0

    def command(scraper: list[str], url: str, output: Path, config: dict) -> list[str]:
        return ["py", "page_fetch.py", url, str(output)]

    monkeypatch.setattr(news_fetch, "_page_command", command)
    monkeypatch.setattr(news_fetch, "_run_hidden", hidden)

    html = news_fetch._fetch_page(PAGE, {})

    assert len(ran) == 1 and ran[0][2] == PAGE
    assert [item["title"] for item in news_fetch.parse_page(html)] == ["Stocks Rise"]
