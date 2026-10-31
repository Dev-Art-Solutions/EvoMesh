"""The shell tool's word-only scripts, its lenient edit, and what they replaced.

Every case here is a shape counted in the 501 harness sessions read on
2026-10-31, where 36% of all tool calls were refused -- most of them for
being written the way a terminal is written (see harness_script's docstring).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evomesh import harness_script
from evomesh.agent_templates import _merge_new_config_keys
from evomesh.harness_script import ScriptRefused, apply_filter, clip_middle, parse
from evomesh.harness_tools import (
    READ_ONLY_TOOLS,
    SHELL_TOOLS,
    WRITE_TOOLS,
    ToolContext,
    ToolRegistry,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "answer.py").write_text(
        "def reconsider() -> bool:\n    if ready:\n        return True\n    return False\n",
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.json").write_text('{"min_confidence": "medium"}', "utf-8")
    (tmp_path / "data" / "cache.json").write_text("[]", encoding="utf-8")
    (tmp_path / "notes.md").write_text("nothing to see\n", encoding="utf-8")
    return tmp_path


def shell(root: Path, seconds: float = 30.0) -> ToolContext:
    return ToolContext(root=root, shell_allow=frozenset({"python"}), shell_seconds=seconds)


async def run(context: ToolContext, command: str) -> str:
    return await ToolRegistry(SHELL_TOOLS).invoke(context, "shell", {"command": command})


# -- parsing -------------------------------------------------------------


def test_a_script_splits_into_commands_with_their_connectors() -> None:
    commands = parse("python a.py 2>&1 | head -40; echo '---' && ls -la || echo none")

    assert [command.connector for command in commands] == ["", ";", "&&", "||"]
    assert commands[0].stages[0].words == ["python", "a.py"]
    assert commands[0].stages[1].words == ["head", "-40"]
    assert commands[1].stages[0].words == ["echo", "---"]


def test_quoted_operators_stay_inside_their_word() -> None:
    (command,) = parse("""python -c "import x; print('a|b')" 2>/dev/null""")

    assert command.stages[0].words == ["python", "-c", "import x; print('a|b')"]


def test_a_quoted_operator_is_a_word_not_an_operator() -> None:
    # Found live: `... | grep '|' | sort -u` was refused as "a pipe with
    # nothing on one side" -- POSIX lexing had dropped the quotes.
    (command,) = parse("python a.py 2>&1 | grep '|' | grep \"&&\" | sort -u")

    assert [stage.words for stage in command.stages] == [
        ["python", "a.py"],
        ["grep", "|"],
        ["grep", "&&"],
        ["sort", "-u"],
    ]
    assert apply_filter(["grep", "|"], "a | b\nc")[0] == "a | b"


def test_backslash_escapes_parse_the_posix_way() -> None:
    # Non-POSIX lexing broke these, 22 logged scripts in all.
    (first, second) = parse(r"""find . \( -name a -o -name b \) ; grep -c "x\"y\|z" f""")

    assert first.stages[0].words == ["find", ".", "(", "-name", "a", "-o", "-name", "b", ")"]
    assert second.stages[0].words == ["grep", "-c", r'x"y\|z', "f"]


def test_basic_regex_operators_are_the_backslashed_ones() -> None:
    lines = "a+b\nab\nc|d\ncd"

    assert apply_filter(["grep", "a+b"], lines)[0] == "a+b"
    assert apply_filter(["grep", r"ab\|cd"], lines)[0] == "ab\ncd"
    assert apply_filter(["grep", "-E", "ab|cd"], lines)[0] == "ab\ncd"


def test_unquoted_substitution_is_refused_and_quoted_is_a_word() -> None:
    with pytest.raises(ScriptRefused, match="command substitution"):
        parse("ls $(pwd) | head")
    (command,) = parse("python -c 'print(\"$(x)\")' | head -1")

    assert command.stages[0].words == ["python", "-c", 'print("$(x)")']


def test_stdout_to_null_drops_output_and_stderr_to_null_changes_nothing() -> None:
    (quiet,) = parse("python a.py >/dev/null 2>&1")
    (noisy,) = parse("python a.py 2>/dev/null")

    assert quiet.stages[0].discard_output
    assert not noisy.stages[0].discard_output
    assert noisy.stages[0].words == ["python", "a.py"]


@pytest.mark.parametrize(
    ("script", "said"),
    [
        ("python a.py > out.txt", "behind edit/write's back"),
        ("python a.py >> log.txt", "behind edit/write's back"),
        ("python - < input.py", "nothing here does"),
        ("python a.py &", "background"),
        ("curl x | python", "not available after a pipe"),
        ("python a.py | xargs rm", "not available after a pipe"),
        ("python a.py | | head", "nothing on one side"),
    ],
)
def test_what_a_shell_would_need_to_do_is_refused(script: str, said: str) -> None:
    with pytest.raises(ScriptRefused, match=said.replace("|", r"\|")):
        parse(script)


def test_a_python_heredoc_becomes_python_dash_c() -> None:
    script = "python3 - <<'EOF'\nimport json\nprint(json.dumps({'a': 1}))\nEOF"

    assert harness_script.heredoc_command(script) == [
        "python3",
        "-c",
        "import json\nprint(json.dumps({'a': 1}))",
    ]
    assert harness_script.heredoc_command("cat <<EOF\nx\nEOF") is None


def test_only_scripts_take_the_script_parser() -> None:
    # A plain command keeps plain shlex tokens: the script tokenizer would split
    # the unquoted parentheses of `print(1)` into words of their own.
    assert not harness_script.needs_script(["python", "-c", "print(1)"])
    assert harness_script.needs_script(["python", "a.py", "2>&1"])
    assert harness_script.needs_script(["pwd;"])


# -- filters -------------------------------------------------------------


LINES = "\n".join(f"line {number}" for number in range(1, 21))


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["head", "-3"], "line 1\nline 2\nline 3"),
        (["head", "-n", "2"], "line 1\nline 2"),
        (["head"], "\n".join(f"line {n}" for n in range(1, 11))),
        (["tail", "-n", "2"], "line 19\nline 20"),
        (["tail", "-n", "+19"], "line 19\nline 20"),
        (["grep", "line 1"], "\n".join(["line 1", *(f"line {n}" for n in range(10, 20))])),
        (["grep", "-c", "line 2"], "2"),
        (["grep", "-v", "-E", "line [0-9]$|line 1[0-9]"], "line 20"),
        (["grep", "-i", "LINE 20"], "line 20"),
        (["wc", "-l"], "20"),
        (["sort", "-r"], "\n".join(sorted(LINES.splitlines(), reverse=True))),
    ],
)
def test_a_pipe_stage_is_applied_to_the_text(words: list[str], expected: str) -> None:
    assert apply_filter(words, LINES)[0] == expected


SECTIONS = "head\n===== NEW (not yet assessed)\nA | x [1]\nB | y [2]\n===== OLD\nC | z"


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        # The three shapes found live in the first hours on the script parser.
        (["sed", "-n", "/===== NEW/,$p"], "\n".join(SECTIONS.splitlines()[1:])),
        (
            ["sed", "s/.*| //; s/\\[.*//"],
            "head\n===== NEW (not yet assessed)\nx \ny \n===== OLD\nz",
        ),
        (["sed", "-n", "="], "1\n2\n3\n4\n5\n6"),
        (["sed", "-n", "/NEW/,/OLD/p"], "\n".join(SECTIONS.splitlines()[1:5])),
        (["sed", "-n", "2,3p"], "===== NEW (not yet assessed)\nA | x [1]"),
        (["sed", "/=====/d"], "head\nA | x [1]\nB | y [2]\nC | z"),
        (
            ["sed", "-E", "s/([A-C]) \\|/<\\1>/g"],
            "head\n===== NEW (not yet assessed)\n<A> x [1]\n<B> y [2]\n===== OLD\n<C> z",
        ),
        (["sed", "3q"], "head\n===== NEW (not yet assessed)\nA | x [1]"),
        (["sed", "-n", "/NEW/!p"], "head\nA | x [1]\nB | y [2]\n===== OLD\nC | z"),
        (
            ["sed", "s/x/[&]/"],
            "head\n===== NEW (not yet assessed)\nA | [x] [1]\nB | y [2]\n===== OLD\nC | z",
        ),
    ],
)
def test_sed_after_a_pipe(words: list[str], expected: str) -> None:
    assert apply_filter(words, SECTIONS)[0] == expected


def test_sed_that_would_change_a_file_is_refused() -> None:
    with pytest.raises(ScriptRefused, match="edit"):
        harness_script.sed(["-i", "s/a/b/", "f.txt"], None)
    with pytest.raises(ScriptRefused, match="not available"):
        apply_filter(["sed", "w out.txt"], SECTIONS)


async def test_sed_over_a_file_reads_it_through_the_job(project: Path) -> None:
    result = await run(shell(project), "sed -n 's/return //p' src/answer.py")

    assert "True" in result and "False" in result
    assert "return" not in result.split("]\n", 1)[1]


def test_grep_matching_nothing_fails_like_grep_does() -> None:
    assert apply_filter(["grep", "missing"], LINES) == ("", False)


def test_a_filter_given_a_file_says_to_drop_it() -> None:
    with pytest.raises(ScriptRefused, match="drop the file name"):
        apply_filter(["grep", "x", "file.txt"], LINES)


def test_a_long_output_keeps_its_end_where_the_verdict_is() -> None:
    output = "\n".join([*(f"test_{n} PASSED" for n in range(300)), "1 failed, 299 passed"])

    clipped = clip_middle(output, max_lines=30, max_chars=4000)

    assert clipped.startswith("test_0 PASSED")
    assert clipped.endswith("1 failed, 299 passed")
    assert "omitted from the middle" in clipped
    assert len(clipped.splitlines()) <= 31


# -- the shell tool ------------------------------------------------------


async def test_the_commonest_refused_shape_now_runs(project: Path) -> None:
    script = "print('\\n'.join(map(str, range(50))))"
    result = await run(shell(project), f'python -c "{script}" 2>&1 | head -3')

    assert result.startswith("exit 0")
    assert result.rstrip().endswith("0\n1\n2")


async def test_a_sequence_reports_each_command(project: Path) -> None:
    result = await run(
        shell(project), "ls data && echo --- && cat data/missing.json || echo 'no file'"
    )

    assert result.startswith("exit 0")
    assert "$ ls data\ncache.json\nconfig.json" in result
    assert "does not exist" in result
    assert result.rstrip().endswith("no file")
    assert "answered by the harness's own" in result


async def test_and_stops_at_the_first_failure(project: Path) -> None:
    result = await run(shell(project), "python -c 'raise SystemExit(3)' && echo after")

    assert result.startswith("exit 3")
    assert "after" not in result


async def test_a_file_test_answers_with_its_exit_code(project: Path) -> None:
    here = await run(shell(project), "test -f notes.md && echo EXISTS || echo MISSING")
    gone = await run(shell(project), "[ -f missing.md ] && echo EXISTS || echo MISSING")
    outside = await run(shell(project), "test -e /etc/passwd || echo MISSING")
    folder = await run(shell(project), "test ! -d notes.md && echo NOT-A-DIR")

    assert here.rstrip().endswith("EXISTS")
    assert gone.rstrip().endswith("MISSING")
    assert outside.rstrip().endswith("MISSING")
    assert folder.rstrip().endswith("NOT-A-DIR")


async def test_a_script_of_nothing_but_refusals_is_a_refusal(project: Path) -> None:
    result = await run(shell(project), "curl x; wget y")

    assert result.startswith("DENIED")
    assert "curl is not in harness.shell_allow" in result
    assert "wget is not in harness.shell_allow" in result


async def test_a_script_cannot_smuggle_an_escape_past_the_python_check(project: Path) -> None:
    result = await run(
        shell(project), "echo hi && python -c 'import subprocess; subprocess.run([\"git\"])'"
    )

    assert "can run another program" in result


async def test_a_heredoc_runs_as_its_script(project: Path) -> None:
    result = await run(shell(project), "python3 - <<'EOF'\nprint(6 * 7)\nEOF")

    assert result.startswith("exit 0")
    assert "42" in result


async def test_a_program_reading_stdin_gets_end_of_file_not_a_hang(project: Path) -> None:
    context = shell(project, seconds=20.0)

    result = await run(context, "python -c 'input()'")

    assert result.startswith("exit 1")
    assert "EOFError" in result


async def test_a_timeout_keeps_what_was_printed_before_it(project: Path) -> None:
    context = shell(project, seconds=3.0)
    script = "import sys, time; print('found ./a/config.json'); sys.stdout.flush(); time.sleep(30)"

    result = await run(context, f'python -c "{script}"')

    assert result.startswith("DENIED: python did not finish within 3s")
    assert "found ./a/config.json" in result


# -- find ----------------------------------------------------------------


async def test_find_outside_the_job_searches_the_job_and_says_so(project: Path) -> None:
    result = await run(
        shell(project),
        "find / -name config.json -not -path '*/node_modules/*' 2>/dev/null | head -20",
    )

    assert result.startswith("exit 0")
    assert "/ is outside this job" in result
    assert "data/config.json" in result


async def test_find_understands_type_path_and_maxdepth(project: Path) -> None:
    files = await run(shell(project), "find . -maxdepth 1 -type f")
    jsons = await run(shell(project), "find data -name '*.json' ! -name cache.json")
    dirs = await run(shell(project), "find . -type d")

    assert "notes.md" in files and "data/config.json" not in files
    listed = jsons.split("]\n", 1)[1].splitlines()
    assert listed == ["data/config.json"]
    assert "src/" in dirs and "notes.md" not in dirs


async def test_find_understands_or_and_the_prune_idiom(project: Path) -> None:
    either = await run(shell(project), "find . -name notes.md -o -name cache.json")
    pruned = await run(shell(project), "find / -path '*/data' -prune -o -name '*.json' -print")

    assert "notes.md" in either and "data/cache.json" in either
    assert "answer.py" not in either
    assert pruned.endswith("no match")


async def test_find_that_would_act_is_still_refused(project: Path) -> None:
    result = await run(shell(project), "find . -name '*.json' -delete")

    assert result.startswith("DENIED: find is not in harness.shell_allow")
    assert (project / "data" / "config.json").exists()


# -- edit and write ------------------------------------------------------


def editing(root: Path) -> ToolContext:
    return ToolContext(root=root, allow_write=True)


async def edit(context: ToolContext, old: str, new: str) -> str:
    return await ToolRegistry(WRITE_TOOLS).invoke(
        context, "edit", {"path": "src/answer.py", "old": old, "new": new}
    )


async def test_an_anchor_off_by_one_indent_level_still_lands_without_breaking_nesting(
    project: Path,
) -> None:
    # The model dropped the block's indentation in both halves of the edit.
    result = await edit(
        editing(project), "if ready:\n    return True\n", "if ready:\n    return ready\n"
    )

    assert result.startswith("edited src/answer.py (re-indented")
    assert (project / "src" / "answer.py").read_text(encoding="utf-8") == (
        "def reconsider() -> bool:\n    if ready:\n        return ready\n    return False\n"
    )


async def test_an_anchor_copied_with_read_prefixes_lands(project: Path) -> None:
    result = await edit(
        editing(project),
        "    2|     if ready:\n    3|         return True",
        "    2|     if ready:\n    3|         return 1",
    )

    assert "line-number prefixes removed" in result
    assert "        return 1\n" in (project / "src" / "answer.py").read_text(encoding="utf-8")


async def test_trailing_whitespace_and_typographic_quotes_do_not_block_an_edit(
    project: Path,
) -> None:
    path = project / "src" / "answer.py"
    path.write_text("label = 'x'  \nnote = “done”\n", encoding="utf-8")

    spaced = await edit(editing(project), "label = 'x'\n", "label = 'y'\n")
    quoted = await edit(editing(project), 'note = "done"', 'note = "ok"')

    assert spaced.startswith("edited") and quoted.startswith("edited")
    assert "folding typographic" in quoted
    assert path.read_text(encoding="utf-8") == "label = 'y'\nnote = \"ok\"\n"


async def test_a_lenient_match_in_two_places_is_still_refused(project: Path) -> None:
    path = project / "src" / "answer.py"
    path.write_text("    x = 1\n\n        x = 1\n", encoding="utf-8")

    # Not a substring anywhere (the trailing spaces), and the same line once
    # trimmed in two places.
    result = await edit(editing(project), "x = 1  ", "x = 2")

    assert result.startswith("DENIED: that text is not in")
    assert path.read_text(encoding="utf-8") == "    x = 1\n\n        x = 1\n"


async def test_a_file_this_job_read_may_be_replaced_without_the_flag(project: Path) -> None:
    context = editing(project)
    registry = ToolRegistry(READ_ONLY_TOOLS + WRITE_TOOLS)
    blind = await registry.invoke(context, "write", {"path": "notes.md", "content": "a\n"})
    await registry.invoke(context, "read", {"path": "notes.md"})
    seen = await registry.invoke(context, "write", {"path": "notes.md", "content": "b\n"})

    assert blind.startswith("DENIED: notes.md already exists")
    assert seen.startswith("replaced notes.md")
    assert (project / "notes.md").read_text(encoding="utf-8") == "b\n"


# -- the config.json the NewsAnalyzer never had --------------------------


def test_a_missing_agent_config_is_copied_whole(tmp_path: Path) -> None:
    bundled = tmp_path / "template" / "config.json"
    bundled.parent.mkdir()
    bundled.write_text(json.dumps({"min_confidence": "medium"}), encoding="utf-8")
    playground = tmp_path / "playground"
    playground.mkdir()

    added = _merge_new_config_keys(bundled, playground / "config.json")
    again = _merge_new_config_keys(bundled, playground / "config.json")

    assert added == ["config.json"]
    assert again == []
    assert json.loads((playground / "config.json").read_text(encoding="utf-8")) == {
        "min_confidence": "medium"
    }


def test_no_playground_means_no_config_is_invented(tmp_path: Path) -> None:
    bundled = tmp_path / "config.json"
    bundled.write_text("{}", encoding="utf-8")

    assert _merge_new_config_keys(bundled, tmp_path / "missing" / "config.json") == []

