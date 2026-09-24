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
from evomesh.bdi import ReflectiveBehavior
from evomesh.cognition import CycleContext
from evomesh.config import HarnessSettings, ProviderSettings, Settings
from evomesh.contracts import AgentDefinition, AgentPhase
from evomesh.environment import Environment
from evomesh.harness import (
    TEXT_PROTOCOL_FORMAT,
    HarnessResult,
    HarnessRunner,
    build_runner,
    compact,
    parse_text_call,
)
from evomesh.harness_queue import (
    HarnessGateway,
    HarnessJob,
    HarnessQueue,
    HarnessWorker,
    JobStatus,
    QueueFull,
)
from evomesh.harness_session import HarnessSession, next_session_path
from evomesh.harness_tools import (
    ALL_TOOLS,
    ASK_TOOLS,
    LEARN_TOOLS,
    SHELL_TOOLS,
    WEB_TOOLS,
    ToolContext,
    ToolLimits,
    ToolRegistry,
    build_custom_tool,
    custom_tool_program,
    tool_grep,
    tool_ls,
    tool_read,
)
from evomesh.models import ChatMessage, ChatTurn, MockProvider, ToolCall
from evomesh.permissions import FilesystemPolicy
from evomesh.processes import CommandResult
from evomesh.skills import MissingSkillError
from evomesh.storage import SQLiteRepository
from evomesh.tools import ToolDefinition, parse_tool


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


# -- skills_root: the mesh-wide skills/ directory is readable from any job's
# own root, not only one whose root happens to be the mesh's project tree --
# see harness_tools._resolve_readable's own docstring for the live bug this
# closes (a NewsAnalyzer job could never actually read its own skill).


@pytest.fixture
def mesh_with_a_skill(tmp_path: Path) -> Path:
    """A mesh project root with one real skill under skills/, and a
    non-system agent's own playground elsewhere -- the shape every
    NewsWatcher/NewsAnalyzer/Trader-style job actually runs with."""
    mesh_root = tmp_path / "mesh"
    (mesh_root / "skills" / "news-impact-analysis").mkdir(parents=True)
    (mesh_root / "skills" / "news-impact-analysis" / "SKILL.md").write_text(
        "---\nname: news-impact-analysis\ndescription: d\n---\n\nReport one line per instrument.\n",
        encoding="utf-8",
    )
    (mesh_root / "secret.txt").write_text("not a skill", encoding="utf-8")
    playground = tmp_path / "workspace" / "agents" / "newsanalyzer" / "playground"
    playground.mkdir(parents=True)
    return mesh_root


async def test_read_falls_back_to_the_mesh_wide_skills_directory(
    mesh_with_a_skill: Path, tmp_path: Path
) -> None:
    playground = tmp_path / "workspace" / "agents" / "newsanalyzer" / "playground"
    context = ToolContext(root=playground, skills_root=mesh_with_a_skill)

    result = await tool_read(context, {"path": "skills/news-impact-analysis/SKILL.md"})

    assert "Report one line per instrument" in result


async def test_grep_and_ls_also_fall_back_to_skills(
    mesh_with_a_skill: Path, tmp_path: Path
) -> None:
    playground = tmp_path / "workspace" / "agents" / "newsanalyzer" / "playground"
    context = ToolContext(root=playground, skills_root=mesh_with_a_skill)

    grepped = await tool_grep(
        context, {"pattern": "one line", "path": "skills", "glob": "*.md"}
    )
    listed = await tool_ls(context, {"path": "skills/news-impact-analysis"})

    assert "one line per instrument" in grepped
    assert "SKILL.md" in listed


async def test_a_skills_fallback_match_is_not_hidden_by_an_unrelated_root_path(
    tmp_path: Path,
) -> None:
    """The bug this guards against: `_inside(context.root, path)` falls back
    to `path`'s full *absolute* parts whenever `path` is not inside
    `context.root` at all -- exactly the skills/ mesh-wide fallback's shape,
    since a non-system agent's root is its own playground, nowhere near the
    mesh-wide skills/ directory. `SKIP_DIRECTORIES` lists "generations", the
    literal directory name every real candidate generation lives under
    (`generations/NNNNNN-candidate/`) -- so on the real project, every single
    skills-fallback grep match was silently discarded as though it sat
    inside a generations/ directory, purely because of where the checkout
    happens to live on disk. Found live, 2026-09-23: this is why every real
    candidate generation ever validated failed
    test_grep_and_ls_also_fall_back_to_skills above, for a reason with
    nothing to do with what it actually changed. "generations" here stands
    in for that real path segment.
    """
    mesh_root = tmp_path / "generations" / "001-candidate" / "mesh"
    (mesh_root / "skills" / "news-impact-analysis").mkdir(parents=True)
    (mesh_root / "skills" / "news-impact-analysis" / "SKILL.md").write_text(
        "---\nname: news-impact-analysis\ndescription: d\n---\n\nReport one line per instrument.\n",
        encoding="utf-8",
    )
    playground = tmp_path / "workspace" / "agents" / "newsanalyzer" / "playground"
    playground.mkdir(parents=True)
    context = ToolContext(root=playground, skills_root=mesh_root)

    result = await tool_grep(
        context, {"pattern": "one line", "path": "skills", "glob": "*.md"}
    )

    assert "one line per instrument" in result


async def test_read_without_skills_root_still_refuses_the_fallback(
    mesh_with_a_skill: Path, tmp_path: Path
) -> None:
    """None (a human's own harness job, or a test with no live mesh) means
    no fallback -- unchanged behaviour from before skills_root existed: the
    path is syntactically inside the job root, just missing there, so the
    refusal is the ordinary "does not exist", not "outside the job root"."""
    playground = tmp_path / "workspace" / "agents" / "newsanalyzer" / "playground"
    context = ToolContext(root=playground)  # skills_root defaults to None

    result = await ToolRegistry(ALL_TOOLS).invoke(
        context, "read", {"path": "skills/news-impact-analysis/SKILL.md"}
    )

    assert result.startswith("DENIED:")
    assert "does not exist" in result


async def test_the_skills_fallback_never_reaches_outside_skills_itself(
    mesh_with_a_skill: Path, tmp_path: Path
) -> None:
    """The fallback is scoped to skills/ specifically -- it must not become
    a second, wider escape hatch into the rest of the mesh's project root."""
    playground = tmp_path / "workspace" / "agents" / "newsanalyzer" / "playground"
    context = ToolContext(root=playground, skills_root=mesh_with_a_skill)

    result = await ToolRegistry(ALL_TOOLS).invoke(context, "read", {"path": "secret.txt"})

    assert result.startswith("DENIED:")
    assert "does not exist" in result


async def test_edit_write_delete_never_get_the_skills_fallback(
    mesh_with_a_skill: Path, tmp_path: Path
) -> None:
    """Only learn_skill/patch_skill may change a skill -- the generic
    write/edit/delete tools stay confined to the job's own root even when
    skills_root is set. edit and delete see the same "does not exist" a
    genuinely missing file would; write would happily create a same-named
    file *inside the job's own playground* (ordinary, harmless behaviour
    for a path that is simply new there) -- the one thing to prove is that
    doing so never touches the real skill in skills_root."""
    playground = tmp_path / "workspace" / "agents" / "newsanalyzer" / "playground"
    context = ToolContext(root=playground, skills_root=mesh_with_a_skill, allow_write=True)
    target = "skills/news-impact-analysis/SKILL.md"
    real_skill = mesh_with_a_skill / "skills" / "news-impact-analysis" / "SKILL.md"
    original = real_skill.read_text(encoding="utf-8")
    registry = ToolRegistry(ALL_TOOLS)

    edited = await registry.invoke(context, "edit", {"path": target, "old": "d", "new": "e"})
    assert edited.startswith("DENIED:") and "does not exist" in edited

    written = await registry.invoke(
        context, "write", {"path": target, "content": "not the real skill"}
    )
    assert written.startswith("created")
    assert (playground / target).read_text(encoding="utf-8") == "not the real skill"
    assert real_skill.read_text(encoding="utf-8") == original

    deleted = await registry.invoke(context, "delete", {"path": target})
    assert deleted.startswith("deleted")
    assert real_skill.read_text(encoding="utf-8") == original


async def test_permit_skips_the_grant_check_for_a_skills_read(
    mesh_with_a_skill: Path, tmp_path: Path
) -> None:
    """render_catalog() is spliced into every job's task unconditionally --
    reading a skill is not gated behind a per-agent FilesystemGrant, the
    same way the job's own root never needed one either."""
    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    policy = FilesystemPolicy(repository)  # no grants given at all
    playground = tmp_path / "workspace" / "agents" / "newsanalyzer" / "playground"
    context = ToolContext(
        root=playground,
        skills_root=mesh_with_a_skill,
        policy=policy,
        agent_id="agent:newsanalyzer",
    )

    result = await tool_read(context, {"path": "skills/news-impact-analysis/SKILL.md"})

    assert "Report one line per instrument" in result


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


