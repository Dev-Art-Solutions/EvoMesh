"""Ideas: proposals a human approves before they become work.

``docs/evolution/improvements.md`` is what the Evolver works on, so nothing
lands there on a model's say-so. Everything that is not a human editing that
file goes through one agent, the Idea Scout, and one file, ``ideas.md``:

- the Scout reads the code on its own, one module at a time, and writes down
  one vetted item per read-only harness job, for as long as fewer than
  ``ideas.max_pending`` ideas wait for review;
- a human sends it a rough idea (a chat message, ``/idea <text>``) and it
  rewrites the idea into the backlog's shape, anchored in the code it read;
- any other agent sends it a ``PROPOSE`` -- a raw line or a finished item.

Each idea is announced with its number. A human approves it with ``/idea
approve <n>``, a reply that says so, or a thumbs-up on its Telegram message,
and only then is it appended to improvements.md and committed. The Scout
itself never approves anything.

``ideas.md`` lives in the workspace, not the repository: pending ideas are a
conversation, and committing each one would dirty the tree the Evolver
refuses to promote over. Like memory.md it is plain Markdown a human can edit
while the mesh runs (rule 18).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from evomesh.codebase import (
    IMPROVEMENTS_FILE,
    SCOUT_RULES,
    Improvement,
    Step,
    append_item,
    done_improvements,
    item_from_answer,
    open_improvements,
    scout_modules,
    scout_needle,
    scout_task,
    vet_item,
    warning_leads,
)
from evomesh.cognition import CycleContext, CycleOutcome
from evomesh.contracts import AgentPhase, Message
from evomesh.evolution import BACKLOG_MAX_SECONDS, BACKLOG_MAX_STEPS
from evomesh.git import GitError, GitIdentity, GitRepository

logger = logging.getLogger(__name__)

IDEAS_AGENT_ID = "ideas"
IDEAS_FILE_NAME = "ideas.md"

IdeaStatus = Literal["pending", "approved", "rejected"]
_MARKS: dict[str, IdeaStatus] = {" ": "pending", "x": "approved", "-": "rejected"}
_MARK_OF = {status: mark for mark, status in _MARKS.items()}
_HEADER = re.compile(r"^- \[(?P<mark>[ x\-])\] #(?P<number>\d+) (?P<title>\S.*?)\s*$")
_META = ("from:", "approved:", "rejected:")

FILE_INTRO = """# Ideas

