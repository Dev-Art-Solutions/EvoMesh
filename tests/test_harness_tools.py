from pathlib import Path

import pytest

from evomesh.harness_tools import (
    ToolContext,
    ToolDenied,
    tool_edit,
    tool_fetch,
    tool_patch_skill,
    tool_write,
    valid_id,
)


async def _patch_skill(name: str, old: str, new: str) -> str:
    return f"patched {name}"


async def test_tool_patch_skill_runs_the_bound_callable():
    ctx = ToolContext(root=Path("/"), patch_skill=_patch_skill)
    result = await tool_patch_skill(
        ctx, {"name": "some_skill", "old_text": "old text", "new_text": "new text"}
    )
    assert result == "patched some_skill"


async def test_tool_write_creates_the_file_it_is_asked_to(tmp_path):
    ctx = ToolContext(root=tmp_path, patch_skill=_patch_skill, allow_write=True)
    await tool_write(ctx, {"path": "hello.txt", "content": "hi"})
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hi"


def test_valid_id_accepts_a_wellformed_id():
    assert valid_id("model") is True


async def test_tool_fetch_is_denied_without_a_configured_fetcher(tmp_path):
    ctx = ToolContext(root=tmp_path)
    with pytest.raises(ToolDenied):
        await tool_fetch(ctx, {"url": "https://example.com"})


async def test_tool_edit_replaces_the_exact_string_it_is_asked_to(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    ctx = ToolContext(root=tmp_path, allow_write=True)
    await tool_edit(ctx, {"path": "sample.txt", "old": "two", "new": "TWO"})
    assert target.read_text(encoding="utf-8") == "one\nTWO\nthree\n"
