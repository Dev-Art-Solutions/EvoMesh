"""Typed procedures: one strict format, seven step kinds, one binding grammar.

This module is the contract (architecture closure plan v2, sections 6-9): the
definition models, the binding and predicate semantics, the schema subset, the
static validator and the canonical digest. It performs no I/O and executes
nothing; `procedure_runtime` does that, and only for definitions this module
has validated and a trusted admission has promoted.

A definition is data. Labels are presentation only: nothing here reads a step's
wording to decide what runs. A string that looks like a template (``{{x}}``) is
a literal string; the only way to refer to a value is ``{"ref": ...}``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ProcedureLimits:
    """Initial constrained profile (plan 13.4); stricter values may be set."""

    max_definition_bytes: int = 65536
    max_steps: int = 32
    max_binding_depth: int = 8
    max_predicate_nodes: int = 64
    max_inline_result_bytes: int = 8192
    max_execution_inline_bytes: int = 65536
    max_step_attempts: int = 3
    max_total_step_attempts: int = 64
    max_delegation_depth: int = 3
    max_root_model_calls: int = 4
    max_schema_repair_calls_per_step: int = 1
    default_execution_deadline_seconds: float = 600.0
    max_in_flight_steps_per_execution: int = 1
    max_issues_reported: int = 50


DEFAULT_LIMITS = ProcedureLimits()


# -- errors -----------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """One validation or execution problem, with a stable code and path."""

    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class ProcedureError(Exception):
    def __init__(self, code: str, message: str, path: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.path = path
        self.message = message


class BindingError(ProcedureError):
    pass


class PredicateError(ProcedureError):
    pass


# -- the definition ------------------------------------------------------------

_ID = r"^[a-z][a-z0-9_.-]{0,63}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ToolStep(_Strict):
    id: str = Field(pattern=_ID)
    kind: Literal["tool"]
    adapter: str
    contract_version: int = Field(ge=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    next: str
    max_attempts: int = Field(default=1, ge=1)


class CognitiveStep(_Strict):
    id: str = Field(pattern=_ID)
    kind: Literal["cognitive"]
    service: str
    reason: str
    instruction: str = Field(min_length=1, max_length=4000)
    inputs: dict[str, Any] = Field(default_factory=dict)
    output_schema: str
    max_model_calls: int = Field(default=1, ge=1)
    repair_calls: int = Field(default=0, ge=0)
    next: str


class BranchStep(_Strict):
    id: str = Field(pattern=_ID)
    kind: Literal["branch"]
    predicate: dict[str, Any]
    then: str
    else_: str = Field(alias="else")


class ValidateStep(_Strict):
    id: str = Field(pattern=_ID)
    kind: Literal["validate"]
    check: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    next: str


class DelegateBudget(_Strict):
    max_model_calls: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=1, ge=1)
    deadline_seconds: float = Field(default=300.0, gt=0)


class DelegateStep(_Strict):
    id: str = Field(pattern=_ID)
    kind: Literal["delegate"]
    work_kind: str
    objective: str = Field(min_length=1, max_length=2000)
    required_capabilities: list[str] = Field(min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    output_schema: str
    success_contract: str
    budget: DelegateBudget = Field(default_factory=DelegateBudget)
    next: str


class AwaitStep(_Strict):
    id: str = Field(pattern=_ID)
    kind: Literal["await"]
    subject: Literal["work", "evidence", "time"]
    reference: Any = None
    timeout_seconds: float = Field(gt=0)
    output_schema: str | None = None
    next: str


class CompleteStep(_Strict):
    id: str = Field(pattern=_ID)
    kind: Literal["complete"]
    result: dict[str, Any] = Field(default_factory=dict)


Step = Annotated[
    ToolStep | CognitiveStep | BranchStep | ValidateStep | DelegateStep | AwaitStep | CompleteStep,
    Field(discriminator="kind"),
]

PRODUCES_RESULT = ("tool", "cognitive", "validate", "delegate", "await")


class ContextDependency(_Strict):
    """A declared input read from state at start (belief or blackboard fact)."""

    name: str = Field(pattern=_ID)
    source: Literal["belief", "fact"]
    key: str = Field(min_length=1, max_length=200)
    max_age_seconds: float | None = Field(default=None, gt=0)


class ProcedureDefinition(_Strict):
    schema_version: Literal[1]
    procedure_id: str = Field(pattern=_ID)
    revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    entry_step_id: str
    goal_kind: str = Field(min_length=1, max_length=100)
    parameter_schema: dict[str, Any]
    output_schema: dict[str, Any]
    required_capabilities: list[str] = Field(default_factory=list)
    preconditions: list[dict[str, Any]] = Field(default_factory=list)
    context_dependencies: list[ContextDependency] = Field(default_factory=list)
    constants: dict[str, Any] = Field(default_factory=dict)
    steps: list[Step] = Field(min_length=1)

    def canonical(self) -> str:
        return canonical_json(self.model_dump(mode="json", by_alias=True))

    def digest(self) -> str:
        return digest_of(self.canonical())

    def step(self, step_id: str) -> Step:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    @property
    def key(self) -> str:
        return f"{self.procedure_id}@{self.revision}"


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def edges(step: Step) -> tuple[str, ...]:
    if isinstance(step, BranchStep):
        return (step.then, step.else_)
    if isinstance(step, CompleteStep):
        return ()
    return (step.next,)


# -- bindings ---------------------------------------------------------------------

Scope = Literal["goal", "result", "context", "work", "constants"]
SCOPES: frozenset[str] = frozenset({"goal", "result", "context", "work", "constants"})
_MISSING = object()


def is_ref(value: object) -> bool:
    return isinstance(value, dict) and set(value) == {"ref"}


def is_literal(value: object) -> bool:
    return isinstance(value, dict) and set(value) == {"literal"}


def iter_refs(value: object, path: str = "$") -> Iterable[tuple[str, str, list[str | int]]]:
    """Every ``ref`` inside a bound value: (json path, scope, path)."""
    if is_ref(value):
        ref = value["ref"]  # type: ignore[index]
        yield path, str(ref.get("scope")), list(ref.get("path") or [])
        return
    if is_literal(value):
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from iter_refs(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from iter_refs(item, f"{path}[{index}]")


def binding_problems(
    value: object, path: str, limits: ProcedureLimits, depth: int = 0
) -> list[Issue]:
    """Shape problems in a bound value: reserved forms, depth, bad refs."""
    if depth > limits.max_binding_depth:
        return [Issue("BINDING_TOO_DEEP", path, "bound value nests too deeply")]
    if isinstance(value, dict):
        if "ref" in value or "literal" in value:
            if len(value) != 1:
                return [Issue("INVALID_BINDING", path, "ref/literal must be the only key")]
            if "literal" in value:
                return []
            ref = value["ref"]
            if not isinstance(ref, dict) or set(ref) != {"scope", "path"}:
                return [Issue("INVALID_BINDING", path, "ref needs exactly scope and path")]
            if ref["scope"] not in SCOPES:
                return [Issue("INVALID_SCOPE", path, f"unknown scope {ref['scope']!r}")]
            parts = ref["path"]
            if (
                not isinstance(parts, list)
                or not parts
                or not all(
                    (isinstance(part, str) and part)
                    or (isinstance(part, int) and not isinstance(part, bool) and part >= 0)
                    for part in parts
                )
            ):
                return [Issue("INVALID_BINDING", path, "ref path must be a non-empty list")]
            return []
        issues: list[Issue] = []
        for key, item in value.items():
            issues += binding_problems(item, f"{path}.{key}", limits, depth + 1)
        return issues
    if isinstance(value, list):
        issues = []
        for index, item in enumerate(value):
            issues += binding_problems(item, f"{path}[{index}]", limits, depth + 1)
        return issues
    if isinstance(value, float) and not math.isfinite(value):
        return [Issue("NON_FINITE_NUMBER", path, "numbers must be finite")]
    if value is None or isinstance(value, str | bool | int | float):
        return []
    return [Issue("INVALID_BINDING", path, f"unsupported value type {type(value).__name__}")]


def lookup(scopes: Mapping[str, Any], scope: str, parts: Sequence[str | int]) -> Any:
    """The value at ``scope``/``parts`` or ``_MISSING``. Never coerces."""
    if scope not in scopes:
        return _MISSING
    node: Any = scopes[scope]
    for part in parts:
        if isinstance(part, int) and not isinstance(part, bool):
            if not isinstance(node, list) or part >= len(node):
                return _MISSING
            node = node[part]
        else:
            if not isinstance(node, dict) or part not in node:
                return _MISSING
            node = node[part]
    return node


def resolve(value: object, scopes: Mapping[str, Any], path: str = "$") -> Any:
    """Resolve a bound value, type-preserving. A missing required binding is
    ``MISSING_BINDING``; nothing is replaced by an empty string or zero."""
    if is_ref(value):
        ref = value["ref"]  # type: ignore[index]
        found = lookup(scopes, str(ref["scope"]), ref["path"])
        if found is _MISSING:
            raise BindingError("MISSING_BINDING", f"no value at {ref['scope']}:{ref['path']}", path)
        return found
    if is_literal(value):
        return value["literal"]  # type: ignore[index]
    if isinstance(value, dict):
        return {key: resolve(item, scopes, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, scopes, f"{path}[{index}]") for index, item in enumerate(value)]
    return value


# -- predicates (three-valued) -----------------------------------------------------

BINARY_OPS = frozenset({"eq", "ne", "gt", "ge", "lt", "le"})
PREDICATE_OPS = BINARY_OPS | {"exists", "all", "any", "not"}


def predicate_problems(pred: object, path: str, limits: ProcedureLimits) -> list[Issue]:
    count = [0]

    def walk(node: object, at: str) -> list[Issue]:
        count[0] += 1
        if not isinstance(node, dict) or "op" not in node:
            return [Issue("INVALID_PREDICATE", at, "a predicate is an object with op")]
        op = node["op"]
        if op not in PREDICATE_OPS:
            return [Issue("UNSUPPORTED_OPERATOR", at, f"unsupported operator {op!r}")]
        allowed = {
            "exists": {"op", "value"},
            "all": {"op", "items"},
            "any": {"op", "items"},
            "not": {"op", "item"},
        }.get(op, {"op", "left", "right"})
        if set(node) != allowed:
            return [Issue("INVALID_PREDICATE", at, f"{op} takes exactly {sorted(allowed)}")]
        if op in {"all", "any"}:
            items = node["items"]
            if not isinstance(items, list) or not items:
                return [Issue("EMPTY_COMPOSITE", at, f"{op} needs at least one item")]
            issues: list[Issue] = []
            for index, item in enumerate(items):
                issues += walk(item, f"{at}.items[{index}]")
            return issues
        if op == "not":
            return walk(node["item"], f"{at}.item")
        issues = []
        for key in ("value", "left", "right"):
            if key in node:
                issues += binding_problems(node[key], f"{at}.{key}", limits)
        return issues

    issues = walk(pred, path)
    if count[0] > limits.max_predicate_nodes:
        issues.append(Issue("PREDICATE_TOO_LARGE", path, "too many predicate nodes"))
    return issues


def _comparable(left: object, right: object) -> bool:
    def kind(value: object) -> str:
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int | float):
            return "number"
        if isinstance(value, str):
            return "string"
        if value is None:
            return "null"
        return type(value).__name__

    return kind(left) == kind(right)


def evaluate(pred: Mapping[str, Any], scopes: Mapping[str, Any]) -> bool | None:
    """True, False, or None for UNKNOWN. Incompatible types raise."""
    op = pred["op"]
    if op == "exists":
        value = pred["value"]
        if is_ref(value):
            ref = value["ref"]
            return lookup(scopes, str(ref["scope"]), ref["path"]) is not _MISSING
        return True
    if op == "not":
        inner = evaluate(pred["item"], scopes)
        return None if inner is None else not inner
    if op in {"all", "any"}:
        results = [evaluate(item, scopes) for item in pred["items"]]
        if op == "all":
            if any(result is False for result in results):
                return False
            return True if all(result is True for result in results) else None
        if any(result is True for result in results):
            return True
        return False if all(result is False for result in results) else None
    try:
        left = resolve(pred["left"], scopes)
        right = resolve(pred["right"], scopes)
    except BindingError:
        return None
    if not _comparable(left, right):
        raise PredicateError(
            "PREDICATE_TYPE_ERROR",
            f"cannot compare {type(left).__name__} with {type(right).__name__}",
        )
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if isinstance(left, bool) or not isinstance(left, int | float | str) or left is None:
        raise PredicateError("PREDICATE_TYPE_ERROR", f"{op} needs numbers or strings")
    assert isinstance(right, int | float | str)
    return {
        "gt": left > right,  # type: ignore[operator]
        "ge": left >= right,  # type: ignore[operator]
        "lt": left < right,  # type: ignore[operator]
        "le": left <= right,  # type: ignore[operator]
    }[op]


# -- the JSON-compatible schema subset ---------------------------------------------

SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "maxItems",
        "enum",
        "minimum",
        "maximum",
        "maxLength",
        "description",
    }
)
SCHEMA_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})


def schema_problems(schema: object, path: str) -> list[Issue]:
    if not isinstance(schema, dict):
        return [Issue("INVALID_SCHEMA", path, "a schema is an object")]
    unknown = set(schema) - SCHEMA_KEYWORDS
    if unknown:
        return [
            Issue("UNSUPPORTED_SCHEMA_KEYWORD", path, f"unsupported keywords {sorted(unknown)}")
        ]
    kind = schema.get("type")
    if kind not in SCHEMA_TYPES:
        return [Issue("INVALID_SCHEMA", path, f"type must be one of {sorted(SCHEMA_TYPES)}")]
    issues: list[Issue] = []
    if kind == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return [Issue("INVALID_SCHEMA", path, "properties must be an object")]
        opaque = schema.get("additionalProperties", False) is True
        if opaque and (properties or schema.get("required")):
            issues.append(Issue("INVALID_SCHEMA", path, "an opaque object declares no properties"))
        elif schema.get("additionalProperties", False) not in (False, True):
            issues.append(
                Issue("INVALID_SCHEMA", path, "additionalProperties must be true or false")
            )
        for name in schema.get("required", []):
            if name not in properties:
                issues.append(Issue("INVALID_SCHEMA", path, f"required {name!r} is not declared"))
        for name, child in properties.items():
            issues += schema_problems(child, f"{path}.properties.{name}")
    if kind == "array":
        if "items" not in schema or "maxItems" not in schema:
            issues.append(Issue("INVALID_SCHEMA", path, "arrays need items and maxItems"))
        else:
            issues += schema_problems(schema["items"], f"{path}.items")
    return issues


def schema_errors(value: object, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Strict validation of ``value``: booleans are not integers, null is not
    missing, and nothing is coerced."""
    kind = schema.get("type")
    errors: list[str] = []
    type_ok = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(float(value)),  # type: ignore[arg-type]
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(str(kind), False)
    if not type_ok:
        return [f"{path}: expected {kind}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        errors.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above {schema['maximum']}")
    if isinstance(value, dict) and schema.get("additionalProperties", False) is True:
        # An opaque JSON object: any keys, but still JSON (finite numbers).
        return errors + _json_errors(value, path)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}.{name}: required")
        for name, item in value.items():
            if name not in properties:
                errors.append(f"{path}.{name}: not allowed")
            else:
                errors += schema_errors(item, properties[name], f"{path}.{name}")
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", 0):
            errors.append(f"{path}: more than {schema.get('maxItems')} items")
        for index, item in enumerate(value):
            errors += schema_errors(item, schema["items"], f"{path}[{index}]")
    return errors


