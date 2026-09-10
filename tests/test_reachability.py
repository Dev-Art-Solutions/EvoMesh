"""Every module the package gains has to be reachable from code that runs.

This is the check that was missing while the Evolver produced ten modules and
431 lines nothing imports. ruff, pyright, pytest and the smoke check all pass
happily on dead code, so a candidate that added a file nobody calls looked
exactly like a candidate that improved the mesh.

It is a ratchet, not a cleanup order: the modules that were already unreachable
are listed in docs/evolution/known-dead-modules.txt and tolerated. Anything that
becomes unreachable from now on fails validation, which sends the candidate into
the repair stage where the model is told to wire it into a module that runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evomesh.codebase import (
    fabricated_references,
    known_dead,
    new_orphans,
    orphans,
    project_map,
    stray_root_files,
    survey,
)

PROJECT = Path(__file__).resolve().parent.parent


def test_no_module_is_added_that_nothing_imports() -> None:
    unreachable = new_orphans(PROJECT)

    assert not unreachable, (
        "these modules are imported by nothing and run by nothing, so none of "
        "their code executes: "
        + ", ".join(f"src/evomesh/{item.name}.py" for item in unreachable)
        + ". Edit a module that already runs so it imports and uses this code, "
        "or delete the file. Adding it to "
        "docs/evolution/known-dead-modules.txt is not a fix."
    )


def test_the_baseline_only_ever_shrinks() -> None:
    """A name that is no longer an orphan may stay listed; a new one may not.

    Keeping the assertion this way round is what lets a one-file mutation wire a
    dead module up: it edits the importer, the module stops being an orphan, and
    the stale line here costs nothing until someone prunes it.
    """
    listed = known_dead(PROJECT)
    actual = {item.name for item in orphans(PROJECT)}

    assert actual <= listed, f"unlisted dead modules: {sorted(actual - listed)}"


def test_the_survey_sees_the_package_it_is_pointed_at() -> None:
    modules = {item.name: item for item in survey(PROJECT)}

    assert "environment" in modules
    # contracts is the most-depended-on module in the package; if the import
    # graph ever reports it as unused, the parser has broken, not the code.
    assert modules["contracts"].imported_by
    assert not modules["contracts"].is_orphan
    assert modules["__main__"].is_entry_point
    assert not modules["__main__"].is_orphan


def test_the_map_names_what_is_load_bearing_and_what_is_dead() -> None:
    text = project_map(PROJECT)

    assert "contracts.py" in text
    assert "DEAD modules" in text
    # The map is prompt text for a small local model, so its size is a contract.
    assert len(text) <= 1800


def test_the_map_quotes_a_dead_modules_real_exports(tmp_path: Path) -> None:
    """A dead module's real names have to be in the prompt, not just its name
    and line count -- otherwise a plan drafted from this map has nothing to
    stop it from inventing a plausible-sounding one instead."""
    package = tmp_path / "src" / "evomesh"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""Package."""\n', encoding="utf-8")
    (package / "cycles.py").write_text(
        '"""Cycle detection."""\n\ndef cycle_agents(x):\n    return x\n\n\n'
        "class Detector:\n    pass\n",
        encoding="utf-8",
    )

    text = project_map(tmp_path)

    assert "cycle_agents()" in text
    assert "Detector" in text


def test_a_module_nobody_imports_is_reported(tmp_path: Path) -> None:
    package = tmp_path / "src" / "evomesh"
    package.mkdir(parents=True)
    # __init__ is an entry point, so importing user from it is what anchors the
    # chain -- exactly how the real package keeps its modules reachable.
    (package / "__init__.py").write_text(
        '"""Package."""\n\nfrom evomesh.user import thing\n', encoding="utf-8"
    )
    (package / "used.py").write_text('"""Used."""\n', encoding="utf-8")
    (package / "user.py").write_text(
        '"""User."""\n\nfrom evomesh.used import thing\n', encoding="utf-8"
    )
    (package / "lonely.py").write_text('"""Nobody calls this."""\n', encoding="utf-8")

    found = {item.name for item in orphans(tmp_path)}

    assert found == {"lonely"}


def test_no_stray_file_sits_in_this_repository_root() -> None:
    """A dozen of exactly this kind of file accumulated here before this check
    existed -- ``new_orphans`` only ever surveys ``src/evomesh/*.py``, so a
    generation that could not invent a dead module there wrote a debugging
    script at the root instead, and nothing failed it for that."""
    assert not stray_root_files(PROJECT)