async def test_a_read_cut_by_characters_says_where_it_really_stopped(project: Path) -> None:
    """Found live 2026-09-24: a read at offset=481 cut by the character budget
    said "1 more lines withheld, use offset=201" whatever it had shown, and a
    small model sent back to line 201 over and over decided its read tool was
    returning fabricated content. The cut is on a whole line, and the hint
    names the real next line."""
    rows = "\n".join(f"line {n:04d} " + "x" * 30 for n in range(1, 501))
    (project / "long.py").write_text(rows, encoding="utf-8")
    context = ToolContext(root=project, limits=ToolLimits(result_chars=500))

    result = await tool_read(context, {"path": "long.py", "offset": 481, "limit": 15})

    body, note = result.rsplit("\n", 1)
    last_row = body.splitlines()[-1]
    last = int(last_row.split("|")[0])
    assert 481 < last < 495
    assert last_row.endswith("x" * 30)  # a whole line, never half of one
    assert note == (
        f"[... {495 - last} more lines withheld, showing lines 481-{last}, "
        f"use offset={last + 1} ...]"
    )


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
    assert "that text is not in" in result


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


async def test_write_is_denied_not_crashed_when_a_parent_is_a_plain_file(
    project: Path,
) -> None:
    """Found live: a generation once wrote a *file* at docs/evolution/plans
    (meaning to write docs/evolution/plans/plan.md), and every candidate
    since inherited it from the checkout -- mkdir(parents=True, exist_ok=True)
    forgives an existing directory, not an existing file, so every later
    attempt to write a file under that path raised a raw OSError that crashed
    the whole harness job rather than reaching the model as a result."""
    (project / "plans").write_text("plan.md\n", encoding="utf-8")

    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project), "write", {"path": "plans/plan.md", "content": "# Plan\n"}
    )

    assert result.startswith("DENIED:")
    assert "plans" in result
    assert not (project / "plans" / "plan.md").exists()


async def test_a_write_outside_the_root_never_reaches_the_disk(project: Path) -> None:
    outside = project.parent / "escaped.py"

    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project / "src"), "write", {"path": "../../escaped.py", "content": "x"}
    )

    assert result.startswith("DENIED:")
    assert not outside.exists()


async def test_delete_removes_a_file_and_records_a_diff(project: Path) -> None:
    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project), "delete", {"path": "notes.md"}
    )

    assert result.startswith("deleted")
    assert not (project / "notes.md").exists()
    assert "-nothing to see" in result


async def test_delete_refuses_a_directory(project: Path) -> None:
    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project), "delete", {"path": "src"}
    )

    assert result.startswith("DENIED:")
    assert (project / "src").is_dir()


async def test_delete_refuses_a_missing_path(project: Path) -> None:
    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project), "delete", {"path": "never-existed.txt"}
    )

    assert result.startswith("DENIED:")
    assert "does not exist" in result


async def test_delete_outside_the_root_never_reaches_the_disk(project: Path) -> None:
    outside = project.parent / "escaped.py"
    outside.write_text("x", encoding="utf-8")

    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project / "src"), "delete", {"path": "../../escaped.py"}
    )

    assert result.startswith("DENIED:")
    assert outside.exists()


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


async def test_a_not_found_edit_shows_the_real_file_instead_of_just_saying_reread(
    project: Path,
) -> None:
    """A model that fabricates `old` from a description, not a real read, needs a
    concrete anchor to correct against -- not another blind re-read that just gets
    re-fabricated the same way. See tests below for the two shapes of hint."""
    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project),
        "edit",
        {
            "path": "src/answer.py",
            "old": "def reconsider() -> bool:\n    return False  # never happens",
            "new": "x",
        },
    )

    assert "DENIED: that text is not in" in result
    # The first line of `old` really is in the file -- shown so the model can see
    # exactly where its guess diverges from the real body.
    assert "This line of 'old' does appear" in result
    assert "def reconsider() -> bool:" in result


async def test_a_wholly_fabricated_edit_shows_the_start_of_the_real_file(
    project: Path,
) -> None:
    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project),
        "edit",
        {
            "path": "src/answer.py",
            "old": (
                "async def run_forever(self) -> None:\n"
                "    while True:\n"
                "        await self.step()"
            ),
            "new": "x",
        },
    )

    assert "No line of 'old' appears anywhere in the file" in result
    assert "def reconsider() -> bool:" in result


async def test_a_not_found_edit_names_the_file_it_actually_came_from(
    project: Path,
) -> None:
    """`old` is not fabricated at all here -- it is real code, copied
    correctly, just aimed at the wrong path. Found live: a job `read`
    agent_strategies.py, then submitted this exact `edit` against
    harness_tools.py instead. That is a different mistake from inventing
    text, and needs a different correction: the real file's name, not
    another dump of whatever the wrong file happens to start with."""
    (project / "src" / "other.py").write_text(
        "def distinctive_marker_line() -> None:\n    pass\n", encoding="utf-8"
    )

    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project),
        "edit",
        {
            "path": "src/answer.py",
            "old": "def distinctive_marker_line() -> None:\n    pass",
            "new": "x",
        },
    )

    assert "DENIED: that text is not in" in result
    assert "it does appear in src/other.py" in result
    assert "you may be editing the wrong path" in result


async def test_a_not_found_edit_prefers_a_distinctive_anchor_over_a_generic_one(
    project: Path,
) -> None:
    """A short generic line (`continue`) can coincidentally match unrelated code --
    picking that as the anchor points the model at the wrong place. The real,
    distinctive line should win even though it appears later in `old`."""
    (project / "src" / "loopy.py").write_text(
        "def first(items):\n"
        "    for item in items:\n"
        "        if item is None:\n"
        "            continue\n"
        "        print(item)\n"
        "\n\n"
        "def second(items):\n"
        "    for item in items:\n"
        "        if not item.enabled:\n"
        "            continue\n"
        "        yield item.value_for_report()\n",
        encoding="utf-8",
    )

    result = await ToolRegistry(ALL_TOOLS).invoke(
        writable(project),
        "edit",
        {
            "path": "src/loopy.py",
            "old": (
                "def second(items):\n"
                "    for item in items:\n"
                "        continue\n"
                "        yield item.value_for_report()"
            ),
            "new": "x",
        },
    )

    assert "This line of 'old' does appear" in result
    # The distinctive, uniquely-matching line is shown, not the ambiguous "continue".
    assert "yield item.value_for_report()" in result


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


async def test_structured_fallback_sends_format_on_the_text_protocol_only(
    project: Path,
) -> None:
    provider = MockProvider(responses=["It is defined in src/answer.py."])
    runner = build_runner(provider, project, structured_fallback=True)

    await runner.run("where does reconsider live?")

    assert provider.calls[-1]["format"] == TEXT_PROTOCOL_FORMAT


async def test_structured_fallback_off_by_default_sends_no_format(project: Path) -> None:
    provider = MockProvider(responses=["It is defined in src/answer.py."])
    runner = build_runner(provider, project)

    await runner.run("where does reconsider live?")

    assert provider.calls[-1]["format"] is None


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


async def test_a_non_adjacent_repeat_read_is_answered_from_cache(project: Path) -> None:
    """Found live: 116 of 1870 tool calls across 60 recent harness sessions
    were an exact repeat of an earlier call in the same job -- every one of
    them non-adjacent (something else ran in between), so the "same call
    three times running" guard above never once caught it. A small model
    re-reading a file it already has should not cost a real tool call."""
    provider = MockProvider(
        turns=[
            ChatTurn(tool_calls=[ToolCall(name="read", arguments={"path": "notes.md"})]),
            ChatTurn(tool_calls=[ToolCall(name="ls", arguments={"path": "."})]),
            ChatTurn(tool_calls=[ToolCall(name="read", arguments={"path": "notes.md"})]),
            ChatTurn(text="the note says nothing to see"),
        ]
    )
    runner = build_runner(provider, project, max_steps=10)

    result = await runner.run("read it, look around, read it again")

    assert result.outcome == "answered"
    assert "cached" in runner.session.kinds()
    # Two read calls happened; only the first one was a real tool invocation.
    assert runner.session.kinds().count("tool") == 2  # one read, one ls


async def test_a_write_between_two_identical_reads_forces_a_real_reread(
    project: Path,
) -> None:
    """The one thing that would make the cache above actively wrong: serving
    a read from before the job's own write, hiding its own edit from it."""
    provider = MockProvider(
        turns=[
            ChatTurn(tool_calls=[ToolCall(name="read", arguments={"path": "notes.md"})]),
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="write",
                        arguments={
                            "path": "notes.md",
                            "content": "something to see now\n",
                            "overwrite": True,
                        },
                    )
                ]
            ),
            ChatTurn(tool_calls=[ToolCall(name="read", arguments={"path": "notes.md"})]),
            ChatTurn(text="done"),
        ]
    )
    runner = build_runner(provider, project, read_only=False, allow_write=True, max_steps=10)

    await runner.run("read it, change it, read it again")

    assert "cached" not in runner.session.kinds()
    # read, write, read -- all three real, nothing served from a stale cache.
    assert runner.session.kinds().count("tool") == 3