def _json_errors(value: object, path: str, depth: int = 0) -> list[str]:
    if depth > 32:
        return [f"{path}: nested too deeply"]
    if isinstance(value, dict):
        errors: list[str] = []
        for key, item in value.items():
            if not isinstance(key, str):
                errors.append(f"{path}: non-string key")
            errors += _json_errors(item, f"{path}.{key}", depth + 1)
        return errors
    if isinstance(value, list):
        return [
            error
            for index, item in enumerate(value)
            for error in _json_errors(item, f"{path}[{index}]", depth + 1)
        ]
    if isinstance(value, float) and not math.isfinite(value):
        return [f"{path}: not a finite number"]
    if value is None or isinstance(value, str | int | float | bool):
        return []
    return [f"{path}: not JSON ({type(value).__name__})"]


OPAQUE_OBJECT: dict[str, Any] = {"type": "object", "additionalProperties": True}


# -- trusted catalogs ---------------------------------------------------------------


class SideEffect(StrEnum):
    PURE = "pure"
    READ = "read"
    LOCAL_WRITE = "local_write"
    EXTERNAL = "external"


class RetrySemantics(StrEnum):
    SAFE = "safe"  # repeating it cannot change anything
    IDEMPOTENT = "idempotent"  # same key and arguments apply at most once
    NONE = "none"  # an uncertain attempt must be reconciled


