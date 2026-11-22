"""The Manager (manager.py): routing, following work, and the command boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evomesh.config import EvolutionSettings, HarnessSettings, RuntimeSettings, Settings
from evomesh.console import ConsoleChannel
from evomesh.contracts import AgentDefinition, GoalStatus
from evomesh.environment import Environment
from evomesh.events import Event, EventType
from evomesh.goal_manager import GoalManager
from evomesh.harness import HarnessResult
from evomesh.harness_queue import HarnessJob, HarnessWorker
from evomesh.harness_tools import ToolDenied
from evomesh.manager import ManagerDesk, classify
from evomesh.models import MockProvider


def settings_for(tmp_path: Path, **harness: object) -> Settings:
    return Settings(
        data_path=tmp_path / "state.db",
        generation_path=tmp_path / "generations",
        workspace_path=tmp_path / "workspace",
        runtime=RuntimeSettings(cycle_seconds=3600, stagger_seconds=0),
        evolution=EvolutionSettings(autonomous=False),
        harness=HarnessSettings(**harness),  # type: ignore[arg-type]
    )


async def mesh(tmp_path: Path) -> tuple[Environment, AgentDefinition, AgentDefinition]:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    manager = AgentDefinition(name="Manager", purpose="Point of contact", manager=True)
    crawler = AgentDefinition(name="Crawler", purpose="Crawl sites", skills=["web-crawling"])
    await environment.register_agent(manager)
    await environment.register_agent(crawler)
    return environment, manager, crawler


def tools(environment: Environment, agent: AgentDefinition) -> dict[str, object]:
    return {tool.name: tool for tool in environment.builtin_tools_for(agent)}


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("/agents", "read"),
        ("/goals Crawler", "read"),
        ("/status", "read"),
        ("/reports NewsWatcher 5", "read"),
        ("/capabilities grants", "read"),
        ("/capabilities pin Crawler", "approve"),
        ("/browser status", "read"),
        ("/browser grant Manager interact", "approve"),
        ("/harness grant Manager", "approve"),
        ("/goal add Crawler \"crawl x\"", "work"),
        ("/goal frobnicate Crawler", "approve"),
        ("/agent delete Crawler", "approve"),
        ("/model Crawler qwen3", "work"),
        ("/telegram set Crawler x", "approve"),
        ("/procedures Crawler", "read"),
        ("/procedures Crawler approve x", "approve"),
        ("/wiki Crawler", "read"),
        ("/wiki lint Crawler", "approve"),
        ("/restart", "approve"),
        ("/some-future-command", "approve"),
        ("/exit", "forbidden"),
        ("/manager approve 1", "forbidden"),
        ("/chat Crawler", "forbidden"),
        ("hello", "forbidden"),
        ('/goals "unterminated', "forbidden"),
    ],
)
def test_what_a_manager_may_run_is_a_table_not_a_judgment(command: str, kind: str) -> None:
    assert classify(command) == kind


async def test_only_a_manager_gets_the_manager_tools(tmp_path: Path) -> None:
    environment, manager, crawler = await mesh(tmp_path)
    try:
        assert {"mesh_agents", "assign_task", "task_status", "mesh_command"} <= set(
            tools(environment, manager)
        )
        assert "mesh_command" not in tools(environment, crawler)
        roster = await tools(environment, manager)["mesh_agents"].run(None, {})  # type: ignore[attr-defined]
        assert "## Crawler" in roster and "Crawl sites" in roster
        assert "## Manager" not in roster, "it is not its own colleague"
    finally:
        await environment.stop()


async def test_assigned_work_is_followed_and_reported_on_the_managers_line(
    tmp_path: Path,
) -> None:
    environment, manager, crawler = await mesh(tmp_path)
    heard: list[str] = []

    async def bot(text: str) -> None:
        heard.append(text)

    await environment.add_agent_notifier(manager.id, bot)
    prompts: list[tuple[str, str]] = []

    async def write_up(manager_id: str, prompt: str) -> str:
        # The Manager writing the human's message (Environment wires its ask).
        prompts.append((manager_id, prompt))
        if "broken.invalid" in prompt:
            raise RuntimeError("model down")  # the raw report still goes out
        return "Готово: 12 страници в crawl.csv."

    environment.manager.report = write_up
    try:
        assign = tools(environment, manager)["assign_task"]
        reply = await assign.run(None, {"agent": "Crawler", "task": "crawl example.com"})  # type: ignore[attr-defined]
        assert "Crawler" in reply
        task = environment.manager.tasks[-1]
        goal = crawler.mind.goal(task.goal_id)
        assert "crawl example.com" in goal.description and goal.priority == 8
        assert "the result itself" in goal.description, "not an account of the work"

        # The crawler finishes; the agent loop publishes this.
        GoalManager(crawler.mind).complete(goal)
        await environment.events.publish(
            Event(
                EventType.GOAL_COMPLETED,
                source="reflective",
                agent_id=crawler.id,
                goal_id=goal.id,
                payload={"summary": "12 pages, saved to crawl.csv"},
            )
        )

        await environment.manager.settled()
        assert task.status == "done"
        assert prompts[0][0] == manager.id
        assert "12 pages, saved to crawl.csv" in prompts[0][1], "the Manager sees the report"
        assert "ask_agent Crawler" in prompts[0][1]
        assert any(
            "✅ Crawler finished" in text and "Готово: 12 страници" in text for text in heard
        ), "the human gets the Manager's message"
        status = await tools(environment, manager)["task_status"].run(None, {})  # type: ignore[attr-defined]
        assert "[done]" in status and "crawl.csv" in status

        # A failure is reported too, by the sweep when no event says so.
        await assign.run(None, {"agent": "Crawler", "task": "crawl broken.invalid"})  # type: ignore[attr-defined]
        failing = environment.manager.tasks[-1]
        bad = crawler.mind.goal(failing.goal_id)
        bad.status = GoalStatus.FAILED
        bad.last_error = "DNS lookup failed"
        await environment.manager.check(failing)
        assert failing.status == "failed"
        assert any("❌" in text and "DNS lookup failed" in text for text in heard)
    finally:
        await environment.stop()


async def test_work_goes_only_to_an_ordinary_agent(tmp_path: Path) -> None:
    environment, manager, _ = await mesh(tmp_path)
    assign = tools(environment, manager)["assign_task"]
    try:
        for target in ("Manager", "guardian", "Nobody"):
            with pytest.raises(ToolDenied):
                await assign.run(None, {"agent": target, "task": "something"})  # type: ignore[attr-defined]
    finally:
        await environment.stop()


async def test_a_command_that_changes_the_mesh_waits_for_the_human(tmp_path: Path) -> None:
    environment, manager, crawler = await mesh(tmp_path)
    heard: list[str] = []

    async def bot(text: str) -> None:
        heard.append(text)

    await environment.add_agent_notifier(manager.id, bot)
    command = tools(environment, manager)["mesh_command"]
    try:
        assert "Crawler" in await command.run(None, {"command": "/agents"})  # type: ignore[attr-defined]

        asked = await command.run(  # type: ignore[attr-defined]
            None, {"command": '/goal add Crawler "watch example.com" 5 3600'}
        )
        assert "confirmation_required" in asked
        assert not any("watch example.com" in g.description for g in crawler.mind.goals)
        assert any("/manager approve|deny 1" in text and "yes" in text for text in heard), (
            "the human is asked"
        )

        for forbidden in ("/exit", "/manager approve 1"):
            with pytest.raises(ToolDenied):
                await command.run(None, {"command": forbidden})  # type: ignore[attr-defined]

        console = ConsoleChannel(environment)
        assert "/manager approve" not in await console.route("/manager pending")
        assert "#1" in await console.route("/manager pending")
        ran = await console.route("/manager approve 1")
        assert "Added goal" in ran
        assert any("watch example.com" in g.description for g in crawler.mind.goals)
        assert "No pending command 1" in await console.route("/manager approve 1"), "once"
    finally:
        await environment.stop()


async def test_followed_work_survives_a_restart(tmp_path: Path) -> None:
    environment, manager, _ = await mesh(tmp_path)
    try:
        await tools(environment, manager)["assign_task"].run(  # type: ignore[attr-defined]
            None, {"agent": "Crawler", "task": "crawl example.com"}
        )
        again = ManagerDesk(environment)
        await again.start()
        try:
            assert [task.request for task in again.tasks] == ["crawl example.com"]
        finally:
            await again.stop()
    finally:
        await environment.stop()


async def test_only_a_human_makes_an_agent_a_manager(tmp_path: Path) -> None:
    environment, _, crawler = await mesh(tmp_path)
    console = ConsoleChannel(environment)
    try:
        assert "is now a manager" in await console.route("/manager set Crawler")
        assert crawler.manager and crawler.harness_root
        assert "system agent" in await console.route("/manager set guardian")
        assert "ordinary agent again" in await console.route("/manager unset Crawler")
        assert not crawler.manager
    finally:
        await environment.stop()


async def test_a_lent_worker_runs_one_job_and_can_be_called_off_while_idle() -> None:
    from evomesh.harness_queue import HarnessQueue

    queue = HarnessQueue()
    delivered: list[int] = []

    async def run(job: HarnessJob) -> HarnessResult:
        return HarnessResult(outcome="answered", answer=f"done {job.number}")

    async def deliver(job: HarnessJob) -> None:
        delivered.append(job.number)

    worker = HarnessWorker(queue, run, deliver, lane="priority")
    task = asyncio.create_task(worker.run_one())
    job = queue.submit("question", agent_id="a", root=Path("."), priority=True)
    await asyncio.wait_for(task, 5)
    assert job.result is not None and job.result.answer == f"done {job.number}"
    assert delivered == [job.number]

    idle = HarnessWorker(queue, run, deliver, lane="priority")
    waiting = asyncio.create_task(idle.run_one())
    await asyncio.sleep(0)
    assert not idle.busy
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    later = queue.submit("later", agent_id="b", root=Path("."), priority=True)
    assert later.open, "cancelling an idle lender lost nothing"


async def test_asking_an_agent_lends_the_worker_the_asker_is_holding(tmp_path: Path) -> None:
    """Found building the Manager: the asking job holds the only priority
    worker while it waits, and the asked agent answers through a priority job
    of its own -- which queued behind the asker until the ask timed out."""
    environment = Environment(
        settings_for(tmp_path, enabled=True, workers=1, priority_workers=1),
        {"ollama": MockProvider()},
    )
    await environment.start()
    try:
        trader = AgentDefinition(name="Trader", purpose="Trade")
        await environment.register_agent(trader)
        trader.harness_root = str(environment.default_harness_root(trader))
        await environment.start_agent(trader.id, start_delay=3600)

        # Every harness worker is busy, the way the asker's own job holds one.
        release = asyncio.Event()

        async def held(job: HarnessJob) -> HarnessResult:
            await release.wait()
            return HarnessResult(outcome="answered", answer="held")

        for worker in environment.harness_workers:
            worker.run = held
        for lane in (True, False):
            environment.harness_queue.submit("hold", agent_id="x", root=tmp_path, priority=lane)
        await asyncio.sleep(0.1)

        async def answer(job: HarnessJob) -> HarnessResult:
            return HarnessResult(outcome="answered", answer="Flat, no positions.")

        environment._run_harness_job = answer  # type: ignore[method-assign]  # noqa: SLF001
        ask = environment._make_ask_agent("manager")  # noqa: SLF001
        reply = await asyncio.wait_for(ask("Trader", "what is your position?"), 20)
        assert reply == "Flat, no positions."
        release.set()
    finally:
        await environment.stop()


async def test_an_agents_own_bot_does_not_prefix_every_answer_with_its_name(
    tmp_path: Path,
) -> None:
    """Found on the Manager's bot: "Manager> Здравей!" -- the name tells a
    shared conversation who answered; a private line has only one agent."""
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider(["Здравей!"])})
    await environment.start()
    try:
        manager = AgentDefinition(name="Manager", purpose="Point of contact", manager=True)
        await environment.register_agent(manager)
        await environment.start_agent(manager.id, start_delay=3600)

        private = ConsoleChannel(environment, locked_agent_id=manager.id)
        assert await private.route("здравей") == "Здравей!"

        shared = ConsoleChannel(environment)
        await shared.route("/chat Manager")
        assert await shared.route("здравей") == "Manager> Здравей!"
    finally:
        await environment.stop()


async def test_the_manager_can_send_separate_messages_but_not_a_flood(tmp_path: Path) -> None:
    """Found on its bot: "count from 10 to 0, each number in its own message"
    got one message and an apology -- an answer is one message."""
    environment, manager, _ = await mesh(tmp_path)
    heard: list[str] = []

    async def bot(text: str) -> None:
        heard.append(text)

    try:
        message = tools(environment, manager)["message_human"]
        assert await message.run(None, {"text": "без бот"}) == "sent"  # type: ignore[attr-defined]
        assert any(item[2] == "без бот" for item in environment.announcement_log), (
            "with no line of its own it reaches the shared channel"
        )

        await environment.add_agent_notifier(manager.id, bot)
        logged = len(environment.announcement_log)
        for number in range(10, -1, -1):
            await message.run(None, {"text": str(number)})  # type: ignore[attr-defined]
        assert heard == [str(number) for number in range(10, -1, -1)]
        assert len(environment.announcement_log) == logged, "not echoed into the shared one"

        for _ in range(8):
            await message.run(None, {"text": "още"})  # type: ignore[attr-defined]
        with pytest.raises(ToolDenied):
            await message.run(None, {"text": "one too many"})  # type: ignore[attr-defined]
    finally:
        await environment.stop()


async def until(condition: object, seconds: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():  # type: ignore[operator]
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def test_anything_can_repeat_on_a_clock_and_stop_stops_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found on its bot: "count +1 every second until I say stop" -- a model
    call takes seconds, so the clock has to be code. And not only a counter:
    "the news every ten minutes" is a question to an agent on the same clock,
    and "stop" has to stop long handed-out work too."""
    from evomesh import manager as manager_module

    monkeypatch.setattr(manager_module, "REPEAT_MIN_SECONDS", 0.01)
    environment, manager, crawler = await mesh(tmp_path)
    heard: list[str] = []

    async def bot(text: str) -> None:
        heard.append(text)

    asked: list[tuple[str, str]] = []

    async def ask(agent_id: str, question: str) -> str:
        asked.append((agent_id, question))
        # Every other run has nothing new.
        return "NOTHING_NEW" if len(asked) % 2 == 0 else f"headlines #{len(asked)}"

    environment.manager.report = ask
    await environment.add_agent_notifier(manager.id, bot)
    manager_tools = tools(environment, manager)
    repeat, stop = manager_tools["repeat"], manager_tools["stop"]
    private = ConsoleChannel(environment, locked_agent_id=manager.id)
    try:
        for bad in (
            {"message": "{n}", "every_seconds": 0.001},
            {"message": "{x}", "every_seconds": 0.02},
            {"every_seconds": 0.02},
            {"message": "{n}", "question": "news?", "every_seconds": 0.02},
            {"agent": "Nobody", "question": "news?", "every_seconds": 0.02},
        ):
            with pytest.raises(ToolDenied):
                await repeat.run(None, bad)  # type: ignore[attr-defined]

        # A counter.
        await repeat.run(None, {"message": "{n}", "every_seconds": 0.02})  # type: ignore[attr-defined]
        await until(lambda: len(heard) >= 3)
        assert heard[:3] == ["1", "2", "3"]

        # A question to an agent, its answer passed on, on the same clock.
        await repeat.run(  # type: ignore[attr-defined]
            None, {"agent": "Crawler", "question": "latest headlines", "every_seconds": 0.02}
        )
        await until(lambda: len(asked) >= 3)
        assert asked[0][0] == crawler.id and asked[0][1].startswith("latest headlines")
        assert "reply exactly NOTHING_NEW" in asked[0][1]
        assert "headlines #1" in heard and "headlines #3" in heard
        assert not any("NOTHING_NEW" in text for text in heard), "nothing new, nothing sent"

        # And long handed-out work.
        await manager_tools["assign_task"].run(  # type: ignore[attr-defined]
            None, {"agent": "Crawler", "task": "crawl a huge site"}
        )
        task = environment.manager.tasks[-1]

        # "стоп" stops all of it, before any model sees the message.
        said = await private.route("Стоп")
        assert said.startswith("⏹ Stopped") and "2 repeating" in said and "Crawler" in said
        assert task.status == "cancelled"
        assert crawler.mind.goal(task.goal_id).status is GoalStatus.CANCELLED
        await asyncio.sleep(0.01)
        count = len(heard)
        await asyncio.sleep(0.1)
        assert len(heard) == count, "nothing after stop"
        assert await environment.manager.stop_on_word(manager.id, "stop") is None, (
            "with nothing running, 'stop' is an ordinary message"
        )

        # One at a time, a bounded one ends by itself, and /manager stop.
        heard.clear()
        await repeat.run(  # type: ignore[attr-defined]
            None,
            {"message": "tick {n}", "every_seconds": 0.01, "start": 10, "step": 10,
             "max_runs": 3},
        )
        await until(lambda: len(heard) >= 3)
        await asyncio.sleep(0.05)
        assert heard == ["tick 10", "tick 20", "tick 30"], "and no more"
        first = await repeat.run(None, {"message": "a", "every_seconds": 0.02})  # type: ignore[attr-defined]
        await repeat.run(None, {"message": "b", "every_seconds": 0.02})  # type: ignore[attr-defined]
        number = int(first.split("#")[1].split()[0])
        assert "stopped 1" in await stop.run(None, {"what": "repeat", "number": number})  # type: ignore[attr-defined]
        assert "1 repeating" in await ConsoleChannel(environment).route("/manager stop")
        assert "Nothing is running" in await ConsoleChannel(environment).route("/manager stop")
    finally:
        await environment.stop()


