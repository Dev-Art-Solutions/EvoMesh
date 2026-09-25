"""The eight scenarios of the Phase 2 benchmark, each against the real runtime."""

from __future__ import annotations

import asyncio
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evomesh.cognitive_services import ModelCallRecord
from evomesh.config import EvolutionSettings, RuntimeSettings, Settings
from evomesh.contracts import AgentDefinition, AgentStatus, GoalStatus, IntentionStatus
from evomesh.coordination import DELEGATED_GOAL_KIND, WorkItem, WorkStatus
from evomesh.environment import Environment
from evomesh.events import EventType
from evomesh.improvements import (
    EVIDENCE_RUNTIME_FAULT,
    Candidate,
    ImprovementBacklog,
    ImprovementControl,
    ImprovementCoordinator,
    ImprovementScout,
    ImprovementStatus,
    ImprovementTriage,
    PriorityFactors,
    ReviewVerdict,
    WorkOutcome,
)
from evomesh.models import MockProvider, ModelUnavailableError

PLANNING_MARKER = "Break this goal into"
PLAN = "1. gather the inputs\n2. work through them\n3. write the result\n"
# The memory-compression call's own prompt, which is about notes, not a goal.
COMPRESSION_PROMPT = "Compress these notes"
STEP = "RESULT: done.\nFACT: NONE\nSTATUS: done\n"


class ScriptedProvider(MockProvider):
    """A plan for a planning prompt, a finished step for anything else."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail

    async def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        num_ctx: int | None = None,
        format: dict[str, Any] | None = None,
    ) -> str:
        self.calls.append({"prompt": prompt, "system": system, "model": model})
        if self.fail:
            raise ModelUnavailableError("scripted outage: the model server refused")
        return PLAN if PLANNING_MARKER in prompt else STEP


@dataclass
class ScenarioResult:
    key: str
    title: str
    success: bool
    llm_calls: int
    planning_calls: int
    input_chars: int
    output_chars: int
    max_prompt_chars: int
    duration_seconds: float
    by_reason: dict[str, int] = field(default_factory=dict)
    measures: dict[str, object] = field(default_factory=dict)


def _settings(root: Path, prompt_chars: int = 6000) -> Settings:
    scale = prompt_chars / 6000
    return Settings(
        data_path=root / "state.db",
        generation_path=root / "generations",
        workspace_path=root / "workspace",
        runtime=RuntimeSettings(
            cycle_seconds=3600,
            stagger_seconds=0,
            prompt_chars=prompt_chars,
            memory_chars=round(3000 * scale),
            context_chars=round(1500 * scale),
            inbox_chars=round(1000 * scale),
            beliefs_chars=round(700 * scale),
        ),
        evolution=EvolutionSettings(autonomous=False),
    )


def _summarize(
    key: str,
    title: str,
    records: tuple[ModelCallRecord, ...],
    started: float,
    success: bool,
    **measures: object,
) -> ScenarioResult:
    reasons = Counter(record.reason.value for record in records)
    return ScenarioResult(
        key=key,
        title=title,
        success=success,
        llm_calls=len(records),
        planning_calls=reasons.get("no_plan_match", 0),
        input_chars=sum(record.input_chars for record in records),
        output_chars=sum(record.output_chars for record in records),
        max_prompt_chars=max((record.input_chars for record in records), default=0),
        duration_seconds=round(time.perf_counter() - started, 3),
        by_reason=dict(sorted(reasons.items())),
        measures=dict(measures),
    )


async def _environment(root: Path, provider: MockProvider, prompt_chars: int = 6000) -> Environment:
    environment = Environment(_settings(root, prompt_chars), {"ollama": provider})
    await environment.start()
    return environment


async def _agent(environment: Environment, name: str, **fields: object) -> AgentDefinition:
    agent = AgentDefinition(name=name, purpose=name, status=AgentStatus.ACTIVE, **fields)  # type: ignore[arg-type]
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)
    return agent


# -- A. known deterministic task -------------------------------------------


async def known_task(root: Path) -> ScenarioResult:
    """The Guardian's standing sweep: a library plan, no model at all."""
    started = time.perf_counter()
    provider = ScriptedProvider()
    environment = await _environment(root, provider)
    await environment.start_agent("guardian", start_delay=3600)
    cycles = 10
    for _ in range(cycles):
        await environment.cycle_agent("guardian")
    records = environment.cognition.metrics.records
    await environment.stop()
    return _summarize(
        "A",
        "Known deterministic task (Guardian sweep x10)",
        records,
        started,
        success=len(records) == 0,
        cycles=cycles,
    )