@dataclass(frozen=True)
class AdapterContract:
    adapter_id: str
    contract_version: int
    argument_schema: Mapping[str, Any]
    result_schema: Mapping[str, Any]
    required_capabilities: tuple[str, ...]
    side_effect: SideEffect
    retry: RetrySemantics
    reconcile_supported: bool = False
    uses_model: bool = False
    timeout_seconds: float = 30.0
    # Arguments that name a file: what delegated work must have been
    # granted by its requester before this adapter may touch it.
    resource_arguments: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckContract:
    """A trusted deterministic verifier a validate step may name."""

    check_id: str
    argument_schema: Mapping[str, Any]
    description: str = ""


@dataclass(frozen=True)
class OutputContract:
    """A registered output schema for cognitive, delegated or awaited results,
    with an optional trusted semantic check over (result, inputs)."""

    schema_id: str
    schema: Mapping[str, Any]
    semantic_check: Callable[[Any, Mapping[str, Any]], list[str]] | None = None
    # Used as a delegation's success contract: validator checks the child's
    # own execution must have passed. A schema-valid result is not enough.
    required_evidence: tuple[str, ...] = ()


@dataclass
class Catalog:
    adapters: dict[str, AdapterContract] = field(default_factory=dict)
    checks: dict[str, CheckContract] = field(default_factory=dict)
    outputs: dict[str, OutputContract] = field(default_factory=dict)
    cognitive_services: frozenset[str] = frozenset()
    cognitive_reasons: frozenset[str] = frozenset()

    def add_adapter(self, contract: AdapterContract) -> None:
        self.adapters[contract.adapter_id] = contract

    def add_check(self, contract: CheckContract) -> None:
        self.checks[contract.check_id] = contract

    def add_output(self, contract: OutputContract) -> None:
        self.outputs[contract.schema_id] = contract


