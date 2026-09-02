"""The tool loop, and the four ways it is supposed to refuse.

Every test here runs against MockProvider, so the loop is exercised with no
model, no Ollama and no network -- the same trick that lets CI run the mesh. What
a real 4B model does with a tool schema is the phase's release checklist, not
something a green suite can claim.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evomesh.harness import HarnessRunner, build_runner, parse_text_call
from evomesh.harness_session import HarnessSession, next_session_path
from evomesh.harness_tools import (
    ALL_TOOLS,
    ToolContext,
    ToolLimits,
    ToolRegistry,
    tool_read,
)
from evomesh.models import ChatTurn, MockProvider, ToolCall


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

    assert result.strip().startswith("2  ")
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
        turns=[ChatTurn(tool_calls=[ToolCall(name="ls", arguments={"path": "."})])]
    )
    runner = build_runner(provider, project, max_steps=3)

    result = await runner.run("keep looking forever")

    assert result.outcome == "capped"
    assert result.steps == 3
    assert "3-step budget" in result.detail


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
