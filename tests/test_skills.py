"""A skill is a description on disk, never code the mesh executes.

Every skill here is a SKILL.md a test writes into tmp_path/skills/ -- the
registry has no other source of truth, on purpose (see skills.py's own
docstring for the mistake this replaced).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evomesh.skills import (
    InvalidSkillError,
    MissingSkillError,
    SkillRegistry,
    parse_skill,
    skill_body,
)

VALID = "---\nname: research\ndescription: Look things up before answering.\n---\n\nUse fetch.\n"


def write_skill(root: Path, name: str, text: str) -> Path:
    directory = root / "skills" / name
    directory.mkdir(parents=True)
    target = directory / "SKILL.md"
    target.write_text(text, encoding="utf-8")
    return target


def test_parse_skill_reads_name_and_description_from_frontmatter() -> None:
    definition = parse_skill(Path("skills/research/SKILL.md"), VALID)

    assert definition.name == "research"
    assert definition.description == "Look things up before answering."
    assert definition.path == Path("skills/research/SKILL.md")


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter here at all\n",
        "---\nname: research\ndescription: unfinished",
        "---\nname: [this, is, a, list]\n---\nbody",
        "---\ndescription: missing a name\n---\nbody",
        "---\nname: missing a description\n---\nbody",
    ],
)
def test_parse_skill_rejects_malformed_frontmatter(text: str) -> None:
    with pytest.raises(InvalidSkillError):
        parse_skill(Path("skills/broken/SKILL.md"), text)


def test_skill_body_strips_the_frontmatter() -> None:
    assert skill_body(VALID) == "Use fetch."
    assert skill_body("no frontmatter\njust text") == "no frontmatter\njust text"


async def test_registry_discovers_a_skill_written_to_disk(tmp_path: Path) -> None:
    write_skill(tmp_path, "research", VALID)
    registry = SkillRegistry(tmp_path)

    await registry.load()

    found = registry.discover()
    assert [skill.name for skill in found] == ["research"]
    assert registry.discover("things up")[0].name == "research"
    assert registry.discover("nothing matches this") == []


async def test_registry_skips_a_broken_skill_but_keeps_the_rest(tmp_path: Path) -> None:
    write_skill(tmp_path, "research", VALID)
    write_skill(tmp_path, "broken", "not a skill file at all")
    registry = SkillRegistry(tmp_path)

    await registry.load()

    assert [skill.name for skill in registry.discover()] == ["research"]


async def test_registry_returns_nothing_when_there_is_no_skills_directory(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path)

    await registry.load()

    assert registry.discover() == []
    assert registry.render_catalog() == ""


async def test_get_and_read_raise_for_a_skill_that_does_not_exist(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path)
    await registry.load()

    with pytest.raises(MissingSkillError):
        registry.get("research")
    with pytest.raises(MissingSkillError):
        await registry.read("research")


async def test_read_returns_the_body_and_get_returns_the_definition(tmp_path: Path) -> None:
    write_skill(tmp_path, "research", VALID)
    registry = SkillRegistry(tmp_path)
    await registry.load()

    assert registry.get("research").description == "Look things up before answering."
    assert await registry.read("research") == "Use fetch."


async def test_render_catalog_names_the_file_to_read_for_more(tmp_path: Path) -> None:
    write_skill(tmp_path, "research", VALID)
    registry = SkillRegistry(tmp_path)
    await registry.load()

    catalog = registry.render_catalog()

    assert "research: Look things up before answering." in catalog
    assert "skills/research/SKILL.md" in catalog
    # The body never appears -- reading it is a step the agent still takes.
    assert "Use fetch." not in catalog


async def test_install_writes_the_file_and_is_discoverable_immediately(tmp_path: Path) -> None:
    """The whole installation mechanism, and the one thing a market needs:
    whatever produced valid frontmatter and a body becomes a skill, without a
    separate reload -- install() updates the live registry itself."""
    registry = SkillRegistry(tmp_path)
    await registry.load()

    installed = await registry.install(VALID, created_by="market")

    assert installed.name == "research"
    assert installed.created_by == "market"
    assert (tmp_path / "skills" / "research" / "SKILL.md").read_text(encoding="utf-8") == VALID
    assert [skill.name for skill in registry.discover()] == ["research"]


async def test_install_rejects_text_with_no_valid_frontmatter(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path)

    with pytest.raises(InvalidSkillError):
        await registry.install("just some text, not a skill")


async def test_install_directory_copies_the_whole_bundle_not_only_skill_md(
    tmp_path: Path,
) -> None:
    """'A skill is one or a group of commands': a script beside SKILL.md is
    part of the skill, so installing it has to bring the script along, not
    just the description that names it."""
    source = tmp_path / "incoming" / "web-check"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: web-check\ndescription: Check a site is still up.\n---\n\n"
        "Run scripts/check.sh with the shell tool.\n",
        encoding="utf-8",
    )
    scripts = source / "scripts"
    scripts.mkdir()
    (scripts / "check.sh").write_text("#!/bin/sh\ncurl -sf \"$1\"\n", encoding="utf-8")
    registry = SkillRegistry(tmp_path)
    await registry.load()

    installed = await registry.install_directory(source, created_by="console")

    assert installed.name == "web-check"
    installed_root = tmp_path / "skills" / "web-check"
    assert (installed_root / "SKILL.md").is_file()
    assert (installed_root / "scripts" / "check.sh").read_text(encoding="utf-8") == (
        "#!/bin/sh\ncurl -sf \"$1\"\n"
    )
    assert [skill.name for skill in registry.discover()] == ["web-check"]


async def test_install_directory_replaces_a_previous_install_of_the_same_name(
    tmp_path: Path,
) -> None:
    source = tmp_path / "incoming" / "web-check"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: web-check\ndescription: v1.\n---\n\nOld body.\n", encoding="utf-8"
    )
    (source / "old-file.txt").write_text("stale", encoding="utf-8")
    registry = SkillRegistry(tmp_path)
    await registry.install_directory(source, created_by="console")

    (source / "SKILL.md").write_text(
        "---\nname: web-check\ndescription: v2.\n---\n\nNew body.\n", encoding="utf-8"
    )
    (source / "old-file.txt").unlink()
    installed = await registry.install_directory(source, created_by="console")

    assert installed.description == "v2."
    assert not (tmp_path / "skills" / "web-check" / "old-file.txt").exists()


async def test_install_directory_rejects_one_with_no_skill_md(tmp_path: Path) -> None:
    source = tmp_path / "incoming" / "empty"
    source.mkdir(parents=True)
    registry = SkillRegistry(tmp_path)

    with pytest.raises(InvalidSkillError):
        await registry.install_directory(source)

    assert not (tmp_path / "skills").exists()
