from datetime import timedelta
from pathlib import Path

import pytest

from evomesh.config import Settings
from evomesh.console import ConsoleChannel
from evomesh.contracts import AgentDefinition, now_utc
from evomesh.environment import Environment
from evomesh.harness_tools import ToolContext, ToolDenied
from evomesh.knowledge import (
    MAX_PAGE_CHARS,
    AgentWiki,
    ReportJournal,
    build_knowledge_tools,
    page_slug,
)
from evomesh.memory import AgentMemory, MemoryBudget
from evomesh.models import MockProvider


def test_page_slug_is_stable_across_spellings() -> None:
    assert page_slug("Gold / XAUUSD drivers") == "gold-xauusd-drivers"
    assert page_slug("  gold xauusd  DRIVERS ") == "gold-xauusd-drivers"
    assert page_slug("!!!") == ""


def test_write_page_keeps_index_and_log_in_step(tmp_path: Path) -> None:
    wiki = AgentWiki(tmp_path / "wiki")
    slug = wiki.write_page("Gold drivers", "Real yields down -> gold up.", "What moves gold")
    assert slug == "gold-drivers"
    assert wiki.read_page("gold drivers").startswith("# Gold drivers")
    entries = wiki.entries()
    assert [entry.slug for entry in entries] == ["gold-drivers"]
    assert entries[0].summary == "What moves gold"
    wiki.write_page("Gold drivers", "Updated.", "Still what moves gold")
    assert [entry.summary for entry in wiki.entries()] == ["Still what moves gold"]
    log = wiki.recent_log()
    assert "create | gold-drivers" in log[0]
    assert "update | gold-drivers" in log[1]


def test_write_page_refuses_empty_and_oversized(tmp_path: Path) -> None:
    wiki = AgentWiki(tmp_path / "wiki")
    with pytest.raises(ValueError, match="content"):
        wiki.write_page("x", "  ", "s")
    with pytest.raises(ValueError, match="split"):
        wiki.write_page("big", "a" * (MAX_PAGE_CHARS + 1), "s")


def test_read_missing_page_suggests_near_matches(tmp_path: Path) -> None:
    wiki = AgentWiki(tmp_path / "wiki")
    wiki.write_page("ECB rate decisions", "The ECB meets every six weeks.", "ECB calendar")
    with pytest.raises(ValueError, match="ecb-rate-decisions"):
        wiki.read_page("ECB meetings")


def test_search_ranks_title_hits_first(tmp_path: Path) -> None:
    wiki = AgentWiki(tmp_path / "wiki")
    wiki.write_page("Oil", "Mentions gold once.", "Crude oil")
    wiki.write_page("Gold", "Gold is gold.", "Gold notes")
    hits = wiki.search("gold")
    assert [hit.slug for hit in hits] == ["gold", "oil"]


def test_lint_repairs_index_drift_and_reports_links(tmp_path: Path) -> None:
    wiki = AgentWiki(tmp_path / "wiki")
    wiki.write_page("A", "See [[b]] and [[missing]].", "a")
    wiki.write_page("B", "Back to [[a]].", "b")
    # A human drops a page in by hand and deletes another.
    (wiki.pages_dir / "c.md").write_text("# C page\n\nstandalone\n", encoding="utf-8")
    wiki.page_path("b").unlink()
    issues = wiki.lint()
    assert any("[[b]] but there is no page" in issue for issue in issues)
    assert any("c is not in the index" in issue for issue in issues)
    assert any("[[missing]]" in issue for issue in issues)
    assert sorted(entry.slug for entry in wiki.entries()) == ["a", "c"]
    assert [entry.summary for entry in wiki.entries() if entry.slug == "c"] == ["C page"]


def test_lint_flags_stale_pages(tmp_path: Path) -> None:
    wiki = AgentWiki(tmp_path / "wiki")
    wiki.write_page("Old", "x", "old")
    issues = wiki.lint(today=now_utc() + timedelta(days=60))
    assert any("not updated since" in issue for issue in issues)


def test_report_journal_returns_newest_last(tmp_path: Path) -> None:
    journal = ReportJournal(tmp_path / "reports.md")
    for number in range(12):
        journal.append(f"XAUUSD bullish (high): headline {number} -- reason", goal="watch")
    recent = journal.recent(10)
    assert len(recent) == 10
    assert recent[-1].text.startswith("XAUUSD bullish (high): headline 11")
    assert recent[0].text.startswith("XAUUSD bullish (high): headline 2")
    assert recent[0].goal == "watch"
    journal.append("   ")
    assert len(journal.recent(50)) == 12


