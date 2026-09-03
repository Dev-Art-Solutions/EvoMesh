"""A custom tool is a declarative TOOL.md over one allow-listed command --
never Python written or loaded into the running process. See tools.py's own
docstring for why this exists alongside the generic, already-general-purpose
`shell` tool.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evomesh.tools import (
    InvalidToolError,
    MissingToolError,
    ToolRegistry,
    parse_tool,
)

VALID = (
    "---\nname: check-site\ndescription: Check a site responds.\n"
    "command: python scripts/check.py\n"
    "parameters:\n  - name: url\n    description: The URL to check.\n"
    "---\n\nOptional notes for a human.\n"
)


def write_tool(root: Path, name: str, text: str) -> Path:
    directory = root / "tools" / name
    directory.mkdir(parents=True)
    target = directory / "TOOL.md"
    target.write_text(text, encoding="utf-8")
    return target


def test_parse_tool_reads_name_description_command_and_parameters() -> None:
    definition = parse_tool(Path("tools/check-site/TOOL.md"), VALID)

    assert definition.name == "check-site"
    assert definition.description == "Check a site responds."
    assert definition.command == "python scripts/check.py"
    assert len(definition.parameters) == 1
    assert definition.parameters[0].name == "url"
    assert definition.parameters[0].required is True


def test_parameters_schema_matches_the_declared_parameters() -> None:
    definition = parse_tool(Path("tools/check-site/TOOL.md"), VALID)

    schema = definition.parameters_schema()

    assert schema["properties"]["url"]["type"] == "string"
    assert schema["required"] == ["url"]


def test_a_parameter_can_be_declared_optional() -> None:
    text = (
        "---\nname: check-site\ndescription: Check a site.\n"
        "command: python scripts/check.py\n"
        "parameters:\n  - name: url\n    required: false\n"
        "---\n"
    )
    definition = parse_tool(Path("tools/check-site/TOOL.md"), text)

    assert definition.parameters[0].required is False
    assert definition.parameters_schema()["required"] == []


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter here at all\n",
        "---\nname: check-site\ndescription: unfinished",
        "---\nname: missing a command\ndescription: d\n---\nbody",
        "---\nname: missing a description\ncommand: python x.py\n---\nbody",
        "---\ndescription: missing a name\ncommand: python x.py\n---\nbody",
        '---\nname: bad-command\ndescription: d\ncommand: "unterminated \'quote\n---\nbody',
        "---\nname: empty-command\ndescription: d\ncommand: \"   \"\n---\nbody",
        (
            "---\nname: bad-params\ndescription: d\ncommand: python x.py\n"
            "parameters: not-a-list\n---\n"
        ),
        (
            "---\nname: bad-param-entry\ndescription: d\ncommand: python x.py\n"
            "parameters:\n  - not: a valid parameter shape\n---\n"
        ),
    ],
)
def test_parse_tool_rejects_malformed_definitions(text: str) -> None:
    with pytest.raises(InvalidToolError):
        parse_tool(Path("tools/broken/TOOL.md"), text)


async def test_registry_discovers_a_tool_written_to_disk(tmp_path: Path) -> None:
    write_tool(tmp_path, "check-site", VALID)
    registry = ToolRegistry(tmp_path)

    await registry.load()

    found = registry.discover()
    assert [tool.name for tool in found] == ["check-site"]
    assert registry.discover("responds")[0].name == "check-site"
    assert registry.discover("nothing matches this") == []


async def test_registry_skips_a_broken_tool_but_keeps_the_rest(tmp_path: Path) -> None:
    write_tool(tmp_path, "check-site", VALID)
    write_tool(tmp_path, "broken", "not a tool file at all")
    registry = ToolRegistry(tmp_path)

    await registry.load()

    assert [tool.name for tool in registry.discover()] == ["check-site"]


async def test_registry_returns_nothing_when_there_is_no_tools_directory(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(tmp_path)

    await registry.load()

    assert registry.discover() == []


async def test_get_and_read_raise_for_a_tool_that_does_not_exist(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path)
    await registry.load()

    with pytest.raises(MissingToolError):
        registry.get("nope")
    with pytest.raises(MissingToolError):
        await registry.read("nope")


async def test_install_writes_the_file_and_is_discoverable_immediately(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(tmp_path)
    await registry.load()

    installed = await registry.install(VALID, created_by="market")

    assert installed.name == "check-site"
    assert installed.created_by == "market"
    assert (tmp_path / "tools" / "check-site" / "TOOL.md").read_text(encoding="utf-8") == VALID
    assert [tool.name for tool in registry.discover()] == ["check-site"]


async def test_install_rejects_text_with_no_valid_frontmatter(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path)

    with pytest.raises(InvalidToolError):
        await registry.install("just some text, not a tool")


async def test_install_directory_copies_the_whole_bundle(tmp_path: Path) -> None:
    source = tmp_path / "incoming" / "check-site"
    source.mkdir(parents=True)
    (source / "TOOL.md").write_text(VALID, encoding="utf-8")
    scripts = source / "scripts"
    scripts.mkdir()
    (scripts / "check.py").write_text("print('ok')\n", encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    installed = await registry.install_directory(source, created_by="console")

    assert installed.name == "check-site"
    installed_root = tmp_path / "tools" / "check-site"
    assert (installed_root / "TOOL.md").is_file()
    assert (installed_root / "scripts" / "check.py").read_text(encoding="utf-8") == (
        "print('ok')\n"
    )


async def test_install_directory_rejects_one_with_no_tool_md(tmp_path: Path) -> None:
    source = tmp_path / "incoming" / "empty"
    source.mkdir(parents=True)
    registry = ToolRegistry(tmp_path)

    with pytest.raises(InvalidToolError):
        await registry.install_directory(source)