Proposals waiting for a human. Nothing here is work yet: an idea moves to
docs/evolution/improvements.md only when a human approves it -- `/idea approve <n>`,
a thumbs-up on its Telegram message, or a reply saying it is good. `/idea reject <n>`
(or a thumbs-down) sets it aside. Kept by the Idea Scout; edit freely, it is read back
every time.
"""

# Deterministic, like every other decision about work (rule 6): a word list,
# not a model asked what the human meant. A rejection word wins, so "не е
# добра" and "not good" read as no.
APPROVE_WORDS = frozenset(
    {
        "да", "добра", "добре", "добро", "одобрявам", "одобри", "одобрено", "харесва",
        "става", "давай", "ок", "ok", "okay", "yes", "y", "approve", "approved", "good",
        "great", "+1", "👍", "✅",
    }
)
REJECT_WORDS = frozenset(
    {
        "не", "лоша", "лошо", "отхвърли", "отхвърлям", "откажи", "отказ", "reject",
        "rejected", "no", "nope", "bad", "not", "-1", "👎", "❌",
    }
)
APPROVE_REACTIONS = frozenset({"👍"})
REJECT_REACTIONS = frozenset({"👎"})
# A human's message to the Scout that starts with one of these skips review:
# the rewritten idea goes straight to improvements.md.
DIRECT_PREFIXES = ("директно", "веднага", "direct", "now")


def verdict_of(text: str) -> Literal["approve", "reject"] | None:
    tokens = set(re.findall(r"[+\-]?\w+|[^\s\w]", text.casefold()))
    if tokens & REJECT_WORDS:
        return "reject"
    if tokens & APPROVE_WORDS:
        return "approve"
    return None


def _step_line(step: Step) -> str:
    return f"    {step.number}. [ ] {step.path} `{step.symbol}` -- {step.change}"


@dataclass
class Idea:
    number: int
    item: Improvement
    source: str
    status: IdeaStatus = "pending"
    note: str = ""

    @property
    def title(self) -> str:
        return self.item.title

    def block(self) -> str:
        lines = [f"- [{_MARK_OF[self.status]}] #{self.number} {self.item.title}"]
        lines.append(f"    from: {self.source}")
        if self.note and self.status != "pending":
            lines.append(f"    {self.status}: {self.note}")
        lines += [f"    {line}" for line in self.item.detail.splitlines() if line.strip()]
        lines += [_step_line(step) for step in self.item.steps]
        return "\n".join(lines)

    def chat_text(self) -> str:
        """How an idea reads on a phone: what it is, and how to say yes."""
        lines = [f"💡 Idea #{self.number} (from {self.source})", self.item.title]
        if self.item.detail:
            lines += ["", self.item.detail]
        if self.item.steps:
            lines += ["", "Steps:"]
            lines += [f"{step.number}. {step.describe()}" for step in self.item.steps]
        lines += [
            "",
            f"👍 or a reply 'да' moves it to improvements.md, 👎 rejects it "
            f"(/idea approve {self.number} | /idea reject {self.number}).",
        ]
        return "\n".join(lines)


def _parse(text: str) -> list[Idea]:
    ideas: list[Idea] = []
    header: re.Match[str] | None = None
    body: list[str] = []

    def close() -> None:
        if header is None:
            return
        source, note, rest = "", "", []
        for line in body:
            bare = line.strip()
            if bare.casefold().startswith("from:") and not source:
                source = bare[len("from:") :].strip()
            elif bare.casefold().startswith(("approved:", "rejected:")) and not note:
                note = bare.split(":", 1)[1].strip()
            elif bare:
                rest.append(bare)
        answer = "\n".join([f"[ ] {header.group('title')}", *rest])
        item = item_from_answer(answer) or Improvement(header.group("title"))
        ideas.append(
            Idea(
                number=int(header.group("number")),
                item=item,
                source=source or "unknown",
                status=_MARKS[header.group("mark")],
                note=note,
            )
        )

    for line in text.splitlines():
        match = _HEADER.match(line)
        if match is not None:
            close()
            header, body = match, []
        elif header is not None and (line.startswith((" ", "\t")) or not line.strip()):
            body.append(line)
        else:
            close()
            header, body = None, []
    close()
    return ideas


class IdeaBook:
    """``ideas.md``, and the one way an approved idea reaches the backlog."""

    def __init__(self, path: Path, root: Path, identity: GitIdentity | None = None) -> None:
        self.path = path
        self.root = root
        self.identity = identity or GitIdentity()
        self._lock = asyncio.Lock()

    def ideas(self) -> list[Idea]:
        if not self.path.is_file():
            return []
        return _parse(self.path.read_text(encoding="utf-8", errors="replace"))

    def pending(self) -> list[Idea]:
        return [idea for idea in self.ideas() if idea.status == "pending"]

    def get(self, number: int) -> Idea | None:
        return next((idea for idea in self.ideas() if idea.number == number), None)

    def _write(self, ideas: list[Idea]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(idea.block() for idea in ideas)
        self.path.write_text(f"{FILE_INTRO}\n{body}\n" if body else FILE_INTRO, encoding="utf-8")

    def known_titles(self) -> list[str]:
        """Everything already proposed, pending, done or rejected, anywhere."""
        return [
            *(idea.title for idea in self.ideas()),
            *(item.title for item in open_improvements(self.root)),
            *done_improvements(self.root),
        ]

    async def add(self, item: Improvement, source: str) -> Idea | None:
        """File a new pending idea; ``None`` when it is already known."""
        async with self._lock:
            known = {title.casefold() for title in self.known_titles()}
            if item.title.casefold() in known:
                return None
            ideas = self.ideas()
            number = max((idea.number for idea in ideas), default=0) + 1
            idea = Idea(number=number, item=item, source=source)
            self._write([*ideas, idea])
            return idea

    async def approve(self, number: int, actor: str) -> str:
        """Move one pending idea to improvements.md and commit that file
        alone, so the Evolver's clean-tree rule (rule 11) still holds."""
        async with self._lock:
            ideas = self.ideas()
            idea = next((item for item in ideas if item.number == number), None)
            if idea is None:
                return f"There is no idea #{number}."
            if idea.status != "pending":
                return f"Idea #{number} is already {idea.status}."
            if idea.title.casefold() in {
                *(item.title.casefold() for item in open_improvements(self.root)),
                *(title.casefold() for title in done_improvements(self.root)),
            }:
                idea.status, idea.note = "approved", f"{actor} (already in the backlog)"
                self._write(ideas)
                return f"Idea #{number} is already in improvements.md."
            append_item(self.root, idea.item)
            idea.status, idea.note = "approved", actor
            self._write(ideas)
            committed = await self._commit(
                f"Ideas: #{number} moves to the improvement backlog\n\n"
                f"{idea.title}\n\nProposed by {idea.source}, approved by {actor}."
            )
        if committed:
            return f"Idea #{number} is in improvements.md now: {idea.title}"
        return (
            f"Idea #{number} was added to improvements.md but could not be committed; "
            "commit docs/evolution/improvements.md by hand, or the Evolver will not "
            "promote over it."
        )

    async def reject(self, number: int, actor: str, reason: str = "") -> str:
        async with self._lock:
            ideas = self.ideas()
            idea = next((item for item in ideas if item.number == number), None)
            if idea is None:
                return f"There is no idea #{number}."
            if idea.status != "pending":
                return f"Idea #{number} is already {idea.status}."
            idea.status = "rejected"
            idea.note = f"{actor}: {reason}".strip(": ") if reason else actor
            self._write(ideas)
        return f"Idea #{number} is rejected and will not be proposed again."

    async def _commit(self, message: str) -> bool:
        repository = GitRepository(self.root, self.identity)
        path = IMPROVEMENTS_FILE.as_posix()
        for attempt in range(3):
            try:
                await repository.run("add", "--", path)
                await repository.run("commit", "-m", message, "--", path)
                return True
            except GitError as exc:
                # Most often another git command (a promotion) holding the
                # index for a moment.
                logger.warning("committing an approved idea failed (%s): %s", attempt + 1, exc)
                await asyncio.sleep(0.5 * (attempt + 1))
        return False


