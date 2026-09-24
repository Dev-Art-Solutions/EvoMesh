"""Memory is file-backed agent memory: per-agent store plus the shared world
context every agent can read."""

from __future__ import annotations

from evomesh.memory import WorldContext


async def test_world_context_write_then_read_round_trips_its_sections(tmp_path) -> None:
    world = WorldContext(tmp_path)
    await world.write({"state": "Open: long 10 ETH"})

    text = await world.read()

    assert "Open: long 10 ETH" in text
