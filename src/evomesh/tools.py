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
import logging
import shlex
import shutil
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

TOOL_FILENAME = "TOOL.md"


class MissingToolError(LookupError):
    pass


class InvalidToolError(ValueError):
    pass


class ToolParameter(BaseModel):
    name: str
    description: str = ""
    required: bool = True


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
    return ToolDefinition(
        name=name,
        description=description,
        command=command,
        parameters=parameters,
        path=path,
        created_by=created_by,
    )


class ToolRegistry:
    """Discovers custom tools under ``root/tools/*/TOOL.md``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._tools: dict[str, ToolDefinition] = {}

    @property
    def tools_dir(self) -> Path:
        return self.root / "tools"

    async def load(self) -> None:
        self._tools = await asyncio.to_thread(self._scan)

    def _scan(self) -> dict[str, ToolDefinition]:
        found: dict[str, ToolDefinition] = {}
        if not self.tools_dir.is_dir():
            return found
        for entry in sorted(self.tools_dir.iterdir()):
            tool_file = entry / TOOL_FILENAME
            if not tool_file.is_file():
                continue
            try:
                text = tool_file.read_text(encoding="utf-8")
                definition = parse_tool(tool_file.relative_to(self.root), text)
            except (InvalidToolError, OSError, UnicodeDecodeError) as exc:
                logger.warning("tool at %s could not be loaded: %s", tool_file, exc)
                continue
            found[definition.name] = definition
        return found

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

    async def read(self, name: str) -> str:
        tool = self.get(name)
        return await asyncio.to_thread((self.root / tool.path).read_text, encoding="utf-8")

    async def install(self, source_text: str, *, created_by: str = "human") -> ToolDefinition:
        """Write a new tool from a TOOL.md's raw text. Same one-step mechanism
        as SkillRegistry.install(): whatever parses becomes a tool, live in
        the registry immediately -- no restart, because nothing here is code
        the process has to load, only a file the next harness job reads."""
        definition = parse_tool(Path("<new tool>"), source_text, created_by=created_by)
        target = self.tools_dir / definition.name / TOOL_FILENAME
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, source_text, encoding="utf-8")
        installed = definition.model_copy(update={"path": target.relative_to(self.root)})
        self._tools[installed.name] = installed
        return installed

    async def install_directory(
        self, source: Path, *, created_by: str = "human"
    ) -> ToolDefinition:
        """Like install(), for a tool whose command is a bundled script --
        the script comes along, copied in as a unit with TOOL.md."""
        tool_file = source / TOOL_FILENAME
        if not await asyncio.to_thread(tool_file.is_file):
            raise InvalidToolError(f"{source}: no {TOOL_FILENAME} in this directory")
        text = await asyncio.to_thread(tool_file.read_text, encoding="utf-8")
        definition = parse_tool(Path("<new tool>"), text, created_by=created_by)
        target = self.tools_dir / definition.name
        await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
        await asyncio.to_thread(shutil.copytree, source, target)
        installed = definition.model_copy(
            update={"path": (target / TOOL_FILENAME).relative_to(self.root)}
        )
        self._tools[installed.name] = installed
        return installed
