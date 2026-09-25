"""B-020: structured beliefs are the state; Markdown memory is a projection."""

from __future__ import annotations

from pathlib import Path

from evomesh.cognition import CycleContext
from evomesh.contracts import AgentDefinition, Belief
from evomesh.memory import AgentMemory, MemoryBudget
from evomesh.models import MockProvider


async def test_a_belief_wins_over_the_memory_line_it_supersedes(tmp_path: Path) -> None:
    definition = AgentDefinition(name="Watcher", purpose="Watch the provider")
    definition.mind.add_goal("Report whether the provider is ready")
    definition.mind.revise([Belief(key="provider.ready", statement="the model provider is ready")])
    memory = AgentMemory(tmp_path, definition)
    await memory.ensure()
    memory.memory_path.write_text(
        "# Memory\n\n## Recent\n"
        "- provider.ready was false when the provider crashed\n"
        "- the model provider is ready\n"
        "- the provider restarts itself after an update\n",
        encoding="utf-8",
    )
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
    )

    prompt = await context.build_prompt(
        "Report the provider state", relevant_belief_keys=("provider.ready",)
    )

    assert "the model provider is ready" in prompt, "the belief itself is kept"
    assert "was false when the provider crashed" not in prompt, "superseded by the belief"
    assert prompt.count("the model provider is ready") == 1, "not repeated from memory"
    assert "restarts itself after an update" in prompt, "unrelated memory stays"
    assert "BELIEFS win where they differ" in prompt
