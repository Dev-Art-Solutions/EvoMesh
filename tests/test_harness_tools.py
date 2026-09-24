from pathlib import Path

from evomesh.harness_tools import ToolContext, tool_patch_skill


async def _patch_skill(name: str, old: str, new: str) -> str:
    return f"patched {name}"


async def test_tool_patch_skill_runs_the_bound_callable():
    ctx = ToolContext(root=Path("/"), patch_skill=_patch_skill)
    result = await tool_patch_skill(
        ctx, {"name": "some_skill", "old_text": "old text", "new_text": "new text"}
    )
    assert result == "patched some_skill"
