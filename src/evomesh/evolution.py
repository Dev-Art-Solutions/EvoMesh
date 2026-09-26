from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from evomesh.codebase import (
    IMPROVEMENTS_FILE,
    REPAIR_RULES,
    RUNTIME_LOG,
    SCOUT_NEEDLE,
    Improvement,
    Module,
    RuntimeFault,
    append_item,
    backlog_objective,
    backlog_target,
    drop_improvements,
    fabricated_references,
    failure_excerpts,
    improvement_needle,
    improvement_objective,
    item_from_answer,
    named_code,
    new_orphans,
    open_improvements,
    plan_needle,
    plan_objective,
    plan_task,
    project_map,
    protected_changes,
    runtime_fault_needle,
    runtime_fault_objective,
    runtime_faults,
    scout_modules,
    scout_needle,
    scout_objective,
    scout_task,
    step_needle,
    step_objective,
    step_task,
    steps_from_answer,
    stray_root_files,
    tick_improvement,
    tick_step,
    untested_objective,
    untested_target,
    vet_new_improvements,
    vet_plan,
    warning_leads,
    write_planned_steps,
    write_test_task,
)
from evomesh.coordination import WorkItem
from evomesh.git import GitError, GitIdentity, GitRepository, PublishPolicy
from evomesh.improvements import (
    DISCOVERY_SOURCE,
    EVIDENCE_FAILING_TESTS,
    EVIDENCE_HUMAN_BACKLOG,
    EVIDENCE_RUNTIME_FAULT,
    RUNTIME_SOURCE,
    Candidate,
    ExecutionScope,
    Observation,
    PriorityFactors,
    WorkHandle,
    WorkInspection,
    WorkOutcome,
    WorkState,
    work_handle,
)
from evomesh.improvements import Improvement as TrackedImprovement
from evomesh.models import ModelProvider
from evomesh.processes import run_command, without_virtual_env
from evomesh.storage import SQLiteRepository

logger = logging.getLogger(__name__)

PIPELINE_STATE_KEY = "evolution.pipeline"

# Where each generation explains itself. Tracked, not ignored: the reasoning
# behind a change has to travel with the change.
BACKLOG_DIR = Path("docs") / "evolution"

# Found live, repeatedly: a job burns several of its steps on `ls`/`read`
# against `docs/evolution/plans` (which does not exist until this job writes
# into it) and on `shell: ls` (denied -- `shell` only runs `python`, per
# harness.shell_allow), before ever trying the `ls`/`read`/`grep` tools that
# actually work. On a 12-step budget that alone can eat the job before it
# writes anything. Said once, up front, in every stage that explores the tree.
TOOL_USAGE_HINT = (
    "- Use the `ls`, `read` and `grep` tools directly to explore the tree -- "
    "`shell` only runs `python`, nothing else, so `shell: ls` (or any other "
    "unix command) is always denied. `docs/evolution/plans` does not exist "
    "until you write the first file into it, so `ls`/`read` against it "
    "report 'does not exist', not a permissions problem -- that is expected, "
    "not a reason to retry the same call.\n"
    "- Do not read AGENTS.md, CLAUDE.md or README.md -- those are onboarding "
    "for a human or coding assistant extending the harness itself, not "
    "needed to write this file, and reading them (found live: one job read "
    "AGENTS.md twice, verbatim, then ran out of budget having written "
    "nothing) can burn the whole step budget before you reach the actual "
    "work. Everything this task needs is already above, in THE PACKAGE AS "
    "IT STANDS, or in the source files it names."
)

# What a generation is asked for, now that the asking goes to an agent that can
# read the project rather than to one prompt which had to carry all of it.
HARNESS_RULES = "\n".join(
    (
        "Rules for this project:",
        "- Change a module that ALREADY RUNS. A brand new file is almost always "
        "the wrong answer: nothing imports it, so none of its code executes, and "
        "the validation suite fails any module nothing imports.",
        "- Wiring one of the DEAD modules above into a load-bearing one is real "
        "work, and is worth more than another new file.",
        "- Read a file before you change it. Use edit, not write, for a file that "
        "already exists, and keep each change as small as the objective allows.",
        "- `old` must be copied, not recalled. Found live: a job read cognition.py "
        "three times, then wrote an `old` for a whole class -- with a docstring "
        "and a property neither named `reliable` -- that appears nowhere in the "
        "file it had just read, and the denial arrived with no budget left to "
        "retry. The longer `old` is, the more likely you are to misremember a "
        "word of it -- keep it to the few lines you are actually changing, "
        "copied character-for-character from your most recent `read` of that "
        "exact file, never reconstructed from what the function is supposed to do.",
        "- If you created a file that turns out to be wrong -- a scratch script, "
        "a false start, a file the hygiene check names -- use delete to remove "
        "it. Leaving it behind is not an option: a candidate cannot land code "
        "nothing runs, so the file has to go, or be wired into something that "
        "already does.",
        "- Everything you touch must stay valid Python: ruff, pyright and pytest "
        "are run against your work as soon as you are finished -- automatically, "
        "in a separate stage, once you stop calling tools. Do not try to run them "
        "yourself: `shell` only runs the harness's own bare Python interpreter, "
        "which has none of those installed and cannot run this project's suite "
        "(found live: a job spent its entire budget on `python -m pytest` and "
        "`import pytest` failing with 'No module named pytest', and never wrote "
        "a single edit). Spend your steps reading and editing, not probing "
        "whether you can test your own change -- you cannot, and do not need to.",
        "- Searching is not the work. Found live: a job re-read the same two or "
        "three files and re-ran near-identical greps a dozen times hunting for "
        "the perfect place to wire something in, and ran out of budget having "
        "never called edit once. If you catch yourself re-reading a file you've "
        "already read, or re-running a grep with only the pattern tweaked, stop "
        "searching -- commit to editing the best candidate site you have already "
        "found. A real, small, imperfect edit beats an unlimited search for a "
        "perfect one that runs out of steps and lands nothing.",
        "- Stay inside this directory. It is a disposable copy, not the running mesh.",
        "- Noticed a different problem on the way? Do not fix it here -- "
        "that is scope creep. Name it on its own line starting exactly with "
        "'PROPOSAL:' (what is wrong, and in which file); it goes to the backlog.",
        "- End your final answer with one sentence starting exactly with "
        "'RATIONALE:' explaining what you changed and why -- it is the only "
        "record of your reasoning that survives into this generation's history.",
    )
)


def harness_objective(objective: str, project: str, context: str = "") -> str:
    """What the Evolver asks the harness for, in the harness's own terms.

    The map still goes first, because the rules refer to it -- "the DEAD modules
    above" needs something above it, or the model is guessing at the codebase
    again. What changed is that the map is now orientation for an agent that can
    go and look, rather than the whole of what it will ever see.
    """
    parts = (context, project, f"OBJECTIVE: {objective}", HARNESS_RULES)
    return "\n\n".join(part for part in parts if part).strip()


def harness_repair_objective(
    failure: dict[str, object],
    project: str,
    touched: Iterable[str] = (),
    *,
    code: str = "",
    rules: str = HARNESS_RULES,
) -> str:
    """Fix what validation reported, with the file it happened in reachable.

    The old repair prompt carried the error text and one whole file, and the
    model had to rewrite that file from whatever it could infer. This one names
    the command, its real output and what this generation has already touched;
    the model reads the rest for itself -- or, as a work order (``code`` and
    ``rules`` from ``EnvironmentEvolver.repair_objective``), starts from the
    code the output points at, under rules short enough to leave room to work.
    """
    changed = ", ".join(touched)
    if failure.get("command") == REVIEW_COMMAND:
        # Not a failing command at all: the suite passed and a reviewer read
        # the change against its objective and found it unfinished. Framing
        # this as "the validation command failed" sent the model hunting for
        # an error message that does not exist.
        return "\n".join(
            part
            for part in (
                project,
                "This generation's change passes validation, but a reviewer who "
                "read it against its objective found it INCOMPLETE:",
                clip(str(failure.get("output", "")), 1500),
                f"Files this generation has already changed: {changed}" if changed else "",
                f"The objective it has to finish:\n{failure.get('objective', '')}",
                code,
                "Finish the objective -- add what the reviewer says is missing, in "
                "the file and function it names. Keep what is already right.",
                rules,
            )
            if part
        )
    is_hygiene = failure.get("command") == "evomesh codebase hygiene check"
    parts = (
        project,
        f"The validation command `{failure.get('command')}` failed with exit "
        f"code {failure.get('exit_code')}.",
        f"OUTPUT:\n{clip(str(failure.get('output', '')), 1500)}",
        code,
        f"Files this generation has already changed: {changed}" if changed else "",
        (
            "The file(s) named above in the OUTPUT are litter this generation "
            "created and cannot land: call delete on each of them by the exact "
            "path shown. Only wire one in instead if it is genuinely worth "
            "keeping and you can do that in the steps you have left."
            if is_hygiene
            else "Read the files involved, then fix the failure and nothing else. "
            "If the output says a module is unreachable, the fix is to edit a "
            "module that already runs so that it imports and uses it -- never "
            "to rewrite the unreachable file again."
        ),
        rules,
    )
    return "\n".join(part for part in parts if part)


# The review gate. Validation proves a candidate is valid Python that breaks
# no test; it cannot tell a finished change from its scaffolding. Found live
# 2026-09-24: generation 1370 was asked to stop uv's VIRTUAL_ENV warning,
# added an `env` parameter to run_command that no caller passes, validated,
# and landed with its backlog item ticked -- the warning untouched.
REVIEW_COMMAND = "review against the objective"
REVIEW_MARKER = "VERDICT:"
_VERDICT_RE = re.compile(r"VERDICT:\s*\**\s*(INCOMPLETE|COMPLETE)\b\**[\s:.\-]*(.*)", re.IGNORECASE)


def review_objective(objective: str, diff: str) -> str:
    """Ask a read-only harness job whether ``diff`` actually does ``objective``."""
    return "\n\n".join(
        (
            "You are reviewing a change another agent made to this project. You "
            "cannot edit anything, and do not need to.",
            f"THE OBJECTIVE IT WAS GIVEN:\n{objective}",
            f"THE CHANGE (git diff against its parent):\n{diff or '(empty)'}",
            "\n".join(
                (
                    "Decide one thing: does this change accomplish the objective, "
                    "completely? Read any file you need -- the change may call code "
                    "the diff does not show, and a new parameter or function is only "
                    "useful if something that runs actually uses it (grep for it).",
                    "It is INCOMPLETE if it only adds scaffolding nothing uses, does "
                    "part of what the objective asks and skips the rest, adds a test "
                    "in place of the behavior asked for, or changes something other "
                    "than what the objective names.",
                    "It already passes ruff, pyright and pytest -- do not judge "
                    "style, naming or taste, and do not ask for extras the objective "
                    "never mentioned.",
                    "End your answer with exactly one line, one of:",
                    f"{REVIEW_MARKER} COMPLETE",
                    f"{REVIEW_MARKER} INCOMPLETE: <what is missing, naming the file and "
                    "the function where it has to happen>",
                )
            ),
        )
    )


def parse_review(answer: str) -> tuple[bool | None, str]:
    """``(True, "")`` for COMPLETE, ``(False, reason)`` for INCOMPLETE, and
    ``(None, "")`` when the reviewer never gave a verdict. The last verdict
    line wins -- a model that quotes the instructions first still means the
    one it ends on."""
    matches = _VERDICT_RE.findall(answer)
    if not matches:
        return None, ""
    verdict, reason = matches[-1]
    if verdict.upper() == "COMPLETE":
        return True, ""
    return False, reason.strip() or "the reviewer found it incomplete but said nothing more"


# Where a generation's plan lives, before any of it is code. Inside the
# candidate for the same reason BACKLOG_DIR is: git add -A has to pick it up so
# the reasoning behind the plan lands in the same commit as what it produced.
PLAN_DIR = Path("docs") / "evolution" / "plans"
PLAN_NODES_DIR = PLAN_DIR / "nodes"
PLAN_FILE = "plan.md"
PLAN_EVAL_FILE = "plan.eval.md"

PLAN_DRAFT_RULES = "\n".join(
    (
        "Rules for this stage:",
        TOOL_USAGE_HINT,
        f"- Do not touch any source file. Write exactly one file, "
        f"`{(PLAN_DIR / PLAN_FILE).as_posix()}`, inside this candidate.",
        "- Before naming a function, class, or attribute the plan depends on, "
        "read that file (or grep it) and copy the name exactly as it appears. "
        "The map above gives a module's name and line count, not its contents "
        "-- a plan that recalls a plausible-sounding name instead of the real "
        "one reads fine here and gets rejected at the next stage regardless.",
        "- State the goal, the approach you intend to take, and the reasoning "
        "behind that approach -- specific enough that someone splitting it "
        "into smaller work items later has something real to split.",
        "- End your final answer with one sentence starting exactly with "
        "'RATIONALE:' summarising the plan in one line.",
    )
)

PLAN_EVAL_RULES = "\n".join(
    (
        "Rules for this stage:",
        TOOL_USAGE_HINT,
        "- You are reviewing a plan someone else proposed. Do not touch any source file.",
        f"- Write exactly one file, `{(PLAN_DIR / PLAN_EVAL_FILE).as_posix()}`, "
        "inside this candidate, holding your review.",
        "- Answer each of these three checks with YES or NO, one per line, "
        "before anything else -- do not skip a check or merge them into one "
        "sentence:",
        "  NAMES A REAL MODULE: <YES|NO> (the plan names a file that already "
        "exists and is imported somewhere, not a new file nothing loads yet)",
        "  NO UNTETHERED FILE: <YES|NO> (the plan does not propose a brand "
        "new file as its only deliverable)",
        "  SMALL ENOUGH TO SPLIT: <YES|NO> (the plan is concrete enough that "
        "someone could break it into a few minimal work items right now)",
        "- Then write exactly one line, 'VERDICT: approve' if all three "
        "checks are YES, otherwise 'VERDICT: reject: <one sentence reason>' "
        "naming the check that failed. That line must be the file's last "
        "non-empty line.",
        "- End your final answer with one sentence starting exactly with "
        "'RATIONALE:' summarising your verdict.",
    )
)

