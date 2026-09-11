"""The NewsWatcher's deterministic keyword watcher (watch_news.py) -- a
script polled on its own interval, never the agent's LLM cycle, that must
stay silent unless a headline it has not already reported matches a
configured keyword. This is exactly the behavior the agent was originally
missing: a BDI goal reported "still reading config.json" as progress on
every cycle, spamming a human with nothing "genuine match" ever meant."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "agent-templates" / "news-watcher"
WATCH_SCRIPT_PATH = TEMPLATE_DIR / "scripts" / "watch_news.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("watch_news_script", WATCH_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


watch_news = _load_module()

RSS_FEED = (
    b"<?xml version='1.0'?><rss><channel>"
    b"<item><title>Gold hits a new high</title><link>https://example.com/gold-1</link></item>"
    b"<item><title>Unrelated market update</title><link>https://example.com/other-1</link></item>"
    b"</channel></rss>"
)


def _fake_news_fetch_module(config: dict):
    class Fake:
        DEFAULT_FEEDS = ["https://example.com/feed.rss"]

        @staticmethod
        def _load_config():
            return config

        @staticmethod
        def _fetch(url):
            return RSS_FEED

        @staticmethod
        def _parse_source(url, raw):
            import xml.etree.ElementTree as ET

            root = ET.fromstring(raw)
            return [
                {
                    "title": item.findtext("title") or "",
                    "link": item.findtext("link") or "",
                    "published": "",
                }
                for item in root.iter("item")
            ]

    return Fake()


def test_stays_silent_with_no_keywords_configured(tmp_path, capsys):
    with patch.object(watch_news, "STATE_PATH", tmp_path / "state.json"), patch.object(
        watch_news, "_load_news_fetch", lambda: _fake_news_fetch_module({"keywords": []})
    ):
        assert watch_news.main() == 0

    assert capsys.readouterr().out == ""


def test_prints_only_a_genuinely_new_keyword_match(tmp_path, capsys):
    config = {"keywords": ["gold"]}
    with patch.object(watch_news, "STATE_PATH", tmp_path / "state.json"), patch.object(
        watch_news, "_load_news_fetch", lambda: _fake_news_fetch_module(config)
    ):
        assert watch_news.main() == 0
        first_output = capsys.readouterr().out
        assert "Gold hits a new high" in first_output
        assert "Unrelated market update" not in first_output

        # Second tick, same feed, same match already seen: must stay silent.
        assert watch_news.main() == 0
        assert capsys.readouterr().out == ""


def test_state_file_remembers_seen_links_across_runs(tmp_path):
    state_path = tmp_path / "state.json"
    config = {"keywords": ["gold"]}
    with patch.object(watch_news, "STATE_PATH", state_path), patch.object(
        watch_news, "_load_news_fetch", lambda: _fake_news_fetch_module(config)
    ):
        watch_news.main()

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert "https://example.com/gold-1" in saved["seen"]