# -- the agent ------------------------------------------------------------------


@dataclass
class Draft:
    """Something to turn into an idea: a rough text to rewrite, or an item
    another agent already wrote, which only needs vetting."""

    source: str
    text: str = ""
    item: Improvement | None = None
    direct: bool = False
    human: bool = False

    def dump(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "text": self.text,
            "direct": self.direct,
            "human": self.human,
            "item": _item_dump(self.item) if self.item is not None else None,
        }

    @classmethod
    def load(cls, payload: dict[str, Any]) -> Draft:
        raw = payload.get("item")
        return cls(
            source=str(payload.get("source") or "unknown"),
            text=str(payload.get("text") or ""),
            item=_item_load(raw) if isinstance(raw, dict) else None,
            direct=bool(payload.get("direct")),
            human=bool(payload.get("human")),
        )


def _item_dump(item: Improvement) -> dict[str, Any]:
    return {
        "title": item.title,
        "detail": item.detail,
        "steps": [
            {"path": step.path, "symbol": step.symbol, "change": step.change}
            for step in item.steps
        ],
    }


def _item_load(payload: dict[str, Any]) -> Improvement | None:
    title = str(payload.get("title") or "").strip()
    if not title:
        return None
    steps = tuple(
        Step(index, str(step.get("path")), str(step.get("symbol")), str(step.get("change")))
        for index, step in enumerate(payload.get("steps") or [], start=1)
        if isinstance(step, dict)
    )
    return Improvement(title, str(payload.get("detail") or ""), steps)


INTAKE_STATE_KEY = "ideas.intake"