# -- static validation ----------------------------------------------------------------

SECRET_NAMES = re.compile(r"(pass(word)?|secret|token|api[_-]?key|credential)", re.IGNORECASE)
DELEGATE_CAPABILITY = "work.delegate"


@dataclass
class ValidationReport:
    ok: bool
    issues: list[Issue]
    definition: ProcedureDefinition | None = None
    effective_capabilities: frozenset[str] = frozenset()
    side_effects: frozenset[str] = frozenset()
    max_model_calls: int = 0

    def codes(self) -> list[str]:
        return [issue.code for issue in self.issues]


def parse_definition(raw: Mapping[str, Any]) -> tuple[ProcedureDefinition | None, list[Issue]]:
    """Strict parse. Unknown fields, kinds, versions and imported admission
    metadata are refused with stable codes."""
    try:
        return ProcedureDefinition.model_validate(dict(raw)), []
    except ValidationError as exc:
        issues: list[Issue] = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"])
            kind = error["type"]
            if kind == "extra_forbidden":
                code = "UNKNOWN_FIELD"
            elif kind == "union_tag_invalid":
                code = "UNKNOWN_STEP_KIND"
            elif kind == "union_tag_not_found":
                code = "MISSING_STEP_KIND"
            elif location == "schema_version":
                code = "UNSUPPORTED_SCHEMA_VERSION"
            else:
                code = "INVALID_FIELD"
            issues.append(Issue(code, location or "$", error["msg"]))
        return None, issues


