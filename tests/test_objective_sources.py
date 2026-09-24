"""Where the Evolver's objectives come from, besides the standing goal's text.

Found 2026-09-24: the only concrete sources were maintenance (wire a dead
module, test an untested export), and the untested one never drained, so ~30
generations straight landed one more small test and nothing else. These cover
the fix to that source and the two substantive ones checked before it.
"""

from __future__ import annotations

import os
from pathlib import Path

from evomesh.codebase import (
    IMPROVEMENTS_FILE,
    RUNTIME_LOG,
    Improvement,
    done_improvements,
    drop_improvements,
    improvement_objective,
    open_improvements,
    recurring_warnings,
    runtime_fault_objective,
    runtime_faults,
    scout_objective,
    tick_improvement,
    untested_target,
    vet_new_improvements,
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
        "- [ ] Imaginary function\n"
        "    In src/evomesh/busy.py, `warm_cache()` does the wrong thing on every single cycle\n"
        "    of the mesh.\n"
        "- [ ] Thin\n"
        "    src/evomesh/busy.py is bad.\n"
        "- [ ] No file at all\n"
        "    Something somewhere does the wrong thing on every single cycle of the mesh, and\n"
        "    it should not.\n",
    )

    kept, dropped = vet_new_improvements([], tmp_path)

    assert kept == []
    reasons = {item.title: reason for item, reason in dropped}
    assert "Old item" not in reasons  # a repeat of done work is skipped silently
    assert "nowhere" in reasons["Imaginary file"]
    assert "warm_cache" in reasons["Imaginary function"]
    assert "too short" in reasons["Thin"]
    assert "names no" in reasons["No file at all"]


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
    text = scout_objective(tmp_path)
    assert text.startswith("Refill the improvement backlog")
    assert "3x Watcher timed out: N" in text
    assert "Back off Telegram polling" in text