async def test_compaction_archives_what_it_folds_away(tmp_path: Path) -> None:
    definition = AgentDefinition(name="Analyst", purpose="p", model_name="m")
    memory = AgentMemory(tmp_path, definition, MemoryBudget(memory_chars=400, prompt_chars=800))
    await memory.ensure()
    for number in range(30):
        await memory.remember(f"fact number {number} about the gold market")
    assert await memory.compact(None)
    raw = list(memory.wiki.raw_dir.glob("memory-*.md"))
    assert len(raw) == 1
    archived = raw[0].read_text(encoding="utf-8")
    assert "fact number 0 about" in archived
    assert "ingest" in memory.wiki.recent_log()[-1]


async def test_knowledge_tools_round_trip(tmp_path: Path) -> None:
    wiki = AgentWiki(tmp_path / "wiki")
    journal = ReportJournal(tmp_path / "reports.md")
    journal.append("EURUSD bearish (medium): ECB cut -- lower rates")
    tools = {tool.name: tool for tool in build_knowledge_tools(wiki, journal, writer="Analyst")}
    context = ToolContext(root=tmp_path)
    assert await tools["wiki_search"].run(context, {"query": "gold"}) == (
        "the wiki has no pages yet"
    )
    saved = await tools["wiki_write"].run(
        context, {"page": "Gold", "content": "Safe haven.", "summary": "gold basics"}
    )
    assert saved == "saved [[gold]]"
    assert "Safe haven." in await tools["wiki_read"].run(context, {"page": "gold"})
    assert "[[gold]] gold basics" in await tools["wiki_search"].run(context, {"query": "haven"})
    with pytest.raises(ToolDenied):
        await tools["wiki_read"].run(context, {"page": "nope"})
    assert "ECB cut" in await tools["recent_reports"].run(context, {"limit": "x"})


async def test_reports_and_wiki_commands(tmp_path: Path) -> None:
    settings = Settings(
        data_path=tmp_path / "data.db",
        generation_path=tmp_path / "generations",
        workspace_path=tmp_path / "workspace",
    )
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    try:
        agent = AgentDefinition(name="Analyst", purpose="p", model_name="mock-model")
        await environment.register_agent(agent)
        console = ConsoleChannel(environment)
        assert "no reports yet" in await console.route("/reports Analyst")
        memory = environment.memory_for(agent)
        for number in range(3):
            memory.reports.append(f"report {number}")
        out = await console.route("/reports Analyst 2")
        assert "last 2 report(s)" in out
        assert out.index("report 2") < out.index("report 1")
        assert "report 0" not in out
        # A private bot's conversation is locked to its agent: no name needed.
        locked = ConsoleChannel(environment, locked_agent_id=agent.id)
        assert "report 2" in await locked.route("/reports")
        only_one = await locked.route("/reports 1")
        assert "last 1 report(s)" in only_one and "report 1" not in only_one
        memory.wiki.write_page("Gold", "Safe haven.", "gold basics")
        assert "[[gold]] -- gold basics" in await console.route("/wiki Analyst")
        assert "Safe haven." in await console.route("/wiki Analyst gold")
        assert "[[gold]]" in await console.route("/wiki search Analyst haven")
        assert "healthy" in await console.route("/wiki lint Analyst")
        tools = {tool.name for tool in environment.builtin_tools_for(agent)}
        assert {"wiki_search", "wiki_read", "wiki_write", "recent_reports"} <= tools
        assert "send_email" not in tools
        assert environment.builtin_tools_for(environment.registry.get("evolver")) == ()
    finally:
        await environment.stop()


async def test_compaction_leaves_room_before_the_next_one(tmp_path: Path) -> None:
    """Found live: halves for summary and kept entries rebuilt memory.md at
    the budget, so every new line compacted again -- a model call a cycle."""
    calls: list[str] = []

    async def summarize(text: str) -> str:
        calls.append(text)
        return "summary of older facts " * 80

    definition = AgentDefinition(name="Analyst", purpose="p", model_name="m")
    memory = AgentMemory(tmp_path, definition, MemoryBudget(memory_chars=3000, prompt_chars=6000))
    await memory.ensure()
    for number in range(60):
        await memory.remember(f"fact number {number} about the euro and the fed, with detail")
    assert await memory.compact(summarize)
    for number in range(5):
        await memory.remember(f"a new fact {number} learned after compaction, also detailed")
        assert not await memory.compact(summarize), f"compacted again after {number + 1} line(s)"
    assert len(calls) == 1