@dataclass
class IdeaScoutBehavior:
    """Keeps ideas coming without ever deciding one: one read-only harness job
    at a time, one idea per job, until ``max_pending`` wait for a human.

    Rewriting what a human or an agent sent comes before scouting; scouting
    yields the harness to any other agent's queued work, since a proposal is
    never more urgent than the work already decided on."""

    max_pending: int = 10
    enabled: bool = True
    name: str = "ideas"
    intake: deque[Draft] = field(default_factory=deque)
    job: int | None = None
    job_draft: Draft | None = None
    job_module: str = ""
    seed: int = 0
    recent_modules: deque[str] = field(default_factory=lambda: deque(maxlen=8))
    _restored: bool = False
    _dirty: bool = False

    # -- intake ---------------------------------------------------------------

    def accept_proposal(self, payload: dict[str, Any], sender: str) -> str:
        """A ``PROPOSE`` from another agent: a raw ``text`` to rewrite, or an
        ``item`` to vet. Never direct -- only a human skips review."""
        raw = payload.get("item")
        item = _item_load(raw) if isinstance(raw, dict) else None
        text = str(payload.get("text") or "").strip()
        if item is None and len(text) < 12:
            return "nothing to propose"
        human = sender == "human"
        self.intake.append(
            Draft(
                source=str(payload.get("source") or sender),
                text=text,
                item=item,
                direct=human and bool(payload.get("direct")),
                human=human,
            )
        )
        self._dirty = True
        return "queued"

    async def respond(self, context: CycleContext, message: Message) -> str:
        """Whatever reaches the Scout in plain words is an idea to rewrite --
        answered without a model call."""
        text = message.content.strip()
        if not text:
            return ""
        human = message.sender_id == "human"
        direct = False
        first = text.split(maxsplit=1)[0].casefold().strip(":,")
        if human and first in DIRECT_PREFIXES:
            direct = True
            text = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
        if len(text) < 12:
            return "Tell me the idea in a sentence or two, and I will write it up."
        source = "human" if human else _name(context, message.sender_id)
        self.intake.append(Draft(source=source, text=text, direct=direct, human=human))
        await self._save(context)
        where = "straight into improvements.md" if direct else "back to you as an idea to approve"
        return (
            f"Got it. I will read the code it touches, rewrite it as a backlog item "
            f"and send it {where}."
        )

    # -- the cycle ------------------------------------------------------------

    async def cycle(self, context: CycleContext) -> CycleOutcome:
        book = context.service("ideas")
        if not isinstance(book, IdeaBook):
            return CycleOutcome.idle("there is no idea book")
        await self._restore(context)
        if self._dirty:
            await self._save(context)
        harness: Any = context.service("harness")
        if self.job is not None:
            job = harness.job(self.job) if harness is not None else None
            if job is None:
                # The queue is not durable: a restart lost the job. Ask again.
                if self.job_draft is not None:
                    self.intake.appendleft(self.job_draft)
                self.job, self.job_draft = None, None
            elif job.open:
                return CycleOutcome(
                    summary=f"harness job {job.number} is reading the code",
                    phase=AgentPhase.AWAITING_HARNESS,
                )
            else:
                answer = job.result.answer if job.result is not None else ""
                draft, self.job, self.job_draft = self.job_draft, None, None
                summary = await self._settle(context, book, item_from_answer(answer), draft)
                await self._save(context)
                return CycleOutcome(summary=summary, step="idea", worked=True, again=True)
        if self.intake and self.intake[0].item is not None:
            draft = self.intake.popleft()
            summary = await self._settle(context, book, draft.item, draft)
            await self._save(context)
            return CycleOutcome(summary=summary, step="idea", worked=True, again=True)
        if not self.enabled and not self.intake:
            return CycleOutcome.idle("ideas are switched off (ideas.enabled)")
        if harness is None:
            return CycleOutcome.idle("the harness is off, so no code can be read for ideas")
        if self.intake:
            draft = self.intake.popleft()
            objective = _rewrite_objective(draft)
            label = f"rewrite an idea from {draft.source}"
        else:
            pending = len(book.pending())
            if pending >= self.max_pending:
                return CycleOutcome.idle(f"{pending} ideas wait for a human's review")
            if _others_waiting(harness, context.definition.id):
                return CycleOutcome.idle("yielding the harness to queued work")
            module = self._next_module(book.root)
            if module is None:
                return CycleOutcome.idle("no module left to read")
            draft = Draft(source=context.definition.name)
            objective = scout_task(
                book.root, _scout_objective(book, module), module
            )
            label = f"look for an idea in {module}.py"
        job = harness.submit(
            objective,
            agent_id=context.definition.id,
            root=book.root,
            label=label,
            max_steps=BACKLOG_MAX_STEPS,
            max_seconds=BACKLOG_MAX_SECONDS,
            notify=False,
            allow_write=False,
            catalog=False,
        )
        self.job, self.job_draft = job.number, draft
        await self._save(context)
        return CycleOutcome(
            summary=f"asked harness job {job.number} to {label}",
            step=label,
            worked=True,
            phase=AgentPhase.AWAITING_HARNESS,
        )

    async def _settle(
        self,
        context: CycleContext,
        book: IdeaBook,
        item: Improvement | None,
        draft: Draft | None,
    ) -> str:
        """Vet what came back and file it. A scout's or an agent's item that
        fails vetting is dropped; a human's idea is never lost -- it is filed
        without steps, which a plan generation adds later."""
        draft = draft or Draft(source=context.definition.name)
        if item is None and draft.human and draft.text:
            item = Improvement(draft.text.splitlines()[0][:100], draft.text)
        if item is None:
            return "the job wrote no idea"
        reason = vet_item(book.root, item)
        if reason is not None:
            if not draft.human:
                logger.info("idea %r dropped: %s", item.title, reason)
                return f"dropped {item.title!r}: {reason}"
            item = Improvement(
                item.title,
                "\n".join(
                    part
                    for part in (item.detail, f"Not yet anchored in the code: {reason}.")
                    if part
                ),
            )
        idea = await book.add(item, draft.source)
        if idea is None:
            return f"{item.title!r} is already proposed or done"
        environment: Any = context.service("environment")
        if draft.direct:
            message = await book.approve(idea.number, actor="human (sent directly)")
            if environment is not None:
                await environment.announce(message)
            return message
        if environment is not None:
            await environment.announce_idea(idea)
        return f"proposed idea #{idea.number}: {idea.title}"

    def _next_module(self, root: Path) -> str | None:
        leads = warning_leads(root)
        modules = [name for name in scout_modules(root, leads) if name not in self.recent_modules]
        if not modules:
            self.recent_modules.clear()
            modules = scout_modules(root, leads)
        if not modules:
            return None
        # The log points somewhere: look there first.
        pool = [name for name in modules if name in leads] or modules
        module = pool[self.seed % len(pool)]
        self.seed += 1
        self.recent_modules.append(module)
        return module

    # -- persistence ----------------------------------------------------------

    async def _restore(self, context: CycleContext) -> None:
        if self._restored:
            return
        self._restored = True
        repository: Any = context.service("repository")
        if repository is None:
            return
        stored = await repository.load_state(INTAKE_STATE_KEY)
        if isinstance(stored, list):
            restored = [Draft.load(entry) for entry in stored if isinstance(entry, dict)]
            self.intake.extendleft(reversed(restored))

    async def _save(self, context: CycleContext) -> None:
        """A human's idea survives a restart; a scout in flight is only redone."""
        repository: Any = context.service("repository")
        self._dirty = False
        if repository is None:
            return
        pending = list(self.intake)
        if self.job_draft is not None and (self.job_draft.text or self.job_draft.item):
            pending.insert(0, self.job_draft)
        await repository.save_state(INTAKE_STATE_KEY, [draft.dump() for draft in pending])


