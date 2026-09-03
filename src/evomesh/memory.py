"""File-backed agent memory.

Every agent owns two Markdown files under the workspace:

``memory.md``   durable facts it has learned, compacted when it outgrows its budget;
``context.md``  volatile working notes for the goal it is on right now.

Both are plain Markdown on purpose: a human can read them, edit them, or delete
them while the mesh is running, and the agent picks the change up on its next
cycle. Everything is budgeted in characters because the models this runs on have
small context windows -- an unbounded memory file is what makes an agent lose
its memory halfway through a goal, since the prompt then gets truncated by the
model server rather than by us.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from evomesh.contracts import AgentDefinition, now_utc

Summarizer = Callable[[str], Awaitable[str]]

MEMORY_HEADER = "# Memory"
CONTEXT_HEADER = "# Context"
SUMMARY_SECTION = "## Summary"
RECENT_SECTION = "## Recent"
TRUNCATION_MARK = "_[older entries compacted]_"


def _stamp(moment: datetime | None = None) -> str:
    return (moment or now_utc()).strftime("%Y-%m-%dT%H:%MZ")


def clip(text: str, budget: int, *, keep: str = "tail") -> str:
    """Trim text to a character budget on a line boundary."""
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    lines = text.splitlines()
    kept: list[str] = []
    size = 0
    for line in reversed(lines) if keep == "tail" else lines:
        if size + len(line) + 1 > budget:
            break
        kept.append(line)
        size += len(line) + 1
    if keep == "tail":
        kept.reverse()
        return "\n".join([TRUNCATION_MARK, *kept]) if kept else text[-budget:]
    return "\n".join([*kept, TRUNCATION_MARK]) if kept else text[:budget]


@dataclass
class MemoryBudget:
    memory_chars: int = 4000
    context_chars: int = 2000
    inbox_chars: int = 1200
    beliefs_chars: int = 900
    prompt_chars: int = 8000


class AgentMemory:
    """The pair of Markdown files that make one agent's mind persistent."""

    def __init__(
        self,
        root: Path,
        definition: AgentDefinition,
        budget: MemoryBudget | None = None,
    ) -> None:
        self.root = root
        self.definition = definition
        self.budget = budget or MemoryBudget()

    @property
    def directory(self) -> Path:
        return self.root / "agents" / self.definition.slug

    @property
    def memory_path(self) -> Path:
        return self.directory / "memory.md"

    @property
    def context_path(self) -> Path:
        return self.directory / "context.md"

    @property
    def playground_path(self) -> Path:
        """Where a non-system agent's harness jobs default to.

        Alongside memory.md and context.md rather than a separate top-level
        setting: an agent already has exactly one directory that is its own,
        and a place to freely read and write is one more thing that belongs
        there, not a new tree to configure.
        """
        return self.directory / "playground"

    async def ensure_playground(self) -> Path:
        await asyncio.to_thread(self.playground_path.mkdir, parents=True, exist_ok=True)
        return self.playground_path

    async def ensure(self) -> None:
        await asyncio.to_thread(self._ensure_sync)

    def _ensure_sync(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if not self.memory_path.exists():
            self.memory_path.write_text(
                f"{MEMORY_HEADER} - {self.definition.name}\n\n"
                f"Purpose: {self.definition.purpose}\n\n"
                f"{SUMMARY_SECTION}\n\n"
                "Nothing compacted yet.\n\n"
                f"{RECENT_SECTION}\n\n",
                encoding="utf-8",
            )
        if not self.context_path.exists():
            self.context_path.write_text(
                f"{CONTEXT_HEADER} - {self.definition.name}\n\nNo cycle has run yet.\n",
                encoding="utf-8",
            )

    async def read_memory(self, budget: int | None = None) -> str:
        text = await asyncio.to_thread(self._read, self.memory_path)
        return clip(text, self.budget.memory_chars if budget is None else budget)

    async def read_context(self, budget: int | None = None) -> str:
        text = await asyncio.to_thread(self._read, self.context_path)
        return clip(text, self.budget.context_chars if budget is None else budget)

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    async def remember(self, fact: str, *, source: str = "cycle") -> bool:
        """Append one durable fact. Duplicates are dropped, so cycles may be noisy."""
        cleaned = " ".join(fact.split()).strip()
        if not cleaned or cleaned.upper() in {"NONE", "N/A", "NOTHING", "-"}:
            return False
        return await asyncio.to_thread(self._remember_sync, cleaned, source)

    def _remember_sync(self, fact: str, source: str) -> bool:
        self._ensure_sync()
        current = self.memory_path.read_text(encoding="utf-8")
        if fact in current:
            return False
        entry = f"- {_stamp()} ({source}) {fact}\n"
        if RECENT_SECTION in current:
            head, _, tail = current.partition(RECENT_SECTION)
            current = f"{head}{RECENT_SECTION}{tail.rstrip()}\n{entry}"
        else:
            current = f"{current.rstrip()}\n\n{RECENT_SECTION}\n\n{entry}"
        self.memory_path.write_text(current, encoding="utf-8")
        return True

    async def write_context(self, sections: dict[str, str]) -> None:
        body = "\n".join(
            f"## {title}\n\n{value.strip()}\n"
            for title, value in sections.items()
            if value and value.strip()
        )
        text = (
            f"{CONTEXT_HEADER} - {self.definition.name}\n\n"
            f"Updated {_stamp()}. Rewritten every cycle; safe to edit between cycles.\n\n"
            f"{body}"
        )
        await asyncio.to_thread(self._write_context_sync, text)

    def _write_context_sync(self, text: str) -> None:
        self._ensure_sync()
        self.context_path.write_text(clip(text, self.budget.context_chars * 3), encoding="utf-8")

    async def compact(self, summarizer: Summarizer | None = None) -> bool:
        """Fold the oldest Recent entries into Summary once the file is over budget.

        The model is asked to summarise when one is available, but a model that is
        down or that returns junk must never cost the agent its memory, so the
        fallback keeps the newest entries verbatim.
        """
        raw = await asyncio.to_thread(self._read, self.memory_path)
        if len(raw) <= self.budget.memory_chars:
            return False
        head, _, recent = raw.partition(RECENT_SECTION)
        entries = [line for line in recent.splitlines() if line.strip().startswith("- ")]
        if len(entries) < 4:
            return False
        keep = entries[len(entries) - max(3, len(entries) // 3) :]
        overflow = entries[: len(entries) - len(keep)]
        summary = ""
        if summarizer is not None and overflow:
            try:
                summary = (await summarizer("\n".join(overflow))).strip()
            except (RuntimeError, ValueError, TimeoutError):
                summary = ""
        if not summary:
            summary = f"{len(overflow)} older entries compacted on {_stamp()}."
        previous = ""
        if SUMMARY_SECTION in head:
            before, _, existing = head.partition(SUMMARY_SECTION)
            previous = existing.strip()
            if previous.startswith("Nothing compacted yet."):
                previous = ""
            head = before
        merged = clip(f"{previous}\n{summary}".strip(), max(200, self.budget.memory_chars // 2))
        rebuilt = (
            f"{head.rstrip()}\n\n{SUMMARY_SECTION}\n\n{merged}\n\n"
            f"{RECENT_SECTION}\n\n" + "\n".join(keep) + "\n"
        )
        await asyncio.to_thread(self._write_memory_sync, rebuilt)
        return True

    def _write_memory_sync(self, text: str) -> None:
        self.memory_path.write_text(text, encoding="utf-8")


class WorldContext:
    """workspace/context.md -- the shared world state every agent can read."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def path(self) -> Path:
        return self.root / "context.md"

    async def write(self, sections: dict[str, str]) -> None:
        body = "\n".join(
            f"## {title}\n\n{value.strip()}\n"
            for title, value in sections.items()
            if value and value.strip()
        )
        text = (
            "# EvoMesh world context\n\n"
            f"Generated by the environment at {_stamp()}. Overwritten on every refresh.\n\n"
            f"{body}"
        )
        await asyncio.to_thread(self._write_sync, text)

    def _write_sync(self, text: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    async def read(self, budget: int = 1200) -> str:
        def _read() -> str:
            try:
                return self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return ""

        return clip(await asyncio.to_thread(_read), budget, keep="head")