def test_a_root_level_script_is_reported(tmp_path: Path) -> None:
    (tmp_path / "scratch.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "src").mkdir()

    assert stray_root_files(tmp_path) == ["scratch.py"]


def test_a_non_python_stray_file_is_also_reported(tmp_path: Path) -> None:
    """Found live: a single evaluate job wrote eleven scratch files at the
    candidate root in one turn, most of them ``.txt`` -- the ``*.py``-only
    check missed every one that wasn't a script."""
    (tmp_path / "inspection_out.txt").write_text("scratch\n", encoding="utf-8")
    (tmp_path / "tiny.txt").write_text("x\n", encoding="utf-8")

    assert stray_root_files(tmp_path) == ["inspection_out.txt", "tiny.txt"]


def test_known_root_files_are_never_flagged(tmp_path: Path) -> None:
    for name in ("README.md", "pyproject.toml", ".gitignore", "uv.lock"):
        (tmp_path / name).write_text("", encoding="utf-8")

    assert stray_root_files(tmp_path) == []


def test_a_candidates_own_scaffolding_is_never_flagged(tmp_path: Path) -> None:
    """Found live: every validation from the day this check stopped being
    *.py-only failed hygiene, because a candidate's root is never quite the
    project's -- `git worktree add` leaves `.git` as a plain file here (not a
    directory), and the pipeline itself writes MUTATION_OBJECTIVE.md before
    authoring anything and validation-result.json after every validation run.
    None of the three is something a model wrote."""
    (tmp_path / ".git").write_text(
        "gitdir: ../../.git/worktrees/000340-candidate\n", encoding="utf-8"
    )
    (tmp_path / "MUTATION_OBJECTIVE.md").write_text("objective\n", encoding="utf-8")
    (tmp_path / "validation-result.json").write_text("{}", encoding="utf-8")

    assert stray_root_files(tmp_path) == []


def test_both_import_spellings_count_as_use(tmp_path: Path) -> None:
    """``from evomesh import x`` keeps a module alive just as ``evomesh.x`` does.

    Missing either spelling would report a module that is plainly used as dead,
    and the repair loop would then be sent to fix something that is not broken.
    """
    package = tmp_path / "src" / "evomesh"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from evomesh import user\n", encoding="utf-8")
    (package / "dotted.py").write_text("", encoding="utf-8")
    (package / "bare.py").write_text("", encoding="utf-8")
    (package / "plain.py").write_text("", encoding="utf-8")
    (package / "user.py").write_text(
        "from evomesh.dotted import a\nfrom evomesh import bare\nimport evomesh.plain\n",
        encoding="utf-8",
    )

    assert not {item.name for item in orphans(tmp_path)}


@pytest.fixture
def cycles_project(tmp_path: Path) -> Path:
    """A minimal project with one real dead module, for the fabrication check."""
    package = tmp_path / "src" / "evomesh"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""Package."""\n', encoding="utf-8")
    (package / "cycles.py").write_text(
        '"""Cycle detection."""\n\n'
        "def cycle_agents(dependencies):\n"
        "    def strongconnect(node):\n"
        "        pass\n"
        "    return set()\n\n\n"
        "class Detector:\n"
        "    def run(self):\n"
        "        pass\n",
        encoding="utf-8",
    )
    return tmp_path


def test_a_fabricated_name_on_a_real_module_is_reported(cycles_project: Path) -> None:
    plan = "Wire the dead `cycles.py` module in by calling `cycles.scc_find_cycles`."

    assert fabricated_references(plan, cycles_project) == ["cycles.scc_find_cycles"]


def test_a_real_top_level_name_is_not_flagged(cycles_project: Path) -> None:
    plan = "Wire `cycles.cycle_agents` into the runtime."

    assert fabricated_references(plan, cycles_project) == []


def test_a_real_nested_or_method_name_is_not_flagged(cycles_project: Path) -> None:
    """``all_names`` -- not just top-level ``exports`` -- backs this check, so
    a real nested helper or class method is never treated as fabricated just
    for not being defined at module level."""
    plan = "Reuse `cycles.strongconnect` and `cycles.run` from the detector."

    assert fabricated_references(plan, cycles_project) == []


def test_naming_the_file_itself_is_not_flagged(cycles_project: Path) -> None:
    """``cycles.py`` is how a plan almost always refers to the module by
    name -- it must never be misread as a symbol called ``py``."""
    plan = "Wire the dead `cycles.py` module into the runtime."

    assert fabricated_references(plan, cycles_project) == []


def test_an_unknown_module_name_is_never_flagged(cycles_project: Path) -> None:
    """Only a module that actually exists in the project is checked -- this is
    what keeps ``os.path`` or ``self.thing`` from ever being flagged."""
    plan = "Use `os.path.join` and `self.thing` for this."

    assert fabricated_references(plan, cycles_project) == []
