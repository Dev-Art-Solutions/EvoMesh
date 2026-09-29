"""Per-agent knowledge wiki and report journal.

The shape is Andrej Karpathy's "LLM wiki": instead of re-deriving what an
agent knows from raw notes on every question (what retrieval over memory.md
amounts to), the agent *compiles* what it learns into a small set of
Markdown pages it maintains itself, and answers from those.

Three layers, all plain Markdown beside memory.md for the reason rule 18
gives -- a human reads or edits any of them while the mesh runs:

``wiki/raw/``      immutable sources. memory.md compaction used to drop its
                   oldest entries for a one-line summary; they land here
                   verbatim first, so compaction no longer loses anything.
``wiki/pages/``    one page per topic, written by the agent (``wiki_write``)
                   and by a human. Pages link each other with ``[[page]]``.
``wiki/index.md``  one line per page -- ``- [[page]] -- summary (date)`` --
                   the catalog an agent reads *first*, and the only part that
                   rides in a cycle prompt (relevant lines, under the memory
                   budget -- see CycleContext.prompt).
``wiki/log.md``    append-only, one ``## [time] op | subject`` line per
                   ingest/write/lint, newest last.

Linting is code, never a model (rule 6): index drift is repaired, broken
links, orphans, stale and oversized pages are reported.

``reports.md`` (beside memory.md, not inside the wiki) is the report
journal: every report a recurring goal actually announced, newest last, so
"give me your last 10 analyses" is a file read -- not a model trying to
remember what it said, which is what NewsAnalyzer's hand-kept log was.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from evomesh.contracts import now_utc
from evomesh.harness_tools import Tool, ToolDenied

INDEX_HEADER = "# Knowledge index"
LOG_HEADER = "# Knowledge log"
REPORTS_HEADER = "# Reports"
# A page is something a small model reads in one step; past this it is
# really two topics, and lint says so rather than truncating it silently.
MAX_PAGE_CHARS = 6000
STALE_AFTER_DAYS = 30
LINK = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
INDEX_LINE = re.compile(
    r"^- \[\[([^\]]+)\]\]\s*(?:--|—)?\s*(.*?)\s*(?:\((\d{4}-\d{2}-\d{2})\))?\s*$"
)
REPORT_HEADING = re.compile(r"^## (\S+)(?: \| (.*))?$")


def _stamp(moment: datetime | None = None) -> str:
    return (moment or now_utc()).strftime("%Y-%m-%dT%H:%MZ")


def page_slug(title: str) -> str:
    """``"Gold / XAUUSD drivers"`` -> ``"gold-xauusd-drivers"``. The slug is
    the page's filename and its ``[[link]]`` name, so a model that writes
    the title a little differently next time still lands on the same page."""
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in title.strip().lower())
    return "-".join(part for part in cleaned.split("-") if part)[:80]


@dataclass(frozen=True)
class IndexEntry:
    slug: str
    summary: str
    updated: str = ""

    def render(self) -> str:
        date = f" ({self.updated})" if self.updated else ""
        return f"- [[{self.slug}]] -- {self.summary}{date}"


@dataclass(frozen=True)
class SearchHit:
    slug: str
    summary: str
    snippet: str
    score: int


@dataclass(frozen=True)
class Report:
    at: str
    text: str
    goal: str = ""


class AgentWiki:
    """One agent's wiki directory. Every method is synchronous file I/O --
    callers on the event loop wrap it in ``asyncio.to_thread``."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def pages_dir(self) -> Path:
        return self.directory / "pages"

    @property
    def raw_dir(self) -> Path:
        return self.directory / "raw"

    @property
    def index_path(self) -> Path:
        return self.directory / "index.md"

    @property
    def log_path(self) -> Path:
        return self.directory / "log.md"

    def page_path(self, slug: str) -> Path:
        return self.pages_dir / f"{slug}.md"

    # -- index ------------------------------------------------------------

    def entries(self) -> list[IndexEntry]:
        try:
            text = self.index_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        found: list[IndexEntry] = []
        for line in text.splitlines():
            match = INDEX_LINE.match(line.strip())
            if match:
                found.append(
                    IndexEntry(page_slug(match.group(1)), match.group(2), match.group(3) or "")
                )
        return found

    def _write_index(self, entries: list[IndexEntry]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        body = "\n".join(entry.render() for entry in sorted(entries, key=lambda e: e.slug))
        self.index_path.write_text(
            f"{INDEX_HEADER}\n\nOne line per page. Read this first; open a page with "
            f"wiki_read.\n\n{body}\n",
            encoding="utf-8",
        )

    def index_text(self) -> str:
        return "\n".join(entry.render() for entry in self.entries())

    # -- log --------------------------------------------------------------

    def log(self, op: str, subject: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            self.log_path.write_text(f"{LOG_HEADER}\n\n", encoding="utf-8")
        line = f"## [{_stamp()}] {op} | {' '.join(subject.split())[:200]}\n"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def recent_log(self, limit: int = 20) -> list[str]:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [line for line in lines if line.startswith("## [")][-limit:]

    # -- pages ------------------------------------------------------------

    def write_page(self, title: str, content: str, summary: str, *, source: str = "agent") -> str:
        """Create or replace one page and keep the index and log in step.

        Returns the slug. Raises ValueError on an empty title/content or a
        page over MAX_PAGE_CHARS -- a refusal the model can act on (split
        the topic) rather than a silent truncation of what it wrote."""
        slug = page_slug(title)
        if not slug:
            raise ValueError("a page needs a title with at least one letter or digit")
        body = content.strip()
        if not body:
            raise ValueError("a page needs content")
        if len(body) > MAX_PAGE_CHARS:
            raise ValueError(
                f"page is {len(body)} chars, over the {MAX_PAGE_CHARS} limit -- split it into "
                "two pages that link each other with [[page]]"
            )
        summary = " ".join(summary.split())[:160] or body.splitlines()[0][:160]
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        existed = self.page_path(slug).exists()
        heading = f"# {title.strip()}\n\n" if not body.startswith("#") else ""
        self.page_path(slug).write_text(f"{heading}{body}\n", encoding="utf-8")
        entries = [entry for entry in self.entries() if entry.slug != slug]
        entries.append(IndexEntry(slug, summary, now_utc().strftime("%Y-%m-%d")))
        self._write_index(entries)
        self.log("update" if existed else "create", f"{slug} ({source}): {summary}")
        return slug

    def read_page(self, title: str) -> str:
        slug = page_slug(title)
        try:
            return self.page_path(slug).read_text(encoding="utf-8")
        except FileNotFoundError:
            near = [hit.slug for hit in self.search(title, limit=3)]
            hint = f" Closest: {', '.join(near)}." if near else " The wiki has no pages yet."
            raise ValueError(f"no page '{slug}'.{hint}") from None

    def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        terms = {term for term in re.findall(r"[a-z0-9]{3,}", query.lower())}
        if not terms:
            return []
        summaries = {entry.slug: entry.summary for entry in self.entries()}
        hits: list[SearchHit] = []
        if not self.pages_dir.is_dir():
            return hits
        for path in sorted(self.pages_dir.glob("*.md")):
            slug = path.stem
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            lowered = text.lower()
            head = f"{slug} {summaries.get(slug, '')}".lower()
            score = sum(lowered.count(term) + 5 * head.count(term) for term in terms)
            if not score:
                continue
            snippet = next(
                (
                    line.strip()
                    for line in text.splitlines()
                    if line.strip() and any(term in line.lower() for term in terms)
                ),
                "",
            )
            hits.append(SearchHit(slug, summaries.get(slug, ""), snippet[:200], score))
        hits.sort(key=lambda hit: (-hit.score, hit.slug))
        return hits[:limit]

    # -- raw sources ------------------------------------------------------

    def archive_raw(self, lines: list[str], label: str) -> Path | None:
        """Append ``lines`` verbatim to this month's raw file. Raw files are
        only ever appended to, never rewritten -- they are the sources the
        pages were compiled from."""
        kept = [line for line in lines if line.strip()]
        if not kept:
            return None
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        path = self.raw_dir / f"{label}-{now_utc().strftime('%Y-%m')}.md"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {_stamp()}\n\n" + "\n".join(kept) + "\n")
        self.log("ingest", f"{len(kept)} {label} entries -> raw/{path.name}")
        return path

    # -- lint -------------------------------------------------------------

    def lint(self, *, fix: bool = True, today: datetime | None = None) -> list[str]:
        """Deterministic health check. With ``fix``, index drift (a page
        with no index line, an index line with no page) is repaired; the
        rest is only reported, because deciding what a page should say is
        the agent's job, not this function's."""
        issues: list[str] = []
        entries = self.entries()
        indexed = {entry.slug for entry in entries}
        pages = (
            {path.stem: path for path in self.pages_dir.glob("*.md")}
            if self.pages_dir.is_dir()
            else {}
        )
        missing_page = sorted(indexed - set(pages))
        unindexed = sorted(set(pages) - indexed)
        for slug in missing_page:
            issues.append(f"index lists [[{slug}]] but there is no page")
        for slug in unindexed:
            issues.append(f"page {slug} is not in the index")
        texts: dict[str, str] = {}
        for slug, path in pages.items():
            try:
                texts[slug] = path.read_text(encoding="utf-8")
            except OSError:
                texts[slug] = ""
        inbound: dict[str, int] = dict.fromkeys(pages, 0)
        for slug, text in texts.items():
            for target in {page_slug(link) for link in LINK.findall(text)}:
                if target == slug:
                    continue
                if target in inbound:
                    inbound[target] += 1
                else:
                    issues.append(f"{slug} links to [[{target}]], which does not exist")
            if len(text) > MAX_PAGE_CHARS + 200:
                issues.append(f"{slug} is {len(text)} chars -- split it")
        if len(pages) > 2:
            for slug, count in sorted(inbound.items()):
                if count == 0:
                    issues.append(f"{slug} is an orphan (no page links to it)")
        cutoff = ((today or now_utc()) - timedelta(days=STALE_AFTER_DAYS)).strftime("%Y-%m-%d")
        for entry in entries:
            if entry.updated and entry.updated < cutoff and entry.slug in pages:
                issues.append(f"{entry.slug} not updated since {entry.updated} -- still true?")
        if fix and (missing_page or unindexed):
            kept = [entry for entry in entries if entry.slug in pages]
            for slug in unindexed:
                first = next(
                    (
                        line.strip("# ").strip()
                        for line in texts.get(slug, "").splitlines()
                        if line.strip()
                    ),
                    slug,
                )
                kept.append(IndexEntry(slug, first[:160], now_utc().strftime("%Y-%m-%d")))
            self._write_index(kept)
        repaired = fix and bool(missing_page or unindexed)
        if issues:
            self.log("lint", f"{len(issues)} issue(s)" + (" (index repaired)" if repaired else ""))
        return issues


class ReportJournal:
    """``reports.md`` -- every report a recurring goal announced, newest
    last. Appended by AgentRuntime._apply on the same branch that calls
    announce(), so what is in here is exactly what a human was sent."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, text: str, *, goal: str = "") -> None:
        body = text.strip()
        if not body:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(f"{REPORTS_HEADER}\n\n", encoding="utf-8")
        goal_part = f" | {' '.join(goal.split())[:80]}" if goal else ""
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"## {_stamp()}{goal_part}\n\n{body}\n\n")

    def recent(self, limit: int = 10) -> list[Report]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        reports: list[Report] = []
        current: Report | None = None
        buffer: list[str] = []
        for line in lines:
            match = REPORT_HEADING.match(line)
            if match:
                if current is not None:
                    reports.append(Report(current.at, "\n".join(buffer).strip(), current.goal))
                current = Report(match.group(1), "", match.group(2) or "")
                buffer = []
            elif current is not None:
                buffer.append(line)
        if current is not None:
            reports.append(Report(current.at, "\n".join(buffer).strip(), current.goal))
        return reports[-limit:] if limit > 0 else []


def render_reports(reports: list[Report]) -> str:
    if not reports:
        return "No reports yet."
    return "\n\n".join(f"[{report.at}]\n{report.text}" for report in reversed(reports))


def build_knowledge_tools(
    wiki: AgentWiki, journal: ReportJournal, *, writer: str
) -> tuple[Tool, ...]:
    """wiki_search / wiki_read / wiki_write / recent_reports for one agent.

    They work on the agent's own wiki directory, never on the job root: a
    job rooted in a candidate or a project must not be able to reach
    another agent's knowledge, and does not need a filesystem grant for its
    own. Descriptions are short on purpose -- the schemas ride on every
    turn of every job, uncounted (see CLAUDE.md rule 14)."""

    async def search(context: Any, args: dict[str, Any]) -> str:
        del context
        hits = await asyncio.to_thread(wiki.search, str(args.get("query") or ""), 5)
        if not hits:
            index = await asyncio.to_thread(wiki.index_text)
            return f"no match. index:\n{index}" if index else "the wiki has no pages yet"
        return "\n".join(f"[[{hit.slug}]] {hit.summary} | {hit.snippet}" for hit in hits)

    async def read(context: Any, args: dict[str, Any]) -> str:
        del context
        try:
            return await asyncio.to_thread(wiki.read_page, str(args.get("page") or ""))
        except ValueError as exc:
            raise ToolDenied(f"DENIED: {exc}") from exc

    async def write(context: Any, args: dict[str, Any]) -> str:
        del context
        try:
            slug = await asyncio.to_thread(
                wiki.write_page,
                str(args.get("page") or ""),
                str(args.get("content") or ""),
                str(args.get("summary") or ""),
                source=writer,
            )
        except ValueError as exc:
            raise ToolDenied(f"DENIED: {exc}") from exc
        return f"saved [[{slug}]]"

    async def reports(context: Any, args: dict[str, Any]) -> str:
        del context
        try:
            limit = max(1, min(50, int(args.get("limit") or 10)))
        except (TypeError, ValueError):
            limit = 10
        return render_reports(await asyncio.to_thread(journal.recent, limit))

    return (
        Tool(
            "wiki_search",
            "Search your knowledge wiki (what you learned before). Use it before "
            "researching something again.",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            search,
        ),
        Tool(
            "wiki_read",
            "Read one page of your knowledge wiki by name.",
            {
                "type": "object",
                "properties": {"page": {"type": "string"}},
                "required": ["page"],
            },
            read,
        ),
        Tool(
            "wiki_write",
            "Create or replace a wiki page with durable knowledge worth keeping (facts, "
            "how-tos, conclusions). Merge with what the page already says; link related "
            "pages as [[page]].",
            {
                "type": "object",
                "properties": {
                    "page": {"type": "string"},
                    "content": {"type": "string", "description": "Markdown"},
                    "summary": {"type": "string", "description": "One line for the index"},
                },
                "required": ["page", "content", "summary"],
            },
            write,
        ),
        Tool(
            "recent_reports",
            "Your own last reports (analyses) as sent to the human, newest first.",
            {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "default 10"}},
            },
            reports,
        ),
    )