# -- B. novel task ------------------------------------------------------------


async def novel_task(root: Path) -> ScenarioResult:
    """A goal nobody has a plan for: one planning call, then execution."""
    started = time.perf_counter()
    provider = ScriptedProvider()
    environment = await _environment(root, provider)
    agent = await _agent(environment, "Worker")
    goal = agent.mind.add_goal("Summarize the notes")
    cycles = 0
    while goal.status is not GoalStatus.DONE and cycles < 12:
        await environment.cycle_agent("Worker")
        cycles += 1
    records = environment.cognition.metrics.records
    await environment.stop()
    planning = sum(record.reason.value == "no_plan_match" for record in records)
    return _summarize(
        "B",
        "Novel task (one goal, model-planned)",
        records,
        started,
        success=goal.status is GoalStatus.DONE and planning <= 2,
        cycles=cycles,
    )


# -- C. repeated task ----------------------------------------------------------


async def repeated_task(root: Path) -> ScenarioResult:
    """A standing goal re-planned every pass until the plan is learned."""
    started = time.perf_counter()
    provider = ScriptedProvider()
    environment = await _environment(root, provider)
    agent = await _agent(environment, "Worker")
    agent.mind.add_goal("Summarize the notes", recurring=True)
    passes = 8
    cycles = 0
    while (
        sum(item.status is IntentionStatus.ACHIEVED for item in agent.mind.intentions) < passes
        and cycles < passes * 5
    ):
        await environment.cycle_agent("Worker")
        cycles += 1
    records = environment.cognition.metrics.records
    await environment.stop()
    planning = sum(record.reason.value == "no_plan_match" for record in records)
    reused = sum(item.plan.startswith("learned:") for item in agent.mind.intentions)
    return _summarize(
        "C",
        f"Repeated task ({passes} passes of one standing goal)",
        records,
        started,
        success=planning < passes and reused > 0,
        passes=passes,
        planning_calls_without_learning=passes,
        passes_on_learned_procedure=reused,
    )


# -- D. dependency DAG -----------------------------------------------------------


async def dependency_dag(root: Path) -> ScenarioResult:
    """gather <- draft <- publish: runs in dependency order, unblocked by code."""
    started = time.perf_counter()
    provider = ScriptedProvider()
    environment = await _environment(root, provider)
    agent = await _agent(environment, "Planner")
    gather = agent.mind.add_goal("gather sources")
    draft = agent.mind.add_goal("draft the report", dependency_goal_ids=[gather.id])
    publish = agent.mind.add_goal("publish the report", dependency_goal_ids=[draft.id])
    order: list[str] = []
    cycles = 0
    while publish.status is not GoalStatus.DONE and cycles < 40:
        await environment.cycle_agent("Planner")
        cycles += 1
        for goal in (gather, draft, publish):
            if goal.status is GoalStatus.DONE and goal.description not in order:
                order.append(goal.description)
    unblocked = sum(event.type is EventType.GOAL_UNBLOCKED for event in environment.events.history)
    records = environment.cognition.metrics.records
    await environment.stop()
    return _summarize(
        "D",
        "Dependency DAG (three goals, chained)",
        records,
        started,
        success=order == ["gather sources", "draft the report", "publish the report"]
        and unblocked == 2,
        completion_order=order,
        goal_unblocked_events=unblocked,
        cycles=cycles,
    )


