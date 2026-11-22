"""Found in Iliya's chat with the Manager (2026-11-22): it kept no context,
its late answer never reached him, it waited minutes on a busy agent, a file
another agent wrote was named under the wrong directory, and work he asked
for queued behind the mesh's own background jobs."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from evomesh.bdi import render_conversation
from evomesh.config import EvolutionSettings, HarnessSettings, RuntimeSettings, Settings
from evomesh.console import ConsoleChannel
from evomesh.contracts import AgentDefinition, TelegramSettings
from evomesh.environment import Environment
from evomesh.harness import HarnessResult
from evomesh.harness_queue import HarnessQueue
from evomesh.models import MockProvider
from evomesh.telegram import TelegramChannel


def settings_for(tmp_path: Path, **harness: object) -> Settings:
    return Settings(
        data_path=tmp_path / "state.db",
        generation_path=tmp_path / "generations",
        workspace_path=tmp_path / "workspace",
        runtime=RuntimeSettings(cycle_seconds=3600, stagger_seconds=0),
        evolution=EvolutionSettings(autonomous=False),
        harness=HarnessSettings(**harness),  # type: ignore[arg-type]
    )


def test_the_conversation_keeps_the_newest_turns_both_sides() -> None:
    turns = [("human", "the 20 headlines, in Bulgarian")]
    turns += [("Manager", "1. Стоковият пазар ...\n2. ...")]
    turns += [("human", "as a PDF")]
    text = render_conversation(turns)
    assert text.splitlines() == [
        "- human: the 20 headlines, in Bulgarian",
        "- Manager: 1. Стоковият пазар ... 2. ...",
        "- human: as a PDF",
    ]
    long = [("human", "x" * 5000)] + [("Manager", f"answer {n}") for n in range(3)]
    kept = render_conversation(long)
    assert "answer 2" in kept and kept.count("x") <= 710, "each turn clipped"
    many = [("human", f"q{n} " + "y" * 600) for n in range(20)]
    assert len(render_conversation(many)) <= 4100 and "q19" in render_conversation(many)


async def test_an_agent_remembers_what_it_said(tmp_path: Path) -> None:
    environment = Environment(
        settings_for(tmp_path), {"ollama": MockProvider(["Here are 20 headlines."])}
    )
    await environment.start()
    try:
        agent = AgentDefinition(name="Reader", purpose="Read")
        await environment.register_agent(agent)
        await environment.start_agent(agent.id, start_delay=3600)
        console = ConsoleChannel(environment)
        await console.route("/chat Reader")
        await console.route("the 20 headlines, please")
        conversation = environment.runtimes[agent.id]._context().conversation  # noqa: SLF001
        assert conversation == [
            ("human", "the 20 headlines, please"),
            ("Reader", "Here are 20 headlines."),
        ]
    finally:
        await environment.stop()


async def test_a_late_answer_goes_to_the_human_with_its_file_where_it_is(
    tmp_path: Path,
) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    heard: list[str] = []

    async def bot(text: str) -> None:
        heard.append(text)

    try:
        agent = AgentDefinition(name="Reader", purpose="Read")
        await environment.register_agent(agent)
        await environment.add_agent_notifier(agent.id, bot)
        job = environment.harness_queue.submit("make the PDF", tmp_path, agent_id=agent.id)
        job.to_human = True
        environment.harness_queue.finish(
            job, HarnessResult(outcome="answered", answer="Done.\nFILE: news-bg.pdf")
        )
        await environment._deliver_harness(job)  # noqa: SLF001
        root = environment.default_harness_root(agent)
        assert heard == [f"Done.\nFILE: {root / 'news-bg.pdf'}"]
        assert not environment.bus._mailboxes.get(agent.id) or (  # noqa: SLF001
            environment.bus._mailboxes[agent.id].empty()  # noqa: SLF001
        ), "not fed back into its own inbox"
    finally:
        await environment.stop()


async def test_asking_a_busy_agent_says_so_at_once(tmp_path: Path) -> None:
    environment = Environment(
        settings_for(tmp_path), {"ollama": MockProvider(["FILE: report.pdf"])}
    )
    await environment.start()
    try:
        reader = AgentDefinition(name="Reader", purpose="Read")
        await environment.register_agent(reader)
        await environment.start_agent(reader.id, start_delay=3600)
        ask = environment._make_ask_agent("manager")  # noqa: SLF001

        # Not busy: the answer, its file named where the reader wrote it.
        answer = await ask("Reader", "where is the report?")
        assert answer == f"FILE: {environment.default_harness_root(reader) / 'report.pdf'}"

        environment.harness_queue.submit(
            "fetch 20 headlines", tmp_path, agent_id=reader.id, label="20 headlines as a PDF"
        )
        started = time.monotonic()
        busy = await ask("Reader", "how far did you get?")
        assert time.monotonic() - started < 2, "no three-minute wait"
        assert "Reader is busy" in busy and "20 headlines as a PDF" in busy
    finally:
        await environment.stop()


async def test_work_a_human_asked_for_goes_first_in_the_background_line() -> None:
    queue = HarnessQueue()
    first = queue.submit("evolver stage", Path("."), agent_id="evolver")
    second = queue.submit("scout", Path("."), agent_id="scout")
    asked = queue.submit("the PDF Iliya asked for", Path("."), agent_id="reader", urgent=True)
    reactive = queue.submit("a question", Path("."), agent_id="manager", priority=True)

    order = [await asyncio.wait_for(queue.take(lane="background"), 1) for _ in range(3)]
    assert [job.number for job in order] == [asked.number, first.number, second.number]
    assert (await asyncio.wait_for(queue.take(lane="priority"), 1)) is reactive

    mixed = HarnessQueue()
    plain = mixed.submit("plain", Path("."), agent_id="a")
    urgent = mixed.submit("urgent", Path("."), agent_id="b", urgent=True)
    assert (await asyncio.wait_for(mixed.take(), 1)) is urgent, "one worker for both lanes"
    assert (await asyncio.wait_for(mixed.take(), 1)) is plain


async def test_a_message_an_agent_sends_on_its_own_carries_its_file(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    try:
        agent = AgentDefinition(name="Reader", purpose="Read")
        await environment.register_agent(agent)
        root = environment.default_harness_root(agent)
        root.mkdir(parents=True, exist_ok=True)
        (root / "news-bg.pdf").write_bytes(b"%PDF-1.4")
        bot = TelegramChannel(
            environment,
            TelegramSettings(enabled=True, token="t", allowed_chat_ids=[42]),
            locked_agent_id=agent.id,
            locked_agent_name=agent.name,
        )
        sent_text: list[str] = []
        sent_files: list[Path] = []

        async def send(chat_id: int, text: str, markup: object = None) -> None:
            sent_text.append(text)

        async def document(chat_id: int, path: Path) -> None:
            sent_files.append(path)

        bot.send = send  # type: ignore[method-assign]
        bot._send_document = document  # type: ignore[method-assign]  # noqa: SLF001
        await bot.announce(f"✅ Done.\nFILE: {root / 'news-bg.pdf'}")
        assert sent_files == [root / "news-bg.pdf"], "the human gets the file, not a path"
    finally:
        await environment.stop()


async def test_a_human_hears_it_is_in_hand_long_before_the_answer(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Found on the Manager's bot: minutes of silence, then "Still working on
    that (harness job 31)", and the answer never came."""
    import pytest

    from evomesh import bdi
    from evomesh.cognition import CycleContext
    from evomesh.contracts import Message
    from evomesh.harness_queue import HarnessGateway
    from evomesh.memory import AgentMemory, MemoryBudget

    patch = pytest.MonkeyPatch()
    patch.setattr(bdi, "HUMAN_ACK_SECONDS", 0.2)
    patch.setattr(bdi, "HARNESS_RESPOND_POLL_SECONDS", 0.05)
    try:
        definition = AgentDefinition(name="Manager", purpose="p", harness_root=str(tmp_path))
        memory = AgentMemory(tmp_path / "workspace", definition)
        await memory.ensure()
        queue = HarnessQueue()
        context = CycleContext(
            definition=definition,
            provider=MockProvider(),
            memory=memory,
            budget=MemoryBudget(),
            services={"harness": HarnessGateway(queue, {})},
        )
        started = time.monotonic()
        said = await bdi.BDIBehavior().respond(
            context, Message(sender_id="human", recipient_id=definition.id, content="PDF")
        )
        assert time.monotonic() - started < 2
        assert said.startswith("Still working on that -- I will send the answer here")
        (job,) = queue.jobs.values()
        assert job.to_human and not job.notify, "its answer goes to the human, not the inbox"
    finally:
        patch.undo()
