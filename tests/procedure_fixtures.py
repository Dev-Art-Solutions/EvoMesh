"""Contract fixtures from the architecture closure plan (sections 7, 8, 14),
translated to the normative JSON-compatible form."""

from __future__ import annotations

import copy
from typing import Any


def ref(scope: str, *path: str | int) -> dict[str, Any]:
    return {"ref": {"scope": scope, "path": list(path)}}


LOCAL_JSON_SNAPSHOT: dict[str, Any] = {
    "schema_version": 1,
    "procedure_id": "local_json_snapshot",
    "revision": 1,
    "name": "Create a validated local JSON snapshot",
    "entry_step_id": "read_source",
    "goal_kind": "local_json_snapshot",
    "parameter_schema": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "maxLength": 500},
            "destination": {"type": "string", "maxLength": 500},
        },
        "required": ["source", "destination"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string", "maxLength": 500},
            "digest": {"type": "string", "maxLength": 100},
        },
        "required": ["artifact_id", "digest"],
        "additionalProperties": False,
    },
    "required_capabilities": ["artifact.read", "artifact.write"],
    "preconditions": [],
    "context_dependencies": [],
    "steps": [
        {
            "id": "read_source",
            "kind": "tool",
            "adapter": "core.json_read",
            "contract_version": 1,
            "arguments": {"path": ref("goal", "parameters", "source")},
            "next": "write_snapshot",
        },
        {
            "id": "write_snapshot",
            "kind": "tool",
            "adapter": "core.json_write",
            "contract_version": 1,
            "arguments": {
                "path": ref("goal", "parameters", "destination"),
                "value": ref("result", "read_source", "value"),
            },
            "next": "check_snapshot",
        },
        {
            "id": "check_snapshot",
            "kind": "validate",
            "check": "artifact_matches_source",
            "arguments": {
                "artifact_id": ref("result", "write_snapshot", "artifact_id"),
                "source_step": {"literal": "read_source"},
            },
            "next": "done",
        },
        {
            "id": "done",
            "kind": "complete",
            "result": {
                "artifact_id": ref("result", "write_snapshot", "artifact_id"),
                "digest": ref("result", "write_snapshot", "digest"),
            },
        },
    ],
}


def snapshot(**changes: Any) -> dict[str, Any]:
    definition = copy.deepcopy(LOCAL_JSON_SNAPSHOT)
    definition.update(changes)
    return definition


BRANCHING: dict[str, Any] = {
    "schema_version": 1,
    "procedure_id": "branch_fixture",
    "revision": 1,
    "name": "Branch on a read value",
    "entry_step_id": "read",
    "goal_kind": "branch_fixture",
    "parameter_schema": {
        "type": "object",
        "properties": {"source": {"type": "string", "maxLength": 500}},
        "required": ["source"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {"route": {"type": "string", "maxLength": 20}},
        "required": ["route"],
        "additionalProperties": False,
    },
    "required_capabilities": ["artifact.read"],
    "steps": [
        {
            "id": "read",
            "kind": "tool",
            "adapter": "core.json_read",
            "contract_version": 1,
            "arguments": {"path": ref("goal", "parameters", "source")},
            "next": "route",
        },
        {
            "id": "route",
            "kind": "branch",
            "predicate": {
                "op": "eq",
                "left": ref("result", "read", "value", "status"),
                "right": "healthy",
            },
            "then": "healthy",
            "else": "unhealthy",
        },
        {"id": "healthy", "kind": "complete", "result": {"route": {"literal": "healthy"}}},
        {"id": "unhealthy", "kind": "complete", "result": {"route": {"literal": "unhealthy"}}},
    ],
}


# W2 (plan 19.2): two structured reports, exactly one schema-bound model
# call on the clean path, and a published artifact the runtime checks equals
# the validated output.
REPORT_COMPARISON: dict[str, Any] = {
    "schema_version": 1,
    "procedure_id": "report_comparison",
    "revision": 1,
    "name": "Compare two structured reports",
    "description": "Model-generated comparison of two reports; every cited id is checked.",
    "entry_step_id": "read_first",
    "goal_kind": "report_comparison",
    "parameter_schema": {
        "type": "object",
        "properties": {
            "first": {"type": "string", "maxLength": 500},
            "second": {"type": "string", "maxLength": 500},
            "destination": {"type": "string", "maxLength": 500},
        },
        "required": ["first", "second", "destination"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string", "maxLength": 500},
            "model_generated": {"type": "boolean"},
        },
        "required": ["artifact_id", "model_generated"],
        "additionalProperties": False,
    },
    "required_capabilities": ["artifact.read", "artifact.write"],
    "steps": [
        {
            "id": "read_first",
            "kind": "tool",
            "adapter": "core.json_read",
            "contract_version": 1,
            "arguments": {"path": ref("goal", "parameters", "first")},
            "next": "read_second",
        },
        {
            "id": "read_second",
            "kind": "tool",
            "adapter": "core.json_read",
            "contract_version": 1,
            "arguments": {"path": ref("goal", "parameters", "second")},
            "next": "compare",
        },
        {
            "id": "compare",
            "kind": "cognitive",
            "service": "synthesize_evidence",
            "reason": "synthesis_required",
            "instruction": (
                "Compare the FIRST and SECOND reports. Summarize what changed and why "
                "it matters in at most five sentences. List in evidence_ids only the "
                "ids of the findings your summary relies on."
            ),
            "inputs": {
                "first": ref("result", "read_first", "value"),
                "second": ref("result", "read_second", "value"),
            },
            "output_schema": "report_comparison_v1",
            "max_model_calls": 1,
            "repair_calls": 1,
            "next": "publish",
        },
        {
            "id": "publish",
            "kind": "tool",
            "adapter": "core.json_write",
            "contract_version": 1,
            "arguments": {
                "path": ref("goal", "parameters", "destination"),
                "value": ref("result", "compare"),
            },
            "next": "check_published",
        },
        {
            "id": "check_published",
            "kind": "validate",
            "check": "artifact_matches_output",
            "arguments": {
                "artifact_id": ref("result", "publish", "artifact_id"),
                "output_step": {"literal": "compare"},
            },
            "next": "done",
        },
        {
            "id": "done",
            "kind": "complete",
            "result": {
                "artifact_id": ref("result", "publish", "artifact_id"),
                "model_generated": {"literal": True},
            },
        },
    ],
}