# -- E. multi-agent -----------------------------------------------------------------


async def multi_agent(root: Path) -> ScenarioResult:
    """A task delegated by capability; the result comes back structured."""
    started = time.perf_counter()
    provider = ScriptedProvider()
    environment = await _environment(root, provider)
    sender = await _agent(environment, "Sender")
    helper = await _agent(environment, "Fetcher", capabilities=["web.fetch"])
    await _agent(environment, "Bystander", capabilities=["code.edit"])
    delegate = environment._make_delegate_work(sender.id)  # pyright: ignore[reportPrivateUsage]
    await delegate("web.fetch", "Fetch the pricing page")
    work = next(iter(environment.blackboard.work_items.values()))
    await _until(lambda: any(g.kind == DELEGATED_GOAL_KIND for g in helper.mind.goals))
    cycles = 0
    while work.status is not WorkStatus.COMPLETED and cycles < 12:
        await environment.cycle_agent("Fetcher")
        cycles += 1
    routed_by_model = sum(
        record.agent_id == sender.id for record in environment.cognition.metrics.records
    )
    records = environment.cognition.metrics.records
    result_fact = environment.blackboard.fact(f"work.{work.id}.result")
    await environment.stop()
    return _summarize(
        "E",
        "Multi-agent task (delegated by capability)",
        records,
        started,
        success=work.status is WorkStatus.COMPLETED
        and work.assigned_agent_id == helper.id
        and result_fact is not None,
        assigned_to="Fetcher",
        routing_model_calls=routed_by_model,
        helper_cycles=cycles,
    )


# -- F. repeated failure ----------------------------------------------------------------


async def repeated_failure(root: Path) -> ScenarioResult:
    """A model that keeps refusing: bounded, one stall, one help request."""
    started = time.perf_counter()
    provider = ScriptedProvider(fail=True)
    environment = await _environment(root, provider)
    await environment.start_agent("guardian", start_delay=3600)
    agent = await _agent(environment, "Unlucky")
    goal = agent.mind.add_goal("Write the weather report")
    cycles = 12
    for _ in range(cycles):
        await environment.cycle_agent("Unlucky")
    stalls = sum(event.type is EventType.AGENT_STALLED for event in environment.events.history)
    assistance = [
        item for item in environment.blackboard.work_items.values() if item.type == "assistance"
    ]
    records = environment.cognition.metrics.records
    await environment.stop()
    return _summarize(
        "F",
        f"Repeated failure (model down, {cycles} cycles)",
        records,
        started,
        success=stalls >= 1 and len(assistance) == stalls and goal.status is not GoalStatus.ACTIVE,
        stall_events=stalls,
        assistance_work_items=len(assistance),
        final_goal_status=goal.status.value,
        failed_calls=sum(record.status.value == "failed" for record in records),
    )


# -- G. oversized memory ----------------------------------------------------------------------


async def oversized_memory(root: Path, prompt_chars: int, label: str) -> ScenarioResult:
    """200 KB of memory and context: every prompt stays within its budget."""
    started = time.perf_counter()
    provider = ScriptedProvider()
    environment = await _environment(root, provider, prompt_chars=prompt_chars)
    agent = await _agent(environment, "Hoarder")
    memory = environment.memory_for(agent)
    await memory.ensure()
    filler = "".join(
        f"- 2026-09-{day % 28 + 1:02d} unrelated note {day} " + "x" * 180 + "\n"
        for day in range(1000)
    )
    memory.memory_path.write_text("# Memory\n\n## Recent\n" + filler, encoding="utf-8")
    memory.context_path.write_text("# Context\n\n" + filler, encoding="utf-8")
    goal = agent.mind.add_goal("Summarize the notes")
    for _ in range(4):
        await environment.cycle_agent("Hoarder")
    prompts = [str(call["prompt"]) for call in provider.calls]
    records = environment.cognition.metrics.records
    await environment.stop()
    largest = max((len(prompt) for prompt in prompts), default=0)
    return _summarize(
        f"G-{label}",
        f"Oversized memory (200 KB) at {label}",
        records,
        started,
        success=bool(prompts)
        and largest <= prompt_chars
        and all(
            goal.description in prompt
            for prompt in prompts
            if not prompt.startswith(COMPRESSION_PROMPT)
        ),
        prompt_budget_chars=prompt_chars,
        largest_prompt_chars=largest,
        memory_on_disk_chars=len(filler),
    )