async def test_shell_is_never_served_from_the_read_cache(project: Path) -> None:
    """Deliberately excluded: a shell command is neither guaranteed pure
    (side effects) nor guaranteed idempotent (the world can change between
    two calls to the same command) the way read/grep/ls are."""
    provider = MockProvider(
        turns=[
            ChatTurn(tool_calls=[ToolCall(name="shell", arguments={"command": "python -V"})]),
            ChatTurn(tool_calls=[ToolCall(name="ls", arguments={"path": "."})]),
            ChatTurn(tool_calls=[ToolCall(name="shell", arguments={"command": "python -V"})]),
            ChatTurn(text="done"),
        ]
    )
    runner = build_runner(
        provider, project, shell_allow=frozenset({"python"}), max_steps=10
    )

    await runner.run("run it, look around, run it again")

    assert "cached" not in runner.session.kinds()
    assert runner.session.kinds().count("tool") == 3


async def test_a_writing_job_is_told_once_that_it_has_changed_nothing(
    project: Path,
) -> None:
    """A 27B model spent all 20 steps reading and never edited anything.

    The repeat guard could not see it: it re-read the same two files at
    different offsets, which is a different call every time. A model cannot
    budget what it cannot see, so past halfway it is told where it stands --
    once, because twice is noise.
    """
    provider = MockProvider(
        turns=[
            ChatTurn(tool_calls=[ToolCall(name="read", arguments={"path": "notes.md"})]),
            ChatTurn(
                tool_calls=[
                    ToolCall(name="read", arguments={"path": "notes.md", "offset": 1})
                ]
            ),
            ChatTurn(
                tool_calls=[
                    ToolCall(name="read", arguments={"path": "notes.md", "offset": 2})
                ]
            ),
            ChatTurn(text="I looked at everything."),
        ]
    )
    runner = build_runner(provider, project, read_only=False, allow_write=True, max_steps=4)

    await runner.run("change something")

    assert runner.session.kinds().count("budget") == 1
    sent = provider.chats[-1]
    assert any("have not changed a file yet" in message.content for message in sent)


async def test_a_job_is_told_once_to_stop_fabricating_old(project: Path) -> None:
    """Two `edit` calls in a row whose `old` text matches nothing in the file
    at all -- not stale, not mis-indented -- is a model composing `old` from
    what it thinks the code should say rather than from an actual `read`.
    Found live: a job did this seven times straight and never once corrected
    itself off the denial's own excerpt of the real file, burning its whole
    budget on edits that could never land.
    """
    provider = MockProvider(
        turns=[
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="edit",
                        arguments={
                            "path": "src/answer.py",
                            "old": "def totally_invented() -> None:\n    pass",
                            "new": "x",
                        },
                    )
                ]
            ),
            # Different `old` text -- a second, differently fabricated guess,
            # not the exact same call the repeat guard already catches.
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="edit",
                        arguments={
                            "path": "src/answer.py",
                            "old": "def another_invention() -> None:\n    pass",
                            "new": "x",
                        },
                    )
                ]
            ),
            ChatTurn(text="giving up"),
        ]
    )
    runner = build_runner(provider, project, read_only=False, allow_write=True, max_steps=5)

    await runner.run("change something")

    assert runner.session.kinds().count("fabrication") == 1
    sent = provider.chats[-1]
    tool_messages = [message.content for message in sent if message.role == "tool"]
    assert not any("stop composing 'old'" in content.lower() for content in tool_messages[:1])
    assert any("stop composing 'old'" in content.lower() for content in tool_messages[1:])


async def test_a_read_between_two_fabricated_edits_does_not_reset_the_count(
    project: Path,
) -> None:
    """Found live: a job re-read the file between two fabricated `edit`
    denials -- a reasonable thing to do, checking itself -- and that alone
    reset the count to zero, so the nudge needed a third fabrication instead
    of a second. It never got a third try before the job ran out of steps.
    Only a landed write should earn a clean slate; a read in between two
    fabrications is still two fabrications.
    """
    provider = MockProvider(
        turns=[
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="edit",
                        arguments={
                            "path": "src/answer.py",
                            "old": "def totally_invented() -> None:\n    pass",
                            "new": "x",
                        },
                    )
                ]
            ),
            ChatTurn(tool_calls=[ToolCall(name="read", arguments={"path": "src/answer.py"})]),
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="edit",
                        arguments={
                            "path": "src/answer.py",
                            "old": "def another_invention() -> None:\n    pass",
                            "new": "x",
                        },
                    )
                ]
            ),
            ChatTurn(text="giving up"),
        ]
    )
    runner = build_runner(provider, project, read_only=False, allow_write=True, max_steps=6)

    await runner.run("change something")

    assert runner.session.kinds().count("fabrication") == 1


async def test_a_denial_for_a_different_reason_does_not_count_as_fabrication(
    project: Path,
) -> None:
    """`old == new` and `old` missing entirely are different failures -- only
    the second one is the model inventing text, so only it should count
    toward the fabrication nudge."""
    provider = MockProvider(
        turns=[
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="edit",
                        arguments={"path": "src/answer.py", "old": "x", "new": "x"},
                    )
                ]
            ),
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="edit",
                        arguments={
                            "path": "src/answer.py",
                            "old": "def invented() -> None:\n    pass",
                            "new": "x",
                        },
                    )
                ]
            ),
            ChatTurn(text="done"),
        ]
    )
    runner = build_runner(provider, project, read_only=False, allow_write=True, max_steps=5)

    await runner.run("change something")

    assert runner.session.kinds().count("fabrication") == 0


async def test_a_job_that_is_already_editing_is_not_nagged(project: Path) -> None:
    provider = MockProvider(
        turns=[
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
            ChatTurn(tool_calls=[ToolCall(name="read", arguments={"path": "notes.md"})]),
            ChatTurn(text="done"),
        ]
    )
    runner = build_runner(provider, project, read_only=False, allow_write=True, max_steps=4)

    await runner.run("change something")

    assert "budget" not in runner.session.kinds()


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


def test_next_session_path_prunes_old_transcripts_beyond_retention(tmp_path: Path) -> None:
    """Found live: 10168 of these accumulated, one per harness job ever run,
    none ever removed. Beyond retention, the oldest have to actually go."""
    directory = tmp_path / "harness"
    directory.mkdir()
    for number in range(1, 6):
        (directory / f"{number:06d}.jsonl").write_text("{}\n", encoding="utf-8")

    next_session_path(directory, keep=2)

    remaining = {path.stem for path in directory.glob("*.jsonl")}
    assert remaining == {"000004", "000005"}


def test_next_session_path_never_prunes_below_the_keep_count(tmp_path: Path) -> None:
    directory = tmp_path / "harness"
    directory.mkdir()
    (directory / "000001.jsonl").write_text("{}\n", encoding="utf-8")

    next_session_path(directory, keep=500)

    assert (directory / "000001.jsonl").exists()


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


# -- self-check ------------------------------------------------------------


async def test_a_failing_self_check_sends_the_job_back_to_fix_it(project: Path) -> None:
    """self_check_command runs against the real file the edit tool actually
    touched -- not a mock -- so this is a genuine end-to-end pass: wrong
    value, blocked from answering, fixed for real, then accepted."""
    (project / "check.py").write_text(
        "import sys\nsys.exit(0 if 'return False' in open('src/answer.py').read() else 1)\n",
        encoding="utf-8",
    )
    provider = MockProvider(
        turns=[
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="edit",
                        arguments={
                            "path": "src/answer.py", "old": "return True", "new": "return 1"
                        },
                    )
                ]
            ),
            ChatTurn(text="done"),
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="edit",
                        arguments={
                            "path": "src/answer.py", "old": "return 1", "new": "return False"
                        },
                    )
                ]
            ),
            ChatTurn(text="done for real"),
        ]
    )
    runner = build_runner(
        provider, project, read_only=False, allow_write=True, self_check_command="python check.py"
    )

    result = await runner.run("fix the return value")

    assert result.outcome == "answered"
    assert result.answer == "done for real"
    assert "return False" in (project / "src" / "answer.py").read_text(encoding="utf-8")


async def test_a_self_check_that_never_passes_still_ends_but_says_so(project: Path) -> None:
    """The attempt budget is not a promise the check will ever pass -- a job
    stuck on a pre-existing, unrelated failure must still end rather than
    burn its whole step budget on a fight it cannot win. The residual
    failure rides along in the answer instead of vanishing silently."""
    (project / "check.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    provider = MockProvider(
        turns=[
            ChatTurn(
                tool_calls=[
                    ToolCall(name="write", arguments={"path": "new.py", "content": "x = 1\n"})
                ]
            ),
            ChatTurn(text="one"),
            ChatTurn(text="two"),
        ]
    )
    runner = build_runner(
        provider,
        project,
        read_only=False,
        allow_write=True,
        self_check_command="python check.py",
        self_check_max_attempts=2,
    )

    result = await runner.run("add a file")

    assert result.outcome == "answered"
    assert result.answer.startswith("two")
    assert "self-check still reports problems after 2 attempt(s)" in result.answer