def _name(context: CycleContext, agent_id: str) -> str:
    registry: Any = context.service("registry")
    try:
        return str(registry.get(agent_id).name) if registry is not None else agent_id
    except KeyError:
        return agent_id


def _others_waiting(harness: Any, own_id: str) -> bool:
    queue = getattr(harness, "queue", None)
    jobs = getattr(queue, "jobs", {}) or {}
    return any(job.status.value == "queued" and job.agent_id != own_id for job in jobs.values())


def _scout_objective(book: IdeaBook, module: str) -> str:
    leads = warning_leads(book.root).get(module)
    lines = [
        f"{scout_needle(module)}: find ONE real problem or missing capability in "
        f"src/evomesh/{module}.py and write it down as one idea with its steps -- do "
        "not fix it. A human reads every idea before it becomes work, so make it "
        "worth their time.",
    ]
    if leads:
        lines.append(
            "What the running mesh logged from it since the file last changed "
            "(numbers masked) -- the strongest lead there is:\n"
            + "\n".join(f"  - {count}x {message}" for count, message in leads[:3])
        )
    lines.append(
        "Otherwise look for: a function that does the wrong thing on an input it "
        "really gets, error handling that swallows the cause, a fixed number that "
        "should come from settings, a loop with no backoff, work repeated every "
        "cycle that could be cached."
    )
    known = book.known_titles()
    if known:
        lines.append(
            "Already proposed, done or rejected -- do not propose these again:\n"
            + "\n".join(f"  - {title}" for title in known[-25:])
        )
    return "\n".join(lines)


def _rewrite_objective(draft: Draft) -> str:
    who = "A human" if draft.human else f"The agent {draft.source}"
    return "\n\n".join(
        (
            f"Rewrite an idea as one backlog item. {who} wrote it:\n\n{draft.text}",
            "Find the code it is about in src/evomesh/ -- grep for the names and words "
            "it mentions, then read two or three functions -- and write the idea down "
            "as ONE item with its steps. Change nothing. Keep the author's intent; "
            "sharpen it, do not replace it. If the code already does what the idea "
            "asks, say so on the detail line.",
            SCOUT_RULES.replace("from the OUTLINE ", "you found "),
        )
    )


__all__ = [
    "IDEAS_AGENT_ID",
    "IDEAS_FILE_NAME",
    "Draft",
    "Idea",
    "IdeaBook",
    "IdeaScoutBehavior",
    "verdict_of",
]
