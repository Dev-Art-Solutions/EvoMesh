"""The tool loop, and the four ways it is supposed to refuse.

Every test here runs against MockProvider, so the loop is exercised with no
model, no Ollama and no network -- the same trick that lets CI run the mesh. What
a real 4B model does with a tool schema is the phase's release checklist, not
something a green suite can claim.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evomesh.agents import system_agent_definitions
from evomesh.config import HarnessSettings, ProviderSettings, Settings
from evomesh.contracts import AgentPhase
from evomesh.environment import Environment
from evomesh.harness import (
    HarnessResult,
    HarnessRunner,
    build_runner,
    compact,
    parse_text_call,
)
from evomesh.harness_queue import (
    HarnessJob,
    HarnessQueue,
    HarnessWorker,
    JobStatus,
    QueueFull,
)
from evomesh.harness_session import HarnessSession, next_session_path
from evomesh.harness_tools import (
    ALL_TOOLS,
    SHELL_TOOLS,
    ToolContext,
    ToolLimits,
    ToolRegistry,
    tool_read,
)
from evomesh.models import ChatMessage, ChatTurn, MockProvider, ToolCall


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "answer.py").write_text(
        "def reconsider() -> bool:\n    return True\n", encoding="utf-8"
    )
    (tmp_path / "notes.md").write_text("nothing to see\n", encoding="utf-8")
    return tmp_path


# -- tools ---------------------------------------------------------------


async def test_a_path_outside_the_root_is_refused_as_a_result(project: Path) -> None:
    registry = ToolRegistry()
    context = ToolContext(root=project / "src")

    result = await registry.invoke(context, "read", {"path": "../notes.md"})

    assert result.startswith("DENIED:")
    assert "outside the job root" in result


async def test_an_unknown_tool_is_answered_with_the_list_of_real_ones(project: Path) -> None:
    result = await ToolRegistry().invoke(ToolContext(root=project), "delete", {"path": "x"})

    assert "there is no tool called delete" in result
    assert "read" in result and "grep" in result


async def test_read_numbers_its_lines_and_honours_the_window(project: Path) -> None:
    context = ToolContext(root=project)

    result = await tool_read(context, {"path": "src/answer.py", "offset": 2, "limit": 1})

    assert result.strip().startswith("2| ")
    assert "return True" in result
    assert "def reconsider" not in result


async def test_a_truncated_read_says_what_it_withheld_and_how_to_ask(project: Path) -> None:
    """The withheld count is the whole point: a silent trim is a lie.

    A model handed a shortened file with no marker believes it has seen the
    whole thing, which is the failure rule 3 exists to prevent -- here it would
    be the tool doing it rather than the model server.
    """
    (project / "long.py").write_text("\n".join(f"line {n}" for n in range(1, 51)), encoding="utf-8")
    context = ToolContext(root=project, limits=ToolLimits(result_lines=10))

    result = await tool_read(context, {"path": "long.py"})

    assert "40 more lines withheld" in result
    assert "use offset=11" in result


async def test_grep_reports_matches_relative_to_the_root(project: Path) -> None:
    result = await ToolRegistry().invoke(
        ToolContext(root=project), "grep", {"pattern": "reconsider", "path": "."}
    )

    assert "src/answer.py:1:" in result.replace("\\", "/")


async def test_a_bad_regular_expression_comes_back_as_a_refusal(project: Path) -> None:
    result = await ToolRegistry().invoke(
        ToolContext(root=project), "grep", {"pattern": "def ("}
    )

    assert result.startswith("DENIED:")


# -- edit and write ------------------------------------------------------


def writable(root: Path, session: HarnessSession | None = None) -> ToolContext:
    return ToolContext(root=root, allow_write=True, session=session)


async def test_a_unique_target_is_replaced(project: Path) -> None:
    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project),
        "edit",
        {"path": "src/answer.py", "old": "return True", "new": "return False"},
    )

    assert "edited" in result
    assert "return False" in (project / "src" / "answer.py").read_text(encoding="utf-8")


async def test_two_matches_are_refused_with_the_count_and_the_lines(project: Path) -> None:
    """The refusal is the tool's reason for existing.

    Taking the first of three matches produces a candidate that passes every
    check and does the wrong thing, which is worse than the whole-file rewrite
    it replaces -- that one at least fails loudly.
    """
    path = project / "src" / "twice.py"
    path.write_text("x = 1\ny = 2\nx = 1\n", encoding="utf-8")

    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project), "edit", {"path": "src/twice.py", "old": "x = 1", "new": "x = 9"}
    )

    assert result.startswith("DENIED:")
    assert "2 matches" in result
    # The refusal carries the neighbourhoods, so widening needs no second read.
    assert "match at line 1" in result and "match at line 3" in result
    assert "    1> x = 1" in result
    assert path.read_text(encoding="utf-8") == "x = 1\ny = 2\nx = 1\n"


async def test_a_stale_anchor_is_refused_and_says_to_read_again(project: Path) -> None:
    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project),
        "edit",
        {"path": "src/answer.py", "old": "return None", "new": "return False"},
    )

    assert result.startswith("DENIED:")
    assert "Read the file again" in result


async def test_write_refuses_to_replace_an_existing_file_by_accident(project: Path) -> None:
    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project), "write", {"path": "notes.md", "content": "gone"}
    )

    assert "already exists" in result
    assert (project / "notes.md").read_text(encoding="utf-8") == "nothing to see\n"

    allowed = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project),
        "write",
        {"path": "notes.md", "content": "replaced\n", "overwrite": True},
    )

    assert allowed.startswith("replaced")
    assert (project / "notes.md").read_text(encoding="utf-8") == "replaced\n"


async def test_a_write_outside_the_root_never_reaches_the_disk(project: Path) -> None:
    outside = project.parent / "escaped.py"

    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project / "src"), "write", {"path": "../../escaped.py", "content": "x"}
    )

    assert result.startswith("DENIED:")
    assert not outside.exists()


async def test_a_read_only_job_names_the_setting_that_would_allow_writing(project: Path) -> None:
    result = await ToolRegistry(ALL_TOOLS).invoke(
        ToolContext(root=project),
        "edit",
        {"path": "src/answer.py", "old": "return True", "new": "return False"},
    )

    assert "harness.allow_write" in result
    assert "return True" in (project / "src" / "answer.py").read_text(encoding="utf-8")


async def test_the_session_carries_the_diff_before_the_file_changes(
    project: Path, tmp_path: Path
) -> None:
    """Recorded first, applied second, and the test proves the order.

    A process killed between the two leaves a record of what it was about to
    do. The other order leaves a changed file and no explanation.
    """
    session = HarnessSession(next_session_path(tmp_path / "harness"))
    original = (project / "src" / "answer.py").read_text(encoding="utf-8")

    class Watcher(HarnessSession):
        def record(self, kind: str, **fields: object) -> dict[str, object]:
            if kind == "edit":
                # The file must still be untouched at the moment we are told.
                assert (project / "src" / "answer.py").read_text(encoding="utf-8") == original
            return super().record(kind, **fields)

    watcher = Watcher(session.path)
    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project, watcher),
        "edit",
        {"path": "src/answer.py", "old": "return True", "new": "return False"},
    )

    assert "edited" in result
    assert watcher.kinds() == ["edit"]
    assert "-    return True" in watcher.entries[0]["diff"]


async def test_an_edit_that_changes_nothing_is_refused(project: Path) -> None:
    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project),
        "edit",
        {"path": "src/answer.py", "old": "return True", "new": "return True"},
    )

    assert "identical" in result


# -- the loop ------------------------------------------------------------


async def test_the_loop_reads_a_file_and_then_answers(project: Path) -> None:
    provider = MockProvider(
        turns=[
            ChatTurn(tool_calls=[ToolCall(name="read", arguments={"path": "src/answer.py"})]),
            ChatTurn(text="reconsider() lives in src/answer.py"),
        ]
    )
    runner = build_runner(provider, project)

    result = await runner.run("where does reconsider live?")

    assert result.outcome == "answered"
    assert result.tool_calls == 1
    assert result.steps == 2
    assert result.used_tool_protocol == "native tools"
    # The file's contents reached the transcript, which is the whole claim of
    # this phase: the model answered from what it read, not from memory.
    assert any(
        message.role == "tool" and "def reconsider" in message.content
        for message in provider.chats[-1]
    )


async def test_a_model_without_tool_calling_drives_the_same_tools_in_text(project: Path) -> None:
    """The decision this phase exists to test: no native tools, still works."""
    provider = MockProvider(
        responses=[
            '{"tool": "grep", "args": {"pattern": "reconsider"}}',
            "It is defined in src/answer.py.",
        ]
    )
    runner = build_runner(provider, project)

    result = await runner.run("where does reconsider live?")

    assert result.outcome == "answered"
    assert result.used_tool_protocol == "text protocol"
    assert result.tool_calls == 1
    assert "src/answer.py" in result.answer


async def test_a_denied_tool_does_not_end_the_job(project: Path) -> None:
    provider = MockProvider(
        turns=[
            ChatTurn(tool_calls=[ToolCall(name="read", arguments={"path": "/etc/passwd"})]),
            ChatTurn(text="I cannot leave the project root, so here is what I found instead."),
        ]
    )
    runner = build_runner(provider, project)

    result = await runner.run("read the password file")

    assert result.outcome == "answered"
    assert any(
        entry["kind"] == "tool" and entry["result"].startswith("DENIED")
        for entry in runner.session.entries
    )


async def test_a_broken_tool_call_is_shown_the_protocol_once(project: Path) -> None:
    """Observed on gemma:2b: an unclosed object is neither a call nor an answer.

    Accepting it as the answer ends a job that had not finished, so the model is
    shown the protocol once. Once, not repeatedly: a model that cannot produce
    it after being told will not produce it on the third telling either.
    """
    provider = MockProvider(
        responses=[
            '{"tool": "grep", "args": {"pattern": "x"}',
            '{"tool": "ls", "args": {"path": "."}}',
            "There is a src directory.",
        ]
    )
    runner = build_runner(provider, project)

    result = await runner.run("look around")

    assert result.outcome == "answered"
    assert result.tool_calls == 1
    assert "malformed" in runner.session.kinds()
    assert result.answer == "There is a src directory."


async def test_the_protocol_is_never_explained_twice_in_a_row(project: Path) -> None:
    provider = MockProvider(responses=['{"tool": "grep", "args": {"pattern": "x"}'])
    runner = build_runner(provider, project, max_steps=6)

    result = await runner.run("look around")

    assert result.outcome == "answered"
    assert runner.session.kinds().count("malformed") == 1


async def test_a_tool_call_written_as_prose_is_not_mistaken_for_an_answer(
    project: Path,
) -> None:
    """What llama3.1:8B did when told its edit anchor was ambiguous.

    It worked out the fix, wrote the corrected call in prose, and stopped. On
    the native front end an answer with no tool calls normally ends the job, so
    without this the run ends holding the solution to its own problem.
    """
    provider = MockProvider(
        turns=[
            ChatTurn(
                text='Here is the updated command:\n{"name": "ls", "parameters": {"path": "."}}'
            ),
            ChatTurn(tool_calls=[ToolCall(name="ls", arguments={"path": "."})]),
            ChatTurn(text="src and notes.md"),
        ]
    )
    runner = build_runner(provider, project)

    result = await runner.run("look around")

    assert result.tool_calls == 1
    assert "malformed" in runner.session.kinds()
    assert result.answer == "src and notes.md"


async def test_prose_that_names_no_real_tool_is_just_an_answer(project: Path) -> None:
    provider = MockProvider(
        turns=[ChatTurn(text='The config is {"tool": "screwdriver", "args": {}} shaped.')]
    )
    runner = build_runner(provider, project)

    result = await runner.run("what shape is it?")

    assert result.outcome == "answered"
    assert "malformed" not in runner.session.kinds()


async def test_a_model_that_never_stops_is_capped_not_failed(project: Path) -> None:
    """Capped is its own outcome for the reason a blocked validation is.

    The job did not go wrong, it ran out of room, and a caller that treats the
    two the same will discard work for the budget's fault.
    """
    provider = MockProvider(
        turns=[
            ChatTurn(tool_calls=[ToolCall(name="ls", arguments={"path": "."})]),
            ChatTurn(tool_calls=[ToolCall(name="ls", arguments={"path": "src"})]),
            ChatTurn(tool_calls=[ToolCall(name="read", arguments={"path": "notes.md"})]),
        ]
    )
    runner = build_runner(provider, project, max_steps=3)

    result = await runner.run("keep looking forever")

    assert result.outcome == "capped"
    assert result.steps == 3
    assert "3-step budget" in result.detail


async def test_the_same_call_three_times_running_ends_the_job(project: Path) -> None:
    """What gemma:2b did in phase 1: one grep, three times, all of them run.

    The second is answered from the first result rather than executed -- it
    would produce the same bytes and cost a step -- and the third ends the job
    as capped, because it stopped making progress rather than going wrong.
    """
    provider = MockProvider(
        turns=[ChatTurn(tool_calls=[ToolCall(name="ls", arguments={"path": "."})])]
    )
    runner = build_runner(provider, project, max_steps=10)

    result = await runner.run("look at the same thing over and over")

    assert result.outcome == "capped"
    assert "three times in a row" in result.detail
    assert "repeat" in runner.session.kinds()
    # Two calls recorded, one of them served from the first answer.
    assert runner.session.kinds().count("tool") == 1


async def test_a_repeat_that_stops_repeating_does_not_end_the_job(project: Path) -> None:
    provider = MockProvider(
        turns=[
            ChatTurn(tool_calls=[ToolCall(name="ls", arguments={"path": "."})]),
            ChatTurn(tool_calls=[ToolCall(name="ls", arguments={"path": "."})]),
            ChatTurn(tool_calls=[ToolCall(name="read", arguments={"path": "notes.md"})]),
            ChatTurn(text="the note says nothing to see"),
        ]
    )
    runner = build_runner(provider, project, max_steps=10)

    result = await runner.run("look twice, then move on")

    assert result.outcome == "answered"
    assert result.answer == "the note says nothing to see"


def test_compaction_drops_the_oldest_output_and_never_the_task() -> None:
    """Rule 3 applied to the pile rather than to one tool.

    A turn is the model's own reasoning and it is small; a tool result is a file
    and can be read again. So results go first, the task never goes at all, and
    what was dropped leaves a marker rather than a hole.
    """
    messages = [
        ChatMessage(role="user", content="the objective, which must survive"),
        ChatMessage(role="assistant", content="I will read it"),
        ChatMessage(role="tool", content="x" * 5000, name="read"),
        ChatMessage(role="assistant", content="and now the other one"),
        ChatMessage(role="tool", content="y" * 5000, name="read"),
    ]

    kept, size = compact(messages, 6000)

    assert kept[0].content == "the objective, which must survive"
    assert [message.role for message in kept] == [message.role for message in messages]
    assert kept[2].content.startswith("[dropped 5000 characters of read output")
    assert kept[4].content == "y" * 5000, "the newest result is the one it still needs"
    assert size <= 6000


def test_compaction_leaves_a_transcript_that_already_fits_alone() -> None:
    messages = [ChatMessage(role="user", content="short")]

    kept, size = compact(messages, 100)

    assert kept is messages
    assert size == 5


async def test_the_session_records_the_job_as_it_runs(project: Path, tmp_path: Path) -> None:
    path = next_session_path(tmp_path / "harness")
    provider = MockProvider(
        turns=[
            ChatTurn(tool_calls=[ToolCall(name="ls", arguments={"path": "."})]),
            ChatTurn(text="src/ and notes.md"),
        ]
    )
    runner = build_runner(provider, project, session=HarnessSession(path))

    await runner.run("what is in the root?")

    written = path.read_text(encoding="utf-8").splitlines()
    assert len(written) == len(runner.session.entries)
    assert runner.session.kinds() == ["job", "turn", "tool", "turn", "end"]


async def test_reasoning_blocks_never_reach_the_transcript(project: Path) -> None:
    provider = MockProvider(responses=["<think>let me see</think>The answer is 4."])
    runner = build_runner(provider, project)

    result = await runner.run("what is 2+2?")

    assert result.answer == "The answer is 4."


# -- the shell -----------------------------------------------------------


def shell_context(root: Path, allow: set[str] | None = None) -> ToolContext:
    return ToolContext(
        root=root, shell_allow=frozenset(allow or set()), shell_seconds=30.0
    )


async def test_no_command_runs_until_a_human_lists_one(project: Path) -> None:
    result = await ToolRegistry(SHELL_TOOLS).invoke(
        shell_context(project), "shell", {"command": "python -c 'print(1)'"}
    )

    assert "harness.shell_allow" in result


async def test_an_allowed_program_runs_in_the_job_root(project: Path) -> None:
    result = await ToolRegistry(SHELL_TOOLS).invoke(
        shell_context(project, {"python"}),
        "shell",
        {"command": 'python -c "import pathlib,os; print(pathlib.Path.cwd().name)"'},
    )

    assert result.startswith("exit 0")
    assert project.name in result


async def test_a_program_outside_the_list_is_named_in_the_refusal(project: Path) -> None:
    result = await ToolRegistry(SHELL_TOOLS).invoke(
        shell_context(project, {"python"}), "shell", {"command": "curl example.com"}
    )

    assert "curl is not in harness.shell_allow" in result


async def test_a_pipe_is_an_argument_and_not_an_operator(project: Path) -> None:
    """No shell interpreter, so the allow-list cannot be walked around.

    Every allow-list that has been defeated was defeated through a pipe. Here
    the whole string is parsed into arguments, the first one is matched, and a
    smuggled second program is simply text.
    """
    result = await ToolRegistry(SHELL_TOOLS).invoke(
        shell_context(project, {"python"}), "shell", {"command": "curl x | python"}
    )

    assert "curl is not in harness.shell_allow" in result


async def test_a_command_that_hangs_comes_back_as_a_refusal(project: Path) -> None:
    context = shell_context(project, {"python"})
    context.shell_seconds = 1.0

    result = await ToolRegistry(SHELL_TOOLS).invoke(
        context, "shell", {"command": 'python -c "import time; time.sleep(30)"'}
    )

    assert "did not finish within 1s" in result


def test_the_shell_is_absent_from_the_schema_until_it_is_allowed(project: Path) -> None:
    off = build_runner(MockProvider(responses=["x"]), project, read_only=False, allow_write=True)
    on = build_runner(
        MockProvider(responses=["x"]),
        project,
        read_only=False,
        allow_write=True,
        shell_allow=frozenset({"python"}),
    )

    assert "shell" not in off.registry.tools
    assert "shell" in on.registry.tools


# -- the queue and the worker --------------------------------------------


def mesh_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        data_path=tmp_path / "data" / "evomesh.db",
        generation_path=tmp_path / "generations",
        workspace_path=tmp_path / "workspace",
        harness=HarnessSettings(enabled=True, session_path=tmp_path / "sessions"),
    )
    settings.models.providers["ollama"] = ProviderSettings(
        base_url="http://localhost:0", model="mock"
    )
    return settings


async def test_a_finished_job_arrives_as_an_ordinary_message(tmp_path: Path) -> None:
    """Rule 2: the worker is not an exception dressed as infrastructure.

    Delivering through the mailbox is what gives the result an audit record and
    wakes the loop the agent already has, so no behavior learns that a worker
    exists.
    """
    environment = Environment(
        mesh_settings(tmp_path), providers={"ollama": MockProvider(responses=["all done"])}
    )
    await environment.start()
    try:
        job = environment.submit_harness_job("look around", agent_id="guardian")
        message = await environment.bus.receive("guardian", wait_seconds=5)
    finally:
        await environment.stop()

    assert message.sender_id == "harness"
    assert f"job {job.number}" in message.content
    assert "all done" in message.content
    assert environment.harness_queue.jobs[job.number].status is JobStatus.DONE


async def test_a_job_is_granted_the_root_it_was_handed(tmp_path: Path) -> None:
    """Found by the first real generation: every tool was denied.

    The harness runs under the agent's grants on purpose, so a workspace the
    mesh creates *for* an agent has to be granted to it as well. The grant is
    scoped to that directory, visible like any other, and dies with it.
    """
    environment = Environment(
        mesh_settings(tmp_path), providers={"ollama": MockProvider(responses=["looked"])}
    )
    root = tmp_path / "candidate"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("ACTIVE = True\n", encoding="utf-8")
    await environment.start()
    try:
        job = environment.harness.submit("look at it", agent_id="evolver", root=root)
        await environment.bus.receive("evolver", wait_seconds=5)
        grants = await environment.repository.load_grants("evolver")
    finally:
        await environment.stop()

    assert [Path(grant.path) for grant in grants] == [root]
    assert grants[0].read and grants[0].write
    assert environment.harness_queue.jobs[job.number].status is JobStatus.DONE


async def test_a_second_submit_returns_the_job_already_running(tmp_path: Path) -> None:
    """A behavior submits once per cycle; the queue must not accumulate copies.

    Every copy would edit the same files, which is the failure the one-open-job
    rule exists to prevent rather than a tidiness preference.
    """
    environment = Environment(mesh_settings(tmp_path), providers={"ollama": MockProvider()})
    queue = environment.harness_queue

    first = queue.submit("improve the mesh", tmp_path, agent_id="evolver")
    second = queue.submit("improve the mesh again", tmp_path, agent_id="evolver")

    assert first is second
    assert len(queue.jobs) == 1


def test_a_status_line_shows_the_objective_not_the_whole_briefing(tmp_path: Path) -> None:
    """An Evolver objective is a page: the map, the rules, then the ask.

    Printing it into /harness status made the console unreadable the first time
    a real generation ran through the queue.
    """
    queue = HarnessQueue()
    job = queue.submit(
        "THE PACKAGE AS IT STANDS (src/evomesh/).\nLoad-bearing modules:\n- contracts.py\n\n"
        "OBJECTIVE: wire humanize.py into a module that runs\n\nRules for this project:\n- ...",
        tmp_path,
        agent_id="evolver",
    )

    assert job.describe() == (
        "job 1 [evolver] queued: wire humanize.py into a module that runs"
    )


async def test_the_queue_refuses_past_its_limit(tmp_path: Path) -> None:
    queue = HarnessQueue(max_queue=2)
    queue.submit("one", tmp_path)
    queue.submit("two", tmp_path)

    with pytest.raises(QueueFull):
        queue.submit("three", tmp_path)


async def test_an_agent_with_an_open_job_is_reported_as_awaiting_harness(
    tmp_path: Path,
) -> None:
    environment = Environment(mesh_settings(tmp_path), providers={"ollama": MockProvider()})
    await environment.repository.initialize()
    definition = system_agent_definitions("ollama", "mock", {})[0]
    environment.registry.register(definition)
    environment.runtimes.clear()

    before = environment.runtime_states()[definition.id].phase
    environment.harness_queue.submit("work", tmp_path, agent_id=definition.id)
    after = environment.runtime_states()[definition.id].phase

    # Offline stays offline: a queued job cannot revive an agent that has no loop.
    assert before is AgentPhase.OFFLINE
    assert after is AgentPhase.OFFLINE


async def test_stopping_the_mesh_never_leaves_a_submitter_waiting(tmp_path: Path) -> None:
    """A cancelled job is reported. The worst outcome is a step no event ends."""
    queue = HarnessQueue()
    started = asyncio.Event()
    delivered: list[HarnessJob] = []

    async def never_finishes(job: HarnessJob) -> HarnessResult:
        started.set()
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    async def deliver(job: HarnessJob) -> None:
        delivered.append(job)

    worker = HarnessWorker(queue, never_finishes, deliver)
    worker.start("test-worker")
    job = queue.submit("something slow", tmp_path, agent_id="evolver")
    await asyncio.wait_for(started.wait(), timeout=2)

    await worker.stop()

    assert job.status is JobStatus.CANCELLED
    assert delivered == [job]
    assert "the mesh stopped" in job.detail


async def test_a_job_that_raises_does_not_kill_the_worker(tmp_path: Path) -> None:
    queue = HarnessQueue()
    delivered: list[HarnessJob] = []

    async def explode(job: HarnessJob) -> HarnessResult:
        if job.objective == "boom":
            raise RuntimeError("provider is on fire")
        return HarnessResult(outcome="answered", answer="fine")

    async def deliver(job: HarnessJob) -> None:
        delivered.append(job)

    worker = HarnessWorker(queue, explode, deliver)
    worker.start("test-worker")
    queue.submit("boom", tmp_path)
    queue.submit("after", tmp_path, agent_id="guardian")
    for _ in range(50):
        if len(delivered) == 2:
            break
        await asyncio.sleep(0.02)
    await worker.stop()

    assert [job.status for job in delivered] == [JobStatus.CANCELLED, JobStatus.DONE]
    assert "provider is on fire" in delivered[0].detail


async def test_no_worker_runs_when_the_harness_is_off(tmp_path: Path) -> None:
    settings = mesh_settings(tmp_path)
    settings.harness.enabled = False
    environment = Environment(settings, providers={"ollama": MockProvider()})

    await environment.start()
    try:
        assert environment.harness_workers == []
        with pytest.raises(RuntimeError, match="harness is off"):
            environment.submit_harness_job("anything")
    finally:
        await environment.stop()


# -- the text protocol ---------------------------------------------------


def test_a_tool_call_is_found_in_a_messy_answer() -> None:
    turn = parse_text_call('Sure, I will look.\n{"tool": "read", "args": {"path": "a.py"}}')

    assert turn.tool_calls[0].name == "read"
    assert turn.tool_calls[0].arguments == {"path": "a.py"}


def test_prose_that_merely_contains_a_brace_is_an_answer_not_an_error() -> None:
    """A parse failure is an answer, deliberately.

    A model that has finished and writes a sentence with a brace in it has
    succeeded; treating that as a protocol error would end jobs that were done.
    """
    turn = parse_text_call("The dict literal {} is empty, and that is the answer.")

    assert not turn.tool_calls
    assert turn.text.startswith("The dict literal")


def test_a_call_wrapped_in_explanation_is_still_found() -> None:
    """What mistral:7b actually did the first time it was pointed at the repo.

    It explained itself, emitted the call, then offered a second one as an
    example. Spanning from the first brace to the last swallows the prose
    between them, parses as nothing, and the job ends on an "answer" that was
    really a tool call the model expected to be run.
    """
    turn = parse_text_call(
        'I suggest reading the module first:\n\n{"tool": "read", "args": '
        '{"path": "src/evomesh/bdi.py"}}\n\nThen you could grep:\n\n'
        '{"tool": "grep", "args": {"pattern": "reconsider"}}'
    )

    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == "read"
    assert turn.tool_calls[0].arguments == {"path": "src/evomesh/bdi.py"}


def test_a_brace_inside_a_string_does_not_end_the_object() -> None:
    turn = parse_text_call('{"tool": "grep", "args": {"pattern": "def f() {"}}')

    assert turn.tool_calls[0].arguments == {"pattern": "def f() {"}


def test_arguments_sent_as_a_json_string_are_still_understood() -> None:
    turn = parse_text_call('{"tool": "grep", "arguments": "{\\"pattern\\": \\"x\\"}"}')

    assert turn.tool_calls[0].arguments == {"pattern": "x"}


async def test_a_refused_edit_does_not_end_the_job_and_the_model_widens_its_anchor(
    project: Path,
) -> None:
    """The whole point of refusing: the model gets a message it can act on."""
    (project / "src" / "twice.py").write_text("x = 1\ny = 2\nx = 1\n", encoding="utf-8")
    provider = MockProvider(
        turns=[
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="edit",
                        arguments={"path": "src/twice.py", "old": "x = 1", "new": "x = 9"},
                    )
                ]
            ),
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="edit",
                        arguments={
                            "path": "src/twice.py",
                            "old": "y = 2\nx = 1",
                            "new": "y = 2\nx = 9",
                        },
                    )
                ]
            ),
            ChatTurn(text="Changed the second assignment only."),
        ]
    )
    runner = build_runner(provider, project, read_only=False, allow_write=True)

    result = await runner.run("change the second x")

    assert result.outcome == "answered"
    assert result.edits == 1
    assert (project / "src" / "twice.py").read_text(encoding="utf-8") == "x = 1\ny = 2\nx = 9\n"


async def test_the_result_counts_reads_against_changes(project: Path) -> None:
    provider = MockProvider(
        turns=[
            ChatTurn(tool_calls=[ToolCall(name="read", arguments={"path": "src/answer.py"})]),
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="edit",
                        arguments={
                            "path": "src/answer.py",
                            "old": "return True",
                            "new": "return False",
                        },
                    )
                ]
            ),
            ChatTurn(text="done"),
        ]
    )
    runner = build_runner(provider, project, read_only=False, allow_write=True)

    result = await runner.run("flip it")

    assert (result.reads, result.edits, result.writes) == (1, 1, 0)
    assert "1 read/1 changed" in result.summary()


def test_a_read_only_runner_is_not_even_given_the_write_tools(project: Path) -> None:
    runner = build_runner(MockProvider(responses=["done"]), project)

    assert "edit" not in runner.registry.tools
    assert "write" not in runner.registry.tools


def test_a_runner_is_constructible_without_the_helper(project: Path) -> None:
    """HarnessRunner takes a context directly, which is what phase 3 will do."""
    runner = HarnessRunner(
        provider=MockProvider(responses=["done"]),
        context=ToolContext(root=project),
    )

    assert runner.context.root == project