def validate_definition(
    raw: Mapping[str, Any] | ProcedureDefinition,
    catalog: Catalog,
    limits: ProcedureLimits = DEFAULT_LIMITS,
) -> ValidationReport:
    if isinstance(raw, ProcedureDefinition):
        definition, issues = raw, []
    else:
        size = len(canonical_json(raw).encode("utf-8"))
        if size > limits.max_definition_bytes:
            return ValidationReport(False, [Issue("DEFINITION_TOO_LARGE", "$", f"{size} bytes")])
        definition, issues = parse_definition(raw)
    if definition is None:
        return ValidationReport(False, issues[: limits.max_issues_reported])
    issues = _graph_issues(definition, limits)
    if not issues:
        issues += _dataflow_issues(definition)
    issues += _contract_issues(definition, catalog, limits)
    effective, effects, model_calls = _effective(definition, catalog)
    missing = sorted(effective - set(definition.required_capabilities))
    if missing:
        issues.append(Issue("UNDECLARED_CAPABILITY", "required_capabilities", f"missing {missing}"))
    return ValidationReport(
        not issues,
        issues[: limits.max_issues_reported],
        definition,
        frozenset(effective),
        frozenset(effects),
        model_calls,
    )


def _graph_issues(definition: ProcedureDefinition, limits: ProcedureLimits) -> list[Issue]:
    issues: list[Issue] = []
    if len(definition.steps) > limits.max_steps:
        issues.append(Issue("TOO_MANY_STEPS", "steps", f"{len(definition.steps)} steps"))
    ids = [step.id for step in definition.steps]
    duplicates = sorted({step_id for step_id in ids if ids.count(step_id) > 1})
    if duplicates:
        issues.append(Issue("DUPLICATE_STEP_ID", "steps", f"duplicate ids {duplicates}"))
        return issues
    known = set(ids)
    if definition.entry_step_id not in known:
        issues.append(Issue("MISSING_ENTRY", "entry_step_id", definition.entry_step_id))
        return issues
    for step in definition.steps:
        for target in edges(step):
            if target not in known:
                issues.append(Issue("DANGLING_EDGE", f"steps.{step.id}", f"-> {target}"))
    if issues:
        return issues
    successors = {step.id: edges(step) for step in definition.steps}
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        state[node] = 1
        for target in successors[node]:
            if state.get(target) == 1:
                return True
            if state.get(target) is None and visit(target):
                return True
        state[node] = 2
        return False

    if visit(definition.entry_step_id):
        issues.append(Issue("CYCLE", "steps", "the step graph must be acyclic"))
        return issues
    unreachable = sorted(known - set(state))
    if unreachable:
        issues.append(Issue("UNREACHABLE_STEP", "steps", f"unreachable {unreachable}"))
    return issues


