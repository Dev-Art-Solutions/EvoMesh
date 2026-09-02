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

from evomesh.codebase import known_dead, new_orphans, orphans, project_map, survey

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
