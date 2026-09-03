import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

import evomesh.control
from evomesh.config import HarnessSettings, Settings
from evomesh.console import ConsoleChannel
from evomesh.contracts import AgentDefinition
from evomesh.control import ControlServer
from evomesh.environment import Environment
from evomesh.models import MockProvider


async def test_console_routes_commands(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    skill_dir = tmp_path / "skills" / "research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: research\ndescription: Look things up before answering.\n---\n\n"
        "Do the research before you answer.\n",
        encoding="utf-8",
    )
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    assert "status: READY" in (await console.route("/status"))
    assert "Agent Architect" in (await console.route("/agents"))
    assert "research: Look things up before answering." in (await console.route("/skills"))
    assert "Do the research before you answer." in (await console.route("/skill show research"))
    assert "mock-specialist" in (await console.route("/models ollama"))
    assert await console.route("/chat architect") == "Talking to Agent Architect."
    agent = AgentDefinition(name="Writer", purpose="Write", model_name="mock-model")
    await environment.register_agent(agent)
    await environment.start_agent(agent.id)
    assert "ollama:mock-specialist" in (
        await console.route('/model "Writer" mock-specialist ollama')
    )
    assert "context window of 32768" in (await console.route('/num-ctx "Writer" 32768'))
    assert "no longer overrides" in (await console.route('/num-ctx "Writer" clear'))
    assert await console.route('/chat "Writer"') == "Talking to Writer."
    assert await console.route("hello") == "Writer> Mock response"
    await environment.stop()


async def test_skill_install_adds_a_skill_from_a_local_file(tmp_path: Path) -> None:
    """The installation mechanism this session was actually asked for: a
    SKILL.md a human already has on disk becomes an installed skill in one
    command, no restart needed to see it in /skills."""
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    source = tmp_path / "shared-skill.md"
    source.write_text(
        "---\nname: wire-a-dead-module\ndescription: Wire an unreachable module in.\n---\n\n"
        "Pick one module from the dead list and give it a real caller.\n",
        encoding="utf-8",
    )

    result = await console.route(f'/skill install "{source}"')

    assert "Installed 'wire-a-dead-module'" in result
    assert "wire-a-dead-module: Wire an unreachable module in." in (
        await console.route("/skills")
    )
    assert "is not a file" in (await console.route("/skill install nowhere.md"))
    await environment.stop()


async def test_skill_install_accepts_a_directory_bundle_with_a_script(tmp_path: Path) -> None:
    """'A skill is one or a group of commands': installing a directory brings
    its bundled script along, not only the SKILL.md that names it."""
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    source = tmp_path / "web-check"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: web-check\ndescription: Check a site is still up.\n---\n\n"
        "Run scripts/check.sh with the shell tool.\n",
        encoding="utf-8",
    )
    (source / "scripts").mkdir()
    (source / "scripts" / "check.sh").write_text("#!/bin/sh\ncurl -sf \"$1\"\n", encoding="utf-8")

    result = await console.route(f'/skill install "{source}"')

    assert "Installed 'web-check'" in result
    assert "web-check: Check a site is still up." in (await console.route("/skills"))
    await environment.stop()


TOOL_TEXT = (
    "---\nname: check-site\ndescription: Check a site responds.\n"
    'command: python "{tool_dir}/scripts/check.py"\n'
    "parameters:\n  - name: url\n    description: The URL to check.\n"
    "---\n"
)


async def test_tool_install_from_a_file_is_inactive_until_allow_listed(
    tmp_path: Path,
) -> None:
    """A custom tool is only ever as permitted as the program it wraps: it
    installs either way, but does not show up as active until a human has
    put that program in harness.shell_allow."""
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    source = tmp_path / "check-site.md"
    source.write_text(TOOL_TEXT, encoding="utf-8")

    result = await console.route(f'/tool install "{source}"')

    assert "Installed 'check-site'" in result
    assert "harness.shell_allow" in result
    assert "check-site: Check a site responds. (inactive" in (await console.route("/tools"))
    assert environment.active_custom_tools() == ()
    await environment.stop()