async def test_self_check_is_skipped_when_nothing_was_changed(project: Path) -> None:
    """A read-only answer has nothing for a linter to check -- running the
    command anyway would just be latency with no signal."""
    (project / "check.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    provider = MockProvider(responses=["nothing needed changing"])
    runner = build_runner(
        provider, project, read_only=False, allow_write=True, self_check_command="python check.py"
    )

    result = await runner.run("is anything broken?")

    assert result.outcome == "answered"
    assert result.answer == "nothing needed changing"


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


async def test_python_cannot_shell_out_to_undo_its_own_edit(project: Path) -> None:
    """A job wrote a real file, then reverted it with `git checkout` run
    through `python -c "import subprocess; ..."` -- the harness's own
    edit/write/delete tracking never saw the revert, so the generation
    looked like a no-op and was discarded. `subprocess` (and the other
    process-spawning entry points) must be denied the same as a program
    outside shell_allow, or `shell_allow: [python]` is not an allow-list at
    all -- it is unrestricted shell access with extra steps.
    """
    result = await ToolRegistry(SHELL_TOOLS).invoke(
        shell_context(project, {"python"}),
        "shell",
        {
            "command": (
                'python -c "import subprocess; '
                "subprocess.run(['git', 'checkout', 'a.py'])\""
            )
        },
    )

    assert "DENIED" in result
    assert "subprocess" in result.lower()


async def test_a_python_snippet_with_no_process_spawning_still_runs(project: Path) -> None:
    result = await ToolRegistry(SHELL_TOOLS).invoke(
        shell_context(project, {"python"}),
        "shell",
        {"command": 'python -c "print(1 + 1)"'},
    )

    assert result.startswith("exit 0")


async def test_python_cannot_write_a_file_directly(project: Path) -> None:
    """Found live: a job that never once got an `edit` to land instead ran
    `python - <<'EOF'` piping a script that did `open(path, 'w').write(...)`
    -- a raw file write with none of edit/write/delete's tracking, and none
    of the fabrication guardrails those tools carry (this same job's `old`
    text didn't even exist in the real file, same as every fabricated
    `edit`). A subprocess is not the only way to mutate a file out from
    under the harness's own bookkeeping -- plain file I/O in the interpreter
    that is already running is another, and `subprocess`-only denylisting
    misses it entirely.
    """
    result = await ToolRegistry(SHELL_TOOLS).invoke(
        shell_context(project, {"python"}),
        "shell",
        {
            "command": (
                "python -c \"open('src/answer.py', 'w').write('x = 1')\""
            )
        },
    )

    assert "DENIED" in result


async def test_python_read_only_file_access_still_runs(project: Path) -> None:
    result = await ToolRegistry(SHELL_TOOLS).invoke(
        shell_context(project, {"python"}),
        "shell",
        {"command": "python -c \"print(open('src/answer.py').read())\""},
    )

    assert result.startswith("exit 0")


async def test_a_heredoc_is_denied_instead_of_hanging_until_the_timeout(
    project: Path,
) -> None:
    """`python - <<'EOF'` pipes a script over stdin -- but nothing here reads
    stdin for it, so instead of failing fast like `&&` does, it just hangs
    until shell_seconds runs out. Found live: 60 of a job's ~240 spent
    seconds went to exactly this. shlex glues `<<'EOF'` into one token,
    `<<EOF`, so this has to be a prefix check, not exact membership.
    """
    result = await ToolRegistry(SHELL_TOOLS).invoke(
        shell_context(project, {"python"}),
        "shell",
        {"command": "python - <<'EOF'"},
    )

    assert "DENIED" in result
    assert "no shell interpreter" in result.lower()


async def test_a_chained_command_is_denied_instead_of_run_as_literal_args(
    project: Path,
) -> None:
    """There is no shell interpreter, so `&&` is not an operator -- it is
    just another argument. Found live: `python -m py_compile x.py && echo OK`
    handed py_compile a file literally named `&&` to compile next, and came
    back as `[Errno 2] No such file or directory: '&&'` -- a result that
    means nothing to a model that just wanted to chain two commands. Denying
    it up front, by name, beats letting the model spend a step on a traceback
    it cannot interpret.
    """
    result = await ToolRegistry(SHELL_TOOLS).invoke(
        shell_context(project, {"python"}),
        "shell",
        {"command": 'python -m py_compile x.py && echo OK'},
    )

    assert "DENIED" in result
    assert "no shell interpreter" in result.lower()


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


# -- custom, declarative tools ---------------------------------------------


def check_site_definition(tmp_path: Path) -> tuple[ToolDefinition, Path]:
    """A tool bundle in its own directory, separate from the job root a test
    later invokes it against -- this is exactly the arrangement {tool_dir}
    exists for: a bundled script found by the tool's own location, not by
    whatever happens to be the calling job's root."""
    bundle = tmp_path / "tool-bundle"
    scripts = bundle / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "check.py").write_text(
        "import sys\nprint(f'checked {sys.argv[1]}')\n", encoding="utf-8"
    )
    definition = parse_tool(
        Path("tools/check-site/TOOL.md"),
        "---\nname: check-site\ndescription: Check a site.\n"
        'command: python "{tool_dir}/scripts/check.py"\n'
        "parameters:\n  - name: url\n---\n",
    )
    return definition, bundle


async def test_a_custom_tool_runs_its_command_with_the_argument_appended(
    project: Path,
) -> None:
    definition, bundle = check_site_definition(project)
    tool = build_custom_tool(definition, tool_dir=bundle)

    result = await ToolRegistry((tool,)).invoke(
        shell_context(project, {"python"}), "check-site", {"url": "https://example.com"}
    )

    assert result.startswith("exit 0")
    assert "checked https://example.com" in result


async def test_a_custom_tool_finds_its_script_regardless_of_the_job_root(
    project: Path, tmp_path: Path
) -> None:
    """The bug {tool_dir} exists to fix: a plain relative path in `command`
    resolves against the job's cwd, which is almost never the tool's own
    directory once a non-system agent's playground is the job root."""
    definition, bundle = check_site_definition(project)
    tool = build_custom_tool(definition, tool_dir=bundle)
    unrelated_job_root = tmp_path / "some-agents-playground"
    unrelated_job_root.mkdir()

    result = await ToolRegistry((tool,)).invoke(
        shell_context(unrelated_job_root, {"python"}),
        "check-site",
        {"url": "https://example.com"},
    )

    assert result.startswith("exit 0")
    assert "checked https://example.com" in result


async def test_a_custom_tool_is_denied_when_its_program_is_not_allowed(
    project: Path,
) -> None:
    definition, bundle = check_site_definition(project)
    tool = build_custom_tool(definition, tool_dir=bundle)

    result = await ToolRegistry((tool,)).invoke(
        shell_context(project), "check-site", {"url": "https://example.com"}
    )

    assert "not in harness.shell_allow" in result


async def test_a_custom_tool_refuses_a_missing_required_argument(project: Path) -> None:
    definition, bundle = check_site_definition(project)
    tool = build_custom_tool(definition, tool_dir=bundle)

    result = await ToolRegistry((tool,)).invoke(
        shell_context(project, {"python"}), "check-site", {}
    )

    assert "needs: url" in result


def test_custom_tool_program_names_the_program_the_allow_list_checks() -> None:
    definition = parse_tool(
        Path("tools/x/TOOL.md"),
        "---\nname: x\ndescription: d\ncommand: Python.exe scripts/x.py\n---\n",
    )

    assert custom_tool_program(definition) == "python"


# -- fetching a URL --------------------------------------------------------


async def test_no_url_is_fetched_until_a_human_configures_the_fetcher(project: Path) -> None:
    context = ToolContext(root=project)

    result = await ToolRegistry(WEB_TOOLS).invoke(context, "fetch", {"url": "https://example.com"})

    assert "scraping.executable" in result


