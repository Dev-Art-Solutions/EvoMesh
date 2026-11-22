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
        ("/goal add Crawler \"crawl x\"", "approve"),
        ("/model Crawler qwen3", "approve"),
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
        assert any("/manager approve 1" in text for text in heard), "the human is asked"

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