async def test_what_the_human_asks_for_runs_and_access_waits_for_a_yes(tmp_path: Path) -> None:
    """Found on its bot: the human asked for a goal, and still had to type
    /manager approve 1. A command that changes what agents do runs when the
    human's own message asked; one that changes what they may do waits for a
    plain "yes" -- never for text another agent or a page sent."""
    environment, manager, crawler = await mesh(tmp_path)
    command = tools(environment, manager)["mesh_command"]
    desk = environment.manager
    try:
        # Not the human's turn (a repeat, a report): even work waits.
        waited = await command.run(None, {"command": '/goal add Crawler "watch a" 5'})  # type: ignore[attr-defined]
        assert "confirmation_required" in waited

        desk.human_turn[manager.id] = True
        ran = await command.run(None, {"command": '/goal add Crawler "watch b" 5'})  # type: ignore[attr-defined]
        assert "Added goal" in ran
        assert any("watch b" in goal.description for goal in crawler.mind.goals)
        grant = await command.run(None, {"command": "/browser grant Crawler interact"})  # type: ignore[attr-defined]
        assert "confirmation_required" in grant, "access never runs unasked"
        assert crawler.browser is None
        desk.human_turn.pop(manager.id)

        private = ConsoleChannel(environment, locked_agent_id=manager.id)
        said = await private.route("Да")
        assert "Added goal" in said and "may now interact" in said, "a yes approves all waiting"
        assert crawler.browser is not None
        assert await desk.answer_pending(manager.id, "да") is None, "nothing left to approve"

        await command.run(None, {"command": "/browser revoke Crawler"})  # type: ignore[attr-defined]
        assert "Denied" in await private.route("не")
        assert crawler.browser is not None, "a no refuses"
    finally:
        await environment.stop()