async def test_a_configured_fetcher_returns_its_output(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_run_command(
        program: str, *arguments: str, cwd: Path | None = None, timeout_seconds: float | None = None
    ):
        calls.append((program, *arguments))
        await asyncio.to_thread(
            Path(arguments[3]).write_text, "# Example\n\nHello.\n", encoding="utf-8"
        )
        return CommandResult(exit_code=0, output="")

    monkeypatch.setattr("evomesh.harness_tools.run_command", fake_run_command)
    context = ToolContext(
        root=project, scraping_executable="fake-scrapling", scraping_timeout=15.0
    )

    result = await ToolRegistry(WEB_TOOLS).invoke(
        context, "fetch", {"url": "https://example.com", "css_selector": "article"}
    )

    assert result == "# Example\n\nHello."
    program, *arguments = calls[0]
    assert program == "fake-scrapling"
    assert arguments[:3] == ["extract", "get", "https://example.com"]
    assert "--css-selector" in arguments and "article" in arguments
    assert context.tally.reads == 1


async def test_dynamic_fetch_uses_the_browser_command_and_millisecond_timeout(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_run_command(
        program: str, *arguments: str, cwd: Path | None = None, timeout_seconds: float | None = None
    ):
        calls.append((program, *arguments))
        await asyncio.to_thread(
            Path(arguments[3]).write_text, "Rendered.\n", encoding="utf-8"
        )
        return CommandResult(exit_code=0, output="")

    monkeypatch.setattr("evomesh.harness_tools.run_command", fake_run_command)
    context = ToolContext(
        root=project, scraping_executable="fake-scrapling", scraping_timeout=15.0
    )

    result = await ToolRegistry(WEB_TOOLS).invoke(
        context, "fetch", {"url": "https://example.com", "dynamic": True}
    )

    assert result == "Rendered."
    _, *arguments = calls[0]
    assert arguments[:3] == ["extract", "fetch", "https://example.com"]
    # Milliseconds, not seconds -- the browser subcommand's own unit.
    assert "15000" in arguments
    assert "15" not in arguments


async def test_a_failed_fetch_is_named_in_the_refusal(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_run_command(
        program: str, *arguments: str, cwd: Path | None = None, timeout_seconds: float | None = None
    ):
        return CommandResult(exit_code=1, output="ConnectionError: name resolution failed")

    monkeypatch.setattr("evomesh.harness_tools.run_command", failing_run_command)
    context = ToolContext(root=project, scraping_executable="fake-scrapling")

    result = await ToolRegistry(WEB_TOOLS).invoke(
        context, "fetch", {"url": "https://nowhere.invalid"}
    )

    assert "DENIED" in result
    assert "name resolution failed" in result


async def test_ask_agent_returns_the_other_agents_real_answer(project: Path) -> None:
    async def fake_ask(agent: str, question: str) -> str:
        assert agent == "Trader"
        assert question == "what is your current position?"
        return "Flat, no open positions."

    context = ToolContext(root=project, ask_agent=fake_ask)

    result = await ToolRegistry(ASK_TOOLS).invoke(
        context, "ask_agent", {"agent": "Trader", "question": "what is your current position?"}
    )

    assert result == "Flat, no open positions."


async def test_ask_agent_is_denied_without_agent_or_question(project: Path) -> None:
    async def unreachable(agent: str, question: str) -> str:
        raise AssertionError("must not be called with missing arguments")

    context = ToolContext(root=project, ask_agent=unreachable)

    missing_agent = await ToolRegistry(ASK_TOOLS).invoke(
        context, "ask_agent", {"question": "hello"}
    )
    missing_question = await ToolRegistry(ASK_TOOLS).invoke(
        context, "ask_agent", {"agent": "Trader"}
    )

    assert "DENIED" in missing_agent
    assert "DENIED" in missing_question


async def test_ask_agent_names_a_timeout_or_missing_agent_in_the_refusal(
    project: Path,
) -> None:
    async def times_out(agent: str, question: str) -> str:
        raise TimeoutError

    async def not_found(agent: str, question: str) -> str:
        raise KeyError(agent)

    timed_out = await ToolRegistry(ASK_TOOLS).invoke(
        ToolContext(root=project, ask_agent=times_out),
        "ask_agent",
        {"agent": "Trader", "question": "hi"},
    )
    missing = await ToolRegistry(ASK_TOOLS).invoke(
        ToolContext(root=project, ask_agent=not_found),
        "ask_agent",
        {"agent": "Nobody", "question": "hi"},
    )

    assert "DENIED" in timed_out and "Trader" in timed_out
    assert "DENIED" in missing and "Nobody" in missing


def test_ask_agent_is_absent_from_the_schema_until_it_is_configured(project: Path) -> None:
    async def fake_ask(agent: str, question: str) -> str:
        return "unused"

    off = build_runner(MockProvider(responses=["x"]), project)
    on = build_runner(MockProvider(responses=["x"]), project, ask_agent=fake_ask)

    assert "ask_agent" not in off.registry.tools
    assert "ask_agent" in on.registry.tools


async def test_learn_skill_forwards_to_the_bound_callback(project: Path) -> None:
    async def fake_learn(name: str, description: str, body: str) -> str:
        assert name == "news-report-export"
        assert description == "Export headlines as a file."
        assert "news_fetch" in body
        return "Learned 'news-report-export': Export headlines as a file. (skills/.../SKILL.md)"

    context = ToolContext(root=project, learn_skill=fake_learn)

    result = await ToolRegistry(LEARN_TOOLS).invoke(
        context,
        "learn_skill",
        {
            "name": "news-report-export",
            "description": "Export headlines as a file.",
            "body": "Call news_fetch, then document_write, then FILE: <path>.",
        },
    )

    assert result.startswith("Learned 'news-report-export'")


async def test_learn_skill_is_denied_without_access(project: Path) -> None:
    context = ToolContext(root=project)  # learn_skill left at its default: None

    result = await ToolRegistry(LEARN_TOOLS).invoke(
        context, "learn_skill", {"name": "x", "description": "y", "body": "z"}
    )

    assert "DENIED" in result
    assert "not been granted" in result


async def test_learn_skill_is_denied_without_each_required_field(project: Path) -> None:
    async def unreachable(name: str, description: str, body: str) -> str:
        raise AssertionError("must not be called with a missing field")

    context = ToolContext(root=project, learn_skill=unreachable)

    missing_name = await ToolRegistry(LEARN_TOOLS).invoke(
        context, "learn_skill", {"description": "d", "body": "b"}
    )
    missing_description = await ToolRegistry(LEARN_TOOLS).invoke(
        context, "learn_skill", {"name": "n", "body": "b"}
    )
    missing_body = await ToolRegistry(LEARN_TOOLS).invoke(
        context, "learn_skill", {"name": "n", "description": "d"}
    )

    assert "DENIED" in missing_name
    assert "DENIED" in missing_description
    assert "DENIED" in missing_body


async def test_learn_skill_surfaces_the_callbacks_own_refusal(project: Path) -> None:
    async def rejecting(name: str, description: str, body: str) -> str:
        raise ValueError(f"'{name}' already exists and this agent did not author it")

    context = ToolContext(root=project, learn_skill=rejecting)

    result = await ToolRegistry(LEARN_TOOLS).invoke(
        context, "learn_skill", {"name": "news-triage", "description": "d", "body": "b"}
    )

    assert "DENIED" in result
    assert "news-triage" in result


def test_learn_skill_is_absent_from_the_schema_until_it_is_configured(project: Path) -> None:
    async def fake_learn(name: str, description: str, body: str) -> str:
        return "unused"

    off = build_runner(MockProvider(responses=["x"]), project)
    on = build_runner(MockProvider(responses=["x"]), project, learn_skill=fake_learn)

    assert "learn_skill" not in off.registry.tools
    assert "learn_skill" in on.registry.tools


async def test_patch_skill_forwards_to_the_bound_callback(project: Path) -> None:
    async def fake_patch(name: str, old_text: str, new_text: str) -> str:
        assert name == "news-report-export"
        assert old_text == "default to .pdf"
        assert new_text == "default to .docx"
        return "Patched 'news-report-export': ... (skills/.../SKILL.md)"

    context = ToolContext(root=project, patch_skill=fake_patch)

    result = await ToolRegistry(LEARN_TOOLS).invoke(
        context,
        "patch_skill",
        {
            "name": "news-report-export",
            "old_text": "default to .pdf",
            "new_text": "default to .docx",
        },
    )

    assert result.startswith("Patched 'news-report-export'")


async def test_patch_skill_is_denied_without_access(project: Path) -> None:
    context = ToolContext(root=project)  # patch_skill left at its default: None

    result = await ToolRegistry(LEARN_TOOLS).invoke(
        context, "patch_skill", {"name": "x", "old_text": "a", "new_text": "b"}
    )

    assert "DENIED" in result
    assert "not been granted" in result


async def test_patch_skill_is_denied_without_a_name_or_old_text(project: Path) -> None:
    async def unreachable(name: str, old_text: str, new_text: str) -> str:
        raise AssertionError("must not be called with a missing field")

    context = ToolContext(root=project, patch_skill=unreachable)

    missing_name = await ToolRegistry(LEARN_TOOLS).invoke(
        context, "patch_skill", {"old_text": "a", "new_text": "b"}
    )
    missing_old = await ToolRegistry(LEARN_TOOLS).invoke(
        context, "patch_skill", {"name": "n", "new_text": "b"}
    )

    assert "DENIED" in missing_name
    assert "DENIED" in missing_old


async def test_patch_skill_surfaces_the_callbacks_own_refusal(project: Path) -> None:
    async def rejecting(name: str, old_text: str, new_text: str) -> str:
        raise ValueError(f"'{old_text[:10]}...' appears 3 times in '{name}'")

    context = ToolContext(root=project, patch_skill=rejecting)

    result = await ToolRegistry(LEARN_TOOLS).invoke(
        context, "patch_skill", {"name": "news-triage", "old_text": "call it", "new_text": "x"}
    )

    assert "DENIED" in result
    assert "news-triage" in result


def test_patch_skill_is_absent_from_the_schema_until_it_is_configured(project: Path) -> None:
    async def fake_patch(name: str, old_text: str, new_text: str) -> str:
        return "unused"

    off = build_runner(MockProvider(responses=["x"]), project)
    on = build_runner(MockProvider(responses=["x"]), project, patch_skill=fake_patch)

    # patch_skill is not gated on its own -- it rides in with learn_skill
    # (see LEARN_TOOLS in harness_tools.py), the same single capability
    # AgentDefinition.can_learn_skills grants both halves of.
    assert "patch_skill" not in off.registry.tools
    assert "patch_skill" not in on.registry.tools
    both_on = build_runner(
        MockProvider(responses=["x"]), project, learn_skill=fake_patch, patch_skill=fake_patch
    )
    assert "patch_skill" in both_on.registry.tools


def test_fetch_is_absent_from_the_schema_until_it_is_configured(project: Path) -> None:
    off = build_runner(MockProvider(responses=["x"]), project)
    on = build_runner(
        MockProvider(responses=["x"]), project, scraping_executable="fake-scrapling"
    )

    assert "fetch" not in off.registry.tools
    assert "fetch" in on.registry.tools


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


def _writing_settings(tmp_path: Path, *, self_check_command: str) -> Settings:
    settings = mesh_settings(tmp_path)
    settings.harness = HarnessSettings(
        enabled=True,
        allow_write=True,
        session_path=tmp_path / "sessions",
        self_check_command=self_check_command,
    )
    return settings


async def test_an_agent_specific_self_check_overrides_the_mesh_wide_one(
    tmp_path: Path,
) -> None:
    """A coding agent working in its own real project needs that project's
    own lint/test command, not whatever the mesh-wide setting happens to be
    pointed at -- see AgentDefinition.self_check_command."""
    always_fails = tmp_path / "always_fails.py"
    always_fails.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    settings = _writing_settings(tmp_path, self_check_command=f'python "{always_fails}"')
    provider = MockProvider(
        turns=[
            ChatTurn(
                tool_calls=[
                    ToolCall(name="write", arguments={"path": "out.py", "content": "x = 1\n"})
                ]
            ),
            ChatTurn(text="done"),
        ]
    )
    environment = Environment(settings, providers={"ollama": provider})
    await environment.start()
    # Empty, not None -- explicitly off for this agent despite the mesh-wide
    # command above always failing.
    agent = AgentDefinition(name="Coder", purpose="Write code", self_check_command="")
    await environment.register_agent(agent)
    try:
        job = environment.submit_harness_job("add a file", agent_id=agent.id, root=tmp_path)
        message = await environment.bus.receive(agent.id, wait_seconds=5)
    finally:
        await environment.stop()

    assert f"job {job.number}" in message.content
    assert "self-check" not in message.content
    assert "done" in message.content


async def test_an_agent_without_its_own_override_uses_the_mesh_wide_self_check(
    tmp_path: Path,
) -> None:
    always_fails = tmp_path / "always_fails.py"
    always_fails.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    settings = _writing_settings(tmp_path, self_check_command=f'python "{always_fails}"')
    settings.harness.self_check_max_attempts = 1
    provider = MockProvider(
        turns=[
            ChatTurn(
                tool_calls=[
                    ToolCall(name="write", arguments={"path": "out.py", "content": "x = 1\n"})
                ]
            ),
            ChatTurn(text="done"),
        ]
    )
    environment = Environment(settings, providers={"ollama": provider})
    await environment.start()
    agent = AgentDefinition(name="Coder", purpose="Write code")  # self_check_command: None
    await environment.register_agent(agent)
    try:
        job = environment.submit_harness_job("add a file", agent_id=agent.id, root=tmp_path)
        message = await environment.bus.receive(agent.id, wait_seconds=5)
    finally:
        await environment.stop()

    assert f"job {job.number}" in message.content
    assert "self-check still reports problems" in message.content


async def test_a_granted_agent_can_learn_a_skill_through_a_real_harness_job(
    tmp_path: Path,
) -> None:
    """The whole wire, not just tool_learn_skill in isolation:
    AgentDefinition.can_learn_skills -> _run_harness_job reads it off the
    registered agent -> build_runner registers learn_skill -> the model
    actually calls it -> Environment._make_learn_skill writes a real
    skills/<name>/SKILL.md the registry can discover afterward."""
    settings = mesh_settings(tmp_path)
    provider = MockProvider(
        turns=[
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="learn_skill",
                        arguments={
                            "name": "news-report-export",
                            "description": "Export headlines as a file.",
                            "body": "Call news_fetch, then document_write, then FILE: <path>.",
                        },
                    )
                ]
            ),
            ChatTurn(text="done"),
        ]
    )
    environment = Environment(settings, providers={"ollama": provider})
    await environment.start()
    agent = AgentDefinition(name="NewsWatcher", purpose="Watch news", can_learn_skills=True)
    await environment.register_agent(agent)
    try:
        job = environment.submit_harness_job(
            "figure out the export", agent_id=agent.id, root=tmp_path
        )
        message = await environment.bus.receive(agent.id, wait_seconds=5)
    finally:
        await environment.stop()

    assert f"job {job.number}" in message.content
    assert "done" in message.content
    learned = environment.skills.get("news-report-export")
    assert learned.description == "Export headlines as a file."
    assert learned.created_by == f"agent:{agent.id}"


