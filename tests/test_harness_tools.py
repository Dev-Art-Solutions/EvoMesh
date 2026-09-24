from pathlib import Path

from evomesh.harness_tools import ToolContext, tool_patch_skill, tool_write, valid_id


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
