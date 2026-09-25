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


# Structured delegation (plan 14): the parent hands a bounded inspection to a
# capable peer and waits, without polling a model, for the child's result.
JSON_INSPECTION: dict[str, Any] = {
    "schema_version": 1,
    "procedure_id": "json_inspection",
    "revision": 1,
    "name": "Inspect an authorized JSON document",
    "entry_step_id": "read",
    "goal_kind": "json_inspection",
    "parameter_schema": {
        "type": "object",
        "properties": {"path": {"type": "string", "maxLength": 500}},
        "required": ["path"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string", "maxLength": 500},
            "keys": {
                "type": "array",
                "items": {"type": "string", "maxLength": 100},
                "maxItems": 64,
            },
            "digest": {"type": "string", "maxLength": 100},
        },
        "required": ["artifact_id", "keys", "digest"],
        "additionalProperties": False,
    },
    "required_capabilities": ["artifact.read"],
    "steps": [
        {
            "id": "read",
            "kind": "tool",
            "adapter": "core.json_read",
            "contract_version": 1,
            "arguments": {"path": ref("goal", "parameters", "path")},
            "next": "done",
        },
        {
            "id": "done",
            "kind": "complete",
            "result": {
                "artifact_id": ref("result", "read", "path"),
                "keys": ref("result", "read", "keys"),
                "digest": ref("result", "read", "source_digest"),
            },
        },
    ],
}

DELEGATED_INSPECTION: dict[str, Any] = {
    "schema_version": 1,
    "procedure_id": "delegated_inspection",
    "revision": 1,
    "name": "Have a peer inspect a JSON document",
    "entry_step_id": "hand_off",
    "goal_kind": "delegated_inspection",
    "parameter_schema": {
        "type": "object",
        "properties": {"path": {"type": "string", "maxLength": 500}},
        "required": ["path"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "keys": {
                "type": "array",
                "items": {"type": "string", "maxLength": 100},
                "maxItems": 64,
            },
            "digest": {"type": "string", "maxLength": 100},
        },
        "required": ["keys", "digest"],
        "additionalProperties": False,
    },
    "required_capabilities": ["work.delegate"],
    "steps": [
        {
            "id": "hand_off",
            "kind": "delegate",
            "work_kind": "json_inspection",
            "objective": "Inspect the JSON document and report its keys and digest.",
            "required_capabilities": ["artifact.read"],
            "inputs": {"path": ref("goal", "parameters", "path")},
            "output_schema": "authorized_json_inspection_v1",
            "success_contract": "authorized_json_inspection_v1",
            "budget": {"max_model_calls": 0, "max_attempts": 1, "deadline_seconds": 600},
            "next": "wait",
        },
        {
            "id": "wait",
            "kind": "await",
            "subject": "work",
            "reference": ref("result", "hand_off", "work_item_id"),
            "timeout_seconds": 600,
            "output_schema": "authorized_json_inspection_v1",
            "next": "done",
        },
        {
            "id": "done",
            "kind": "complete",
            "result": {
                "keys": ref("result", "wait", "keys"),
                "digest": ref("result", "wait", "digest"),
            },
        },
    ],
}


def evidence_wait(timeout: float = 60) -> dict[str, Any]:
    """Wait for a named blackboard fact, then complete with its value."""
    return {
        "schema_version": 1,
        "procedure_id": "evidence_wait",
        "revision": 1,
        "name": "Wait for a fact",
        "entry_step_id": "wait",
        "goal_kind": "evidence_wait",
        "parameter_schema": {
            "type": "object",
            "properties": {"key": {"type": "string", "maxLength": 200}},
            "required": ["key"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"value": {"type": "string", "maxLength": 4000}},
            "required": ["value"],
            "additionalProperties": False,
        },
        "required_capabilities": [],
        "steps": [
            {
                "id": "wait",
                "kind": "await",
                "subject": "evidence",
                "reference": ref("goal", "parameters", "key"),
                "timeout_seconds": timeout,
                "next": "done",
            },
            {
                "id": "done",
                "kind": "complete",
                "result": {"value": ref("result", "wait", "value")},
            },
        ],
    }