async def test_a_granted_agent_can_patch_its_own_skill_through_a_real_harness_job(
    tmp_path: Path,
) -> None:
    settings = mesh_settings(tmp_path)
    provider = MockProvider(
        turns=[
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="patch_skill",
                        arguments={
                            "name": "news-report-export",
                            "old_text": "Default to .pdf",
                            "new_text": "Default to .docx",
                        },
                    )
                ]
            ),
            ChatTurn(text="done"),
        ]
    )
    environment = Environment(settings, providers={"ollama": provider})
    await environment.start()
    agent = AgentDefinition(name="NewsWatcher", purpose="Watch news", can_learn_skills=True)
    await environment.register_agent(agent)
    # learn_skill doesn't touch the provider at all -- calling it directly
    # here (rather than through a scripted turn) leaves the single
    # ChatTurn above free for the harness job below to consume.
    learn = environment._make_learn_skill(agent.id)  # noqa: SLF001
    await learn("news-report-export", "v1", "Default to .pdf unless told otherwise.")
    try:
        job = environment.submit_harness_job(
            "fix the export default", agent_id=agent.id, root=tmp_path
        )
        message = await environment.bus.receive(agent.id, wait_seconds=5)
    finally:
        await environment.stop()

    assert f"job {job.number}" in message.content
    assert "done" in message.content
    body = await environment.skills.read("news-report-export")
    assert "Default to .docx unless told otherwise." in body


async def test_an_ungranted_agent_cannot_learn_a_skill(tmp_path: Path) -> None:
    """can_learn_skills defaults to False -- learn_skill is never even
    registered for this job (see test_learn_skill_is_absent_from_the_schema_
    until_it_is_configured), so the model calling it anyway gets an unknown-
    tool result, same as any other tool name it might hallucinate."""
    settings = mesh_settings(tmp_path)
    provider = MockProvider(
        turns=[
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="learn_skill",
                        arguments={"name": "x", "description": "y", "body": "z"},
                    )
                ]
            ),
            ChatTurn(text="never mind"),
        ]
    )
    environment = Environment(settings, providers={"ollama": provider})
    await environment.start()
    agent = AgentDefinition(name="NewsWatcher", purpose="Watch news")  # can_learn_skills: False
    await environment.register_agent(agent)
    try:
        job = environment.submit_harness_job(
            "try to learn something", agent_id=agent.id, root=tmp_path
        )
        message = await environment.bus.receive(agent.id, wait_seconds=5)
    finally:
        await environment.stop()

    assert f"job {job.number}" in message.content
    with pytest.raises(MissingSkillError):
        environment.skills.get("x")


