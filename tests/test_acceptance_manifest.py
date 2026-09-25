"""The acceptance manifest cannot claim what was not shown (closure plan v2
25.3): every matrix row maps to tests that exist, and an accepted manifest
with missing, failing or foreign evidence is rejected."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from benchmarks.closure.acceptance import BENCHMARKS, GATES, MATRIX, validate_manifest

ROOT = Path(__file__).resolve().parents[1]


def test_every_matrix_row_and_benchmark_is_mapped() -> None:
    assert sorted(MATRIX) == [f"T{n:02d}" for n in range(1, 61)]
    assert sorted(BENCHMARKS, key=lambda key: int(key[1:])) == [f"B{n}" for n in range(1, 13)]
    assert sorted(GATES) == [f"CG{n}" for n in range(1, 9)]
    for spec in BENCHMARKS.values():
        assert set(spec["tests"]) <= set(MATRIX)
    for spec in GATES.values():
        assert set(spec.get("tests", [])) <= set(MATRIX)
        assert set(spec.get("benchmarks", [])) <= set(BENCHMARKS)


def test_every_mapped_test_exists() -> None:
    nodes = {node for nodes in MATRIX.values() for node in nodes}
    nodes |= {node for spec in [*BENCHMARKS.values(), *GATES.values()]
              for node in spec.get("extra", [])}
    missing = []
    for node in sorted(nodes):
        path, name = node.split("::")
        source = (ROOT / path).read_text(encoding="utf-8")
        if not re.search(rf"^(async )?def {re.escape(name)}\(", source, flags=re.M):
            missing.append(node)
    assert missing == []


def _accepted() -> dict[str, Any]:
    return {
        "plan": "EVOMESH_ARCHITECTURE_CLOSURE_PLAN_V2",
        "tested_commit": "abc1234",
        "working_tree_clean": True,
        "status": "ARCHITECTURE_ACCEPTED_LOCAL_VALIDATION_PENDING",
        "core_gates": {key: "passed" for key in GATES},
        "local_model_validation": {"status": "not_run"},
        "blocking_findings": [],
        "evidence_index": [{"id": "T01", "result": "passed", "path": "x"}],
    }


def test_a_well_formed_acceptance_passes() -> None:
    assert validate_manifest(_accepted(), "abc1234def") == []


def test_accepted_manifests_are_rejected_for_each_missing_piece() -> None:
    cases = {
        "a failed gate": ("core_gates", {**{k: "passed" for k in GATES}, "CG3": "failed"}),
        "an unknown gate": ("core_gates", {**{k: "passed" for k in GATES}, "CG9": "passed"}),
        "no evidence": ("evidence_index", []),
        "failing evidence": ("evidence_index", [{"id": "T01", "result": "failed"}]),
        "a blocking finding": ("blocking_findings", ["CG4 missing_evidence"]),
        "a dirty tree": ("working_tree_clean", False),
    }
    for reason, (field, value) in cases.items():
        manifest = copy.deepcopy(_accepted())
        manifest[field] = value
        assert validate_manifest(manifest, "abc1234def"), reason


def test_another_revision_or_an_unearned_local_claim_is_rejected() -> None:
    assert validate_manifest(_accepted(), "fff9999")
    claimed = {**_accepted(), "status": "ARCHITECTURE_ACCEPTED_AND_LOCALLY_VALIDATED"}
    assert validate_manifest(claimed, "abc1234def")


def test_not_accepted_needs_no_evidence_to_be_honest() -> None:
    assert validate_manifest({"status": "NOT_ACCEPTED"}) == []
    assert validate_manifest({"status": "DONE"}) == ["unknown status 'DONE'"]
