"""The Idea Scout: the one agent that adds to the improvement backlog, and
only what a human approved -- by command, by a reply, or by a thumbs-up."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from evomesh.codebase import IMPROVEMENTS_FILE, Improvement, Step, open_improvements
from evomesh.config import HarnessSettings
from evomesh.contracts import Message, TelegramSettings
from evomesh.environment import Environment
from evomesh.evolution import (
    PICK_SCOUT,
    CandidateWorkspace,
    EnvironmentEvolver,
    Generation,
    GenerationStatus,
)
from evomesh.ideas import IDEAS_AGENT_ID, IdeaBook, IdeaScoutBehavior, verdict_of
from evomesh.models import ChatTurn, MockProvider
from evomesh.storage import SQLiteRepository
from evomesh.telegram import TelegramChannel
from tests.test_bdi import settings_for
from tests.test_cycles import git_project
from tests.test_w3_improvement import _fixture

TOTAL = Improvement(
    "Stop total() adding a stray one",
    "total() in src/evomesh/pricing.py returns the sum plus one, so every price the "
    "mesh reports is off by one.\n> return sum(items) + 1",
    (Step(1, "src/evomesh/pricing.py", "total", "return the plain sum of the items"),),
)
ANSWER = (
    "I read pricing.py.\n"
    "[ ] Stop total() adding a stray one\n"
    "total() in src/evomesh/pricing.py returns the sum plus one, so every price the "
    "mesh reports is off by one.\n"
    "> return sum(items) + 1\n"
    "1. src/evomesh/pricing.py `total` -- return the plain sum of the items\n"
    "RATIONALE: an off-by-one in every total"
)


async def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    _fixture(root)
    (root / IMPROVEMENTS_FILE).parent.mkdir(parents=True)
    (root / IMPROVEMENTS_FILE).write_text("# Improvement backlog\n", encoding="utf-8")
    return await git_project(root)


def _git_output(project: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(project), *arguments], capture_output=True, text=True, check=True
    ).stdout


def _log(project: Path) -> list[str]:
    return _git_output(project, "log", "--format=%s").splitlines()


# -- the words and the file ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("да", "approve"),
        ("тази е добра", "approve"),
        ("👍", "approve"),
        ("ok, go", "approve"),
        ("не е добра", "reject"),
        ("👎", "reject"),
        ("what does it change?", None),
    ],
)
def test_a_verdict_is_read_from_words_not_a_model(text: str, verdict: str | None) -> None:
    assert verdict_of(text) == verdict


async def test_approving_moves_that_idea_alone_and_commits_only_the_backlog(
    tmp_path: Path,
) -> None:
    project = await _project(tmp_path)
    (project / "scratch.txt").write_text("somebody else's work", encoding="utf-8")
    book = IdeaBook(tmp_path / "workspace" / "ideas.md", project)
    first = await book.add(TOTAL, "Idea Scout")
    second = await book.add(Improvement("Cache the survey", "x" * 90), "human")
    assert first is not None and second is not None
    assert await book.add(TOTAL, "someone else") is None, "one idea per title"

    reply = await book.approve(first.number, "human:console")

    assert "improvements.md" in reply
    items = open_improvements(project)
    assert [item.title for item in items] == [TOTAL.title], "only the approved idea"
    assert items[0].steps[0].symbol == "total"
    assert _log(project)[0].startswith("Ideas: #1 moves to the improvement backlog")
    status = _git_output(project, "status", "--porcelain")
    assert "improvements.md" not in status and "scratch.txt" in status
    assert [idea.number for idea in book.pending()] == [second.number]
    again = IdeaBook(book.path, project)
    assert again.get(first.number) is not None
    assert again.get(first.number).status == "approved"  # type: ignore[union-attr]
    assert "already approved" in await book.approve(first.number, "human:console")


async def test_a_rejected_idea_is_never_proposed_again(tmp_path: Path) -> None:
    project = await _project(tmp_path)
    book = IdeaBook(tmp_path / "ideas.md", project)
    idea = await book.add(TOTAL, "Idea Scout")
    assert idea is not None

    await book.reject(idea.number, "human:console", "not worth it")

    assert book.pending() == []
    assert await book.add(TOTAL, "Idea Scout") is None
    assert open_improvements(project) == []
    assert "rejected: human:console: not worth it" in book.path.read_text(encoding="utf-8")


# -- the agent, in a running mesh ------------------------------------------------


async def _mesh(
    tmp_path: Path, turns: list[ChatTurn]
) -> tuple[Environment, Path, MockProvider]:
    project = await _project(tmp_path)
    settings = settings_for(tmp_path)
    settings.harness = HarnessSettings(enabled=True, allow_write=False)
    provider = MockProvider(["no plan"], turns=turns)
    environment = Environment(settings, {"ollama": provider})
    await environment.start()
    # The fixture project is what this Scout reads and whose backlog it keeps.
    environment.ideas = IdeaBook(tmp_path / "workspace" / "ideas.md", project)
    await environment.start_agent(IDEAS_AGENT_ID, start_delay=3600)
    return environment, project, provider


async def _cycle_until(environment: Environment, condition, limit: int = 60) -> None:  # type: ignore[no-untyped-def]
    for _ in range(limit):
        await environment.cycle_agent(IDEAS_AGENT_ID)
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the Scout never got there")


def _scout(environment: Environment) -> IdeaScoutBehavior:
    behavior = environment.runtimes[IDEAS_AGENT_ID].behavior
    assert isinstance(behavior, IdeaScoutBehavior)
    return behavior


async def test_the_scout_reads_the_code_and_asks_a_human(tmp_path: Path) -> None:
    environment, project, provider = await _mesh(tmp_path, [ChatTurn(text=ANSWER)])
    book = environment.ideas

    await _cycle_until(environment, lambda: bool(book.pending()))

    idea = book.pending()[0]
    assert idea.title == TOTAL.title and idea.source == "Idea Scout"
    assert open_improvements(project) == [], "nothing is work before a human says so"
    jobs = list(environment.harness.queue.jobs.values())
    assert jobs and all(job.allow_write is False for job in jobs), "it only reads"
    assert any("Idea #1" in text for _, _, text in environment.announcement_log)
    await environment.stop()


async def test_the_scout_waits_once_enough_ideas_wait(tmp_path: Path) -> None:
    environment, _, _ = await _mesh(tmp_path, [ChatTurn(text=ANSWER)])
    await environment.ideas.add(TOTAL, "Idea Scout")
    _scout(environment).max_pending = 1

    outcome = await environment.cycle_agent(IDEAS_AGENT_ID)

    assert "wait for a human" in outcome.summary
    assert not environment.harness.queue.jobs, "no job while the human is behind"
    await environment.stop()


async def test_a_humans_rough_idea_is_rewritten_and_offered_back(tmp_path: Path) -> None:
    environment, _, _ = await _mesh(tmp_path, [ChatTurn(text=ANSWER)])
    book = environment.ideas
    await environment.send_message(
        Message(
            sender_id="human",
            recipient_id=IDEAS_AGENT_ID,
            content="prices look one too high, check the total function",
        )
    )
    reply = await environment.bus.receive("human", wait_seconds=10)
    assert "rewrite it" in reply.content

    await _cycle_until(environment, lambda: bool(book.pending()))

    idea = book.pending()[0]
    assert idea.source == "human" and idea.item.steps, "rewritten into the backlog's shape"
    job = next(iter(environment.harness.queue.jobs.values()))
    assert "prices look one too high" in job.objective
    await environment.stop()


async def test_idea_now_goes_straight_to_the_backlog_once_rewritten(tmp_path: Path) -> None:
    from evomesh.console import ConsoleChannel

    environment, project, _ = await _mesh(tmp_path, [ChatTurn(text=ANSWER)])
    console = ConsoleChannel(environment)

    reply = await console.route('/idea now "prices look one too high, fix total"')
    assert "straight into improvements.md" in reply
    await _cycle_until(environment, lambda: bool(open_improvements(project)))

    assert open_improvements(project)[0].title == TOTAL.title
    assert _log(project)[0].startswith("Ideas: #1")
    assert environment.ideas.pending() == []
    await environment.stop()


async def test_other_agents_propose_and_the_scout_files(tmp_path: Path) -> None:
    environment, project, _ = await _mesh(tmp_path, [ChatTurn(text="nothing")])
    book = environment.ideas
    bad = Improvement("Rewrite everything", "It would be nicer. " * 6)

    assert await environment.submit_idea(item=TOTAL, source="Guardian", sender_id="guardian")
    assert await environment.submit_idea(item=bad, source="Guardian", sender_id="guardian")
    scout = _scout(environment)
    await _cycle_until(environment, lambda: not scout.intake and bool(book.ideas()))
    for _ in range(3):
        await environment.cycle_agent(IDEAS_AGENT_ID)

    assert [idea.title for idea in book.ideas()] == [TOTAL.title], "the unanchored one is dropped"
    assert book.ideas()[0].source == "Guardian"
    assert open_improvements(project) == []
    await environment.stop()


async def test_the_evolvers_scout_hands_its_item_to_the_idea_scout(tmp_path: Path) -> None:
    project = await _project(tmp_path)
    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    evolver = EnvironmentEvolver(CandidateWorkspace(project, tmp_path / "generations"), repository)
    taken: list[Improvement] = []
    evolver.idea_sink = lambda item: taken.append(item) is None
    candidate = tmp_path / "candidate"
    (candidate / IMPROVEMENTS_FILE).parent.mkdir(parents=True)
    (candidate / IMPROVEMENTS_FILE).write_text("# Improvement backlog\n", encoding="utf-8")
    generation = Generation(number=1, status=GenerationStatus.CANDIDATE, path=candidate)

    entries = evolver.apply_backlog_answer(generation, PICK_SCOUT, "", ANSWER)

    assert entries == [] and [item.title for item in taken] == [TOTAL.title]
    assert "Stop total()" not in (candidate / IMPROVEMENTS_FILE).read_text(encoding="utf-8")


async def test_a_mesh_with_an_idea_scout_stops_the_evolver_scouting(tmp_path: Path) -> None:
    from evomesh.behaviors import EvolverBehavior

    settings = settings_for(tmp_path)
    settings.evolution.scout_when_idle = True
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()

    behavior = environment.behaviors["evolver"]
    assert isinstance(behavior, EvolverBehavior) and behavior.scout_when_idle is False
    await environment.stop()


# -- Telegram: a reply or a thumbs-up on the idea's own message ------------------


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.next_id = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "sendMessage":
            body = json.loads(request.content)
            self.next_id += 1
            self.sent.append({**body, "message_id": self.next_id})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})
        return httpx.Response(200, json={"ok": True, "result": []})


async def _telegram(tmp_path: Path) -> tuple[Environment, TelegramChannel, FakeTelegram, Path]:
    environment, project, _ = await _mesh(tmp_path, [ChatTurn(text="nothing")])
    fake = FakeTelegram()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    channel = TelegramChannel(
        environment,
        TelegramSettings(enabled=True, token="t", allowed_chat_ids=[42]),
        client,
    )
    channel._register_listener()  # pyright: ignore[reportPrivateUsage]
    return environment, channel, fake, project


async def test_a_thumbs_up_on_the_ideas_message_moves_it(tmp_path: Path) -> None:
    environment, channel, fake, project = await _telegram(tmp_path)
    idea = await environment.ideas.add(TOTAL, "Idea Scout")
    assert idea is not None
    await environment.announce_idea(idea)
    message_id = fake.sent[-1]["message_id"]

    await channel._consume(  # pyright: ignore[reportPrivateUsage]
        {
            "update_id": 1,
            "message_reaction": {
                "chat": {"id": 42},
                "message_id": message_id,
                "new_reaction": [{"type": "emoji", "emoji": "👍"}],
            },
        }
    )

    assert [item.title for item in open_improvements(project)] == [TOTAL.title]
    assert "improvements.md" in fake.sent[-1]["text"]
    await environment.stop()


async def test_a_thumbs_down_or_a_stranger_does_not_move_it(tmp_path: Path) -> None:
    environment, channel, fake, project = await _telegram(tmp_path)
    idea = await environment.ideas.add(TOTAL, "Idea Scout")
    assert idea is not None
    await environment.announce_idea(idea)
    message_id = fake.sent[-1]["message_id"]

    for chat, emoji in ((7, "👍"), (42, "🔥"), (42, "👎")):
        await channel._consume(  # pyright: ignore[reportPrivateUsage]
            {
                "update_id": 1,
                "message_reaction": {
                    "chat": {"id": chat},
                    "message_id": message_id,
                    "new_reaction": [{"type": "emoji", "emoji": emoji}],
                },
            }
        )

    assert open_improvements(project) == []
    assert environment.ideas.get(idea.number).status == "rejected"  # type: ignore[union-attr]
    await environment.stop()


async def test_a_reply_saying_it_is_good_moves_that_idea_alone(tmp_path: Path) -> None:
    environment, channel, fake, project = await _telegram(tmp_path)
    first = await environment.ideas.add(TOTAL, "Idea Scout")
    second = await environment.ideas.add(Improvement("Cache the survey", "x" * 90), "human")
    assert first is not None and second is not None
    await environment.announce_idea(first)
    await environment.announce_idea(second)
    first_message = fake.sent[0]["message_id"]

    await channel._consume(  # pyright: ignore[reportPrivateUsage]
        {
            "update_id": 2,
            "message": {
                "chat": {"id": 42},
                "text": "тази е добра",
                "reply_to_message": {"message_id": first_message},
            },
        }
    )

    assert [item.title for item in open_improvements(project)] == [TOTAL.title]
    assert [idea.number for idea in environment.ideas.pending()] == [second.number]
    await environment.stop()
