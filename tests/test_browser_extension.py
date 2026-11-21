"""The extension's two halves, each where it really runs.

- page_agent.js in real headless Chrome, on local fixture pages that behave
  like a modern app: a single-page search with pushState and late results,
  late-loading buttons, infinite scroll, a modal overlay, a login form.
- background.js under Node against a mocked chrome.* API: tabs, navigation
  and its failures, chunked replies, EvoMesh-owned tabs, screenshots.

Skipped where Chrome or Node is not installed (both are on GitHub's
ubuntu-latest runners). The extension itself is never loaded: branded
Chrome 137+ ignores --load-extension, and installing it any other way
without a human is exactly what this project must not do.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evomesh.browser import find_chrome

from .browser_fixture import run_fixture

needs_chrome = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")


@pytest.fixture(scope="module")
def agent_page() -> dict[str, Any]:
    if find_chrome() is None:
        pytest.skip("Chrome is not installed")
    return run_fixture("agent.html")


def error(result: dict[str, Any]) -> str:
    assert result["success"] is False, result
    return result["error"]["code"]


@needs_chrome
def test_a_snapshot_is_compact_and_numbered(agent_page: dict[str, Any]) -> None:
    snapshot = agent_page["snapshot"]
    items = snapshot["elements"]["items"]
    refs = [item["ref"] for item in items]

    assert snapshot["title"] == "Fixture shop"
    assert len(refs) == len(set(refs)) and all(":e" in ref for ref in refs)
    assert {"role": "button", "name": "Go"}.items() <= next(
        i for i in items if i["name"] == "Go"
    ).items()
    assert any(item["role"] == "combobox" and item["name"] == "Size" for item in items)
    assert not any(item["name"] == "Secret action" for item in items), (
        "hidden elements are left out"
    )
    assert len(json.dumps(snapshot)) < 8000


@needs_chrome
def test_secrets_never_leave_the_page(agent_page: dict[str, Any]) -> None:
    dump = json.dumps(agent_page)
    assert "hunter2" not in dump
    assert "tok-123" not in dump
    password = next(
        i for i in agent_page["snapshot"]["elements"]["items"] if i.get("type") == "password"
    )
    assert password["value_redacted"] is True
    assert "value" not in agent_page["password_attrs"]["attributes"]


@needs_chrome
def test_targets_resolve_by_meaning_not_only_selectors(agent_page: dict[str, Any]) -> None:
    assert agent_page["by_text"]["role"] == "link" and agent_page["by_text"]["name"] == "Read more"
    assert agent_page["by_label"]["role"] == "combobox"
    assert agent_page["by_placeholder"]["name"] == "Search"
    assert agent_page["nth"]["success"] is True


@needs_chrome
def test_ambiguous_hidden_disabled_missing_and_stale_are_errors_not_guesses(
    agent_page: dict[str, Any],
) -> None:
    assert error(agent_page["ambiguous"]) == "ambiguous_target"
    assert agent_page["ambiguous"]["error"]["count"] == 2
    assert len(agent_page["ambiguous"]["error"]["matches"]) == 2
    assert error(agent_page["hidden"]) == "element_hidden"
    assert error(agent_page["disabled"]) == "element_disabled"
    assert error(agent_page["missing"]) == "element_not_found"
    assert error(agent_page["stale"]) == "stale_ref"
    assert error(agent_page["other_epoch"]) == "stale_ref"
    assert error(agent_page["obscured"]) == "element_obscured"
    assert error(agent_page["no_target"]) == "invalid_request"
    assert error(agent_page["bad_css"]) == "invalid_request"


@needs_chrome
def test_there_is_no_way_to_run_arbitrary_code(agent_page: dict[str, Any]) -> None:
    assert error(agent_page["unknown"]) == "invalid_request"
    assert error(agent_page["bad_key"]) == "invalid_request"


@needs_chrome
def test_forms_controls_and_a_single_page_app(agent_page: dict[str, Any]) -> None:
    assert agent_page["select"]["selected"] == {"value": "m", "label": "Medium"}
    assert error(agent_page["select_disabled"]) == "element_disabled"
    assert agent_page["check"]["checked"] is True
    assert agent_page["uncheck"]["checked"] is False
    assert error(agent_page["file_input_refused"]) == "not_supported"
    assert error(agent_page["checkbox_not_text"]) == "invalid_request"
    # The page's own input listener saw every keystroke (what React needs).
    inputs = [event for event in agent_page["events"] if event.startswith("input:")]
    assert inputs[-1] == "input:python tutorials"
    assert agent_page["enter"]["submitted"] is True
    assert agent_page["url"].endswith("?q=python%20tutorials")
    links = agent_page["results_links"]["links"]["items"]
    assert [link["text"] for link in links] == [
        "Python tutorial one",
        "Python tutorial two",
        "Python tutorial three",
    ]


@needs_chrome
def test_waits_are_bounded_and_see_late_content(agent_page: dict[str, Any]) -> None:
    assert agent_page["wait_late"]["success"] is True
    assert 0 < agent_page["wait_late"]["waited_ms"] < 5000
    assert agent_page["wait_results"]["success"] is True
    scroll = agent_page["scroll"]
    assert scroll["new_content"] is True and scroll["height_after"] > scroll["height_before"]


@needs_chrome
def test_a_bot_check_asks_for_a_human_and_is_not_worked_around() -> None:
    page = run_fixture("captcha.html")
    assert page["snapshot"]["human_required"] == "a CAPTCHA is on the page"
    assert "human" in page["read_page"]["human_required"] or page["read_page"]["human_required"]


@needs_node
def test_the_background_worker_against_a_mocked_chrome() -> None:
    script = Path(__file__).parent / "js" / "background.test.mjs"
    completed = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert result["ping"]["extension_id"] == "fgefcelipaiabacacgjcdkgachkdchcm"
    assert result["missing_tab"]["error"]["code"] == "tab_not_found"
    assert result["close_not_own"]["error"]["code"] == "permission_denied"
    assert result["close_own"]["success"] is True
    assert result["navigate"]["load"] == "complete"
    assert result["navigate_fail"]["error"]["code"] == "navigation_failed"
    assert result["back"]["url"].endswith("/previous")
    assert result["inactive_shot"]["error"]["code"] == "tab_not_active"
    assert result["shot"]["chunks"] > 1, "a large screenshot is sent in pieces"
    assert result["max_message"] < 1024 * 1024, "every native message stays under Chrome's 1 MB"
    assert result["unknown"]["error"]["code"] == "invalid_request"
    assert result["wait_nav"]["error"]["code"] == "navigation_timeout"
    assert result["listeners_left"] == {"updated": 0, "errors": 0}, "listeners leak"