async def test_tool_install_directory_bundle_becomes_active_once_allow_listed(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_path=tmp_path / "data.db",
        generation_path=tmp_path / "generations",
        harness=HarnessSettings(enabled=True, shell_allow=["python"]),
    )
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    source = tmp_path / "check-site"
    source.mkdir()
    (source / "TOOL.md").write_text(TOOL_TEXT, encoding="utf-8")
    (source / "scripts").mkdir()
    (source / "scripts" / "check.py").write_text("print('ok')\n", encoding="utf-8")

    result = await console.route(f'/tool install "{source}"')

    assert "Installed 'check-site'" in result
    assert "inactive" not in result
    listing = await console.route("/tools")
    assert "check-site: Check a site responds." in listing
    assert "inactive" not in listing
    active = environment.active_custom_tools()
    assert [tool.name for tool in active] == ["check-site"]
    await environment.stop()


async def test_tool_show_previews_the_raw_tool_md(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    await environment.tools.install(TOOL_TEXT, created_by="console")

    result = await console.route("/tool show check-site")

    assert 'command: python "{tool_dir}/scripts/check.py"' in result
    assert "There is no tool" in (await console.route("/tool show nope"))
    await environment.stop()


async def test_goal_add_with_an_interval_sets_recurring_automatically(tmp_path: Path) -> None:
    """The command surface for the cadence feature: a human giving an
    interval_seconds should never also have to remember --recurring, because
    a standing check that permanently fails after max_attempts defeats the
    reason it was given an interval in the first place."""
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    agent = AgentDefinition(name="Watcher", purpose="Watch things")
    await environment.register_agent(agent)

    result = await console.route('/goal add "Watcher" "Check example.com" 5 3600')

    assert "Re-checked every 3600s" in result
    assert "cycle_seconds" in result
    goal = next(iter(agent.mind.goals))
    assert goal.interval_seconds == 3600
    assert goal.recurring is True
    await environment.stop()


async def test_goal_add_accepts_a_cron_expression_in_place_of_an_interval(
    tmp_path: Path,
) -> None:
    """The other schedule shape: '/goal add ... "0 * * * *"' triggers the
    agent on a real cron schedule instead of a fixed offset from last run,
    and gets the same automatic recurring=True as a plain interval."""
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    agent = AgentDefinition(name="Watcher", purpose="Watch things")
    await environment.register_agent(agent)

    result = await console.route('/goal add "Watcher" "Check example.com" 5 "0 * * * *"')

    assert "Triggers on schedule '0 * * * *'" in result
    assert "cycle_seconds" in result
    goal = next(iter(agent.mind.goals))
    assert goal.cron == "0 * * * *"
    assert goal.interval_seconds is None
    assert goal.recurring is True
    assert goal.next_attempt_at is not None
    await environment.stop()


async def test_goal_add_rejects_a_malformed_cron_expression(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    agent = AgentDefinition(name="Watcher", purpose="Watch things")
    await environment.register_agent(agent)

    result = await console.route('/goal add "Watcher" "Check example.com" 5 "not a cron"')

    assert "Bad schedule" in result
    assert not agent.mind.goals, "a rejected schedule must not leave a half-added goal behind"
    await environment.stop()


async def test_goal_notify_toggles_the_flag_and_defaults_to_on(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    agent = AgentDefinition(name="Watcher", purpose="Watch things")
    await environment.register_agent(agent)
    goal = agent.mind.add_goal("Check example.com", recurring=True)

    on_result = await console.route(f'/goal notify "Watcher" {goal.id}')
    assert goal.notify is True
    assert "will announce" in on_result

    off_result = await console.route(f'/goal notify "Watcher" {goal.id} off')
    assert goal.notify is False
    assert "will no longer announce" in off_result
    await environment.stop()


async def test_notifications_is_pulled_by_a_cursor_the_caller_advances(tmp_path: Path) -> None:
    """This is the desktop Control Center's path to the same summaries
    Telegram gets pushed: it cannot be pushed to over a request-response
    control connection, so it polls with the last id it has already seen."""
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)

    assert "No new notifications since 0" in (await console.route("/notifications"))

    await environment.announce("Watcher finished checking example.com")
    first = await console.route("/notifications")
    assert "Watcher finished checking example.com" in first
    first_id = int(first.split("\t", 1)[0])

    assert f"No new notifications since {first_id}" in (
        await console.route(f"/notifications {first_id}")
    )

    await environment.announce("a second thing happened")
    second = await console.route(f"/notifications {first_id}")
    assert "a second thing happened" in second
    assert "Watcher finished checking example.com" not in second
    await environment.stop()


async def test_a_new_non_system_agent_gets_its_own_playground_on_disk(tmp_path: Path) -> None:
    """The actual ask: a place of its own to freely read and write, ready the
    moment the agent exists -- not only once harness access is granted."""
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="Scraper", purpose="Scrape things")

    await environment.register_agent(agent)

    playground = environment.memory_for(agent).playground_path
    assert playground.is_dir()
    assert playground.is_relative_to(settings.workspace_path)
    await environment.stop()


async def test_harness_grant_with_no_path_defaults_a_custom_agent_to_its_playground(
    tmp_path: Path,
) -> None:
    """Not the project's own source tree -- that default stays for system
    agents only (see the next test). A fresh agent granted harness access
    with no path named should land somewhere that is only ever its own."""
    settings = Settings(
        data_path=tmp_path / "data.db",
        generation_path=tmp_path / "generations",
        harness=HarnessSettings(enabled=True),
    )
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    agent = AgentDefinition(name="Scraper", purpose="Scrape things")
    await environment.register_agent(agent)

    result = await console.route('/harness grant "Scraper"')

    playground = environment.memory_for(agent).playground_path
    assert str(playground) in result
    assert agent.harness_root == str(playground)
    assert playground != environment.project_root
    await environment.stop()


async def test_harness_grant_with_no_path_defaults_a_system_agent_to_the_project_root(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_path=tmp_path / "data.db",
        generation_path=tmp_path / "generations",
        harness=HarnessSettings(enabled=True),
    )
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    agent = environment.registry.get("Guardian")  # bootstrapped by Environment.start()

    result = await console.route('/harness grant "Guardian"')

    assert str(environment.project_root) in result
    assert agent.harness_root == str(environment.project_root)
    await environment.stop()


async def test_control_server_accepts_commands_and_shutdown(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    shutdown = asyncio.Event()
    server = ControlServer(environment, shutdown, port=0)
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    async def request(command: str) -> dict[str, object]:
        writer.write((json.dumps({"command": command}) + "\n").encode())
        await writer.drain()
        return json.loads(await reader.readline())

    assert (await request("/ping"))["output"] == "EvoMesh control ready"
    assert "status: READY" in str((await request("/status"))["output"])
    assert (await request("/exit"))["shutdown"] is True
    assert shutdown.is_set()
    writer.close()
    await writer.wait_closed()
    await server.stop()
    await environment.stop()


class ExplodingChannel(ConsoleChannel):
    """A handler that raises exactly what the old dispatcher tuple did not list."""

    async def _command_boom(self, parts: list[str]) -> str:
        raise sqlite3.OperationalError("no such table: state")


async def test_control_server_reports_a_failed_command_and_keeps_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing command must reach the Control Center as text, not a dropped socket.

    The dispatcher used to catch a hand-listed tuple of exceptions. A wiped
    database raised sqlite3.OperationalError, which is not a RuntimeError, so
    it escaped, closed the writer, and reached a human as "the mesh control
    connection was lost" for every /evolution status they typed.

    The handler raises here rather than the storage layer: a repository heals a
    wiped database on its own now, and this is a claim about the dispatcher,
    which owes the same answer for whatever any handler raises.
    """
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    shutdown = asyncio.Event()
    monkeypatch.setattr(evomesh.control, "ConsoleChannel", ExplodingChannel)
    server = ControlServer(environment, shutdown, port=0)
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    async def request(command: str) -> dict[str, object]:
        writer.write((json.dumps({"command": command}) + "\n").encode())
        await writer.drain()
        return json.loads(await reader.readline())

    response = await request("/boom")
    assert response["error"] is True
    assert response["output"] == "Error: OperationalError: no such table: state"
    # The point of the fix: the same connection still answers afterwards.
    assert (await request("/ping"))["output"] == "EvoMesh control ready"

    writer.close()
    await writer.wait_closed()
    await server.stop()
    await environment.stop()
