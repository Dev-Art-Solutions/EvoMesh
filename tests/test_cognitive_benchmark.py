"""The Phase 2 benchmark as a quality gate: its claims hold on every run."""

from __future__ import annotations

from pathlib import Path

from benchmarks.cognitive_runtime import run_all
from evomesh.contracts import AgentDefinition
from evomesh.memory import AgentMemory, MemoryBudget


async def test_every_benchmark_scenario_holds(tmp_path: Path) -> None:
    results = {result.key: result for result in await run_all(tmp_path)}

    failed = [key for key, result in results.items() if not result.success]
    assert not failed, failed
    assert results["A"].llm_calls == 0, "known work needs no model"
    assert results["C"].planning_calls < results["C"].measures["passes"]  # type: ignore[operator]
    assert results["E"].measures["routing_model_calls"] == 0
    for key in ("G-4k", "G-8k", "G-16k"):
        result = results[key]
        assert result.max_prompt_chars <= result.measures["prompt_budget_chars"]  # type: ignore[operator]
    assert results["H"].measures["unrelated_improvements"] == 0


async def test_memory_compaction_never_sends_more_than_the_prompt_budget(
    tmp_path: Path,
) -> None:
    """Found by the benchmark: 200 KB of memory went to the model in one
    compression prompt, and the file compacted again on every cycle."""
    budget = MemoryBudget(memory_chars=3000, prompt_chars=6000)
    memory = AgentMemory(tmp_path, AgentDefinition(name="Hoarder", purpose="p"), budget)
    await memory.ensure()
    lines = "".join(f"- entry {index} " + "x" * 200 + "\n" for index in range(1000))
    memory.memory_path.write_text("# Memory\n\n## Recent\n" + lines, encoding="utf-8")
    seen: list[str] = []

    async def summarizer(text: str) -> str:
        seen.append(text)
        return "- compressed"

    assert await memory.compact(summarizer)

    assert len(seen) == 1 and len(seen[0]) <= budget.prompt_chars
    after = memory.memory_path.read_text(encoding="utf-8")
    assert len(after) <= budget.memory_chars
    assert "dropped unsummarized" in after
    assert not await memory.compact(summarizer), "within budget: nothing to do next cycle"