async def test_learn_skill_refuses_to_overwrite_a_human_authored_skill(tmp_path: Path) -> None:
    settings = mesh_settings(tmp_path)
    environment = Environment(settings, providers={"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="NewsWatcher", purpose="Watch news", can_learn_skills=True)
    await environment.register_agent(agent)
    await environment.skills.install(
        "---\nname: news-triage\ndescription: Human-curated.\n---\n\nDo the human's way.\n",
        created_by="human",
    )
    learn = environment._make_learn_skill(agent.id)  # noqa: SLF001

    with pytest.raises(ValueError, match="did not author it"):
        await learn("news-triage", "A model's own version.", "Do it a different way.")

    assert environment.skills.get("news-triage").description == "Human-curated."
    await environment.stop()


async def test_learn_skill_can_update_its_own_earlier_skill(tmp_path: Path) -> None:
    settings = mesh_settings(tmp_path)
    environment = Environment(settings, providers={"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="NewsWatcher", purpose="Watch news", can_learn_skills=True)
    await environment.register_agent(agent)
    learn = environment._make_learn_skill(agent.id)  # noqa: SLF001

    first = await learn("news-report-export", "v1", "Do it the first way.")
    second = await learn("news-report-export", "v2", "Do it the better way.")

    assert first.startswith("Learned")
    assert second.startswith("Updated")
    assert environment.skills.get("news-report-export").description == "v2"
    await environment.stop()


async def test_learn_skill_rejects_a_name_that_is_not_a_safe_path_segment(
    tmp_path: Path,
) -> None:
    settings = mesh_settings(tmp_path)
    environment = Environment(settings, providers={"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="NewsWatcher", purpose="Watch news", can_learn_skills=True)
    await environment.register_agent(agent)
    learn = environment._make_learn_skill(agent.id)  # noqa: SLF001

    with pytest.raises(ValueError, match="not a valid skill name"):
        await learn("../../etc/passwd", "d", "b")

    await environment.stop()


async def test_patch_skill_replaces_the_one_unique_match(tmp_path: Path) -> None:
    settings = mesh_settings(tmp_path)
    environment = Environment(settings, providers={"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="NewsWatcher", purpose="Watch news", can_learn_skills=True)
    await environment.register_agent(agent)
    learn = environment._make_learn_skill(agent.id)  # noqa: SLF001
    patch = environment._make_patch_skill(agent.id)  # noqa: SLF001
    await learn("news-report-export", "v1", "Default to .pdf unless told otherwise.")

    result = await patch("news-report-export", "Default to .pdf", "Default to .docx")

    assert result.startswith("Patched")
    body = await environment.skills.read("news-report-export")
    assert "Default to .docx unless told otherwise." in body
    await environment.stop()


async def test_patch_skill_refuses_a_missing_skill(tmp_path: Path) -> None:
    settings = mesh_settings(tmp_path)
    environment = Environment(settings, providers={"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="NewsWatcher", purpose="Watch news", can_learn_skills=True)
    await environment.register_agent(agent)
    patch = environment._make_patch_skill(agent.id)  # noqa: SLF001

    with pytest.raises(ValueError, match="does not exist yet"):
        await patch("no-such-skill", "a", "b")

    await environment.stop()


async def test_patch_skill_refuses_a_skill_it_did_not_author(tmp_path: Path) -> None:
    settings = mesh_settings(tmp_path)
    environment = Environment(settings, providers={"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="NewsWatcher", purpose="Watch news", can_learn_skills=True)
    await environment.register_agent(agent)
    await environment.skills.install(
        "---\nname: news-triage\ndescription: Human-curated.\n---\n\nDo it the human's way.\n",
        created_by="human",
    )
    patch = environment._make_patch_skill(agent.id)  # noqa: SLF001

    with pytest.raises(ValueError, match="did not author it"):
        await patch("news-triage", "the human's way", "some other way")

    await environment.stop()


async def test_patch_skill_refuses_a_non_unique_match(tmp_path: Path) -> None:
    settings = mesh_settings(tmp_path)
    environment = Environment(settings, providers={"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="NewsWatcher", purpose="Watch news", can_learn_skills=True)
    await environment.register_agent(agent)
    learn = environment._make_learn_skill(agent.id)  # noqa: SLF001
    patch = environment._make_patch_skill(agent.id)  # noqa: SLF001
    await learn("dup", "d", "Step one. Step two. Step one again.")

    with pytest.raises(ValueError, match="appears 2 times"):
        await patch("dup", "Step one", "Step zero")

    await environment.stop()


# -- HarnessSettings.skill_write_approval: stage, review, approve, reject ---


async def test_learn_skill_stages_instead_of_writing_when_approval_is_on(
    tmp_path: Path,
) -> None:
    settings = mesh_settings(tmp_path)
    settings.harness.skill_write_approval = True
    environment = Environment(settings, providers={"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="NewsWatcher", purpose="Watch news", can_learn_skills=True)
    await environment.register_agent(agent)
    learn = environment._make_learn_skill(agent.id)  # noqa: SLF001

    result = await learn("news-report-export", "Export headlines.", "Do the thing.")

    assert "Staged as #1" in result
    assert "not written yet" in result
    with pytest.raises(MissingSkillError):
        environment.skills.get("news-report-export")
    assert 1 in environment.pending_skill_writes
    await environment.stop()


async def test_approve_skill_write_commits_exactly_what_was_staged(tmp_path: Path) -> None:
    settings = mesh_settings(tmp_path)
    settings.harness.skill_write_approval = True
    environment = Environment(settings, providers={"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="NewsWatcher", purpose="Watch news", can_learn_skills=True)
    await environment.register_agent(agent)
    learn = environment._make_learn_skill(agent.id)  # noqa: SLF001
    await learn("news-report-export", "Export headlines.", "Do the thing.")

    definition = await environment.approve_skill_write(1)

    assert definition.name == "news-report-export"
    assert definition.created_by == f"agent:{agent.id}"
    assert environment.skills.get("news-report-export").description == "Export headlines."
    assert 1 not in environment.pending_skill_writes
    await environment.stop()


async def test_reject_skill_write_discards_it_without_touching_disk(tmp_path: Path) -> None:
    settings = mesh_settings(tmp_path)
    settings.harness.skill_write_approval = True
    environment = Environment(settings, providers={"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="NewsWatcher", purpose="Watch news", can_learn_skills=True)
    await environment.register_agent(agent)
    learn = environment._make_learn_skill(agent.id)  # noqa: SLF001
    await learn("news-report-export", "Export headlines.", "Do the thing.")

    discarded = environment.reject_skill_write(1)

    assert discarded.name == "news-report-export"
    assert 1 not in environment.pending_skill_writes
    with pytest.raises(MissingSkillError):
        environment.skills.get("news-report-export")
    await environment.stop()


async def test_approve_and_reject_raise_for_an_unknown_number(tmp_path: Path) -> None:
    settings = mesh_settings(tmp_path)
    environment = Environment(settings, providers={"ollama": MockProvider()})
    await environment.start()

    with pytest.raises(MissingSkillError):
        await environment.approve_skill_write(99)
    with pytest.raises(MissingSkillError):
        environment.reject_skill_write(99)

    await environment.stop()


async def test_approve_skill_write_rechecks_authorship_at_commit_time(tmp_path: Path) -> None:
    """The skill landscape can move between staging and approval -- a human
    could write a same-named skill in the meantime. Approving must not be a
    way around the same collision rule a live (unstaged) write enforces."""
    settings = mesh_settings(tmp_path)
    settings.harness.skill_write_approval = True
    environment = Environment(settings, providers={"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="NewsWatcher", purpose="Watch news", can_learn_skills=True)
    await environment.register_agent(agent)
    learn = environment._make_learn_skill(agent.id)  # noqa: SLF001
    await learn("news-report-export", "Export headlines.", "Do the thing.")
    await environment.skills.install(
        "---\nname: news-report-export\ndescription: A human wrote this meanwhile.\n"
        "---\n\nDo it the human's way.\n",
        created_by="human",
    )

    with pytest.raises(ValueError, match="did not author it"):
        await environment.approve_skill_write(1)

    assert environment.skills.get("news-report-export").created_by == "human"
    await environment.stop()


async def test_a_notify_false_job_never_reaches_the_mailbox(tmp_path: Path) -> None:
    """A step that polls its own job's result (`through_harness`, the Evolver
    pipeline) must not also get it delivered as an inbox message -- an agent
    that answers every inbound message via the harness would otherwise be
    handed its own finished step as a fresh "question" to answer, whose
    answer is delivered the same way, forever. Live, this was NewsAnalyzer
    stuck answering its own last answer in a growing loop that never
    touched news again."""
    environment = Environment(
        mesh_settings(tmp_path), providers={"ollama": MockProvider(responses=["all done"])}
    )
    await environment.start()
    try:
        job = environment.harness.submit(
            "look around", agent_id="guardian", root=tmp_path, notify=False
        )
        with pytest.raises(TimeoutError):
            await environment.bus.receive("guardian", wait_seconds=0.5)
    finally:
        await environment.stop()

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


async def test_a_priority_job_is_taken_before_an_earlier_normal_one(tmp_path: Path) -> None:
    """A human waiting on a reactive chat reply (bdi.py's
    _respond_through_harness) must not sit behind background work (the
    Evolver's pipeline, another agent's own plan step) that happened to be
    queued first -- it cannot preempt a job already running, but it can cut
    ahead of whatever is still waiting."""
    queue = HarnessQueue()
    background = queue.submit("background work", tmp_path, agent_id="evolver")
    urgent = queue.submit("a human is waiting", tmp_path, agent_id="news-watcher", priority=True)

    taken = await queue.take()

    assert taken.number == urgent.number
    assert taken is not background


async def test_priority_jobs_stay_fifo_among_themselves(tmp_path: Path) -> None:
    queue = HarnessQueue()
    first = queue.submit("first", tmp_path, agent_id="a", priority=True)
    second = queue.submit("second", tmp_path, agent_id="b", priority=True)

    assert (await queue.take()).number == first.number
    assert (await queue.take()).number == second.number


async def test_a_priority_lane_worker_never_sees_a_background_job(tmp_path: Path) -> None:
    """harness.priority_workers exists precisely so a human's reactive
    question is never stuck behind the background lane -- which only holds
    if a worker reading lane="priority" truly never drains a background job,
    not merely prefers priority when both are ready (that was already the
    old shared-PriorityQueue's behavior, and did not fix the underlying
    problem: a background job already running still was not preempted)."""
    queue = HarnessQueue()
    queue.submit("background work", tmp_path, agent_id="evolver")

    take_task = asyncio.ensure_future(queue.take(lane="priority"))
    await asyncio.sleep(0.05)
    assert not take_task.done()  # nothing priority-lane to hand it

    urgent = queue.submit("a human is waiting", tmp_path, agent_id="news-watcher", priority=True)
    taken = await asyncio.wait_for(take_task, timeout=1)

    assert taken.number == urgent.number


async def test_a_background_lane_worker_never_sees_a_priority_job(tmp_path: Path) -> None:
    queue = HarnessQueue()
    urgent = queue.submit("a human is waiting", tmp_path, agent_id="news-watcher", priority=True)

    take_task = asyncio.ensure_future(queue.take(lane="background"))
    await asyncio.sleep(0.05)
    assert not take_task.done()  # the priority job is not this lane's to take

    background = queue.submit("background work", tmp_path, agent_id="evolver")
    taken = await asyncio.wait_for(take_task, timeout=1)

    assert taken.number == background.number
    # Untouched -- a real priority worker (lane="priority") still owns it.
    assert urgent.status is JobStatus.QUEUED


async def test_lane_any_drains_both_when_they_arrive_at_once(tmp_path: Path) -> None:
    """The race branch inside take(lane="any"): both queues can have an
    item ready in the same instant (two submit() calls before any worker
    has run), and the background number drawn alongside the winning
    priority one must go back to its own queue rather than being dropped
    -- a job silently vanishing from every listing is worse than one
    briefly out of FIFO order."""
    queue = HarnessQueue()
    background = queue.submit("background work", tmp_path, agent_id="evolver")
    urgent = queue.submit("a human is waiting", tmp_path, agent_id="news-watcher", priority=True)

    first = await queue.take(lane="any")
    second = await queue.take(lane="any")

    assert {first.number, second.number} == {background.number, urgent.number}
    assert first.number == urgent.number  # priority still wins the tie


async def test_a_normal_job_already_running_is_not_interrupted(tmp_path: Path) -> None:
    """Priority only decides what is picked up *next* -- a job the single
    worker this project's target hardware usually has is already mid-run
    keeps running regardless of what arrives after it."""
    queue = HarnessQueue()
    running = queue.submit("already running", tmp_path, agent_id="evolver")
    taken = await queue.take()
    assert taken.number == running.number  # the only worker has it now

    queue.submit("a human is waiting", tmp_path, agent_id="news-watcher", priority=True)

    assert running.status is JobStatus.RUNNING


def test_a_priority_job_is_flagged_in_its_status_line(tmp_path: Path) -> None:
    queue = HarnessQueue()
    job = queue.submit("give me last 10 news", tmp_path, agent_id="news-watcher", priority=True)

    assert job.describe() == (
        "job 1 [news-watcher] queued (priority): give me last 10 news"
    )


async def test_harness_gateway_forwards_priority_to_the_queue(tmp_path: Path) -> None:
    queue = HarnessQueue()
    gateway = HarnessGateway(queue, {})
    background = gateway.submit("background work", agent_id="evolver", root=tmp_path)
    urgent = gateway.submit(
        "a human is waiting", agent_id="news-watcher", root=tmp_path, priority=True
    )

    taken = await queue.take()

    assert taken.number == urgent.number
    assert taken is not background


async def test_the_queue_refuses_past_its_limit(tmp_path: Path) -> None:
    queue = HarnessQueue(max_queue=2)
    queue.submit("one", tmp_path)
    queue.submit("two", tmp_path)

    with pytest.raises(QueueFull):
        queue.submit("three", tmp_path)


def test_a_finished_job_is_pruned_once_retention_is_exceeded(tmp_path: Path) -> None:
    """`jobs` is process memory kept for the mesh's entire uptime -- nothing
    here ever stops running on its own, so an unpruned dict has no ceiling,
    the same class of bug already fixed for generation worktrees, filesystem
    grants, and mesh.log. A finished job's own consumer reads its result once
    and moves on; nothing needs it kept around indefinitely."""
    queue = HarnessQueue(retain_finished=2)
    first = queue.submit("one", tmp_path)
    queue.finish(first, HarnessResult(outcome="answered"))
    second = queue.submit("two", tmp_path)
    queue.finish(second, HarnessResult(outcome="answered"))
    third = queue.submit("three", tmp_path)
    queue.finish(third, HarnessResult(outcome="answered"))

    # Pruning runs on submit, against the finished jobs on record at that
    # moment -- one more submit is what actually pushes the count over
    # retain_finished and prunes the oldest.
    queue.submit("four", tmp_path)

    assert first.number not in queue.jobs
    assert second.number in queue.jobs
    assert third.number in queue.jobs


def test_an_open_job_is_never_pruned_regardless_of_retention(tmp_path: Path) -> None:
    queue = HarnessQueue(retain_finished=1)
    still_open = queue.submit("standing job", tmp_path, agent_id="watcher")
    for index in range(5):
        finished = queue.submit(f"job {index}", tmp_path)
        queue.finish(finished, HarnessResult(outcome="answered"))

    assert still_open.number in queue.jobs


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


# -- the harness for an ordinary agent ------------------------------------


async def granted_agent(tmp_path: Path) -> tuple[Environment, AgentDefinition]:
    environment = Environment(
        mesh_settings(tmp_path),
        providers={"ollama": MockProvider(["1. investigate the notes\n", "found it"])},
    )
    await environment.start()
    agent = AgentDefinition(name="Scout", purpose="Look into things")
    agent.harness_root = str(tmp_path)
    agent.mind.add_goal("Work out why the importer fails")
    await environment.register_agent(agent)
    return environment, agent


async def test_a_granted_agent_takes_a_looking_step_with_tools(tmp_path: Path) -> None:
    """The verb decides, not a second model call.

    Rule 6 again: asking the model whether it wants tools is one extra
    inference per cycle to answer what a prefix answers for free, and on a 4B
    model the answer would be noise.
    """
    environment, agent = await granted_agent(tmp_path)
    behavior = ReflectiveBehavior()
    memory = environment.memory_for(agent)
    await memory.ensure()
    context = CycleContext(
        definition=agent,
        provider=environment.providers["ollama"],
        memory=memory,
        budget=environment.budget,
        services=environment._services(),  # noqa: SLF001 - the environment's own wiring
    )
    try:
        first = await behavior.cycle(context)  # plans, then takes step one
        job = environment.harness_queue.open_jobs()
        assert job, "a looking step became a job"
        assert first.phase is AgentPhase.AWAITING_HARNESS
        assert environment.runtime_states()  # the roster still answers
        # The step is not consumed while the job runs, so the agent keeps one
        # commitment instead of re-adopting a plan every tick.
        intention = agent.mind.current_intention()
        assert intention is not None and intention.cursor == 0
        assert intention.steps[0].job == job[0].number
    finally:
        await environment.stop()


async def test_an_agent_without_a_grant_still_just_prompts(tmp_path: Path) -> None:
    environment, agent = await granted_agent(tmp_path)
    agent.harness_root = ""
    behavior = ReflectiveBehavior()
    memory = environment.memory_for(agent)
    await memory.ensure()
    context = CycleContext(
        definition=agent,
        provider=environment.providers["ollama"],
        memory=memory,
        budget=environment.budget,
        services=environment._services(),  # noqa: SLF001
    )
    try:
        await behavior.cycle(context)
        assert not environment.harness_queue.jobs
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


def test_an_answer_key_is_the_finished_case_under_structured_fallback() -> None:
    """structured_fallback (harness.py's TEXT_PROTOCOL_FORMAT) forces every
    fallback response to be one JSON object, so the model's "I'm done" case
    can no longer be plain text -- it uses an 'answer' key instead, a fourth
    terminal spelling alongside the three tool-name ones."""
    turn = parse_text_call('{"answer": "the fix is in bdi.py"}')

    assert not turn.tool_calls
    assert turn.text == "the fix is in bdi.py"


def test_an_answer_key_does_not_shadow_a_real_tool_call() -> None:
    turn = parse_text_call('{"tool": "read", "args": {"path": "a.py"}, "answer": "ignored"}')

    assert turn.tool_calls[0].name == "read"


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
    assert "delete" not in runner.registry.tools


def test_a_runner_is_constructible_without_the_helper(project: Path) -> None:
    """HarnessRunner takes a context directly, which is what phase 3 will do."""
    runner = HarnessRunner(
        provider=MockProvider(responses=["done"]),
        context=ToolContext(root=project),
    )

    assert runner.context.root == project