async def test_trust_lets_what_the_human_asked_for_run_grants_included(tmp_path: Path) -> None:
    environment, manager, crawler = await mesh(tmp_path)
    command = tools(environment, manager)["mesh_command"]
    desk = environment.manager
    console = ConsoleChannel(environment)
    try:
        assert "Trust is OFF" in await console.route("/manager trust")
        assert "Trust is ON" in await console.route("/manager trust on")
        desk.human_turn[manager.id] = True
        ran = await command.run(None, {"command": "/browser grant Crawler read"})  # type: ignore[attr-defined]
        assert "may now read" in ran and crawler.browser is not None
        desk.human_turn.pop(manager.id)
        unasked = await command.run(None, {"command": "/browser revoke Crawler"})  # type: ignore[attr-defined]
        assert "confirmation_required" in unasked, "not asked by the human: still waits"
        with pytest.raises(ToolDenied):
            await command.run(None, {"command": "/exit"})  # type: ignore[attr-defined]

        again = ManagerDesk(environment)
        await again.start()
        try:
            assert again.trusted, "kept across restarts"
        finally:
            await again.stop()
        assert "Trust is OFF" in await console.route("/manager trust off")
    finally:
        await environment.stop()


async def test_one_numbered_list_and_stop_by_its_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Give me your tasks" -> one numbered list; "stop 1" -> that one, and
    the rest keeps running. Asked for on its bot."""
    from evomesh import manager as manager_module

    monkeypatch.setattr(manager_module, "REPEAT_MIN_SECONDS", 0.01)
    environment, manager, crawler = await mesh(tmp_path)
    manager_tools = tools(environment, manager)
    private = ConsoleChannel(environment, locked_agent_id=manager.id)
    console = ConsoleChannel(environment)
    try:
        await manager_tools["repeat"].run(None, {"message": "news {n}", "every_seconds": 60})  # type: ignore[attr-defined]
        await manager_tools["repeat"].run(None, {"message": "count {n}", "every_seconds": 60})  # type: ignore[attr-defined]
        await manager_tools["assign_task"].run(  # type: ignore[attr-defined]
            None, {"agent": "Crawler", "task": "crawl a site"}
        )
        listing = await manager_tools["task_status"].run(None, {})  # type: ignore[attr-defined]
        lines = listing.splitlines()
        assert lines[0].startswith("1. every 60s") and "news {n}" in lines[0]
        assert lines[1].startswith("2. every 60s") and "count {n}" in lines[1]
        assert lines[2].startswith("3. Crawler [runnable]") and "crawl a site" in lines[2]
        assert "Asked by" not in lines[2], "the instructions for the agent are not shown"
        assert "1. every 60s" in await console.route("/manager tasks")

        # "спри 1" stops the first, before any model; the rest keeps running.
        assert await private.route("спри 1") == "⏹ Stopped 1."
        after = await manager_tools["task_status"].run(None, {})  # type: ignore[attr-defined]
        assert "news {n}" not in after and "count {n}" in after and "crawl a site" in after

        # Numbers follow the latest list.
        assert await private.route("Стоп номер 2") == "⏹ Stopped 2 (Crawler)."
        task = environment.manager.tasks[-1]
        assert task.status == "cancelled"
        assert crawler.mind.goal(task.goal_id).status is GoalStatus.CANCELLED
        assert "no 7" in await console.route("/manager stop 7")
        assert "⏹ Stopped 1." == await console.route("/manager stop 1")
        assert "Nothing is running" in await console.route("/manager tasks")
        assert "Finished lately" in await console.route("/manager tasks")
    finally:
        await environment.stop()


async def test_repeats_survive_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mesh restarts itself whenever a generation lands; "every 30
    seconds" must not end quietly then."""
    from evomesh import manager as manager_module

    monkeypatch.setattr(manager_module, "REPEAT_MIN_SECONDS", 0.01)
    environment, manager, _ = await mesh(tmp_path)
    try:
        await tools(environment, manager)["repeat"].run(  # type: ignore[attr-defined]
            None, {"message": "tick {n}", "every_seconds": 60, "start": 5}
        )
        await until(lambda: environment.manager.repeats[manager.id][0].runs >= 1)
        await environment.manager.stop()  # the mesh stopping
        again = ManagerDesk(environment)
        await again.start()
        try:
            restored = again.repeats[manager.id]
            assert len(restored) == 1 and restored[0].description == "tick {n}"
            assert restored[0].spec["start"] >= 5
            assert "1. every 60s" in again.listing(manager.id)[0]
            assert await again.stop_listed(manager.id, 1) == "⏹ Stopped 1."
            await again.settled()
        finally:
            await again.stop()
        nothing = ManagerDesk(environment)
        await nothing.start()
        try:
            assert not nothing.repeats.get(manager.id), "a stopped repeat stays stopped"
        finally:
            await nothing.stop()
    finally:
        await environment.stop()