def _predecessors(definition: ProcedureDefinition) -> dict[str, list[str]]:
    preds: dict[str, list[str]] = {step.id: [] for step in definition.steps}
    for step in definition.steps:
        for target in edges(step):
            preds[target].append(step.id)
    return preds


def _topological(definition: ProcedureDefinition) -> list[str]:
    preds = _predecessors(definition)
    remaining = {key: len(value) for key, value in preds.items()}
    order: list[str] = []
    ready = [definition.entry_step_id]
    while ready:
        node = ready.pop()
        order.append(node)
        for target in edges(definition.step(node)):
            remaining[target] -= 1
            if remaining[target] == 0:
                ready.append(target)
    return order


def _dataflow_issues(definition: ProcedureDefinition) -> list[Issue]:
    """A result reference must be produced on every path reaching its use."""
    preds = _predecessors(definition)
    available: dict[str, frozenset[str]] = {}
    issues: list[Issue] = []
    for step_id in _topological(definition):
        incoming = [
            available[pred] | ({pred} if definition.step(pred).kind in PRODUCES_RESULT else set())
            for pred in preds[step_id]
        ]
        here = frozenset.intersection(*incoming) if incoming else frozenset()
        available[step_id] = here
        step = definition.step(step_id)
        for path, scope, parts in _step_refs(step):
            if scope == "result" and (not parts or parts[0] not in here):
                issues.append(
                    Issue(
                        "UNAVAILABLE_RESULT",
                        f"steps.{step_id}{path[1:]}",
                        f"result {parts[:1]} is not produced on every path here",
                    )
                )
            if scope == "goal":
                properties = definition.parameter_schema.get("properties", {})
                if len(parts) < 2 or parts[0] != "parameters" or parts[1] not in properties:
                    issues.append(
                        Issue("UNKNOWN_PARAMETER", f"steps.{step_id}{path[1:]}", str(parts))
                    )
            if scope == "constants" and (not parts or parts[0] not in definition.constants):
                issues.append(Issue("UNKNOWN_CONSTANT", f"steps.{step_id}{path[1:]}", str(parts)))
            if scope == "context" and (
                not parts or parts[0] not in {item.name for item in definition.context_dependencies}
            ):
                issues.append(Issue("UNDECLARED_CONTEXT", f"steps.{step_id}{path[1:]}", str(parts)))
    return issues