PLAN_DECOMPOSE_RULES = "\n".join(
    (
        "Rules for this stage:",
        TOOL_USAGE_HINT,
        "- Do not touch any source file. Write exactly one file at the NODE "
        "PATH given below, inside this candidate.",
        "- Decide whether this item is already minimal -- one small change to "
        "one module that already runs -- or whether it is still big enough to "
        "split into several independent-ish smaller items.",
        "- If it is minimal, the file's last non-empty line must be exactly 'LEAF'.",
        "- If it should split, write one line per child, each in the exact "
        "form '- <title> :: <reasoning>', optionally followed by "
        "' :: depends on: <n>' naming an earlier child in this same list by "
        "its 1-based position, when it can only be done after that one. Do "
        "not force a binary split -- write as many or as few children as the "
        "item actually needs.",
        "- End your final answer with one sentence starting exactly with "
        "'RATIONALE:' explaining your decision.",
    )
)


def draft_plan_objective(objective: str, project: str, context: str = "") -> str:
    """The harness job that drafts a plan for the objective, before any code."""
    parts = (context, project, f"OBJECTIVE: {objective}", PLAN_DRAFT_RULES)
    return "\n\n".join(part for part in parts if part).strip()


def evaluate_plan_objective(plan_text: str, project: str) -> str:
    """The harness job that reviews a drafted plan and writes a verdict.

    Three separate yes/no checks, not one holistic judgment -- a single
    open-ended "is this plan good" call is the shape of question a model
    with a small active reasoning path answers least reliably; naming the
    checks up front gives it something concrete to answer instead.
    """
    parts = (
        project,
        f"PLAN TO REVIEW:\n{plan_text}",
        "Check the plan against these three things, in this order: does it "
        "name a module that already exists and is imported somewhere (not a "
        "new file nothing loads yet); does it avoid proposing a brand new "
        "untethered file as its only deliverable; is it concrete enough to "
        "split into a few minimal work items right now.",
        PLAN_EVAL_RULES,
    )
    return "\n\n".join(part for part in parts if part).strip()


def decompose_objective(node: PlanNode, project: str) -> str:
    """The harness job that splits one plan item, or declares it minimal."""
    parts = (
        project,
        f"NODE PATH: {(PLAN_NODES_DIR / f'{node.id}.md').as_posix()}",
        f"ITEM TITLE: {node.title}",
        f"ITEM REASONING SO FAR:\n{node.reasoning}",
        PLAN_DECOMPOSE_RULES,
    )
    return "\n\n".join(part for part in parts if part).strip()


def parse_plan_verdict(text: str) -> tuple[bool, str]:
    """Pull the 'VERDICT: approve|reject: ...' line an evaluation ends with.

    Defaults to reject when the line is missing, the same fail-closed choice
    ``ENVIRONMENT_MARKERS`` makes elsewhere: a plan that cannot even be read
    back as approved has not earned the work of decomposing it.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("VERDICT:"):
            body = stripped.split(":", 1)[1].strip()
            if body.lower().startswith("approve"):
                return True, body
            return False, body.split(":", 1)[1].strip() if ":" in body else body
    return False, "no VERDICT line was found in the review"


def parse_plan_children(text: str) -> list[dict[str, Any]] | None:
    """Parse a decompose node's file: ``None`` for a declared leaf, else its
    children as ``{"title", "reasoning", "depends_on"}`` dicts (``depends_on``
    holding 1-based positions into this same list, resolved by the caller).
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(line.upper() == "LEAF" for line in lines):
        return None
    children: list[dict[str, Any]] = []
    for line in lines:
        if not line.startswith("-"):
            continue
        segments = [segment.strip() for segment in line[1:].split("::")]
        if len(segments) < 2:
            continue
        depends_on: list[int] = []
        for extra in segments[2:]:
            if extra.lower().startswith("depends on:"):
                for token in extra.split(":", 1)[1].split(","):
                    token = token.strip()
                    if token.isdigit():
                        depends_on.append(int(token))
        children.append({"title": segments[0], "reasoning": segments[1], "depends_on": depends_on})
    return children


class GenerationStatus(StrEnum):
    ACTIVE = "active"
    CANDIDATE = "candidate"
    LAST_KNOWN_GOOD = "last-known-good"
    FAILED = "failed"


class GenerationChange(BaseModel):
    """One file the generation wrote, and the reason it gave for writing it."""

    path: str
    rationale: str
    kind: str = "mutation"
    # The unified diff the harness recorded before it touched the file, so the
    # backlog entry can show what happened rather than only where.
    diff: str = ""
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PlanNode(BaseModel):
    """One node of a generation's plan tree: a draft, a split, or a leaf.

    A flat list rather than a nested structure, so it round-trips through
    ``model_dump(mode="json")`` the same way ``Generation.changes`` already
    does -- ``parent_id`` alone encodes the tree, and ``None`` marks a root (a
    plan draft; a redrafted plan appends a new root rather than overwriting
    the old one, so a rejected draft is never lost, only superseded).
    """

    id: str
    parent_id: str | None = None
    title: str
    reasoning: str
    kind: str = "root"  # "root" | "split" | "leaf"
    status: str = "open"  # "open" | "superseded" | "leaf" | "done"
    depends_on: list[str] = Field(default_factory=list)
    doc_path: str = ""
    # Root nodes only: the Evaluator's verdict on this draft.
    approved: bool | None = None
    eval_reasoning: str = ""
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Generation(BaseModel):
    number: int
    status: GenerationStatus
    path: Path
    parent: int | None = None
    git_commit: str | None = None
    objective: str = ""
    # Kept on the generation rather than only in the mutation log, because the
    # rationale has to survive into the commit that lands: a generation whose
    # reasoning exists only in a database nobody opens is a change nobody can
    # review a month later.
    changes: list[GenerationChange] = Field(default_factory=list)
    # The plan tree behind this generation's changes, when planning is on
    # (``EvolverBehavior(auto_plan=True)``). Empty for a generation authored
    # the old, flat way -- nothing here assumes it is populated.
    plan: list[PlanNode] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Proof in the output that the machine, not the mutation, broke the run. A
# candidate is a copy of a tree that already validated, so when the toolchain
# cannot open a directory or reach the network, no rewrite of one source file
# will help -- and asking a model to "fix" it wastes the attempt budget on a
# failure the candidate never caused.
ENVIRONMENT_MARKERS = (
    "PermissionError",
    "[WinError 5]",
    "Access is denied",
    "No space left on device",
    "OSError: [Errno 28]",
    # Windows, and seen on this machine: uv could not replace a file in the
    # candidate's venv because something else had it open. Nothing about the
    # candidate's source caused it and no rewrite of it would help.
    "os error 32",
    "being used by another process",
    "Connection refused",
    "Temporary failure in name resolution",
)


class ValidationResult(BaseModel):
    passed: bool
    commands: list[dict[str, object]]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def environment_blocker(self) -> str | None:
        """The proof that the host broke this run, or None if it did not.

        A flag first, markers second. Matching strings in output is a guess that
        happens to be right often; a command that *knows* it could not run says
        so, and the missing-toolchain case is the one where guessing was wrong
        and cost the candidate a verdict it never earned.
        """
        failure = self.failure()
        if failure is None:
            return None
        if failure.get("blocked"):
            return str(failure.get("output", "")).strip().splitlines()[0][:120]
        output = str(failure.get("output", ""))
        return next((marker for marker in ENVIRONMENT_MARKERS if marker in output), None)

    def failure(self) -> dict[str, object] | None:
        """The command that broke the candidate, or None when nothing did."""
        for command in self.commands:
            if command.get("exit_code") != 0:
                return command
        return None

    def digest(self) -> str:
        """Fingerprint the failure, so a repair that changed nothing is visible.

        Without this the pipeline cannot tell "the model rewrote the file and it
        still fails the same way" from "the model made progress", and it would
        spend every remaining attempt on a repair that provably does nothing.
        """
        failure = self.failure()
        if failure is None:
            return ""
        raw = f"{failure.get('command')}\n{failure.get('output')}"
        return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _touched_paths(entries: Iterable[dict[str, Any]]) -> list[str]:
    """The paths a harness job actually wrote, from its recorded entries."""
    return [
        str(entry["path"])
        for entry in entries
        if entry.get("kind") in ("edit", "write", "delete") and entry.get("path")
    ]


def excerpt(text: str, limit: int = 200) -> str:
    flattened = " ".join(text.split())
    return flattened[:limit] + ("..." if len(flattened) > limit else "")


def clip(text: str, limit: int, *, keep_end: bool = True) -> str:
    """Truncate without flattening, unlike ``excerpt``.

    A repair prompt carries tool output and source code, and both are unreadable
    once their line structure is collapsed. Tool output fails at the bottom, a
    file has to start at the top, so which end survives is the caller's choice.
    """
    if len(text) <= limit:
        return text
    return "...\n" + text[-limit:] if keep_end else text[:limit] + "\n..."