async def test_any_agent_can_be_given_the_coordination_tools_but_not_the_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asked for: whatever the Manager can do should be usable by other agents
    too -- except running the mesh's commands, which stays a manager's."""
    from evomesh import manager as manager_module

    monkeypatch.setattr(manager_module, "REPEAT_MIN_SECONDS", 0.01)
    environment, _, crawler = await mesh(tmp_path)
    console = ConsoleChannel(environment)
    coordination = {
        "mesh_agents", "assign_task", "task_status", "message_human", "repeat", "stop",
    }
    try:
        assert not coordination & set(tools(environment, crawler)), "off by default"
        said = await console.route("/coordination Crawler on")
        assert "now has the coordination tools" in said and crawler.harness_root
        names = set(tools(environment, crawler))
        assert coordination <= names
        assert "mesh_command" not in names, "commands stay a manager's"
        assert "system agent" in await console.route("/coordination guardian on")

        heard: list[str] = []

        async def bot(text: str) -> None:
            heard.append(text)

        await environment.add_agent_notifier(crawler.id, bot)
        await tools(environment, crawler)["repeat"].run(  # type: ignore[attr-defined]
            None, {"message": "page {n}", "every_seconds": 0.02}
        )
        await until(lambda: len(heard) >= 2)
        own_line = ConsoleChannel(environment, locked_agent_id=crawler.id)
        assert await own_line.route("stop 1") == "⏹ Stopped 1."
        assert "1. every" not in await console.route("/manager tasks")

        await console.route("/coordination Crawler off")
        assert not coordination & set(tools(environment, crawler))
    finally:
        await environment.stop()


async def test_stop_names_one_thing_in_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found live: "спри да ми пращаш новини" went to the model, which could
    not see its repeats and stopped nothing; the news kept coming."""
    from evomesh import manager as manager_module

    monkeypatch.setattr(manager_module, "REPEAT_MIN_SECONDS", 0.01)
    environment, manager, _ = await mesh(tmp_path)
    repeat = tools(environment, manager)["repeat"]
    desk = environment.manager
    try:
        await repeat.run(None, {"message": "Последните новини {n}", "every_seconds": 60})  # type: ignore[attr-defined]
        await repeat.run(None, {"message": "брой {n}", "every_seconds": 60})  # type: ignore[attr-defined]

        assert await desk.stop_on_word(manager.id, "спри да ми пращаш") is None, (
            "names nothing: the model decides"
        )
        assert await desk.stop_on_word(manager.id, "спри да ми пращаш новини") == "⏹ Stopped 1."
        assert [r.description for r in desk.repeats[manager.id]] == ["брой {n}"]

        await repeat.run(None, {"message": "новини от София {n}", "every_seconds": 60})  # type: ignore[attr-defined]
        await repeat.run(None, {"message": "новини от Варна {n}", "every_seconds": 60})  # type: ignore[attr-defined]
        asked = await desk.stop_on_word(manager.id, "Спри новините")
        assert asked is not None and asked.startswith("Which one?")
        assert "София" in asked and "Варна" in asked
        assert len(desk.repeats[manager.id]) == 3, "nothing stopped on a guess"
        assert await desk.stop_on_word(manager.id, "stop варна") == "⏹ Stopped 3."
    finally:
        await environment.stop()


