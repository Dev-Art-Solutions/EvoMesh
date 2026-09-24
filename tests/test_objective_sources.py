"""Where the Evolver's objectives come from, besides the standing goal's text.

Found 2026-09-24: the only concrete sources were maintenance (wire a dead
module, test an untested export), and the untested one never drained, so ~30
generations straight landed one more small test and nothing else. These cover
the fix to that source and the two substantive ones checked before it.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from evomesh.codebase import (
    IMPROVEMENTS_FILE,
    RUNTIME_LOG,
    Improvement,
    Step,
    append_item,
    done_improvements,
    drop_improvements,
    failure_excerpts,
    failure_locations,
    find_symbol,
    improvement_objective,
    item_from_answer,
    module_outline,
    named_code,
    open_improvements,
    outline_focus,
    plan_task,
    recurring_warnings,
    runtime_fault_objective,
    runtime_faults,
    scout_modules,
    scout_objective,
    scout_task,
    step_objective,
    step_task,
    steps_from_answer,
    symbol_excerpt,
    tick_improvement,
    tick_step,
    untested_target,
    vet_new_improvements,
    vet_plan,
    warning_leads,
    write_planned_steps,
    write_test_task,
)


def _live_package(root: Path) -> None:
    package = root / "src" / "evomesh"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        '"""Package."""\n\nfrom evomesh.busy import helper\n', encoding="utf-8"
    )
    (package / "busy.py").write_text(
        '"""Does the real work."""\n\ndef helper(value):\n    return value\n',
        encoding="utf-8",
    )


def test_an_export_called_with_arguments_counts_as_tested(tmp_path: Path) -> None:
    """The regression: the check looked for the literal ``helper()``, which a
    test calling ``helper(1)`` never contains, so no function ever left the
    backlog."""
    _live_package(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_busy.py").write_text(
        "from evomesh.busy import helper\n\n\ndef test_helper():\n    assert helper(1) == 1\n",
        encoding="utf-8",
    )

    assert untested_target(tmp_path, seed=0) is None


def test_a_name_only_inside_a_longer_word_is_still_untested(tmp_path: Path) -> None:
    _live_package(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_other.py").write_text("def test_x():\n    helpers = 1\n", encoding="utf-8")

    target = untested_target(tmp_path, seed=0)

    assert target is not None
    assert target[1] == "helper()"


def _write_backlog(root: Path, text: str) -> None:
    path = root / IMPROVEMENTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_open_improvements_reads_open_items_with_their_detail(tmp_path: Path) -> None:
    _write_backlog(
        tmp_path,
        "# Backlog\n\n"
        "- [x] Already done\n"
        "    detail of a done item\n"
        "- [ ] Make the thing faster\n"
        "    In src/evomesh/busy.py, helper() is slow.\n"
        "    Cache it.\n"
        "- [ ] Second item\n",
    )

    items = open_improvements(tmp_path)

    assert items == [
        Improvement(
            "Make the thing faster", "In src/evomesh/busy.py, helper() is slow.\nCache it."
        ),
        Improvement("Second item", ""),
    ]


def test_open_improvements_is_empty_without_a_backlog(tmp_path: Path) -> None:
    assert open_improvements(tmp_path) == []


def test_an_improvement_objective_demands_a_source_change() -> None:
    text = improvement_objective(Improvement("Make it faster", "In busy.py."))

    assert text.startswith("Implement this improvement to EvoMesh: Make it faster")
    assert "In busy.py." in text
    assert "src/evomesh/" in text


def test_tick_improvement_marks_only_the_named_item(tmp_path: Path) -> None:
    _write_backlog(tmp_path, "- [ ] First\n    why\n- [ ] Second\n")

    assert tick_improvement(tmp_path, "Second") is True
    assert tick_improvement(tmp_path, "Missing") is False

    assert [item.title for item in open_improvements(tmp_path)] == ["First"]
    assert "- [x] Second" in (tmp_path / IMPROVEMENTS_FILE).read_text(encoding="utf-8")


def _traceback_entry(time: str, module: str = "control", function: str = "_handle_client") -> str:
    return (
        f'{{"time":"{time}","level":"ERROR","message":"Unhandled exception"}}\n'
        "Traceback (most recent call last):\n"
        f'  File "C:\\x\\src\\evomesh\\{module}.py", line 52, in {function}\n'
        "    await writer.wait_closed()\n"
        "OSError: [WinError 64] The specified network name is no longer available\n"
    )


def _write_log(root: Path, *entries: str) -> None:
    path = root / RUNTIME_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    info = '{"time":"2026-09-21 03:03:08,000","level":"INFO","message":"fine"}\n'
    path.write_text(info.join(entries) + info, encoding="utf-8")


def _source(root: Path, module: str = "control", *, modified: float = 0.0) -> None:
    package = root / "src" / "evomesh"
    package.mkdir(parents=True, exist_ok=True)
    path = package / f"{module}.py"
    path.write_text('"""Module."""\n', encoding="utf-8")
    os.utime(path, (modified, modified))


def test_a_repeated_traceback_becomes_a_runtime_fault(tmp_path: Path) -> None:
    _source(tmp_path)
    _write_log(
        tmp_path,
        _traceback_entry("2026-09-21 03:03:07,267"),
        _traceback_entry("2026-09-21 09:43:54,089"),
    )

    faults = runtime_faults(tmp_path)

    assert len(faults) == 1
    fault = faults[0]
    assert (fault.module, fault.function) == ("control", "_handle_client")
    assert fault.exception == "OSError"
    assert fault.count == 2
    assert fault.last_seen == "2026-09-21 09:43:54,089"
    objective = runtime_fault_objective(fault)
    assert objective.startswith("Fix a real bug the running mesh hit: `OSError`")
    assert "WinError 64" in objective


def test_a_single_traceback_is_not_yet_a_fault(tmp_path: Path) -> None:
    _source(tmp_path)
    _write_log(tmp_path, _traceback_entry("2026-09-21 03:03:07,267"))

    assert runtime_faults(tmp_path) == []


def test_a_fault_in_a_file_changed_since_counts_as_fixed(tmp_path: Path) -> None:
    """A promoted generation rewrites the file, moving its mtime past every
    logged occurrence -- the fault drops out until it happens again."""
    _source(tmp_path, modified=4_000_000_000.0)
    _write_log(
        tmp_path,
        _traceback_entry("2026-09-21 03:03:07,267"),
        _traceback_entry("2026-09-21 09:43:54,089"),
    )

    assert runtime_faults(tmp_path) == []


def test_runtime_faults_without_a_log_is_empty(tmp_path: Path) -> None:
    assert runtime_faults(tmp_path) == []


REAL_ITEM = (
    "- [ ] Cache helper's result\n"
    "    In src/evomesh/busy.py, `helper()` is recomputed on every call even though its\n"
    "    input never changes between cycles; memoize it.\n"
    "    > return value\n"
    "    1. [ ] src/evomesh/busy.py `helper` -- memoize it with functools.cache\n"
)


def test_vet_keeps_an_item_that_names_real_code(tmp_path: Path) -> None:
    _live_package(tmp_path)
    _write_backlog(tmp_path, "- [x] Old\n" + REAL_ITEM)

    kept, dropped = vet_new_improvements([], tmp_path)

    assert [item.title for item in kept] == ["Cache helper's result"]
    assert dropped == []


def test_vet_drops_imaginary_files_functions_and_thin_items(tmp_path: Path) -> None:
    _live_package(tmp_path)
    _write_backlog(
        tmp_path,
        "- [x] Old item\n"
        "- [ ] Old item\n"
        "    In src/evomesh/busy.py, `helper()` again, long enough detail to pass the length\n"
        "    check on its own merits.\n"
        "- [ ] Imaginary file\n"
        "    In src/evomesh/nowhere.py, `helper()` does the wrong thing on every single cycle\n"
        "    of the mesh.\n"
        "    1. [ ] src/evomesh/nowhere.py `helper` -- make it do the right thing\n"
        "- [ ] Imaginary function\n"
        "    In src/evomesh/busy.py, `warm_cache()` does the wrong thing on every single cycle\n"
        "    of the mesh.\n"
        "    1. [ ] src/evomesh/busy.py `helper` -- stop calling warm_cache\n"
        "- [ ] Imaginary anchor\n"
        "    In src/evomesh/busy.py, the helper does the wrong thing on every single cycle\n"
        "    of the mesh.\n"
        "    1. [ ] src/evomesh/busy.py `Busy.warm` -- make it do the right thing\n"
        "- [ ] Thin\n"
        "    src/evomesh/busy.py is bad.\n"
        "- [ ] No steps\n"
        "    In src/evomesh/busy.py, `helper()` does the wrong thing on every single cycle of\n"
        "    the mesh, and it should not.\n",
    )

    kept, dropped = vet_new_improvements([], tmp_path)

    assert kept == []
    reasons = {item.title: reason for item, reason in dropped}
    assert "Old item" not in reasons  # a repeat of done work is skipped silently
    assert "nowhere" in reasons["Imaginary file"]
    assert "warm_cache" in reasons["Imaginary function"]
    assert "`Busy.warm`" in reasons["Imaginary anchor"]
    assert "too short" in reasons["Thin"]
    assert "no steps" in reasons["No steps"]


def test_vet_wants_the_code_quoted_and_the_quote_real(tmp_path: Path) -> None:
    """Found live 2026-09-24: generation 1386 described a "[?]" fallback, a
    ValueError and role sets in a function that is one `dict.get` -- with an
    anchor that existed, so only a quote copied from the file could catch it."""
    _live_package(tmp_path)
    item = REAL_ITEM.replace("Cache helper's result", "{title}")
    _write_backlog(
        tmp_path,
        item.format(title="Quoted")
        + item.format(title="Unquoted").replace("    > return value\n", "")
        + item.format(title="Misquoted").replace(
            "> return value", "> if role in {'active'}: return '[?]'"
        )
        + item.format(title="Too short to count").replace("> return value", "> value"),
    )

    kept, dropped = vet_new_improvements([], tmp_path)

    assert [entry.title for entry in kept] == ["Quoted"]
    reasons = {entry.title: reason for entry, reason in dropped}
    assert "quotes no code" in reasons["Unquoted"]
    assert "in none of its files" in reasons["Misquoted"]
    assert "quotes no code" in reasons["Too short to count"]


def test_an_item_one_space_in_and_a_backticked_path_still_parse(tmp_path: Path) -> None:
    """Generation 1385 wrote its item as ` - [ ] ...` and 1386 its steps with
    the path in backticks; both were lost to the parser, not to the vet."""
    _write_backlog(
        tmp_path,
        "- [x] Old\n"
        "    why\n"
        " - [ ] One space in\n"
        "    why\n"
        "    1. [ ] `src/evomesh/busy.py` `helper` -- change it\n"
        " - [x] Done, one space in\n"
        "    why\n",
    )

    (item,) = open_improvements(tmp_path)

    assert item.title == "One space in"
    assert item.detail == "why"
    assert [(step.path, step.symbol) for step in item.steps] == [("src/evomesh/busy.py", "helper")]
    assert done_improvements(tmp_path) == ["Old", "Done, one space in"]
    assert tick_step(tmp_path, "One space in", 1) is True
    assert open_improvements(tmp_path) == []


def test_vet_ignores_items_that_were_already_open(tmp_path: Path) -> None:
    _live_package(tmp_path)
    _write_backlog(tmp_path, REAL_ITEM)
    before = open_improvements(tmp_path)

    kept, dropped = vet_new_improvements(before, tmp_path)

    assert kept == [] and dropped == []


def test_drop_improvements_removes_only_the_named_items(tmp_path: Path) -> None:
    _write_backlog(tmp_path, "# B\n\n- [ ] Keep\n    why\n- [ ] Drop\n    because\n    more\n")

    drop_improvements(tmp_path, {"Drop"})

    text = (tmp_path / IMPROVEMENTS_FILE).read_text(encoding="utf-8")
    assert text == "# B\n\n- [ ] Keep\n    why\n"


def test_scout_objective_carries_the_logs_leads_and_the_done_list(tmp_path: Path) -> None:
    _write_backlog(tmp_path, "- [x] Back off Telegram polling\n")
    log = tmp_path / RUNTIME_LOG
    log.parent.mkdir(parents=True)
    warning = (
        '{"time":"2026-09-24 10:00:0%d,000","level":"WARNING",'
        '"message":"Watcher timed out: %d"}\n'
    )
    log.write_text("".join(warning % (i, i) for i in range(3)), encoding="utf-8")

    assert done_improvements(tmp_path) == ["Back off Telegram polling"]
    assert recurring_warnings(tmp_path) == [(3, "Watcher timed out: N")]
    text = scout_objective(tmp_path, "busy", recurring_warnings(tmp_path))
    assert text.startswith("Refill the improvement backlog from src/evomesh/busy.py")
    assert "ONE real problem" in text
    assert "3x Watcher timed out: N" in text
    assert "Back off Telegram polling" in text


# -- Work orders for a small context window -----------------------------------
# Found 2026-09-24 in the transcripts of three failed generations: the task
# alone took 6.6-7.9K of a 12000-char transcript, leaving room for about one
# read, and the model rebuilt code it had lost from memory. A backlog item is
# now split into steps anchored to one function each, and the job for a step is
# handed that function's source up front.

STEPPED = (
    "- [ ] Remember outcomes\n"
    "    Nothing counts discarded generations.\n"
    "    1. [x] src/evomesh/busy.py `helper` -- return the value doubled\n"
    "    2. [ ] src/evomesh/busy.py `Counter.bump` -- count discards too\n"
    "    3. [ ] src/evomesh/busy.py `LIMIT` -- raise it to 20\n"
    "- [ ] Plain item\n"
    "    why\n"
)

BUSY = (
    '"""Does the real work."""\n'
    "\n"
    "LIMIT = 10\n"
    "\n"
    "\n"
    "def helper(value):\n"
    "    return value\n"
    "\n"
    "\n"
    "class Counter:\n"
    '    """Counts things."""\n'
    "\n"
    "    def bump(self):\n"
    "        return 1\n"
    "\n"
    "    @property\n"
    "    def total(self):\n"
    "        return 2\n"
)


def _busy_package(root: Path) -> None:
    _live_package(root)
    (root / "src" / "evomesh" / "busy.py").write_text(BUSY, encoding="utf-8")


def test_open_improvements_reads_steps_apart_from_the_detail(tmp_path: Path) -> None:
    _write_backlog(tmp_path, STEPPED)

    first, plain = open_improvements(tmp_path)

    assert first.detail == "Nothing counts discarded generations."
    assert [(s.number, s.symbol, s.done) for s in first.steps] == [
        (1, "helper", True),
        (2, "Counter.bump", False),
        (3, "LIMIT", False),
    ]
    assert first.next_step == Step(2, "src/evomesh/busy.py", "Counter.bump", "count discards too")
    assert first.source_paths == ["src/evomesh/busy.py"]
    assert plain.steps == () and plain.next_step is None


def test_tick_step_ticks_the_item_with_its_last_step(tmp_path: Path) -> None:
    _write_backlog(tmp_path, STEPPED)

    assert tick_step(tmp_path, "Remember outcomes", 2) is True
    assert tick_step(tmp_path, "Remember outcomes", 2) is False  # already done
    assert tick_step(tmp_path, "Plain item", 1) is False  # no such step
    assert [item.title for item in open_improvements(tmp_path)] == [
        "Remember outcomes",
        "Plain item",
    ]

    assert tick_step(tmp_path, "Remember outcomes", 3) is True

    text = (tmp_path / IMPROVEMENTS_FILE).read_text(encoding="utf-8")
    assert "- [x] Remember outcomes" in text
    assert "    3. [x] src/evomesh/busy.py `LIMIT`" in text
    assert [item.title for item in open_improvements(tmp_path)] == ["Plain item"]


def test_find_symbol_knows_functions_methods_and_constants(tmp_path: Path) -> None:
    _busy_package(tmp_path)
    path = "src/evomesh/busy.py"

    assert find_symbol(tmp_path, path, "helper") == (6, 7)
    assert find_symbol(tmp_path, path, "Counter.bump") == (13, 14)
    assert find_symbol(tmp_path, path, "Counter.total") == (16, 18)  # decorator included
    assert find_symbol(tmp_path, path, "LIMIT") == (3, 3)
    assert find_symbol(tmp_path, path, "Counter.missing") is None
    assert find_symbol(tmp_path, path, "helper.inner") is None
    assert find_symbol(tmp_path, "src/evomesh/nowhere.py", "helper") is None


def test_an_excerpt_is_numbered_like_a_read(tmp_path: Path) -> None:
    _busy_package(tmp_path)

    excerpt = symbol_excerpt(tmp_path, "src/evomesh/busy.py", "Counter.bump")

    assert excerpt == "   13|     def bump(self):\n   14|         return 1"


def test_a_long_function_is_cut_at_a_line_and_says_where_the_rest_is(tmp_path: Path) -> None:
    _live_package(tmp_path)
    body = "".join(f"    total = {n}  # padding padding padding\n" for n in range(200))
    (tmp_path / "src" / "evomesh" / "busy.py").write_text(
        f"def helper(value):\n{body}    return value\n", encoding="utf-8"
    )

    excerpt = symbol_excerpt(tmp_path, "src/evomesh/busy.py", "helper", budget=600)

    assert excerpt is not None
    shown, note = excerpt.rsplit("\n", 1)
    assert len(shown) <= 600
    resume = len(shown.splitlines()) + 1
    assert note == (
        f"[... lines {resume}-202 not shown: read offset={resume} limit={203 - resume} "
        "for them ...]"
    )


def test_a_class_too_long_to_show_is_shown_as_its_outline(tmp_path: Path) -> None:
    _live_package(tmp_path)
    methods = "".join(
        f"    def method_{n}(self):\n" + "".join(f"        x = {i}\n" for i in range(20))
        for n in range(10)
    )
    (tmp_path / "src" / "evomesh" / "busy.py").write_text(
        f'class Big:\n    """Big."""\n\n{methods}', encoding="utf-8"
    )

    excerpt = symbol_excerpt(tmp_path, "src/evomesh/busy.py", "Big", budget=1200)

    assert excerpt is not None
    assert "    1| class Big:" in excerpt
    assert "|     def method_9(self):" in excerpt
    assert "x = 19" not in excerpt
    assert "too long to show whole" in excerpt


def test_module_outline_lists_members_while_they_fit(tmp_path: Path) -> None:
    _busy_package(tmp_path)
    path = "src/evomesh/busy.py"

    full = module_outline(tmp_path, path)
    short = module_outline(tmp_path, path, budget=80)

    assert full == (
        "    6| def helper(value):\n"
        "   10| class Counter:  (2 methods, lines 10-18)\n"
        "   13|     def bump(self):\n"
        "   17|     def total(self):"
    )
    assert short == "    6| def helper(value):\n   10| class Counter:  (2 methods, lines 10-18)"


def test_an_outline_cut_short_keeps_what_the_item_is_about(tmp_path: Path) -> None:
    """Found on the live tree: cut from the top, evolution.py's outline ended
    150 lines before the class its item named."""
    _live_package(tmp_path)
    fillers = "".join(f"def filler_{n}(value):\n    return value\n\n\n" for n in range(40))
    methods = "".join(f"    def method_{n}(self):\n        pass\n\n" for n in range(30))
    (tmp_path / "src" / "evomesh" / "busy.py").write_text(
        f"{fillers}class Supervisor:\n{methods}    def discard(self):\n        pass\n",
        encoding="utf-8",
    )
    item = Improvement("Count discarded generations", "In `Supervisor`, nothing counts.")

    outline = module_outline(
        tmp_path, "src/evomesh/busy.py", budget=1400, focus=outline_focus(item)
    )

    assert outline is not None
    assert "| class Supervisor:" in outline
    assert "|     def discard(self):" in outline  # the whole named class
    assert "|     def method_29(self):" in outline
    assert "more definitions" in outline  # the fillers made room
    lines = [int(row.split("|")[0]) for row in outline.splitlines()[:-1]]
    assert lines == sorted(lines)


def test_a_step_task_carries_the_code_and_not_the_whole_project(tmp_path: Path) -> None:
    _busy_package(tmp_path)
    _write_backlog(tmp_path, STEPPED)
    item = open_improvements(tmp_path)[0]
    step = item.next_step
    assert step is not None

    objective = step_objective(item, step)
    task = step_task(tmp_path, objective, step.path, step.symbol)

    assert objective.startswith(
        "Implement this improvement to EvoMesh: Remember outcomes [step 2]"
    )
    assert "THIS STEP: in src/evomesh/busy.py, `Counter.bump`: count discards too" in objective
    assert "Already landed: 1. src/evomesh/busy.py `helper`" in objective
    assert "Later steps, NOT this one: 3. src/evomesh/busy.py `LIMIT`" in objective
    assert (
        "CURRENT CODE -- src/evomesh/busy.py, `Counter.bump`:\n   13|     def bump(self):"
        in task
    )
    assert "THE PACKAGE AS IT STANDS" not in task
    # Everything but the code: small enough to leave a 12000-char transcript
    # most of its room.
    assert len(task) < 3000


def test_a_plan_task_carries_the_outline_and_asks_for_steps_in_the_answer(
    tmp_path: Path,
) -> None:
    _busy_package(tmp_path)
    _write_backlog(tmp_path, "- [ ] Count\n    In src/evomesh/busy.py nothing counts.\n")

    task = plan_task(tmp_path, "Plan this improvement to EvoMesh: Count", "Count")

    assert "OUTLINE -- src/evomesh/busy.py" in task
    assert "   13|     def bump(self):" in task
    assert "END YOUR ANSWER with the steps" in task
    assert "1. src/evomesh/<module>.py `<Name or Class.method>`" in task
    assert "edit" not in task.replace("edit anything", "")


def test_a_scout_task_carries_the_outline_and_asks_for_the_item_in_the_answer(
    tmp_path: Path,
) -> None:
    _busy_package(tmp_path)
    _write_backlog(tmp_path, "# B\n\n- [x] Old\n    why\n")

    task = scout_task(tmp_path, scout_objective(tmp_path, "busy"), "busy")

    assert "OUTLINE -- src/evomesh/busy.py" in task
    assert "END YOUR ANSWER with the item" in task
    assert "ENDS WITH" not in task


def test_steps_are_read_out_of_an_answer_however_they_are_dressed() -> None:
    answer = (
        "I looked at both.\n"
        "```\n"
        "1. src/evomesh/busy.py `Counter.bump` -- count discards too\n"
        "- 2. [ ] `src/evomesh/busy.py` `LIMIT`: raise it to 20\n"
        "3) src/evomesh/busy.py `helper` — double it\n"
        "```\n"
        "RATIONALE: 1. src/evomesh/busy.py `helper` -- not a step\n"
    )

    steps = steps_from_answer(answer)

    assert [(s.number, s.symbol, s.change) for s in steps] == [
        (1, "Counter.bump", "count discards too"),
        (2, "LIMIT", "raise it to 20"),
        (3, "helper", "double it"),
    ]


def test_an_item_is_read_out_of_a_scouts_answer() -> None:
    answer = (
        "Found one.\n"
        "- [ ] **Stop swallowing the cause**\n"
        "`helper` hides the error.\n"
        "> return value\n"
        "1. src/evomesh/busy.py `helper` -- raise instead\n"
        "Some closing remark.\n"
        "- [ ] A second item nobody asked for\n"
    )

    item = item_from_answer(answer)

    assert item is not None
    assert item.title == "Stop swallowing the cause"
    assert item.detail == "`helper` hides the error.\n> return value"
    assert [step.symbol for step in item.steps] == ["helper"]
    assert item_from_answer("no item here") is None


def test_written_steps_and_items_read_back_as_the_same_backlog(tmp_path: Path) -> None:
    _write_backlog(tmp_path, "# B\n\n- [ ] Count\n    why it matters\n- [x] Old\n    done\n")

    written = write_planned_steps(
        tmp_path, "Count", steps_from_answer("1. src/evomesh/busy.py `helper` -- count")
    )
    appended = append_item(
        tmp_path,
        Improvement(
            "New one", "> return value", (Step(1, "src/evomesh/busy.py", "LIMIT", "raise"),)
        ),
    )

    assert written == "    1. [ ] src/evomesh/busy.py `helper` -- count\n"
    assert appended.startswith("- [ ] New one\n    > return value\n")
    count, new = open_improvements(tmp_path)
    assert count.detail == "why it matters"
    assert [step.symbol for step in count.steps] == ["helper"]
    assert new.detail == "> return value"
    assert [step.symbol for step in new.steps] == ["LIMIT"]
    assert done_improvements(tmp_path) == ["Old"]
    assert write_planned_steps(tmp_path, "Missing", list(count.steps)) is None


def test_vet_plan_wants_real_anchors_and_nothing_else_touched(tmp_path: Path) -> None:
    _busy_package(tmp_path)
    before_text = "- [ ] Count\n    In src/evomesh/busy.py nothing counts.\n- [ ] Other\n"
    _write_backlog(tmp_path, before_text)
    before = open_improvements(tmp_path)

    assert "no step" in str(vet_plan(before, tmp_path, "Count"))

    step = "counts.\n    1. [ ] src/evomesh/busy.py `Counter.bump` -- count\n"
    _write_backlog(tmp_path, before_text.replace("counts.\n", step))
    assert vet_plan(before, tmp_path, "Count") is None

    _write_backlog(tmp_path, before_text.replace("counts.\n", step.replace("bump", "count")))
    assert "`Counter.count`" in str(vet_plan(before, tmp_path, "Count"))

    _write_backlog(tmp_path, "- [ ] Count\n    1. [ ] src/evomesh/busy.py `helper` -- x\n")
    assert "added, removed or renamed" in str(vet_plan(before, tmp_path, "Count"))


def test_warning_leads_point_at_the_module_and_forget_what_it_fixed(tmp_path: Path) -> None:
    _busy_package(tmp_path)
    busy = tmp_path / "src" / "evomesh" / "busy.py"
    busy.write_text(
        BUSY + '\nlogger.warning("Watcher timed out waiting for %s", name)\n', encoding="utf-8"
    )
    log = tmp_path / RUNTIME_LOG
    log.parent.mkdir(parents=True)
    line = (
        '{"time":"2026-09-24 10:00:0%d,000","level":"WARNING",'
        '"message":"Watcher timed out waiting for job %d"}\n'
    )
    log.write_text("".join(line % (i, i) for i in range(3)), encoding="utf-8")
    old = datetime(2026, 9, 24, 9, 0).timestamp()
    os.utime(busy, (old, old))

    leads = warning_leads(tmp_path)

    assert leads == {"busy": [(3, "Watcher timed out waiting for job N")]}
    assert scout_modules(tmp_path, leads)[0] == "busy"
    # The file changed after every one of those lines: nothing left to point at.
    new = datetime(2026, 9, 24, 11, 0).timestamp()
    os.utime(busy, (new, new))
    assert warning_leads(tmp_path) == {}


# -- work orders for test-writing and repair jobs -------------------------------


def test_failure_locations_read_ruff_pyright_and_pytest_output(tmp_path: Path) -> None:
    """The three shapes validation prints, pyright's and pytest's as absolute
    Windows paths with spaces in them. Only files really under the root count."""
    _busy_package(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_busy.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")
    output = (
        "F821 Undefined name `os`\n  --> src\\evomesh\\busy.py:7:5\n"
        "  d:\\Projects\\Dev-art solutions\\EvoMesh\\generations\\001348-candidate\\tests\\"
        "test_busy.py:2:17 - error: nope\n"
        "D:\\Projects\\Dev-art solutions\\x\\tests\\test_busy.py:2: AssertionError\n"
        "src/evomesh/nowhere.py:3: gone\n"
    )

    assert failure_locations(tmp_path, output) == [
        ("src/evomesh/busy.py", 7),
        ("tests/test_busy.py", 2),
    ]


def test_failure_excerpts_show_the_code_around_each_location(tmp_path: Path) -> None:
    _busy_package(tmp_path)

    code = failure_excerpts(tmp_path, "--> src/evomesh/busy.py:14:9")

    assert code.startswith("CODE AT THE FAILURE -- src/evomesh/busy.py, around line 14:")
    assert "   13|     def bump(self):" in code
    assert "    2| " in code  # FAILURE_WINDOW lines either side, clamped to the file


def test_named_code_finds_what_a_review_points_at(tmp_path: Path) -> None:
    _busy_package(tmp_path)
    review = "the change never calls it -- see `Counter.bump` in src/evomesh/busy.py"

    code = named_code(tmp_path, review)

    assert code.startswith("CODE THE REVIEW NAMES -- src/evomesh/busy.py, `Counter.bump`:")
    assert "   14|         return 1" in code
    assert named_code(tmp_path, "nothing named here") == ""


def test_a_test_work_order_carries_the_code_and_the_test_files_edges(tmp_path: Path) -> None:
    _busy_package(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_busy.py").write_text(
        "from evomesh.busy import LIMIT\n\n\ndef test_limit():\n    assert LIMIT == 10\n",
        encoding="utf-8",
    )

    task = write_test_task(
        tmp_path, "Write ONE small test", "src/evomesh/busy.py", "helper", "tests/test_busy.py"
    )
    fresh = write_test_task(
        tmp_path, "Write ONE small test", "src/evomesh/busy.py", "helper", "tests/test_new.py"
    )

    assert "CODE UNDER TEST -- src/evomesh/busy.py, `helper`:\n    6| def helper(value):" in task
    assert "Its imports:\n    1| from evomesh.busy import LIMIT" in task
    assert "It ENDS WITH:\n    3| \n    4| def test_limit():\n    5|     assert LIMIT == 10" in task
    assert "Never change anything under src/evomesh/" in task
    assert "tests/test_new.py does not exist yet: create it with write." in fresh
    assert len(task) < 3000
