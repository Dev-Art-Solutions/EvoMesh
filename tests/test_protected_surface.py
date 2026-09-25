"""A candidate cannot pass by rewriting its own oracle (closure plan v2 18.4,
T57): changes to the admission, verification and acceptance surface fail
validation and need a human's review."""

from __future__ import annotations

from pathlib import Path

from evomesh.codebase import protected_changes
from evomesh.evolution import CandidateValidator
from tests.test_cycles import git_project


def test_the_protected_surface_is_matched_by_path() -> None:
    changed = [
        "src/evomesh/improvements.py",
        "src/evomesh/busy.py",
        "tests/test_procedure_crash.py",
        "tests/test_busy.py",
        "procedures/admissions.json",
        "docs/evolution/improvements.md",
    ]

    assert protected_changes(changed) == [
        "procedures/admissions.json",
        "src/evomesh/improvements.py",
        "tests/test_procedure_crash.py",
    ]


async def _candidate(tmp_path: Path) -> Path:
    root = tmp_path / "candidate"
    (root / "src" / "evomesh").mkdir(parents=True)
    (root / "src" / "evomesh" / "__init__.py").write_text('"""P."""\n', encoding="utf-8")
    (root / "src" / "evomesh" / "improvements.py").write_text("RULE = 1\n", encoding="utf-8")
    return await git_project(root)


async def test_editing_the_verification_rules_fails_validation(tmp_path: Path) -> None:
    candidate = await _candidate(tmp_path)
    (candidate / "src" / "evomesh" / "improvements.py").write_text("RULE = 0\n", encoding="utf-8")

    failure = await CandidateValidator._protected_failure(candidate)  # pyright: ignore[reportPrivateUsage]

    assert failure is not None
    assert "src/evomesh/improvements.py" in str(failure["output"])


async def test_adding_a_protected_test_file_fails_validation(tmp_path: Path) -> None:
    candidate = await _candidate(tmp_path)
    (candidate / "tests").mkdir()
    oracle = candidate / "tests" / "test_procedures.py"
    oracle.write_text("def test_ok(): pass\n", encoding="utf-8")

    failure = await CandidateValidator._protected_failure(candidate)  # pyright: ignore[reportPrivateUsage]

    assert failure is not None and "tests/test_procedures.py" in str(failure["output"])


async def test_ordinary_code_changes_are_not_protected(tmp_path: Path) -> None:
    candidate = await _candidate(tmp_path)
    (candidate / "src" / "evomesh" / "busy.py").write_text("X = 1\n", encoding="utf-8")

    assert await CandidateValidator._protected_failure(candidate) is None  # pyright: ignore[reportPrivateUsage]