# -- H. improvement lifecycle ------------------------------------------------------------------


class _Executor:
    def __init__(self) -> None:
        self.finished: set[str] = set()

    def outcome(self, item: WorkItem) -> WorkOutcome | None:
        return WorkOutcome.COMPLETED if item.id in self.finished else None


async def improvement_lifecycle(root: Path) -> ScenarioResult:
    """One logged fault from evidence to verified, and nothing else worked."""
    started = time.perf_counter()
    backlog = ImprovementBacklog()

    async def save() -> None:
        return None

    control = ImprovementControl(
        backlog,
        ImprovementCoordinator(backlog),
        ImprovementTriage(),
        ImprovementScout(),
        save=save,
    )
    fault = Candidate(
        ref="fault:environment.status:KeyError",
        kind=EVIDENCE_RUNTIME_FAULT,
        title="Fix KeyError in environment.status",
        problem="KeyError raised at environment:1480",
        component="environment",
        evidence={"count": 4},
        factors=PriorityFactors(impact=2, urgency=2.5, recurrence=4),
        observations=3,
    )
    states: list[str] = []
    await control.sync([fault], {fault.ref})
    item = control.choose()
    assert item is not None
    states.append(item.status.value)
    work = await control.begin(item, objective=item.title, generation=1, route=lambda _: "evolver")
    assert work is not None
    await control.record_review(item.id, ReviewVerdict.COMPLETE)
    await control.record_validation(item.id, passed=True)
    executor = _Executor()
    executor.finished.add(work.id)
    await control.settle(executor, present=set())  # the fault stopped being logged
    states.append(item.status.value)
    for _ in range(3):
        await control.sync([], set())
        states.append(item.status.value)
    unrelated = [entry for entry in backlog.items.values() if entry is not item]
    return _summarize(
        "H",
        "Improvement lifecycle (logged fault -> verified)",
        (),
        started,
        success=item.status is ImprovementStatus.VERIFIED and not unrelated,
        states=states,
        unrelated_improvements=len(unrelated),
    )


async def _until(condition: Callable[[], bool], within: float = 5.0) -> None:
    for _ in range(round(within / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)


Scenario = Callable[[Path], Awaitable[ScenarioResult]]

SCENARIOS: tuple[tuple[str, Scenario], ...] = (
    ("A", known_task),
    ("B", novel_task),
    ("C", repeated_task),
    ("D", dependency_dag),
    ("E", multi_agent),
    ("F", repeated_failure),
    ("G-4k", lambda root: oversized_memory(root, 6000, "4k")),
    ("G-8k", lambda root: oversized_memory(root, 12000, "8k")),
    ("G-16k", lambda root: oversized_memory(root, 24000, "16k")),
    ("H", improvement_lifecycle),
)


async def run_all(root: Path | None = None) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for key, scenario in SCENARIOS:
        if root is None:
            with tempfile.TemporaryDirectory(prefix=f"evomesh-bench-{key}-") as scratch:
                results.append(await scenario(Path(scratch)))
        else:
            folder = root / key
            folder.mkdir(parents=True, exist_ok=True)
            results.append(await scenario(folder))
    return results