def _step_refs(step: Step) -> list[tuple[str, str, list[str | int]]]:
    values: list[object] = []
    if isinstance(step, ToolStep | ValidateStep):
        values.append(step.arguments)
    elif isinstance(step, CognitiveStep | DelegateStep):
        values.append(step.inputs)
    elif isinstance(step, BranchStep):
        values.append(step.predicate)
    elif isinstance(step, AwaitStep):
        values.append(step.reference)
    elif isinstance(step, CompleteStep):
        values.append(step.result)
    refs: list[tuple[str, str, list[str | int]]] = []
    for value in values:
        refs += list(iter_refs(value))
    return refs


def _contract_issues(
    definition: ProcedureDefinition, catalog: Catalog, limits: ProcedureLimits
) -> list[Issue]:
    issues: list[Issue] = []
    issues += schema_problems(definition.parameter_schema, "parameter_schema")
    issues += schema_problems(definition.output_schema, "output_schema")
    if definition.parameter_schema.get("type") != "object":
        issues.append(Issue("INVALID_SCHEMA", "parameter_schema", "must be an object schema"))
    for name, value in definition.constants.items():
        if SECRET_NAMES.search(name):
            issues.append(
                Issue("SECRET_CONSTANT", f"constants.{name}", "no secrets in definitions")
            )
        issues += binding_problems(value, f"constants.{name}", limits)
    for index, pred in enumerate(definition.preconditions):
        issues += predicate_problems(pred, f"preconditions[{index}]", limits)
    required_output = set(definition.output_schema.get("required", []))
    for step in definition.steps:
        at = f"steps.{step.id}"
        if isinstance(step, ToolStep):
            contract = catalog.adapters.get(step.adapter)
            if contract is None:
                issues.append(Issue("UNKNOWN_ADAPTER", at, step.adapter))
                continue
            if contract.contract_version != step.contract_version:
                issues.append(
                    Issue(
                        "CONTRACT_VERSION_MISMATCH",
                        at,
                        f"{step.adapter} is v{contract.contract_version}",
                    )
                )
            if contract.uses_model:
                issues.append(Issue("MODEL_BACKED_TOOL", at, "tool steps may not call a model"))
            if contract.side_effect is SideEffect.EXTERNAL:
                issues.append(
                    Issue("EXTERNAL_EFFECT_UNSUPPORTED", at, "external mutation is not admitted")
                )
            if step.max_attempts > limits.max_step_attempts:
                issues.append(Issue("TOO_MANY_ATTEMPTS", at, str(step.max_attempts)))
            issues += _argument_issues(step.arguments, contract.argument_schema, at, limits)
        elif isinstance(step, ValidateStep):
            check = catalog.checks.get(step.check)
            if check is None:
                issues.append(Issue("UNKNOWN_CHECK", at, step.check))
                continue
            issues += _argument_issues(step.arguments, check.argument_schema, at, limits)
        elif isinstance(step, CognitiveStep):
            if step.service not in catalog.cognitive_services:
                issues.append(Issue("UNKNOWN_COGNITIVE_SERVICE", at, step.service))
            if step.reason not in catalog.cognitive_reasons:
                issues.append(Issue("UNKNOWN_COGNITIVE_REASON", at, step.reason))
            if step.output_schema not in catalog.outputs:
                issues.append(Issue("UNKNOWN_OUTPUT_SCHEMA", at, step.output_schema))
            if step.repair_calls > limits.max_schema_repair_calls_per_step:
                issues.append(Issue("TOO_MANY_REPAIRS", at, str(step.repair_calls)))
            issues += binding_problems(step.inputs, f"{at}.inputs", limits)
        elif isinstance(step, BranchStep):
            issues += predicate_problems(step.predicate, f"{at}.predicate", limits)
        elif isinstance(step, DelegateStep):
            for schema_id in (step.output_schema, step.success_contract):
                if schema_id not in catalog.outputs:
                    issues.append(Issue("UNKNOWN_OUTPUT_SCHEMA", at, schema_id))
            issues += binding_problems(step.inputs, f"{at}.inputs", limits)
        elif isinstance(step, AwaitStep):
            if step.subject in {"work", "evidence"}:
                if not is_ref(step.reference):
                    issues.append(Issue("INVALID_AWAIT", at, "work/evidence await needs a ref"))
            if step.output_schema is not None and step.output_schema not in catalog.outputs:
                issues.append(Issue("UNKNOWN_OUTPUT_SCHEMA", at, step.output_schema))
            if step.timeout_seconds > limits.default_execution_deadline_seconds:
                issues.append(Issue("TIMEOUT_TOO_LONG", at, str(step.timeout_seconds)))
        elif isinstance(step, CompleteStep):
            issues += binding_problems(step.result, f"{at}.result", limits)
            missing = sorted(required_output - set(step.result))
            if missing:
                issues.append(Issue("OUTPUT_CONTRACT_MISMATCH", at, f"missing {missing}"))
    return issues


def _argument_issues(
    arguments: Mapping[str, Any], schema: Mapping[str, Any], at: str, limits: ProcedureLimits
) -> list[Issue]:
    issues = binding_problems(dict(arguments), f"{at}.arguments", limits)
    properties = schema.get("properties", {})
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        issues.append(Issue("UNKNOWN_ARGUMENT", at, f"not in the contract: {unknown}"))
    missing = sorted(set(schema.get("required", [])) - set(arguments))
    if missing:
        issues.append(Issue("MISSING_ARGUMENT", at, f"required by the contract: {missing}"))
    return issues


def _effective(definition: ProcedureDefinition, catalog: Catalog) -> tuple[set[str], set[str], int]:
    capabilities: set[str] = set()
    effects: set[str] = set()
    model_calls = 0
    for step in definition.steps:
        if isinstance(step, ToolStep) and (contract := catalog.adapters.get(step.adapter)):
            capabilities |= set(contract.required_capabilities)
            effects.add(contract.side_effect.value)
        elif isinstance(step, CognitiveStep):
            model_calls += step.max_model_calls + step.repair_calls
        elif isinstance(step, DelegateStep):
            capabilities.add(DELEGATE_CAPABILITY)
    return capabilities, effects, model_calls