@dataclass
class GenerationSupervisor:
    root: Path

    @property
    def metadata_path(self) -> Path:
        return self.root / "supervisor.json"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.metadata_path.exists():
            self._write({"active": 1, "last_known_good": 1, "candidates": {}})

    def metadata(self) -> dict[str, Any]:
        self.initialize()
        return json.loads(self.metadata_path.read_text(encoding="utf-8"))

    def total_created(self) -> int:
        """How many generations have ever been opened, discards included --
        read from the same persisted, monotonically-increasing counter
        ``next_candidate_number()`` itself advances, so a rotation seed
        built from this can never plateau.

        Unlike ``candidates()`` (open only -- ``discard()`` removes the
        metadata entry) or ``metadata()['active']`` (only moves on a real
        land), this was meant to keep climbing by exactly one on every
        single ``create()`` call regardless of what happens to it after --
        but it used to count ``*-candidate`` directories still on disk,
        which quietly broke that exact promise once ``prune_stale()`` (added
        later, for a different reason -- unbounded disk/worktree growth)
        started deleting the oldest ones past ``GENERATION_RETENTION``: the
        on-disk count then plateaus at the retention ceiling instead of ever
        climbing further. Found live, 2026-09-23: frozen at 53 (this mesh's
        retention cap) for hours, so ``untested_objective()``'s seed
        rotation (behaviors.py's ``_open()``) deterministically handed the
        exact same target (``parse()`` in cron.py) to generation after
        generation across two different models -- not a model quality
        issue at all, a stuck rotation seed. ``next_candidate_number()``
        already solves "monotonic, survives pruning" for the generation
        *number*; this reads that same persisted counter for the *seed*
        instead of recomputing something that can plateau. Falls back to
        the old disk-count for a supervisor.json that predates the
        ``next_number`` field (before it has ever been written once).
        """
        metadata = self.metadata()
        stored = metadata.get("next_number")
        if isinstance(stored, int) and stored > 0:
            return stored
        return sum(1 for _ in self.root.glob("*-candidate"))

    def record_no_op(self) -> int:
        """Bump the persisted count of consecutive generations that wrote no
        file, and return the new total.

        Counts across restarts and across candidates being discarded (their
        metadata entry disappears, same as every other counter on this class
        would plateau if it lived there instead) -- the streak this exists to
        catch is exactly the one ``total_created()``'s docstring already
        found live: dozens of generations in a row burning a full step budget
        with nothing to show, silent until a human happened to read
        mesh.log. Reset with :meth:`reset_no_op_streak` the moment any
        generation actually changes a file.
        """
        metadata = self.metadata()
        streak = int(metadata.get("no_op_streak", 0)) + 1
        metadata["no_op_streak"] = streak
        self._write(metadata)
        return streak

    def reset_no_op_streak(self) -> None:
        metadata = self.metadata()
        if metadata.get("no_op_streak"):
            metadata["no_op_streak"] = 0
            self._write(metadata)

    def next_candidate_number(self) -> int:
        """The next generation number to use -- persisted, and strictly
        increasing regardless of what happens to any candidate after it is
        opened.

        create() used to compute ``max(active, *open_candidates) + 1``
        fresh every time. That recomputes the same answer every time a
        candidate is discarded and its metadata entry removed -- exactly
        the "sits still for many creates in a row" failure shape
        total_created()'s own docstring already names for a different
        rotation seed, just never fixed here too. Found live: once
        ``active`` stopped advancing (nothing had landed in hours), ~200 of
        this mesh's ~405 all-time discards logged as only two numbers,
        1218 and 1219, ping-ponging back and forth for over three hours --
        the number only ever moved on when prune_stale() happened to
        delete whichever directory was occupying it. Not just cosmetic
        confusion: recent_target_failure() sorts candidate directories by
        this same number to find "the most recent attempt at this exact
        target", so reused numbers actively mis-order the reflective-
        feedback mechanism that depends on it.
        """
        metadata = self.metadata()
        stored = metadata.get("next_number")
        if isinstance(stored, int) and stored > 0:
            number = stored
        else:
            # One-time migration for a supervisor.json predating this field:
            # bootstrap from the highest number already in play (active, any
            # open candidate, anything still on disk) so numbering only ever
            # climbs from here, never restarts at 1 and never collides with
            # history a human might still be looking at.
            existing = [int(item) for item in dict(metadata.get("candidates", {}))]
            on_disk = [
                int(entry.name.split("-", 1)[0])
                for entry in self.root.glob("*-candidate")
                if entry.name.split("-", 1)[0].isdigit()
            ]
            number = max([int(metadata["active"]), *existing, *on_disk], default=0) + 1
        metadata["next_number"] = number + 1
        self._write(metadata)
        return number

    def candidates(self) -> list[Generation]:
        raw = dict(self.metadata().get("candidates", {}))
        items = [Generation.model_validate(value) for value in raw.values()]
        return sorted(items, key=lambda item: item.number)

    def candidate(self, number: int) -> Generation:
        raw = dict(self.metadata().get("candidates", {})).get(str(number))
        if raw is None:
            raise KeyError(f"Generation {number} is not a known candidate")
        return Generation.model_validate(raw)

    def record_candidate(self, generation: Generation) -> None:
        metadata = self.metadata()
        candidates = dict(metadata.get("candidates", {}))
        candidates[str(generation.number)] = generation.model_dump(mode="json")
        metadata["candidates"] = candidates
        self._write(metadata)

    def promote(self, number: int) -> None:
        """Land the numbers, then drop the candidate entry the same way
        discard() does -- a promoted generation's code lives on as a real
        commit on main (and its own docs/evolution/NNNNNN.md, committed
        alongside it), so nothing is lost by no longer treating its
        directory as "still open".

        Found live: it never had before. `candidates()` -- and, through it,
        prune_stale()'s "still an open candidate" protection -- stayed
        entangled with every generation this pipeline had ever promoted,
        forever, because nothing here ever popped the entry the way
        discard() already did for its own case. On a mesh that has been
        promoting real work for hundreds of generations, that is the exact
        unbounded worktree/branch/directory growth prune_stale() exists to
        stop, just reached from its one remaining blind spot.
        """
        metadata = self.metadata()
        candidates = dict(metadata.get("candidates", {}))
        candidate = candidates.get(str(number))
        if not candidate or candidate.get("status") == GenerationStatus.FAILED:
            raise ValueError(f"Generation {number} is not promotable")
        metadata["last_known_good"] = metadata["active"]
        metadata["active"] = number
        candidates.pop(str(number), None)
        metadata["candidates"] = candidates
        history = list(metadata.get("recent_outcomes", []))
        history.append("promoted")
        metadata["recent_outcomes"] = history[-20:]
        _record_outcome(metadata, number, "promoted")
        self._write(metadata)

    def sweep_applied(self) -> list[int]:
        """One-time catch-up for candidates promote() landed *before* it
        started clearing its own entry (see promote()'s own docstring).

        Every one of those is still sitting in the candidates dict today,
        permanently protected from prune_stale() -- a generation that landed
        as a real commit long ago, still holding its worktree, branch, and
        directory open forever, for no reason the fix arriving later could
        undo on its own. Safe by the same test as promote() itself: a
        candidate with a recorded git_commit already has its code and its
        own docs/evolution/NNNNNN.md on a real commit, so removing its
        candidates-dict entry loses nothing. The currently active and
        last-known-good numbers are left alone regardless -- prune_stale()
        already protects those through a separate check, but there is no
        reason for this one to duplicate that judgment.

        Idempotent: a mesh that has never hit the old bug, or has already
        been swept once, finds nothing to do here on every later call.
        """
        metadata = self.metadata()
        candidates = dict(metadata.get("candidates", {}))
        protected = {str(metadata["active"]), str(metadata["last_known_good"])}
        swept: list[int] = []
        for key, value in list(candidates.items()):
            if key in protected:
                continue
            if value.get("git_commit"):
                swept.append(int(key))
                del candidates[key]
        if swept:
            metadata["candidates"] = candidates
            self._write(metadata)
        return sorted(swept)

    def discard(self, number: int) -> None:
        metadata = self.metadata()
        candidates = dict(metadata.get("candidates", {}))
        if candidates.pop(str(number), None) is None:
            raise ValueError(f"Generation {number} is not a known candidate")
        metadata["candidates"] = candidates
        history = list(metadata.get("recent_outcomes", []))
        history.append("discarded")
        metadata["recent_outcomes"] = history[-20:]
        _record_outcome(metadata, number, "discarded")
        self._write(metadata)

    def outcome(self, number: int) -> str | None:
        """``promoted``/``discarded`` once generation ``number`` is decided;
        ``None`` while it is still an open candidate. A decided generation
        older than the kept record reads as discarded."""
        metadata = self.metadata()
        if str(number) in metadata.get("candidates", {}):
            return None
        recorded = metadata.get("outcomes", {}).get(str(number))
        if recorded is not None:
            return str(recorded)
        return "discarded" if number <= self.total_created() else None

    def rollback(self) -> None:
        metadata = self.metadata()
        metadata["active"] = metadata["last_known_good"]
        self._write(metadata)

    def record_commits(self, *, active: str, last_known_good: str) -> None:
        """Remember what the tree held before and after a generation landed.

        The running process still executes the code it started with, so this
        also raises the flag that says a restart is owed. Nothing here restarts
        anything: the rollback path has to outlive a process that may not come
        back up, which means it cannot live inside that process.
        """
        metadata = self.metadata()
        metadata["last_known_good_commit"] = last_known_good
        metadata["active_commit"] = active
        metadata["restart_required"] = True
        self._write(metadata)

    def clear_restart_flag(self) -> None:
        metadata = self.metadata()
        metadata["restart_required"] = False
        self._write(metadata)

    def record_publish(self, *, commit: str, published: bool, detail: str) -> None:
        """Remember whether the landed commit reached the remote, and why not.

        A push that failed leaves a tree that is a commit ahead of the remote,
        which is invisible until someone runs git themselves. Writing the reason
        next to the commit means /evolution status can say it out loud.
        """
        metadata = self.metadata()
        metadata["published_commit"] = commit if published else metadata.get("published_commit")
        metadata["publish_ok"] = published
        metadata["publish_detail"] = detail
        self._write(metadata)

    def _write(self, data: dict[str, Any]) -> None:
        temporary = self.metadata_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # Seen live: generation 1218's own validation run failed a real test
        # over exactly this line, with the process at fault never named --
        # Path.replace() on Windows (os.replace/MoveFileEx) can raise
        # PermissionError: [WinError 5] when Defender's real-time scan or an
        # indexer has the just-written temp file open for a few
        # milliseconds. Nothing about the data being written caused it, and
        # the same write always succeeds a moment later, so a handful of
        # short retries costs one JSON write's worth of latency here against
        # a whole generation held for a human to unblock by hand.
        for attempt in range(5):
            try:
                temporary.replace(self.metadata_path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (2**attempt))


PYTEST_TEMP_DIR = ".pytest-tmp"

IGNORED_NAMES = (
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    PYTEST_TEMP_DIR,
    ".ruff_cache",
    ".runtime",
    "dist",
)

# How many old, unprotected generation directories (and the git worktree and
# branch each one holds) are left on disk for a human to look at directly.
# Everything beyond this, oldest first, is pruned on the next generation
# opened -- see CandidateWorkspace.prune_stale.
GENERATION_RETENTION = 50


class CandidateWorkspace:
    """Copies the code into an isolated generation, and nothing else.

    Runtime state is deliberately excluded. Copying the live SQLite database and
    the agents' memory into every candidate leaks state into a tree that may
    later be promoted, bloats each generation, and races the running mesh for
    the database file.
    """

    def __init__(
        self,
        repository_root: Path,
        generations_root: Path,
        exclude: Iterable[Path] = (),
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.supervisor = GenerationSupervisor(generations_root.resolve())
        self.exclude = {Path(item).resolve() for item in exclude}
        self.exclude.add(self.supervisor.root)

    def _ignore(self, directory: str, names: list[str]) -> set[str]:
        ignored = shutil.ignore_patterns(*IGNORED_NAMES)(directory, names)
        here = Path(directory).resolve()
        for name in names:
            target = (here / name).resolve()
            if target in self.exclude or any(root in target.parents for root in self.exclude):
                ignored.add(name)
        return set(ignored)

    async def _repository_root_is_a_real_repository(self) -> bool:
        """Whether `repository_root` is actually the top of its own git
        repository, rather than a plain directory `git -C` would silently
        walk up and out of.

        `git -C <dir> worktree add` does not require `<dir>` to be a
        repository at all -- it behaves exactly as running the command from
        inside `<dir>` would, which means it finds the *nearest ancestor*
        `.git` when `<dir>` has none of its own. On a repository_root that is
        a plain directory nested inside an unrelated repository -- found
        live, from a test fixture sitting under this project's own
        `.pytest-tmp` -- that silently creates a real worktree and branch in
        the *ancestor's* repository instead of failing over to the copytree
        fallback below, which is what actually happened the one time this
        went unchecked. A mismatch here means "not this one, don't ask git
        to try" rather than a real failure to report.
        """
        try:
            result = await run_command(
                "git", "-C", str(self.repository_root), "rev-parse", "--show-toplevel"
            )
        except OSError:
            return False
        if result.exit_code != 0:
            return False
        top_level = await asyncio.to_thread(lambda: Path(result.output.strip()).resolve())
        resolved_root = await asyncio.to_thread(self.repository_root.resolve)
        return top_level == resolved_root

    async def create(self, objective: str) -> Generation:
        metadata = self.supervisor.metadata()
        number = self.supervisor.next_candidate_number()
        # A discarded candidate keeps its directory so a human can still look at
        # it, and its metadata entry is gone. next_candidate_number() already
        # climbs monotonically, but skip past anything already on disk too --
        # cheap insurance against a manually-copied or pre-migration directory
        # occupying the number it just handed back.
        destination = self.supervisor.root / f"{number:06d}-candidate"
        while destination.exists():
            number = self.supervisor.next_candidate_number()
            destination = self.supervisor.root / f"{number:06d}-candidate"
        if await self._repository_root_is_a_real_repository():
            result = await run_command(
                "git",
                "-C",
                str(self.repository_root),
                "worktree",
                "add",
                "-b",
                f"evomesh/candidate-{number:06d}",
                str(destination),
                "HEAD",
            )
        else:
            result = None
        if result is None or result.exit_code != 0:
            await asyncio.to_thread(
                shutil.copytree, self.repository_root, destination, ignore=self._ignore
            )
        generation = Generation(
            number=number,
            status=GenerationStatus.CANDIDATE,
            path=destination,
            parent=int(metadata["active"]),
        )
        self.supervisor.record_candidate(generation)
        (destination / "MUTATION_OBJECTIVE.md").write_text(objective + "\n", encoding="utf-8")
        await self.prune_stale()
        return generation

    async def prune_stale(
        self, keep: int = GENERATION_RETENTION, *, max_per_call: int = 20
    ) -> list[int]:
        """Delete old, no-longer-referenced candidate directories along with
        the git worktree and branch each one holds open.

        Found live: 1017 generation directories on disk (~2.3MB each, plus a
        `git worktree add` per one) and 1013 `evomesh/candidate-NNNNNN`
        branches still registered in this repository, growing by one of each
        on every single generation this pipeline has ever opened -- discard()
        only ever drops the JSON metadata entry, never the worktree or
        branch it came with. Nothing here ever stops running on its own, so
        that growth has no ceiling; a process meant to run indefinitely
        cannot carry a resource that does not.

        A generation earns "worth keeping forever" from nothing computed
        here -- promoted work lives on as a real commit on main, and a
        discarded one's verdict is already in the mutation log the
        repository keeps, not only in the directory itself. Only the most
        recent ``keep`` are left on disk for a human to look at directly;
        anything still active, still last-known-good, or still an open
        candidate is protected regardless of age or count.

        ``max_per_call`` caps how much of a backlog this one call works off --
        called from create(), on the critical path of opening the very next
        generation. A repository that has been running unpruned for a while
        (this one: ~967 overflow on the day this was added) does its
        catch-up a little at a time across many generations rather than
        making the first `create()` after this ships pay for a thousand
        `git worktree remove` calls in one blocking stretch.
        """
        metadata = self.supervisor.metadata()
        protected = {int(metadata["active"]), int(metadata["last_known_good"])}
        protected.update(int(item) for item in dict(metadata.get("candidates", {})))
        numbered = sorted(
            (
                (int(match.group(1)), entry)
                for entry in self.supervisor.root.iterdir()
                if entry.is_dir() and (match := re.match(r"(\d+)-candidate$", entry.name))
            ),
            key=lambda pair: pair[0],
        )
        prunable = [pair for pair in numbered if pair[0] not in protected]
        overflow = min(max(0, len(prunable) - keep), max_per_call)
        removed: list[int] = []
        for number, path in prunable[:overflow]:
            await self._remove_generation_worktree(number, path)
            removed.append(number)
        if removed:
            await run_command("git", "-C", str(self.repository_root), "worktree", "prune")
        return removed

    async def _remove_generation_worktree(self, number: int, path: Path) -> None:
        result = await run_command(
            "git", "-C", str(self.repository_root), "worktree", "remove", "--force", str(path)
        )
        if result.exit_code != 0 and await asyncio.to_thread(path.exists):
            # Not a worktree at all (the copytree fallback path this class
            # falls back to outside a real git repository) or the worktree
            # registration was already gone -- either way a plain directory
            # left behind is still safe to remove.
            await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
        await run_command(
            "git",
            "-C",
            str(self.repository_root),
            "branch",
            "-D",
            f"evomesh/candidate-{number:06d}",
        )


def uv_executable(start: Path) -> str:
    """Find uv the way the Windows launcher does: PATH first, then `.tools`.

    The launcher runs EvoMesh through a uv that is often not on PATH at all, so
    a validator that only knows the bare name fails every candidate with a
    FileNotFoundError that reads like a broken mutation. A candidate lives a few
    directories below the checkout, so walk up instead of guessing the depth.
    """
    if found := shutil.which("uv"):
        return found
    for directory in (start, *start.parents):
        candidate = directory / ".tools" / "uv" / "bin" / "uv.exe"
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        "uv is not on PATH and no .tools/uv/bin/uv.exe was found above "
        f"{start}; a candidate generation cannot be validated without it"
    )


