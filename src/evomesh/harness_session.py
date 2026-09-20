"""The record of one harness job, written while it runs.

One JSONL file per job. Every model turn, tool call and tool result is appended
and flushed as it happens, so a job that hangs, is killed, or takes the process
down with it still leaves the whole story up to that moment -- which is the only
state in which anyone ever wants to read one of these.

It is a plain file for the reason memory.md and context.md are plain files: the
first thing a human does with a run that went wrong is read it, and a .jsonl can
be tailed while a row in a database cannot.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .humanize import humanize_duration
from .metrics import mean
from .verdict_label import VERDICT_LABELS


class HarnessSession:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.entries: list[dict[str, Any]] = []
        self.elapsed_values: list[float] = []
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def record(self, kind: str, **fields: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "kind": kind,
            **fields,
        }
        if "elapsed" in fields:
            self.elapsed_values.append(float(fields["elapsed"]))
            entry["humanize_duration"] = humanize_duration(fields["elapsed"])
            entry["mean_elapsed"] = humanize_duration(mean(self.elapsed_values))
        if kind == "verdict" and "code" in fields:
            entry["label"] = VERDICT_LABELS.get(fields["code"], fields["code"])
        self.entries.append(entry)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                handle.flush()
        return entry

    def kinds(self) -> list[str]:
        return [entry["kind"] for entry in self.entries]


# How many session transcripts are kept on disk. Found live alongside the
# generations/ backlog: 10168 of these, one per harness job ever run, none
# ever removed -- a process meant to run indefinitely cannot let a resource
# grow forever just because nothing was ever told to stop it.
SESSION_RETENTION = 500


def next_session_path(directory: Path, *, keep: int = SESSION_RETENTION) -> Path:
    """The next free ``000001.jsonl`` in ``directory``.

    Numbered rather than timestamped so a sorted listing is chronological on
    every platform, and so the number can be spoken: "job 7 went wrong".

    Prunes the oldest transcripts beyond ``keep`` first -- every job's own
    verdict and diff already lands in the generation's mutation log or
    MUTATION_OBJECTIVE.md, which is what a human actually reads back later;
    the raw turn-by-turn transcript is for debugging a job while it is
    fresh, not a permanent record.
    """
    directory.mkdir(parents=True, exist_ok=True)
    numbered = sorted(
        (int(path.stem), path) for path in directory.glob("*.jsonl") if path.stem.isdigit()
    )
    overflow = max(0, len(numbered) - keep)
    for _, path in numbered[:overflow]:
        path.unlink(missing_ok=True)
    used = {number for number, _ in numbered[overflow:]}
    return directory / f"{(max(used) + 1 if used else 1):06d}.jsonl"
