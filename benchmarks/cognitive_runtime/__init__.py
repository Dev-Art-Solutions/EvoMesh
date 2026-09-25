"""Cognitive runtime benchmark (Phase 2, section 15).

Each scenario drives the real runtime -- Environment, AgentRuntime, BDI,
GoalManager, rules, procedures, coordination, ImprovementControl -- against a
scripted local provider, so the counts are reproducible and every model call
is the runtime's own, read from CognitiveModelService telemetry. The provider
is scripted; the decisions about *whether* to call it are not.

Run: ``python -m benchmarks.cognitive_runtime`` (writes the report).
"""

from benchmarks.cognitive_runtime.scenarios import SCENARIOS, ScenarioResult, run_all

__all__ = ["SCENARIOS", "ScenarioResult", "run_all"]
