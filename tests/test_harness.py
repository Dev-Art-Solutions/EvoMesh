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


def test_a_runner_is_constructible_without_the_helper(project: Path) -> None:
    """HarnessRunner takes a context directly, which is what phase 3 will do."""
    runner = HarnessRunner(
        provider=MockProvider(responses=["done"]),
        context=ToolContext(root=project),
    )

    assert runner.context.root == project