async def test_every_goal_knows_who_asked_for_it_from_either_side(tmp_path: Path) -> None:
    """Asked for: the human gave work to one agent, which created it on
    another, and the human could not find it. However a goal is created for
    another agent -- assign_task, a command a manager ran, a delegation -- it
    carries who asked, and both sides list it."""
    from evomesh.coordination import WorkItem
    from evomesh.manager import requester_of

    environment, manager, crawler = await mesh(tmp_path)
    desk = environment.manager
    console = ConsoleChannel(environment)
    try:
        # A command the manager ran, because the human asked for it.
        desk.human_turn[manager.id] = True
        await tools(environment, manager)["mesh_command"].run(  # type: ignore[attr-defined]
            None, {"command": '/goal add Crawler "watch example.com" 5 3600'}
        )
        desk.human_turn.pop(manager.id)
        goal = next(g for g in crawler.mind.goals if "watch example.com" in g.description)
        assert goal.requested_by == manager.id

        listing = "\n".join(desk.listing(manager.id))
        assert "1. Crawler [" in listing and "every 3600s" in listing
        assert "(from Manager)" in await console.route("/goals Crawler")
        assert "1. mine, from Manager" in "\n".join(desk.listing(crawler.id)), (
            "the agent that got it says where it came from"
        )

        # A goal the human added by hand is "from you" on that agent.
        await console.route('/goal add Crawler "count the pages" 5')
        human = next(g for g in crawler.mind.goals if "count the pages" in g.description)
        assert human.requested_by == "human"
        assert "mine, from you" in "\n".join(desk.listing(crawler.id))

        # A delegation carries its requester too, and an older one is read
        # from the parameters it always recorded.
        await environment.start_agent(crawler.id, start_delay=3600)
        staged = environment.runtimes[crawler.id]._staged_goal(  # noqa: SLF001
            WorkItem(parent_goal_id="", objective="crawl x"), manager.id
        )
        assert staged.requested_by == manager.id
        staged.requested_by = None
        assert requester_of(staged) == manager.id

        # And stopping by number reaches it wherever it lives.
        assert await desk.stop_listed(manager.id, 1) == "⏹ Stopped 1 (Crawler)."
        assert goal.status is GoalStatus.CANCELLED
    finally:
        await environment.stop()
