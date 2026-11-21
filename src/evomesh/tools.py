"""A custom tool: a name and a description over one allow-listed command.

The harness already has a general-purpose way to run a program: the `shell`
tool, any allow-listed command, arguments and all. What it does not have is a
*named*, *described*, *parameterized* affordance a model can reach for the
way it reaches for `read` or `fetch` -- something that says "check_website(url)"
instead of asking a small model to construct a shell command line correctly
from prose, which is exactly the kind of step that goes wrong on the hardware
this project targets.

A tool here is declarative on purpose: a `command` template plus named
parameters, described in `tools/<name>/TOOL.md`, no Python written or loaded
into the running process. `harness_tools.build_custom_tool()` turns one into
a real `Tool` whose `run` shells out through the exact same allow-list and
subprocess path `tool_shell` already uses -- so a custom tool can never run
anything a human has not already trusted by name in `harness.shell_allow`,
and adding one is editing a file, not a code change or a restart.

A harness job's root is whatever that job is about, not necessarily this
project's own tree -- an agent's own playground, most of the time. A bundled
script belongs to the *tool*, not to whatever happens to be the caller's
root, so `command` may use the placeholder `{tool_dir}` for the tool's own
directory: `command: python "{tool_dir}/scripts/check.py"` finds the script
regardless of which job root is calling it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from evomesh.bundles import copy_into, overlay_file, replace_directory, restore_previous

logger = logging.getLogger(__name__)

TOOL_FILENAME = "TOOL.md"
# A tool's name is a directory under tools/ and a function name a model
# calls: no separators (an install named "../x" would land outside tools/),
# and the character set every OpenAI-compatible server accepts for one.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# What `requires:` may name: a program on PATH, a module the tool's own
# Python must be able to import, or a file inside the tool's directory.
REQUIREMENT_KINDS = ("program", "python", "file")


class MissingToolError(LookupError):
    pass


class InvalidToolError(ValueError):
    pass


class ToolParameter(BaseModel):
    name: str
    description: str = ""
    required: bool = True
    type: str = "str"

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        if value in ("str", "int", "float", "bool", "list", "dict", "bytes"):
            return value
        raise TypeError(
            f"'{cls.__name__}' 'type' is not a recognized Python type: {value!r}"
        )


MAX_TOOL_SECONDS = 600.0


class ToolDefinition(BaseModel):
    name: str
    description: str
    # The program and its fixed arguments, e.g. "python scripts/check.py".
    # Split once at parse time; a parameter's value is appended as one more
    # argv entry, never interpolated into a string that gets re-parsed --
    # the same reason tool_shell never runs through a shell interpreter.
    command: str
    parameters: list[ToolParameter] = Field(default_factory=list)
    path: Path
    created_by: str = "system"
    # How long one call may run, when harness.shell_seconds is too short: a
    # tool that falls back through headless browsers needs minutes, not 60 s.
    # None means shell_seconds; capped at MAX_TOOL_SECONDS.
    timeout_seconds: float | None = None
    # Declared runtime dependencies, "kind:value" (see REQUIREMENT_KINDS).
    # Checked by capabilities.tool_problems, never installed by anything here.
    requires: list[str] = Field(default_factory=list)

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                param.name: {"type": "string", "description": param.description}
                for param in self.parameters
            },
            "required": [param.name for param in self.parameters if param.required],
        }


def parse_tool(path: Path, text: str, *, created_by: str = "system") -> ToolDefinition:
    """Split a TOOL.md into its frontmatter and the definition it describes."""
    if not text.startswith("---"):
        raise InvalidToolError(f"{path}: missing YAML frontmatter (a leading '---' block)")
    end = text.find("\n---", 3)
    if end == -1:
        raise InvalidToolError(f"{path}: frontmatter is opened but never closed with '---'")
    try:
        meta = yaml.safe_load(text[3:end].strip("\n")) or {}
    except yaml.YAMLError as exc:
        raise InvalidToolError(f"{path}: frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        kind = type(meta).__name__
        raise InvalidToolError(f"{path}: frontmatter must be a mapping, not a {kind}")
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    command = str(meta.get("command") or "").strip()
    if not name or not description or not command:
        raise InvalidToolError(f"{path}: frontmatter needs 'name', 'description', and 'command'")
    if not NAME_PATTERN.match(name):
        raise InvalidToolError(
            f"{path}: name {name!r} must be letters, digits, '-' and '_' (at most 64)"
        )
    try:
        parsed_command = shlex.split(command, posix=True)
    except ValueError as exc:
        raise InvalidToolError(f"{path}: 'command' could not be parsed: {exc}") from exc
    if not parsed_command:
        raise InvalidToolError(f"{path}: 'command' is empty")
    raw_parameters = meta.get("parameters") or []
    if not isinstance(raw_parameters, list):
        raise InvalidToolError(f"{path}: 'parameters' must be a list")
    try:
        parameters = [ToolParameter.model_validate(item) for item in raw_parameters]
    except (TypeError, ValueError) as exc:
        raise InvalidToolError(f"{path}: invalid entry in 'parameters': {exc}") from exc
    timeout = meta.get("timeout_seconds")
    if timeout is not None:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise InvalidToolError(f"{path}: 'timeout_seconds' must be a number") from exc
        if timeout <= 0:
            raise InvalidToolError(f"{path}: 'timeout_seconds' must be positive")
        timeout = min(timeout, MAX_TOOL_SECONDS)
    requires = meta.get("requires") or []
    if not isinstance(requires, list) or not all(
        isinstance(item, str) and item.partition(":")[0] in REQUIREMENT_KINDS
        and item.partition(":")[2].strip()
        for item in requires
    ):
        raise InvalidToolError(
            f"{path}: 'requires' must be a list of 'program:<name>', 'python:<module>' "
            "or 'file:<path in the tool directory>'"
        )
    return ToolDefinition(
        name=name,
        description=description,
        command=command,
        parameters=parameters,
        path=path,
        created_by=created_by,
        timeout_seconds=timeout,
        requires=[item.strip() for item in requires],
    )


def bundled_files(definition: ToolDefinition) -> list[str]:
    """Files inside the tool's own directory that its command or its
    `requires:` names -- `"{tool_dir}/scripts/check.py"` -> scripts/check.py.
    A bundle missing one of these installs fine and fails on its first call."""
    found: list[str] = []
    try:
        words = shlex.split(definition.command, posix=True)
    except ValueError:
        words = []
    for word in words:
        if "{tool_dir}" in word:
            relative = word.split("{tool_dir}", 1)[1].lstrip("/\\")
            if relative:
                found.append(relative)
    found += [item.partition(":")[2].strip() for item in definition.requires
              if item.startswith("file:")]
    return found


def directory_fingerprint(directory: Path) -> str:
    """A digest of every file under ``directory`` (names and bytes), so a
    refresh can tell an edited script from an untouched bundle -- comparing
    only the parsed TOOL.md would call a changed script "unchanged"."""
    digest = hashlib.sha256()
    for item in sorted(directory.rglob("*")):
        if not item.is_file() or "__pycache__" in item.parts or item.name.startswith("."):
            continue
        digest.update(item.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


@dataclass(frozen=True)
class RefreshReport:
    """What a rescan of tools/ or skills/ changed in the live registry."""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    # Directory name -> why it could not be loaded.
    invalid: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    def render(self, kind: str) -> str:
        rows = [
            f"{label}: {', '.join(names)}"
            for label, names in (
                ("added", self.added),
                ("updated", self.updated),
                ("removed", self.removed),
            )
            if names
        ]
        rows += [f"invalid {name}: {why}" for name, why in sorted(self.invalid.items())]
        return f"{kind}: " + ("; ".join(rows) if rows else "no change")


def diff_scan(
    before: dict[str, str], after: dict[str, str], invalid: dict[str, str]
) -> RefreshReport:
    """Compare two name -> fingerprint maps."""
    return RefreshReport(
        added=sorted(set(after) - set(before)),
        updated=sorted(name for name in set(after) & set(before) if after[name] != before[name]),
        removed=sorted(set(before) - set(after)),
        invalid=dict(invalid),
    )


class ToolRegistry:
    """Discovers custom tools under ``root/tools/*/TOOL.md``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._tools: dict[str, ToolDefinition] = {}
        # Directory name -> why it is not a tool, from the last scan. Before
        # this a broken TOOL.md was one log line and then simply absent.
        self.errors: dict[str, str] = {}
        self._fingerprints: dict[str, str] = {}
        self._lock = asyncio.Lock()

    @property
    def tools_dir(self) -> Path:
        return self.root / "tools"

    async def load(self) -> None:
        await self.refresh()

    async def refresh(self) -> RefreshReport:
        """Rescan tools/ and swap the result in: a tool copied or edited on
        disk (a git pull, a hand edit) is live for the next harness job, no
        restart. A job already running keeps the tools it was built with."""
        async with self._lock:
            before = dict(self._fingerprints)
            found, errors, fingerprints = await asyncio.to_thread(self._scan)
            self._tools, self.errors, self._fingerprints = found, errors, fingerprints
        for name, why in errors.items():
            logger.warning("tool at tools/%s could not be loaded: %s", name, why)
        return diff_scan(before, fingerprints, errors)

    def _scan(self) -> tuple[dict[str, ToolDefinition], dict[str, str], dict[str, str]]:
        found: dict[str, ToolDefinition] = {}
        errors: dict[str, str] = {}
        fingerprints: dict[str, str] = {}
        if not self.tools_dir.is_dir():
            return found, errors, fingerprints
        for entry in sorted(self.tools_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith((".", "_")):
                continue
            tool_file = entry / TOOL_FILENAME
            if not tool_file.is_file():
                errors[entry.name] = f"no {TOOL_FILENAME}"
                continue
            try:
                text = tool_file.read_text(encoding="utf-8")
                definition = parse_tool(tool_file.relative_to(self.root), text)
                fingerprint = directory_fingerprint(entry)
            except (InvalidToolError, OSError, UnicodeDecodeError) as exc:
                errors[entry.name] = str(exc)
                continue
            if (other := found.get(definition.name)) is not None:
                # The directory named after the tool is the one an install
                # writes and a rollback restores; a stray copy never wins.
                loser, keep = entry.name, other
                if entry.name == definition.name:
                    loser, keep = other.path.parent.name, definition
                errors[loser] = (
                    f"name '{definition.name}' is already taken by "
                    f"{keep.path.parent.as_posix()}; this one is ignored"
                )
                if keep is other:
                    continue
            found[definition.name] = definition
            fingerprints[definition.name] = fingerprint
        return found, errors, fingerprints

    def discover(self, query: str = "") -> list[ToolDefinition]:
        needle = query.lower()
        return [
            tool
            for tool in self._tools.values()
            if needle in tool.name.lower() or needle in tool.description.lower()
        ]

    def get(self, name: str) -> ToolDefinition:
        tool = self._tools.get(name)
        if tool is None:
            raise MissingToolError(name)
        return tool

    def directory(self, definition: ToolDefinition) -> Path:
        return self.root / definition.path.parent

    async def read(self, name: str) -> str:
        tool = self.get(name)
        return await asyncio.to_thread((self.root / tool.path).read_text, encoding="utf-8")

    def _validator(
        self, created_by: str, expected: str | None = None, *, complete: bool = True
    ) -> Callable[[Path], ToolDefinition]:
        """``complete``: refuse a bundle missing a file its command names.
        Off only for a brand-new TOOL.md installed on its own, which has
        always been allowed (its script may be added next) and is reported
        as missing a dependency instead."""

        def validate(stage: Path) -> ToolDefinition:
            text = (stage / TOOL_FILENAME).read_text(encoding="utf-8")
            definition = parse_tool(Path("<new tool>"), text, created_by=created_by)
            if expected is not None and definition.name != expected:
                raise InvalidToolError(
                    f"{TOOL_FILENAME} names '{definition.name}', not '{expected}'"
                )
            missing = [item for item in bundled_files(definition) if not (stage / item).is_file()]
            if missing and complete:
                raise InvalidToolError(
                    f"{definition.name}: refused -- its command needs "
                    f"{', '.join(missing)}, which the bundle does not contain"
                )
            return definition

        return validate

    async def _swap_in(
        self,
        definition_name: str,
        fill: Callable[[Path], None],
        validate: Callable[[Path], ToolDefinition],
    ) -> ToolDefinition:
        target = self.tools_dir / definition_name
        async with self._lock:
            definition = await asyncio.to_thread(
                replace_directory, self.root, "tools", target, fill, validate
            )
            installed = definition.model_copy(
                update={"path": (target / TOOL_FILENAME).relative_to(self.root)}
            )
            self._tools[installed.name] = installed
            self.errors.pop(installed.name, None)
            self._fingerprints[installed.name] = await asyncio.to_thread(
                directory_fingerprint, target
            )
        return installed

    async def install(self, source_text: str, *, created_by: str = "human") -> ToolDefinition:
        """Write a new tool from a TOOL.md's raw text. Same one-step mechanism
        as SkillRegistry.install(): whatever parses becomes a tool, live in
        the registry immediately -- no restart, because nothing here is code
        the process has to load, only a file the next harness job reads.

        Over an installed bundle, its scripts are kept and only TOOL.md is
        replaced -- staged and validated first (see bundles.py), so a TOOL.md
        naming a script the installed bundle lacks is refused rather than
        breaking a tool that works."""
        definition = parse_tool(Path("<new tool>"), source_text, created_by=created_by)
        target = self.tools_dir / definition.name
        exists = await asyncio.to_thread(target.is_dir)
        return await self._swap_in(
            definition.name,
            overlay_file(target, TOOL_FILENAME, source_text),
            self._validator(created_by, definition.name, complete=exists),
        )

    async def install_directory(
        self, source: Path, *, created_by: str = "human"
    ) -> ToolDefinition:
        """Like install(), for a tool whose command is a bundled script --
        the script comes along, copied in as a unit with TOOL.md. The copy
        is staged and validated before it replaces anything; the version it
        replaces is kept for rollback()."""
        tool_file = source / TOOL_FILENAME
        if not await asyncio.to_thread(tool_file.is_file):
            raise InvalidToolError(f"{source}: no {TOOL_FILENAME} in this directory")
        text = await asyncio.to_thread(tool_file.read_text, encoding="utf-8")
        definition = parse_tool(Path("<new tool>"), text, created_by=created_by)
        return await self._swap_in(
            definition.name, copy_into(source), self._validator(created_by, definition.name)
        )

    async def validate_directory(self, source: Path) -> ToolDefinition:
        """Everything install_directory() checks, without installing."""
        tool_file = source / TOOL_FILENAME
        if not await asyncio.to_thread(tool_file.is_file):
            raise InvalidToolError(f"{source}: no {TOOL_FILENAME} in this directory")
        return await asyncio.to_thread(self._validator("validate"), source)

    async def rollback(self, name: str) -> ToolDefinition:
        """Put back the version the last install of ``name`` replaced."""
        target = self.tools_dir / name
        async with self._lock:
            definition = await asyncio.to_thread(
                restore_previous, self.root, "tools", target, self._validator("rollback", name)
            )
            restored = definition.model_copy(
                update={"path": (target / TOOL_FILENAME).relative_to(self.root)}
            )
            self._tools[name] = restored
            self.errors.pop(name, None)
            self._fingerprints[name] = await asyncio.to_thread(directory_fingerprint, target)
        return restored