class CandidateValidator:
    """Runs the same five commands a human would, inside the candidate only.

    pytest gets an explicit ``--basetemp`` under the candidate rather than the
    machine's shared temp root. On a host where that root is not writable by
    this user, every ``tmp_path`` test errors at fixture setup and the candidate
    is reported as failed for something it did not do. Keeping the temp tree
    inside the generation also means a discarded candidate takes its scratch
    files with it. The directory is relative because every command already runs
    with the generation as its working directory.
    """

    COMMANDS = (
        ("uv", "sync"),
        ("uv", "run", "ruff", "check", "."),
        ("uv", "run", "pyright"),
        ("uv", "run", "pytest", "--basetemp", PYTEST_TEMP_DIR),
        ("uv", "run", "python", "-m", "evomesh.smoke"),
    )

    @staticmethod
    def _hygiene_failure(path: Path) -> dict[str, object] | None:
        """Dead code the model shipped, caught before any subprocess runs.

        ``new_orphans``/``stray_root_files`` used to be advisory only: their
        output fed the prompt but nothing ever failed a candidate over them, so
        a model that could not find real work to do had no reason not to invent
        a module nobody calls or a scratch file at the repository root -- both
        pass ruff, pyright and pytest without complaint. This runs first,
        against the tree on disk rather than a subprocess, so that kind of
        no-op generation fails fast instead of spending a full validation run
        (and a repair attempt) to land nothing of value.
        """
        problems: list[str] = []
        if orphans := new_orphans(path):
            problems.append(
                "new dead module(s) nothing imports: "
                + ", ".join(f"{module.name}.py" for module in orphans)
            )
        if stray := stray_root_files(path):
            problems.append("stray file(s) in the repository root: " + ", ".join(stray))
        if not problems:
            return None
        return {
            "command": "evomesh codebase hygiene check",
            "exit_code": 1,
            "output": (
                "; ".join(problems) + ". Wire the module into something that already imports and "
                "runs it, or delete the file -- a candidate cannot land code "
                "nothing executes."
            ),
        }

    @staticmethod
    async def _protected_failure(path: Path) -> dict[str, object] | None:
        """A candidate that changed the protected surface (its own oracle,
        the admission/verification rules, the reviewed approvals) is not
        validated by the commands it could have rewritten.

        Only the candidate's own repository is read (rule 11): git walks up
        from a directory that is not one, and the checkout it finds -- this
        one, for a test candidate under .pytest-tmp -- is somebody else's
        uncommitted work, not this candidate's change."""
        try:
            top = await run_command("git", "rev-parse", "--show-toplevel", cwd=path)
            if top.exit_code != 0:
                return None
            own = await asyncio.to_thread(
                lambda: Path(top.output.strip()).resolve() == path.resolve()
            )
            if not own:
                return None
            status = await run_command("git", "status", "--porcelain", "-uall", cwd=path)
        except (OSError, FileNotFoundError):
            return None
        if status.exit_code != 0:
            return None
        changed = [line[3:].split(" -> ")[-1].strip('"') for line in status.output.splitlines()]
        touched = protected_changes(changed)
        if not touched:
            return None
        return {
            "command": "evomesh protected-surface check",
            "exit_code": 1,
            "output": (
                "this candidate changes protected files: "
                + ", ".join(touched)
                + ". They decide whether work is admitted, verified or promoted, so a "
                "candidate may not change them on its own -- leave them as they are "
                "and change the code they judge instead, or ask a human to review it."
            ),
        }

    async def validate(self, generation: Generation) -> ValidationResult:
        outcomes: list[dict[str, object]] = []
        if hygiene := self._hygiene_failure(generation.path):
            return self._write(generation, ValidationResult(passed=False, commands=[hygiene]))
        if protected := await self._protected_failure(generation.path):
            return self._write(generation, ValidationResult(passed=False, commands=[protected]))
        try:
            uv = uv_executable(generation.path)
        except FileNotFoundError as exc:
            # Marked blocked, not failed. A candidate cannot be blamed for a
            # toolchain that is not installed, and without this flag the
            # pipeline reads "no uv" as a verdict and spends the repair budget
            # asking a model to fix somebody's PATH.
            return self._write(
                generation,
                ValidationResult(
                    passed=False,
                    commands=[
                        {
                            "command": "uv",
                            "exit_code": -1,
                            "output": str(exc),
                            "blocked": True,
                        }
                    ],
                ),
            )
        for command in self.COMMANDS:
            result = await run_command(
                uv,
                *command[1:],
                cwd=generation.path,
                env=without_virtual_env(),
            )
            outcomes.append(
                {
                    "command": " ".join(command),
                    "exit_code": result.exit_code,
                    "output": result.output,
                }
            )
            if result.exit_code != 0:
                break
        return self._write(
            generation,
            ValidationResult(
                passed=len(outcomes) == len(self.COMMANDS)
                and all(x["exit_code"] == 0 for x in outcomes),
                commands=outcomes,
            ),
        )

    @staticmethod
    def _write(generation: Generation, result: ValidationResult) -> ValidationResult:
        (generation.path / "validation-result.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )
        return result


class CandidateRepairer:
    """Mechanical repair: let the linter fix whatever the linter can fix.

    Most of what a small local model gets wrong in a generated file is a style
    rule that ships with a documented autofix. Burning a model call -- and a
    whole generation -- on ``UP017`` is waste, so the deterministic fixer runs
    first and the model is only asked about what survives it.

    The fixer runs over the whole candidate tree rather than the mutated file
    alone. That is safe because a candidate starts as a copy of a tree that
    already passes ``ruff check``, so the only fixable findings in it are the
    ones the mutation just introduced.
    """

    AUTOFIX = ("uv", "run", "ruff", "check", "--fix", ".")

    def can_repair(self, failure: dict[str, object] | None) -> bool:
        """Whether the mechanical fixer has any chance against this failure."""
        if failure is None:
            return False
        if "ruff" not in str(failure.get("command", "")):
            return False
        # Ruff itself says which findings it can fix; anything else is the
        # model's problem, and asking the fixer to try would waste a cycle.
        return "[*]" in str(failure.get("output", ""))

    async def autofix(self, generation: Generation) -> dict[str, object]:
        uv = uv_executable(generation.path)
        result = await run_command(
            uv,
            *self.AUTOFIX[1:],
            cwd=generation.path,
            env=without_virtual_env(),
        )
        return {
            "command": " ".join(self.AUTOFIX),
            "exit_code": result.exit_code,
            "output": result.output,
        }


@dataclass
class ValidationRun:
    """One suite running against one candidate, off the agent's cycle.

    The README has claimed since the pipeline was written that one stage per
    cycle means a tick never becomes a ten-minute validation run. It did: the
    stage awaited the suite inline, and an agent's mailbox and cycle share one
    lock, so the Evolver stopped answering for the whole of it and looked
    exactly like an agent that had hung.
    """

    generation: int
    task: asyncio.Task[ValidationResult]
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def running(self) -> bool:
        return not self.task.done()

    @property
    def seconds(self) -> float:
        return (datetime.now(UTC) - self.started_at).total_seconds()

    def describe(self) -> str:
        state = "running" if self.running else "finished"
        return f"validating generation {self.generation} ({state}, {self.seconds:.0f}s)"


PICK_RUNTIME_FAULT = "runtime-fault"
PICK_IMPROVEMENT = "improvement"
PICK_PLAN = "plan-item"
PICK_SCOUT = "scout"
# The untested-export backlog: write one test for code that already runs. The
# mirror image of SOURCE_PICKS -- a candidate answering one must NOT differ
# under src/evomesh/. Found live 2026-09-24: generation 1388 wrote a test that
# fed build_adjacency() an undirected frozenset edge and expected both
# directions back, and its repair "fixed" the directed graph code to accept a
# set -- whose tuple() order is hash-seeded -- instead of fixing the test.
PICK_TEST = "test"
TEST_ONLY_NOTE = (
    "This generation's objective is a TEST of code that already runs. If the "
    "test fails, the test is what is wrong: fix it or delete it. Never change "
    "anything under src/evomesh/ -- the code under test is not this objective's "
    "to change, and a candidate that changes it is discarded."
)
# The picks that are, by definition, a change to how EvoMesh behaves -- a
# candidate answering one must still differ under src/evomesh/ when it lands.
# An improvement from the V2 backlog that only runtime events evidence.
PICK_V2 = "improvement-v2"
V2_NEEDLE = "Resolve improvement"
SOURCE_PICKS = frozenset({PICK_RUNTIME_FAULT, PICK_IMPROVEMENT, PICK_V2})
# The picks that write docs/evolution/improvements.md and nothing else.
BACKLOG_PICKS = frozenset({PICK_PLAN, PICK_SCOUT})
# A plan or a scout only reads and then answers: the first ones took 9 to 21
# steps. harness.max_steps (150/2700s, sized for code changes) would let one
# that lost its way hold the pipeline for three quarters of an hour.
BACKLOG_MAX_STEPS = 40
BACKLOG_MAX_SECONDS = 900.0
# How many of the recent generations may aim at one substantive target before
# it is set aside for the others -- see `substantive_objective`.
MAX_TARGET_ATTEMPTS = 3
# Scouts aim at one module each, so MAX_TARGET_ATTEMPTS sets aside a module, not
# scouting; this caps scouts of any module in the same window, so a model that
# cannot scout at all still leaves the maintenance backlogs a turn.
MAX_SCOUT_ATTEMPTS = 6
# Red tests on the live tree come before any other objective. Found live
# 2026-09-25: generation 1463 landed a test that could never pass on this
# (Windows) host, and from then on every candidate failed validation on it --
# the mesh then kept writing more small tests on top of a red suite.
PICK_BASELINE = "baseline-fix"
BASELINE_NEEDLE = "Make the test suite pass again"
# All under .runtime/ (gitignored, never copied into a candidate). The venv is
# its own: the live .venv is in use by the running mesh and has no dev deps.
BASELINE_FILE = Path(".runtime") / "baseline-tests.json"
BASELINE_VENV = Path(".runtime") / "baseline-venv"
BASELINE_TEMP = Path(".runtime") / "baseline-pytest"
BASELINE_FAILURE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
BASELINE_PASSED = re.compile(r"\b[1-9]\d* passed\b")
# What of pytest's output goes into the objective: a harness transcript is
# ~12000 chars and its fixed prompt already takes most of that.
BASELINE_OUTPUT_CHARS = 1500


@dataclass(frozen=True)
class BaselineResult:
    """The whole test suite, run on the live tree before any objective is picked.

    ``key`` is the tree it ran on (HEAD plus uncommitted tracked changes), so a
    verdict is reused until the code changes. ``blocked`` means the machine,
    not the code, stopped the run (no uv, sync failed, timed out) -- that is no
    verdict, and evolution is not held up over it.
    """

    key: str
    passed: bool
    failures: tuple[str, ...] = ()
    output: str = ""
    blocked: bool = False

    def objective(self) -> str:
        listed = "\n".join(f"- {name}" for name in self.failures[:10])
        more = len(self.failures) - 10
        if more > 0:
            listed += f"\n- ... and {more} more"
        return (
            f"{BASELINE_NEEDLE}: `uv run pytest` fails on the live tree BEFORE any "
            "change, so every candidate fails validation for something it did "
            "not do.\n\n"
            f"Failing:\n{listed}\n\n"
            "Find the root cause of each failure. If the code under test is "
            "wrong, fix the code; if the test is wrong, fix the test. This mesh "
            "runs on Windows: a test that can only work on POSIX (os.killpg, "
            "/tmp paths, signals) is fixed to work on both or skipped on "
            "Windows with pytest.mark.skipif and a reason -- not deleted. Never "
            "weaken an assertion just to go green, and change nothing unrelated "
            "to these failures.\n\n"
            f"End of the pytest output:\n```\n{self.output[-BASELINE_OUTPUT_CHARS:]}\n```"
        )


def parse_baseline(key: str, exit_code: int, output: str) -> BaselineResult:
    """A pytest run's verdict: the failing node ids out of its ``-rfE`` summary."""
    if exit_code == 0:
        return BaselineResult(key=key, passed=True)
    # Not one test passed: the run itself broke, not the code. Found live
    # 2026-09-25: "790 errors", every one a PermissionError at tmp_path setup
    # while an orphaned earlier run still held the temp dir -- read as a red
    # suite, and a generation then "fixed" code that had nothing wrong.
    if not BASELINE_PASSED.search(output):
        return BaselineResult(key=key, passed=False, output=output, blocked=True)
    failures = tuple(dict.fromkeys(BASELINE_FAILURE.findall(output)))
    # A crash or collection error with no summary line is still a red suite.
    return BaselineResult(
        key=key,
        passed=False,
        failures=failures or (f"pytest exited {exit_code}",),
        output=output,
    )


def _record_outcome(metadata: dict[str, Any], number: int, outcome: str) -> None:
    outcomes = dict(metadata.get("outcomes", {}))
    outcomes[str(number)] = outcome
    metadata["outcomes"] = dict(list(outcomes.items())[-KEPT_OUTCOMES:])


# Per-generation outcomes kept in supervisor.json (improvement work items read
# the fate of their generation from it).
KEPT_OUTCOMES = 200


@dataclass(frozen=True)
class ObjectivePick:
    """One substantive objective, with what the pipeline needs to follow it.

    ``needle`` is the prefix the objective starts with (what the look-back
    helpers match a generation's MUTATION_OBJECTIVE.md on); ``key`` is what a
    later stage acts on -- the improvement's title, for ticking it off.
    ``work`` is what the job for it is built from once the candidate exists
    (see `EnvironmentEvolver.work_order`): a step's anchor, or the module a
    scout looks at. Empty for a pick that gets the full, unanchored prompt.
    """

    kind: str
    objective: str
    needle: str
    key: str
    step: int = 0
    work: dict[str, str] = field(default_factory=dict[str, str])


class EnvironmentEvolver:
    """Owns the candidate lifecycle. Driven one stage per cycle by EvolverBehavior."""

    def __init__(
        self,
        workspace: CandidateWorkspace,
        repository: SQLiteRepository,
        provider: ModelProvider | None = None,
        validator: CandidateValidator | None = None,
        repairer: CandidateRepairer | None = None,
        identity: GitIdentity | None = None,
        publish: PublishPolicy | None = None,
    ) -> None:
        self.workspace = workspace
        self.repository = repository
        self.provider = provider
        self.validator = validator or CandidateValidator()
        self.repairer = repairer or CandidateRepairer()
        self.identity = identity or GitIdentity()
        self.publish_policy = publish or PublishPolicy()
        # Set by the Environment so a landed generation can ask the process to
        # restart into it. The evolver never restarts anything itself: it does
        # not own the process, and it must stay usable from a test and a script.
        self.on_generation_landed: Callable[[int, str], None] | None = None
        # Set by the Environment to wake the agent driving this pipeline when
        # a validation run finishes, so its verdict is consumed right away.
        self.on_lane_finished: Callable[[], None] | None = None
        # Where a scout's item goes when the mesh has an Idea Scout: to it, as
        # a proposal a human approves, never straight into the backlog. True
        # when it was taken.
        self.idea_sink: Callable[[Improvement], bool] | None = None
        # What the last publish attempt did, so the cycle that applied the
        # generation can put it in the sentence a human actually reads.
        self.last_publish: str = ""
        # The suite running against the open candidate, if one is. At most one:
        # rule 7 means at most one candidate is open, so a second lane would be
        # a lane with nothing in it.
        self.validation: ValidationRun | None = None
        # The live tree's own suite, while it runs: (tree key, task).
        self._baseline_run: tuple[str, asyncio.Task[BaselineResult]] | None = None
        # The runtime log as last observed: (size, mtime_ns).
        self._last_log_reading: tuple[int, int] | None = None

    # -- baseline -------------------------------------------------------

    async def baseline_key(self) -> str:
        """HEAD plus a digest of uncommitted tracked changes; "" outside git."""
        repository = GitRepository(self.workspace.repository_root)
        try:
            head = await repository.current_commit()
            dirty = await repository.run("status", "--porcelain", "--untracked-files=no")
        except (GitError, OSError):
            return ""
        return f"{head}:{hashlib.sha256(dirty.encode()).hexdigest()[:12]}"

    async def baseline(self, timeout_seconds: float = 1800.0) -> BaselineResult | None:
        """The live tree's test verdict for its current code, or ``None`` while
        the suite is still running (started here, off the caller's cycle).

        Reused from ``.runtime/baseline-tests.json`` until the tree changes, so
        the suite runs once per landed change -- not once per generation, and
        not again just because a promotion restarted the process.
        """
        key = await self.baseline_key()
        if not key:
            # Not a git checkout (a test's scratch tree): nothing to key a
            # verdict on, so no verdict -- never a reason to hold evolution.
            return BaselineResult(key="", passed=True, blocked=True)
        if self._baseline_run is not None:
            running_key, task = self._baseline_run
            if not task.done():
                return None
            self._baseline_run = None
            result = self._finished_baseline(running_key, task)
            self._save_baseline(result)
            if running_key == key:
                return result
        cached = self._load_baseline()
        if cached is not None and cached.key == key:
            return cached

        async def run() -> BaselineResult:
            return await asyncio.wait_for(self._run_baseline(key), timeout=timeout_seconds)

        task = asyncio.create_task(run(), name="evomesh-baseline-tests")
        task.add_done_callback(self._lane_finished)
        self._baseline_run = (key, task)
        return None

    async def _run_baseline(self, key: str) -> BaselineResult:
        root = self.workspace.repository_root
        try:
            uv = uv_executable(root)
        except FileNotFoundError as exc:
            return BaselineResult(key=key, passed=False, output=str(exc), blocked=True)
        env = {**without_virtual_env(), "UV_PROJECT_ENVIRONMENT": str(root / BASELINE_VENV)}
        sync = await run_command(uv, "sync", "--frozen", cwd=root, env=env)
        if sync.exit_code != 0:
            return BaselineResult(key=key, passed=False, output=sync.output, blocked=True)
        # A directory of its own per run: an orphan of an earlier run (a mesh
        # killed mid-suite) may still hold the last one open. Old ones are
        # swept best-effort; a locked one just stays until it is free.
        shutil.rmtree(root / BASELINE_TEMP, ignore_errors=True)
        basetemp = root / BASELINE_TEMP / f"run-{time.time_ns()}"
        suite = await run_command(
            uv,
            "run",
            "--frozen",
            "pytest",
            "-q",
            "-rfE",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(basetemp),
            cwd=root,
            env=env,
        )
        return parse_baseline(key, suite.exit_code, suite.output)

    @staticmethod
    def _finished_baseline(key: str, task: asyncio.Task[BaselineResult]) -> BaselineResult:
        try:
            return task.result()
        except TimeoutError:
            output = "the live tree's test suite did not finish in time"
        except Exception as exc:  # noqa: BLE001 - any crash is "no verdict", not a verdict
            output = f"the live tree's test suite could not run: {exc}"
        return BaselineResult(key=key, passed=False, output=output, blocked=True)

    def _load_baseline(self) -> BaselineResult | None:
        path = self.workspace.repository_root / BASELINE_FILE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return BaselineResult(
                key=str(raw["key"]),
                passed=bool(raw["passed"]),
                failures=tuple(str(x) for x in raw.get("failures", ())),
                output=str(raw.get("output", "")),
                blocked=bool(raw.get("blocked", False)),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_baseline(self, result: BaselineResult) -> None:
        path = self.workspace.repository_root / BASELINE_FILE
        with suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "key": result.key,
                        "passed": result.passed,
                        "failures": list(result.failures),
                        "output": result.output[-20000:],
                        "blocked": result.blocked,
                        "at": datetime.now(UTC).isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    def improvement_pick(self, improvement: TrackedImprovement) -> ObjectivePick:
        """The objective for a backlog improvement no file-level source
        backs (one the scout proposed from recurring runtime events)."""
        evidence = "\n".join(
            f"- {item.kind} {item.reference}: {item.value}" for item in improvement.evidence[-3:]
        )
        criteria = "\n".join(f"- {line}" for line in improvement.success_criteria)
        return ObjectivePick(
            kind=PICK_V2,
            objective=(
                f"{V2_NEEDLE} {improvement.id}: {improvement.title}.\n"
                f"Problem (seen {improvement.occurrences} times): {improvement.problem}\n"
                f"Evidence:\n{evidence}\nSuccess criteria:\n{criteria}\n"
                "Find the code responsible under src/evomesh/ and change it so this "
                "stops happening. Change only what that needs."
            ),
            needle=f"{V2_NEEDLE} {improvement.id}",
            key=improvement.id,
        )

    def baseline_pick(self, result: BaselineResult) -> ObjectivePick | None:
        """The objective a red baseline makes, or ``None`` once it has been
        tried ``MAX_TARGET_ATTEMPTS`` times recently -- then it is a human's."""
        tried = sum(text.startswith(BASELINE_NEEDLE) for text in self._recent_objectives())
        if tried >= MAX_TARGET_ATTEMPTS:
            return None
        return ObjectivePick(
            kind=PICK_BASELINE,
            objective=result.objective(),
            needle=BASELINE_NEEDLE,
            key=result.key,
        )

    # -- pipeline state -------------------------------------------------

    async def pipeline_state(self) -> dict[str, Any]:
        raw = await self.repository.load_state(PIPELINE_STATE_KEY)
        return dict(raw) if isinstance(raw, dict) else {"stage": "plan"}

    async def set_pipeline_state(self, state: dict[str, Any]) -> None:
        await self.repository.save_state(PIPELINE_STATE_KEY, state)

    async def reset_pipeline(self) -> None:
        await self.set_pipeline_state({"stage": "plan"})

    # -- candidates -----------------------------------------------------

    def candidate(self, number: int) -> Generation:
        return self.workspace.supervisor.candidate(number)

    def latest_candidate(self) -> Generation | None:
        candidates = self.workspace.supervisor.candidates()
        return candidates[-1] if candidates else None

    def read_validation(self, generation: Generation) -> ValidationResult | None:
        path = generation.path / "validation-result.json"
        if not path.exists():
            return None
        return ValidationResult.model_validate_json(path.read_text(encoding="utf-8"))

    async def create_candidate(self, objective: str) -> Generation:
        generation = await self.workspace.create(objective)
        await self.repository.record_mutation(
            {"generation": generation.number, "objective": objective, "status": "candidate"}
        )
        return generation

    def project_map(self) -> str:
        """What the package looks like right now, for the model to aim at."""
        return project_map(self.workspace.repository_root)

    def backlog_target(self, seed: int) -> Module | None:
        """The dead module a backlog objective would target right now."""
        return backlog_target(self.workspace.repository_root, seed)

    def backlog_objective(self, seed: int, *, nudge_delete: bool = False) -> str | None:
        """A concrete dead-module objective, or ``None`` when the backlog is empty."""
        return backlog_objective(self.workspace.repository_root, seed, nudge_delete=nudge_delete)

    def untested_objective(self, seed: int) -> str | None:
        """A concrete untested-export objective, or ``None`` when every live
        module's exports are all mentioned under tests/ -- the second-tier
        fallback once :meth:`backlog_objective` itself is empty."""
        return untested_objective(self.workspace.repository_root, seed)

    def untested_target(self, seed: int) -> tuple[Module, str] | None:
        """The (module, exported name) pair an untested objective would
        target right now."""
        return untested_target(self.workspace.repository_root, seed)

    def substantive_candidates(self) -> list[tuple[ObjectivePick, Candidate]]:
        """Every substantive objective available now, each with the evidence
        it rests on as an improvement proposal: logged faults first, then
        open items of ``docs/evolution/improvements.md``. A target already
        attempted ``MAX_TARGET_ATTEMPTS`` times recently is left out."""
        root = self.workspace.repository_root
        recent = self._recent_objectives()

        def fresh(needle: str) -> bool:
            return sum(text.startswith(needle) for text in recent) < MAX_TARGET_ATTEMPTS

        found: list[tuple[ObjectivePick, Candidate]] = []
        for fault in runtime_faults(root):
            if not fresh(runtime_fault_needle(fault)):
                continue
            pick = ObjectivePick(
                kind=PICK_RUNTIME_FAULT,
                objective=runtime_fault_objective(fault),
                needle=runtime_fault_needle(fault),
                key=f"{fault.module}.{fault.function}:{fault.exception}",
            )
            found.append((pick, fault_candidate(fault)))
        for item in open_improvements(root):
            if (pick := self._item_pick(item, fresh)) is not None:
                found.append((pick, backlog_candidate(item)))
        return found

    def observations(self, baseline: BaselineResult | None = None) -> list[Observation]:
        """This pass's real observations for verification (closure plan
        15.3). The suite observes failing-test evidence once per tree state
        (its key); the runtime log observes faults and runtime events, and
        only counts when the mesh actually ran since the last reading -- a
        quiet log is no evidence that a fault is gone."""
        found: list[Observation] = [self._log_observation()]
        if baseline is not None:
            found.append(
                Observation(
                    observation_id=f"suite:{baseline.key}",
                    observer_id="baseline_suite",
                    covers=frozenset({EVIDENCE_FAILING_TESTS}),
                    eligible=0 if baseline.blocked else 1,
                    healthy=not baseline.blocked,
                )
            )
        return found

    def _log_observation(self) -> Observation:
        covers = frozenset({EVIDENCE_RUNTIME_FAULT, RUNTIME_SOURCE, DISCOVERY_SOURCE})
        path = self.workspace.repository_root / RUNTIME_LOG
        if not path.is_file():
            return Observation("log:missing", "runtime_log", covers, eligible=0, healthy=False)
        stat = path.stat()
        reading = (stat.st_size, stat.st_mtime_ns)
        ran = self._last_log_reading is not None and reading != self._last_log_reading
        self._last_log_reading = reading
        # The log shows that the mesh ran, never that a repaired path did: a
        # fault it still shows is target-specific, its absence is not. So it
        # speaks only for the faults it can see; the rest stay VERIFYING
        # until a real probe or a human verifies them.
        seen = frozenset(
            fault_candidate(fault).ref
            for fault in runtime_faults(self.workspace.repository_root)
        )
        return Observation(
            f"log:{reading[0]}:{reading[1]}",
            "runtime_log",
            covers,
            eligible=1 if ran else 0,
            targets=seen,
        )

    async def candidate_revision(self, generation: Generation) -> str:
        """The identity of what a verdict judged: a digest of the candidate's
        code change against its parent. ``docs/evolution`` bookkeeping (a
        ticked backlog step) is left out, so it never invalidates a verdict;
        any code edit does."""
        candidate = await self._own_repository(generation)
        if candidate is None:
            return ""
        try:
            await candidate.run("add", "-A", "-N")
            patch = await candidate.run("diff", "HEAD", "--", ".", ":(exclude)docs/evolution")
        except GitError:
            return ""
        return hashlib.sha256(patch.encode("utf-8")).hexdigest()

    def evidence_refs(self, baseline: BaselineResult | None = None) -> set[str]:
        """Every piece of evidence present now, attempted or not: what
        verification checks is gone after a fix is deployed."""
        root = self.workspace.repository_root
        refs = {fault_candidate(fault).ref for fault in runtime_faults(root)}
        refs |= {backlog_candidate(item).ref for item in open_improvements(root)}
        if baseline is not None and not baseline.passed and baseline.failures:
            refs.add(baseline_candidate(baseline).ref)
        return refs

    def scout_pick(
        self, seed: int, scout_cap: int | None = MAX_SCOUT_ATTEMPTS
    ) -> ObjectivePick | None:
        """Find more work when no evidenced objective is left: scout a
        module for backlog items, where the project keeps a backlog file."""
        root = self.workspace.repository_root
        recent = self._recent_objectives()

        def fresh(needle: str) -> bool:
            return sum(text.startswith(needle) for text in recent) < MAX_TARGET_ATTEMPTS

        scouted = sum(text.startswith(SCOUT_NEEDLE) for text in recent)
        # ``scout_cap=None``: no test backlog to fall back on, so scouting is
        # the only way to find work and is never capped.
        capped = scout_cap is not None and scouted >= scout_cap
        if not (root / IMPROVEMENTS_FILE).is_file() or capped:
            return None
        leads = warning_leads(root)
        modules = [name for name in scout_modules(root, leads) if fresh(scout_needle(name))]
        # The log points somewhere: scout there until each of those is set aside.
        pool = [name for name in modules if name in leads] or modules
        if not pool:
            return None
        module = pool[seed % len(pool)]
        return ObjectivePick(
            kind=PICK_SCOUT,
            objective=scout_objective(root, module, leads.get(module)),
            needle=scout_needle(module),
            key="",
            work={"module": module},
        )

    def substantive_objective(
        self, seed: int, scout_cap: int | None = MAX_SCOUT_ATTEMPTS, *, scout: bool = True
    ) -> ObjectivePick | None:
        """A real behavioral change to make, or ``None`` if there is none,
        rotated by ``seed``: a logged fault first, then a backlog item, then
        a scout. The prioritized path (ImprovementControl) ranks
        :meth:`substantive_candidates` instead; this is the fallback without it.
        """
        pairs = self.substantive_candidates()
        faults = [pick for pick, _ in pairs if pick.kind == PICK_RUNTIME_FAULT]
        if faults:
            return faults[seed % len(faults)]
        items = [pick for pick, _ in pairs if pick.kind != PICK_RUNTIME_FAULT]
        if items:
            return items[seed % len(items)]
        return self.scout_pick(seed, scout_cap) if scout else None

    @staticmethod
    def _item_pick(item: Improvement, fresh: Callable[[str], bool]) -> ObjectivePick | None:
        """What to do next about one backlog item, or ``None`` for nothing yet.

        An item with steps hands out its first open one. An item without is
        planned first -- split into anchored steps by a job whose output is
        checked by code -- and only once planning has used up its attempts is
        it handed out whole, the way every item was before steps existed.
        """
        if item.steps:
            step = item.next_step
            if step is None or not fresh(step_needle(item, step)):
                return None
            return ObjectivePick(
                kind=PICK_IMPROVEMENT,
                objective=step_objective(item, step),
                needle=step_needle(item, step),
                key=item.title,
                step=step.number,
                work={"path": step.path, "symbol": step.symbol},
            )
        if fresh(plan_needle(item)):
            return ObjectivePick(
                kind=PICK_PLAN,
                objective=plan_objective(item),
                needle=plan_needle(item),
                key=item.title,
                work={"title": item.title},
            )
        if fresh(improvement_needle(item)):
            return ObjectivePick(
                kind=PICK_IMPROVEMENT,
                objective=improvement_objective(item),
                needle=improvement_needle(item),
                key=item.title,
            )
        return None

    def work_order(
        self, generation: Generation, objective: str, pick: str, work: dict[str, Any]
    ) -> str | None:
        """The whole harness task for an anchored pick, built from the
        candidate's own files, or ``None`` for a pick that has no anchor and
        gets :meth:`mutation_objective`'s full prompt instead.

        No package map, no skills catalog, rules a third the length of
        HARNESS_RULES: the room goes to the code the job needs instead, so
        a small model's transcript can hold the task and still do the work.
        """
        root = generation.path
        if pick == PICK_IMPROVEMENT and work.get("path") and work.get("symbol"):
            return step_task(root, objective, str(work["path"]), str(work["symbol"]))
        if pick == PICK_PLAN and work.get("title"):
            return plan_task(root, objective, str(work["title"]))
        if pick == PICK_SCOUT and work.get("module"):
            return scout_task(root, objective, str(work["module"]))
        if pick == PICK_TEST and work.get("path") and work.get("symbol"):
            return write_test_task(
                root, objective, str(work["path"]), str(work["symbol"]), str(work["tests"])
            )
        return None

    def vet_scouted_items(
        self, generation: Generation
    ) -> tuple[list[Improvement], list[tuple[Improvement, str]]]:
        """Check the items a scout generation added against the live tree's
        backlog, and strip the ones that fail from the candidate's copy, so
        only items naming real code can ever become an objective."""
        before = open_improvements(self.workspace.repository_root)
        kept, dropped = vet_new_improvements(before, generation.path)
        drop_improvements(generation.path, {item.title for item, _ in dropped})
        return kept, dropped

    def apply_backlog_answer(
        self, generation: Generation, pick: str, title: str, answer: str
    ) -> list[dict[str, Any]]:
        """Write what a read-only plan or scout job answered into the
        candidate's backlog, as the change entries a recorder expects -- none
        when the answer held no step (plan) or no item (scout). What is
        written is vetted by the same `accept` either way."""
        root = generation.path
        written: str | None = None
        if pick == PICK_PLAN:
            written = write_planned_steps(root, title, steps_from_answer(answer))
        elif pick == PICK_SCOUT and (item := item_from_answer(answer)) is not None:
            if self.idea_sink is not None and self.idea_sink(item):
                return []
            written = append_item(root, item)
        if not written:
            return []
        diff = "\n".join(f"+{line}" for line in written.splitlines())
        return [{"kind": "edit", "path": IMPROVEMENTS_FILE.as_posix(), "diff": diff}]

    def vet_plan(self, generation: Generation, title: str) -> str | None:
        """Why the steps a plan generation wrote under ``title`` cannot be
        used, or ``None`` -- the evaluation a plan gets, by code, not a model."""
        before = open_improvements(self.workspace.repository_root)
        return vet_plan(before, generation.path, title)

    async def candidate_diff(self, generation: Generation, limit: int = 9000) -> str:
        """What the candidate changed against its parent, new files included,
        clipped to ``limit`` -- the evidence a review reads first.

        ``add -A -N`` records untracked files as intent-to-add so ``diff``
        shows them; the promotion commit's own ``add -A`` supersedes it, so
        nothing about what lands changes.
        """
        candidate = await self._own_repository(generation)
        if candidate is None:
            return ""
        try:
            await candidate.run("add", "-A", "-N")
            stat = await candidate.run("diff", "HEAD", "--stat")
            patch = await candidate.run("diff", "HEAD")
        except GitError:
            return ""
        return clip(f"{stat.strip()}\n\n{patch}", limit, keep_end=False)

    def tick_improvement(self, generation: Generation, title: str) -> bool:
        """Tick ``title`` off inside the candidate, so it lands in the same
        commit as the change that implemented it -- and disappears with the
        candidate if that change is discarded."""
        return tick_improvement(generation.path, title)

    def tick_step(self, generation: Generation, title: str, number: int) -> bool:
        """Tick one step of ``title`` off inside the candidate, and the item
        itself when that was its last open step -- same commit, same reason."""
        return tick_step(generation.path, title, number)

    def _recent_objectives(self, lookback: int = 20) -> list[str]:
        """The MUTATION_OBJECTIVE.md text of the newest ``lookback``
        generation directories, newest first."""
        numbered = sorted(
            (
                (int(entry.name.split("-", 1)[0]), entry)
                for entry in self.workspace.supervisor.root.glob("*-candidate")
                if entry.name.split("-", 1)[0].isdigit()
            ),
            key=lambda pair: -pair[0],
        )
        texts: list[str] = []
        for _, entry in numbered[:lookback]:
            path = entry / "MUTATION_OBJECTIVE.md"
            if path.is_file():
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
        return texts

    def record_no_op(self) -> int:
        """Forwarded to :meth:`GenerationSupervisor.record_no_op`."""
        return self.workspace.supervisor.record_no_op()

    def reset_no_op_streak(self) -> None:
        """Forwarded to :meth:`GenerationSupervisor.reset_no_op_streak`."""
        self.workspace.supervisor.reset_no_op_streak()

    def recent_target_failure(self, needles: tuple[str, ...], lookback: int = 20) -> str | None:
        """Why the most recent generation aimed at this exact target failed,
        or ``None`` if none of the last ``lookback`` generation directories
        were aimed at it (or one was, but there's nothing concrete to say).

        The GEPA-style idea worth taking without taking GEPA itself: don't
        hand the model a fresh, stateless attempt at a target it (or an
        earlier generation) already failed -- say how, so the next attempt
        can avoid the specific mistake instead of rediscovering it. Read the
        objective's own MUTATION_OBJECTIVE.md prefix to identify a match, the
        same way :meth:`recent_backlog_streak` does, but scans a wider window
        (the seed rotation means the *immediately* preceding generation is
        rarely the same target -- what matters here is the most recent one
        that was, however many unrelated picks came between) and returns the
        failure itself rather than just a count.
        """
        numbered = sorted(
            (
                (int(entry.name.split("-", 1)[0]), entry)
                for entry in self.workspace.supervisor.root.glob("*-candidate")
                if entry.name.split("-", 1)[0].isdigit()
            ),
            key=lambda pair: -pair[0],
        )
        for _, entry in numbered[:lookback]:
            objective_path = entry / "MUTATION_OBJECTIVE.md"
            if not objective_path.is_file():
                continue
            text = objective_path.read_text(encoding="utf-8", errors="replace")
            if not text.startswith(needles):
                continue
            validation_path = entry / "validation-result.json"
            if not validation_path.is_file():
                return (
                    "it made no real edit at all -- the candidate was discarded "
                    "before anything could even be validated."
                )
            try:
                result = ValidationResult.model_validate_json(
                    validation_path.read_text(encoding="utf-8", errors="replace")
                )
            except ValueError:
                return None
            failure = result.failure()
            if failure is None:
                return None
            command = str(failure.get("command", ""))
            output = excerpt(str(failure.get("output", "")), 600)
            return f"`{command}` failed:\n{output}"
        return None

    def recent_backlog_streak(self, module_name: str, lookback: int = 3) -> int:
        """How many of the most recent generations, newest first, targeted
        this exact backlog module and consecutively failed to land it.

        Reads generation directories directly rather than
        ``workspace.supervisor.candidates()`` -- that only holds still-open
        candidates, and a discarded one's directory (with its
        MUTATION_OBJECTIVE.md) is deliberately kept on disk for exactly this
        kind of look-back. Counts back from the newest generation and stops
        at the first one that either targeted something else or is not a
        backlog objective at all, so a streak never counts through an
        unrelated generation in between.
        """
        numbered = sorted(
            (
                (int(entry.name.split("-", 1)[0]), entry)
                for entry in self.workspace.supervisor.root.glob("*-candidate")
                if entry.name.split("-", 1)[0].isdigit()
            ),
            key=lambda pair: -pair[0],
        )
        # Both phrasings backlog_objective() can produce for this module --
        # not just "Wire". Checking only the wire phrasing meant the streak
        # reset to zero the instant nudge_delete=True actually fired: that
        # generation's own objective starts with "Delete", which broke the
        # very next lookback immediately and reverted the one after it back
        # to "Wire" -- found live, cycles.py flip-flopping Wire/Delete/Wire
        # every four generations instead of staying escalated until the
        # delete attempt either landed or the module stopped being an orphan.
        needles = (
            f"Wire src/evomesh/{module_name}.py",
            f"Delete src/evomesh/{module_name}.py",
        )
        streak = 0
        for _, entry in numbered[:lookback]:
            objective_path = entry / "MUTATION_OBJECTIVE.md"
            if not objective_path.is_file():
                break
            text = objective_path.read_text(encoding="utf-8", errors="replace")
            if not text.startswith(needles):
                break
            streak += 1
        return streak

    def mutation_objective(self, objective: str, context: str = "") -> str:
        """The harness job that authors this generation."""
        return harness_objective(objective, self.project_map(), context)

    def repair_objective(
        self,
        failure: dict[str, object],
        touched: Iterable[str] = (),
        root: Path | None = None,
    ) -> str:
        """The harness job that fixes what validation reported.

        With the candidate's ``root``, a work order: the code the failing
        command points at (or, after an INCOMPLETE review, the function the
        reviewer names) instead of the package map, and REPAIR_RULES instead
        of HARNESS_RULES.
        """
        if root is None:
            return harness_repair_objective(failure, self.project_map(), touched)
        # The same tail the prompt shows as OUTPUT: pyright lists every error,
        # and code around the first one -- clipped out of what the model sees
        # -- would be an excerpt of a failure it was never shown.
        output = clip(str(failure.get("output", "")), 1500)
        code = (
            named_code(root, output)
            if failure.get("command") == REVIEW_COMMAND
            else failure_excerpts(root, output)
        )
        return harness_repair_objective(failure, "", touched, code=code, rules=REPAIR_RULES)

    def leaf_objective(self, node: PlanNode, context: str = "") -> str:
        """The harness job that authors one minimal item from the plan tree."""
        objective = f"{node.title}\n\n{node.reasoning}".strip()
        return harness_objective(objective, self.project_map(), context)

    def draft_plan_objective_text(self, objective: str, context: str = "") -> str:
        return draft_plan_objective(objective, self.project_map(), context)

    def evaluate_plan_objective_text(self, plan_text: str) -> str:
        return evaluate_plan_objective(plan_text, self.project_map())

    def fabricated_plan_references(self, plan_text: str) -> list[str]:
        """``module.symbol`` mentions in the plan naming code that isn't there.

        A mechanical stand-in for the one thing the plan evaluator has spent
        this session rejecting plans for, over and over: a name the model
        recalled instead of read. Checking it here costs a regex and an AST
        lookup; checking it by handing the plan to the evaluator costs a whole
        harness job for a verdict this already knows.
        """
        return fabricated_references(plan_text, self.workspace.repository_root)

    def decompose_plan_objective_text(self, node: PlanNode) -> str:
        return decompose_objective(node, self.project_map())

    # -- the plan tree ----------------------------------------------------

    @staticmethod
    def plan_node(generation: Generation, node_id: str) -> PlanNode | None:
        return next((node for node in generation.plan if node.id == node_id), None)

    @staticmethod
    def current_plan_root(generation: Generation) -> PlanNode | None:
        """The plan draft in force -- the newest one not superseded by a
        revision -- or ``None`` when nothing has been drafted yet."""
        roots = [
            node
            for node in generation.plan
            if node.parent_id is None and node.status != "superseded"
        ]
        return roots[-1] if roots else None

    async def record_plan_draft(
        self,
        generation: Generation,
        entries: Iterable[dict[str, Any]],
        objective: str,
        rationale: str,
        status: str = "planned",
    ) -> list[str]:
        """Record a drafted (or redrafted) plan as a new root ``PlanNode``.

        A redraft appends rather than overwrites: ``record_plan_eval`` already
        marked the rejected root superseded the moment it was rejected (not
        here, or ``current_plan_root`` would keep answering with a plan a
        human could see was already turned down, for the whole cycle it takes
        the harness to redraft), so this only ever adds a new one.
        """
        touched = _touched_paths(entries)
        plan_path = generation.path / PLAN_DIR / PLAN_FILE
        if not plan_path.exists():
            return touched
        revision = sum(1 for node in generation.plan if node.parent_id is None) + 1
        generation.plan.append(
            PlanNode(
                id=f"root-{revision}",
                title=objective,
                reasoning=plan_path.read_text(encoding="utf-8", errors="replace"),
                kind="root",
                status="open",
                doc_path=(PLAN_DIR / PLAN_FILE).as_posix(),
            )
        )
        self.workspace.supervisor.record_candidate(generation)
        await self.repository.record_mutation(
            {"generation": generation.number, "status": status, "rationale": rationale}
        )
        return touched

    async def record_plan_eval(
        self,
        generation: Generation,
        entries: Iterable[dict[str, Any]],
        objective: str,
        rationale: str,
        status: str = "evaluated",
    ) -> list[str]:
        touched = _touched_paths(entries)
        eval_path = generation.path / PLAN_DIR / PLAN_EVAL_FILE
        if not eval_path.exists():
            return touched
        approved, reason = parse_plan_verdict(
            eval_path.read_text(encoding="utf-8", errors="replace")
        )
        root = self.current_plan_root(generation)
        if root is not None:
            root.approved = approved
            root.eval_reasoning = reason
            if not approved:
                # Superseded the moment it is turned down, not on the next
                # draft: otherwise `current_plan_root` keeps answering with a
                # plan already rejected for as long as the redraft takes.
                root.status = "superseded"
            self.workspace.supervisor.record_candidate(generation)
        await self.repository.record_mutation(
            {
                "generation": generation.number,
                "status": status,
                "approved": approved,
                "rationale": rationale,
            }
        )
        return touched

    async def mechanical_reject_plan(self, generation: Generation, reasons: list[str]) -> None:
        """Reject a plan without a harness job, for the fabrication check.

        No ``plan.eval.md`` was ever written -- there is no harness job here to
        write one -- so this does by hand what ``record_plan_eval`` does from
        that file: mark the root rejected and superseded, so a human (or the
        next redraft) sees the same shape of history either way.
        """
        root = self.current_plan_root(generation)
        if root is None:
            return
        root.approved = False
        root.eval_reasoning = "names code that does not exist: " + ", ".join(reasons)
        root.status = "superseded"
        self.workspace.supervisor.record_candidate(generation)
        await self.repository.record_mutation(
            {
                "generation": generation.number,
                "status": "evaluated",
                "approved": False,
                "rationale": f"mechanical check (no harness job): {', '.join(reasons)}",
            }
        )

    async def record_plan_decompose(
        self,
        generation: Generation,
        entries: Iterable[dict[str, Any]],
        objective: str,
        rationale: str,
        status: str = "decomposed",
    ) -> list[str]:
        """Record a decomposition. ``objective`` is the id of the node being
        split, not the generation's standing objective -- ``EvolverBehavior.
        _decompose`` passes it through ``_through_harness``'s ``record_key``
        override, since a decompose job's target varies node to node while
        the recorder signature it shares with ``record_harness_changes``
        does not carry the pipeline ``state`` to read it from otherwise.
        """
        node_id = objective
        touched = _touched_paths(entries)
        node = self.plan_node(generation, node_id)
        if node is None:
            return touched
        doc_path = generation.path / PLAN_NODES_DIR / f"{node_id}.md"
        if not doc_path.exists():
            return touched
        children = parse_plan_children(doc_path.read_text(encoding="utf-8", errors="replace"))
        node.doc_path = (PLAN_NODES_DIR / f"{node_id}.md").as_posix()
        if children is None:
            node.kind = "leaf"
            node.status = "leaf"
        else:
            node.kind = "split"
            node.status = "done"
            created: list[PlanNode] = [
                PlanNode(
                    id=f"{node_id}.{index}",
                    parent_id=node_id,
                    title=child["title"],
                    reasoning=child["reasoning"],
                    kind="split",
                    status="open",
                )
                for index, child in enumerate(children, start=1)
            ]
            for child_node, child in zip(created, children, strict=True):
                child_node.depends_on = [
                    created[position - 1].id
                    for position in child["depends_on"]
                    if 1 <= position <= len(created) and created[position - 1] is not child_node
                ]
            generation.plan.extend(created)
        self.workspace.supervisor.record_candidate(generation)
        await self.repository.record_mutation(
            {
                "generation": generation.number,
                "status": status,
                "node": node_id,
                "rationale": rationale,
            }
        )
        return touched

    async def mark_plan_node_undecomposed(self, generation: Generation, node_id: str) -> None:
        """A decompose job that answered without writing this node's file.

        Found live: discarding the whole generation over one stuck node threw
        away every sibling it had already split -- sometimes a dozen of them.
        Treated as a leaf instead of lost: an item the model could not
        classify inside its step budget is conservatively minimal, not
        absent, and the queue can carry on around it.
        """
        node = self.plan_node(generation, node_id)
        if node is None:
            return
        node.kind = "leaf"
        node.status = "leaf"
        self.workspace.supervisor.record_candidate(generation)

    async def autofix(self, generation: Generation) -> dict[str, object]:
        outcome = await self.repairer.autofix(generation)
        await self.repository.record_mutation(
            {
                "generation": generation.number,
                "status": "repaired",
                "how": "ruff --fix",
                "exit_code": outcome.get("exit_code"),
            }
        )
        return outcome

    async def candidate_changed_nothing(self, generation: Generation) -> bool:
        """Whether the candidate's working tree is, right now, identical to its
        parent commit -- the ground truth `apply_generation` checks before
        committing, asked earlier so a generation the free repair fixed back
        into nothing does not spend a validation run first.

        `ruff --fix` runs outside `record_harness_changes` (it is a subprocess,
        not a harness edit), so `generation.changes` still lists the propose
        stage's edit even after the fix undoes it byte-for-byte. The list is
        a record of what was written, not of what is still there -- only git
        status answers "is there still a diff" honestly.

        A candidate is ordinarily a `git worktree`, sharing the checkout's own
        history; a host where `git worktree add` itself failed falls back to a
        plain directory copy with no `.git` of its own (see `CandidateWorkspace.
        create`). `git -C` does not refuse there -- it walks up looking for a
        `.git` the way it always does, and a candidate created under this
        project's own generations/ (or, in a test, a pytest tmp_path nested
        inside this checkout) sits right below one: the checkout's. Trusting
        `status` there answers "is the checkout clean", a different generation
        entirely, and it can say yes while the candidate itself is full of
        uncommitted work. `rev-parse --show-toplevel` catches this before
        `status` ever gets asked: a real worktree's top level is the candidate
        itself, and anything else -- a foreign repository, or none at all --
        is an environment limit, not evidence of a clean tree, so it reads as
        "no" rather than short-circuiting a repair that may have real work
        left to validate.
        """
        candidate = await self._own_repository(generation)
        if candidate is None:
            return False
        try:
            return await candidate.is_clean()
        except GitError:
            return False

    async def candidate_changed_source(self, generation: Generation) -> bool | None:
        """Whether the candidate still differs from its parent under
        ``src/evomesh/``, or ``None`` when git cannot say (same fallback as
        :meth:`candidate_changed_nothing`).

        Asked at promotion, not at propose: a repair can undo the source edit
        a substantive objective was answered with and leave only a test
        behind, which validates just as well.
        """
        candidate = await self._own_repository(generation)
        if candidate is None:
            return None
        try:
            status = await candidate.run("status", "--porcelain", "--", "src/evomesh")
        except GitError:
            return None
        return bool(status.strip())

    async def _own_repository(self, generation: Generation) -> GitRepository | None:
        """The candidate as a repository, or ``None`` when its top level is
        not the candidate itself -- see :meth:`candidate_changed_nothing`."""
        candidate = GitRepository(generation.path, self.identity)
        try:
            top_level = (await candidate.run("rev-parse", "--show-toplevel")).strip()
        except GitError:
            return None
        resolved_top_level = await asyncio.to_thread(lambda: Path(top_level).resolve())
        resolved_candidate = await asyncio.to_thread(generation.path.resolve)
        return candidate if resolved_top_level == resolved_candidate else None

    async def record_harness_changes(
        self,
        generation: Generation,
        entries: Iterable[dict[str, Any]],
        objective: str,
        rationale: str,
        status: str = "applied",
    ) -> list[str]:
        """Record what the harness actually wrote, not what the model said.

        The old contract took the model's word for the path it had changed. A
        session records every applied edit and write with its diff, so the
        generation's history now comes from what reached the disk, and the
        model's prose is only the reason attached to it.
        """
        if generation.status != GenerationStatus.CANDIDATE:
            raise ValueError("Mutations may only be applied to candidates")
        generation.objective = generation.objective or objective
        touched: list[str] = []
        for entry in entries:
            if entry.get("kind") not in ("edit", "write", "delete"):
                continue
            path = str(entry.get("path") or "")
            if not path:
                continue
            touched.append(path)
            generation.changes.append(
                GenerationChange(
                    path=path,
                    rationale=rationale,
                    kind="repair" if status == "repaired" else "mutation",
                    diff=str(entry.get("diff") or ""),
                )
            )
            await self.repository.record_mutation(
                {
                    "generation": generation.number,
                    "objective": objective,
                    "path": path,
                    "rationale": rationale,
                    "status": status,
                }
            )
        self.workspace.supervisor.record_candidate(generation)
        return touched

    async def validate(self, generation: Generation) -> ValidationResult:
        result = await self.validator.validate(generation)
        await self.repository.record_mutation(
            {
                "generation": generation.number,
                "status": "validated",
                "passed": result.passed,
            }
        )
        return result

    def begin_validation(self, generation: Generation, timeout: float = 1800.0) -> ValidationRun:
        """Start the suite off the caller's cycle, and hand back a handle.

        A separate lane from the harness worker on purpose: a tool loop is the
        GPU and a validation run is CPU and disk, so making one wait for the
        other would be a queue whose only effect is to slow the machine down.
        """
        if self.validation is not None and self.validation.generation == generation.number:
            return self.validation

        async def run() -> ValidationResult:
            return await asyncio.wait_for(self.validate(generation), timeout=timeout)

        self.validation = ValidationRun(
            generation=generation.number,
            task=asyncio.create_task(run(), name=f"evomesh-validate-{generation.number}"),
        )
        self.validation.task.add_done_callback(self._lane_finished)
        return self.validation

    def _lane_finished(self, _task: asyncio.Task[Any]) -> None:
        if self.on_lane_finished is not None:
            self.on_lane_finished()

    def validation_run(self, number: int) -> ValidationRun | None:
        run = self.validation
        return run if run is not None and run.generation == number else None

    async def take_validation(self, run: ValidationRun) -> ValidationResult:
        """The verdict, and the lane is free again.

        A timeout is reported as blocked rather than failed: the candidate never
        got a verdict, and a suite the machine could not finish is not something
        the candidate did wrong.
        """
        self.validation = None
        try:
            return run.task.result()
        except TimeoutError:
            return ValidationResult(
                passed=False,
                commands=[
                    {
                        "command": "uv run pytest",
                        "exit_code": -1,
                        "output": (
                            f"validation of generation {run.generation} did not finish "
                            f"in {run.seconds:.0f}s and was stopped"
                        ),
                        "blocked": True,
                    }
                ],
            )

    async def cancel_validation(self) -> None:
        """Stop a run the mesh is not going to wait for.

        Nothing is resumed: a candidate is a copy on disk, and re-running the
        suite on it costs time and nothing else -- which is cheaper than a
        pipeline waiting on a task that no longer exists.
        """
        run, self.validation = self.validation, None
        if run is None:
            return
        run.task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await run.task

    async def finish_candidate(self, number: int, *, passed: bool) -> Generation:
        generation = self.candidate(number)
        if not passed:
            generation.status = GenerationStatus.FAILED
            self.workspace.supervisor.record_candidate(generation)
        await self.repository.record_mutation(
            {"generation": number, "status": "reviewed", "passed": passed}
        )
        return generation

    async def apply_generation(self, number: int, objective: str = "") -> str:
        """Land a candidate's change on the tree the mesh is checked out from.

        Until this ran, promotion moved a number in a metadata file and the mesh
        went on executing exactly the code it always had. Git is the lineage, so
        the candidate's commit is cherry-picked onto the checkout rather than the
        directory being swapped: one canonical tree, an ordinary history, and a
        commit to reset to when the generation turns out to be a mistake.
        """
        generation = self.candidate(number)
        checkout = self.checkout()
        if not await checkout.is_clean():
            raise GitError(
                "the working tree has uncommitted changes; a generation is never "
                "applied over work a human has not committed"
            )
        commit = generation.git_commit
        if commit is None:
            candidate = GitRepository(generation.path, self.identity)
            # Checked before the backlog doc is written: write_backlog() adds a
            # new file to the candidate's own tree, which makes `status()`
            # non-empty on its own -- checking after that write, as this used
            # to, meant a candidate whose real edits net to zero (an edit and
            # its own later revert, both "recorded" but cancelling out) always
            # had *something* to commit: the doc describing a change that was
            # not actually there. That is exactly how a no-op generation
            # landed on main with nothing but a docs/evolution/*.md entry to
            # show for it.
            if not (await candidate.status()).strip():
                raise GitError(f"generation {number} changed nothing to apply")
            # Written before the commit, so a generation always carries its
            # own explanation into the commit that lands it.
            generation.objective = generation.objective or objective
            state = await self.pipeline_state()
            self.write_backlog(generation, int(state.get("repairs", 0)))
            commit = await candidate.commit_mutation(number, objective)
            generation.git_commit = commit
            self.workspace.supervisor.record_candidate(generation)
        previous = await checkout.current_commit()
        applied = await checkout.cherry_pick(commit)
        self.workspace.supervisor.record_commits(active=applied, last_known_good=previous)
        await self.repository.record_mutation(
            {
                "generation": number,
                "status": "applied-to-tree",
                "commit": applied,
                "previous": previous,
            }
        )
        # Publish before the restart is asked for: this process may not be here
        # a moment from now, and an unpublished generation would then sit in a
        # local tree with nothing left running to notice.
        self.last_publish = await self.publish(applied)
        if self.on_generation_landed is not None:
            self.on_generation_landed(number, applied)
        return applied

    def checkout(self) -> GitRepository:
        return GitRepository(self.workspace.repository_root, self.identity)

    # -- the backlog ----------------------------------------------------

    def write_backlog(self, generation: Generation, repairs: int = 0) -> Path:
        """Write why this generation exists, into the generation itself.

        It goes inside the candidate so ``git add -A`` picks it up and the
        reasoning lands in the same commit as the code. A month from now the
        question about any of these commits is "why did it do that", and the
        answer has to be in the repository, not in a SQLite file on one machine.
        """
        directory = generation.path / BACKLOG_DIR
        directory.mkdir(parents=True, exist_ok=True)
        entry = directory / f"{generation.number:06d}.md"
        entry.write_text(self.render_backlog(generation, repairs), encoding="utf-8")
        self._reindex_backlog(directory)
        return entry

    @staticmethod
    def _generation_summary(generation: Generation) -> str:
        """One line on what *this* generation is actually trying to do.

        ``generation.objective`` is the Evolver's standing goal -- the same
        sentence on every single generation, because it is the goal, not the
        plan. Printing it under "Why this change" answered a question nobody
        asked and left the one that matters ("why did it touch *this* file")
        unanswered. The model's own rationale for its edit is what actually
        varies generation to generation, so that is the headline now; the
        standing goal moves to a quiet note underneath for context.

        A planned generation's headline comes from the plan itself when one
        was approved: a leaf's own end-of-job ``RATIONALE:`` line answers "why
        this file", one sentence at a time, and is exactly as likely to be
        missing here as anywhere else in the harness -- the plan's prose is
        the one place a human wrote (had the model write) a paragraph about
        the whole generation before any of it existed, and it survives even
        when every leaf's own rationale came back empty.
        """
        approved_root = next(
            (
                node
                for node in generation.plan
                if node.parent_id is None and node.approved is True and node.reasoning.strip()
            ),
            None,
        )
        if approved_root is not None:
            return f"**What it set out to do.** {excerpt(approved_root.reasoning, 400)}"
        mutations = [change for change in generation.changes if change.kind != "repair"]
        for change in mutations:
            reason = change.rationale.strip()
            if reason:
                return f"**What it set out to do.** {reason}"
        if mutations:
            files = ", ".join(sorted({change.path for change in mutations}))
            return (
                f"**What it set out to do.** The model changed `{files}` but gave "
                "no rationale for it -- see the diff below for what actually moved."
            )
        return "**What it set out to do.** No file changes were recorded."

    @staticmethod
    def _plan_children(nodes: list[PlanNode], parent_id: str | None) -> list[PlanNode]:
        return [node for node in nodes if node.parent_id == parent_id]

    def _render_plan_node(
        self, nodes: list[PlanNode], node: PlanNode, depth: int, lines: list[str]
    ) -> None:
        indent = "  " * depth
        tag = " (leaf)" if node.kind == "leaf" else ""
        headline = node.reasoning.strip().splitlines()[0] if node.reasoning.strip() else ""
        summary = f" — {excerpt(headline, 160)}" if headline else ""
        lines.append(f"{indent}- **{node.title}**{tag}{summary}")
        for child in self._plan_children(nodes, node.id):
            self._render_plan_node(nodes, child, depth + 1, lines)

    def _render_plan_tree(self, generation: Generation) -> list[str]:
        """The plan behind this generation's changes, when it was planned.

        Nothing here assumes ``generation.plan`` is populated -- a generation
        authored the old, flat way (``auto_plan=False``) renders no section
        at all, same as before this existed.
        """
        if not generation.plan:
            return []
        lines = ["## How it was planned", ""]
        for root in self._plan_children(generation.plan, None):
            state = " (superseded)" if root.status == "superseded" else ""
            if root.approved is True:
                verdict = " — approved"
            elif root.approved is False:
                reason = root.eval_reasoning.strip()
                verdict = f" — rejected: {reason}" if reason else " — rejected"
            else:
                verdict = ""
            lines.append(f"- **Plan draft**{state}{verdict}")
            # The plan's own prose -- what the model actually wrote to justify
            # this generation -- not just whether it passed review. Without
            # this, the only trace of *why* a plan was drafted the way it was
            # lived in a diff nobody reads a month later.
            plan_text = root.reasoning.strip()
            if plan_text:
                lines.append("")
                lines.extend(f"  > {line}" for line in clip(plan_text, 1500).splitlines())
                lines.append("")
            for child in self._plan_children(generation.plan, root.id):
                self._render_plan_node(generation.plan, child, 1, lines)
        lines.append("")
        return lines

    def render_backlog(self, generation: Generation, repairs: int = 0) -> str:
        validation = self.read_validation(generation)
        lines = [
            f"# Generation {generation.number}",
            "",
            f"- **Opened:** {generation.created_at:%Y-%m-%d %H:%M UTC}",
            f"- **Parent generation:** {generation.parent if generation.parent else '-'}",
            f"- **Author:** {self.identity}",
            "",
            *self._render_plan_tree(generation),
            "## Why this change",
            "",
            self._generation_summary(generation),
            "",
            f"*Standing goal: {generation.objective or '(none recorded)'}*",
            "",
        ]
        if generation.changes:
            lines += ["### What it changed, and the reason it gave", ""]
            for index, change in enumerate(generation.changes, start=1):
                label = "Repair" if change.kind == "repair" else "Change"
                reason = change.rationale.strip() or "(the model gave no rationale)"
                lines += [f"{index}. **{label} to `{change.path}`** — {reason}"]
                # The diff travels with the reason. "Why did it do that" is asked
                # about a commit a month later, and the answer belongs in the
                # repository rather than in a session file on one machine.
                if change.diff.strip():
                    lines += [
                        "",
                        "   ```diff",
                        *(
                            f"   {line}"
                            for line in clip(change.diff, 1200, keep_end=False).splitlines()
                        ),
                        "   ```",
                        "",
                    ]
            lines.append("")
        else:
            lines += ["No file changes were recorded for this generation.", ""]

        lines += ["## How it was checked", ""]
        if validation is None:
            lines += [
                "Validation did not run, so this generation carries no verdict.",
                "",
            ]
        else:
            verdict = "passed" if validation.passed else "failed"
            lines.append(f"The suite **{verdict}**:")
            lines.append("")
            lines.append("| Command | Exit |")
            lines.append("| --- | --- |")
            for command in validation.commands:
                lines.append(f"| `{command.get('command')}` | {command.get('exit_code')} |")
            lines.append("")
            if failure := validation.failure():
                lines += [
                    "The failing command reported:",
                    "",
                    "```",
                    clip(str(failure.get("output", "")), 1200).strip(),
                    "```",
                    "",
                ]
        if repairs:
            lines += [
                f"It repaired itself **{repairs} time{'s' if repairs != 1 else ''}** "
                "before reaching that verdict.",
                "",
            ]
        return "\n".join(lines)

    @staticmethod
    def _reindex_backlog(directory: Path) -> None:
        """Rebuild the index from the entries on disk, newest first."""
        entries = sorted(
            (path for path in directory.glob("[0-9]*.md")),
            key=lambda path: path.stem,
            reverse=True,
        )
        rows = []
        for path in entries:
            heading = ""
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("**What it set out to do.**"):
                    heading = line.removeprefix("**What it set out to do.**").strip()
                    break
            rows.append(f"- [Generation {int(path.stem)}]({path.name}) — {heading or '-'}")
        (directory / "README.md").write_text(
            "\n".join(
                [
                    "# Evolution backlog",
                    "",
                    "One entry per generation the Environment Evolver produced: what it",
                    "changed, the reason it gave, and how the change was checked. Written",
                    "by the mesh itself, into the same commit as the code.",
                    "",
                    *rows,
                    "",
                ]
            ),
            encoding="utf-8",
        )

    async def publish(self, commit: str) -> str:
        """Push the landed commit, and report the outcome as one plain sentence.

        A push is the last step, never a gate: the generation is already in the
        tree, so a remote that refuses it is news to report, not a reason to
        unwind work that validated.
        """
        if not self.publish_policy.enabled:
            self.workspace.supervisor.record_publish(
                commit=commit, published=False, detail="auto_push is off"
            )
            return "not published (auto_push is off)"
        checkout = self.checkout()
        try:
            await checkout.push(self.publish_policy.remote, self.publish_policy.branch)
        except GitError as exc:
            detail = excerpt(str(exc), 300)
            logger.warning("Could not publish %s: %s", commit[:8], detail)
            self.workspace.supervisor.record_publish(commit=commit, published=False, detail=detail)
            await self.repository.record_mutation(
                {"status": "publish-failed", "commit": commit, "detail": detail}
            )
            return f"not published: {detail}"
        branch = self.publish_policy.branch or await checkout.current_branch()
        where = f"{self.publish_policy.remote}/{branch}"
        self.workspace.supervisor.record_publish(commit=commit, published=True, detail=where)
        await self.repository.record_mutation(
            {"status": "published", "commit": commit, "remote": where}
        )
        return f"published to {where}"

    async def revert_tree(self) -> str | None:
        """Put the checkout back on the commit the last promotion replaced."""
        metadata = self.workspace.supervisor.metadata()
        target = metadata.get("last_known_good_commit")
        if not target:
            return None
        checkout = self.checkout()
        if not await checkout.is_clean():
            raise GitError("the working tree has uncommitted changes; refusing to reset over them")
        restored = await checkout.reset_to(str(target))
        self.workspace.supervisor.record_commits(active=restored, last_known_good=str(target))
        await self.repository.record_mutation({"status": "reverted", "commit": restored})
        return restored

    async def promote_candidate(self, number: int, objective: str = "") -> str:
        """Apply the generation first; only a landed change earns the promotion."""
        applied = await self.apply_generation(number, objective)
        self.workspace.supervisor.promote(number)
        await self.repository.record_mutation(
            {"generation": number, "status": "promoted", "commit": applied}
        )
        return applied

    async def decide_candidate(self, number: int, *, promote: bool, objective: str = "") -> str:
        """Promote or discard without a human, and leave a record that it happened."""
        if promote:
            applied = await self.promote_candidate(number, objective)
            return applied
        self.workspace.supervisor.discard(number)
        await self.repository.record_mutation(
            {"generation": number, "status": "discarded", "decided_by": "policy"}
        )
        return ""

    async def commit_candidate(self, generation: Generation, objective: str) -> str:
        git = GitRepository(generation.path, self.identity)
        commit = await git.commit_mutation(generation.number, objective)
        generation.git_commit = commit
        self.workspace.supervisor.record_candidate(generation)
        return commit


class GenerationExecutor:
    """The candidate-generation pipeline as a WorkExecutor: the work runs in
    the candidate generation opened for it; promoted means completed,
    discarded means failed, and cancelling discards the open candidate."""

    kind = "generation"

    def __init__(self, supervisor: GenerationSupervisor) -> None:
        self.supervisor = supervisor

    async def submit(self, work: WorkItem, scope: ExecutionScope) -> WorkHandle:
        number = int(scope.reference)
        if str(number) not in self.supervisor.metadata().get("candidates", {}):
            raise ValueError(f"generation {number} is not an open candidate")
        return {
            "executor": self.kind,
            "ref": str(number),
            "assignee": scope.assignee,
            "workspace": scope.workspace,
        }

    def inspect(self, handle: Mapping[str, str]) -> WorkInspection:
        number = int(handle["ref"])
        decided = self.supervisor.outcome(number)
        evidence = {"generation": number, "outcome": decided}
        if decided is None:
            return WorkInspection(WorkState.PENDING, evidence)
        if decided == "promoted":
            return WorkInspection(WorkState.COMPLETED, evidence)
        return WorkInspection(WorkState.FAILED, evidence)

    async def request_cancel(self, handle: Mapping[str, str]) -> WorkInspection:
        number = int(handle["ref"])
        if self.supervisor.outcome(number) is None:
            self.supervisor.discard(number)
            return WorkInspection(WorkState.CANCELLED, {"generation": number})
        return self.inspect(handle)

    def outcome(self, item: WorkItem) -> WorkOutcome | None:
        """How ``item`` ended, or ``None`` while it runs (a convenience over
        :meth:`inspect` for callers that only need the verdict)."""
        handle = work_handle(item)
        if handle is None:
            return None
        state = self.inspect(handle).state
        if state is WorkState.COMPLETED:
            return WorkOutcome.COMPLETED
        return WorkOutcome.FAILED if state is WorkState.FAILED else None


def fault_candidate(fault: RuntimeFault) -> Candidate:
    """A logged traceback as an improvement proposal: urgent, and more so the
    more often it happened. Verified only after three opens without it."""
    return Candidate(
        ref=f"fault:{fault.module}.{fault.function}:{fault.exception}",
        kind=EVIDENCE_RUNTIME_FAULT,
        title=f"Fix {fault.exception} in {fault.module}.{fault.function}",
        problem=f"{fault.exception} raised at {fault.module}:{fault.line}",
        component=fault.module,
        evidence={"count": fault.count, "last_seen": fault.last_seen, "line": fault.line},
        factors=PriorityFactors(
            impact=2.0, urgency=2.5, recurrence=float(min(5, max(1, fault.count)))
        ),
        observations=3,
    )


def backlog_candidate(item: Improvement) -> Candidate:
    """A human-written backlog item: strategic, and cheaper once planned."""
    open_steps = [step for step in item.steps if not step.done]
    return Candidate(
        ref=f"item:{item.title}",
        kind=EVIDENCE_HUMAN_BACKLOG,
        title=item.title,
        problem=item.detail or item.title,
        component=next(iter(item.source_paths), "evomesh"),
        evidence={"steps": len(item.steps), "open_steps": len(open_steps)},
        factors=PriorityFactors(
            impact=2.0, strategic_value=2.0, estimated_effort=1.0 if item.steps else 1.5
        ),
        # More than one step is more than one piece of work: a stage DAG.
        stages=tuple(f"step:{step.number}" for step in open_steps)
        if len(item.steps) > 1
        else (),
    )


def baseline_candidate(result: BaselineResult) -> Candidate:
    """A red suite on the live tree: the most urgent thing there is."""
    failures = sorted(result.failures)
    return Candidate(
        ref="tests:" + hashlib.sha256("|".join(failures).encode("utf-8")).hexdigest()[:12],
        kind=EVIDENCE_FAILING_TESTS,
        title=f"Make {len(failures)} failing test(s) pass",
        problem=", ".join(failures[:5]),
        component="tests",
        evidence={"failures": failures[:20]},
        factors=PriorityFactors(impact=3.0, urgency=3.0),
    )
