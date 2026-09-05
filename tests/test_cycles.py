import asyncio
import logging
from pathlib import Path

import pytest

from evomesh.behaviors import STAGE_REPAIR, EvolverBehavior, GuardianBehavior, _extract_rationale
from evomesh.cognition import CycleContext, parse_cycle_reply, strip_reasoning
from evomesh.config import EvolutionSettings, RuntimeSettings, Settings
from evomesh.console import ConsoleChannel
from evomesh.contracts import AgentDefinition, AgentPhase, AgentStatus, GoalStatus, Message
from evomesh.environment import Environment
from evomesh.evolution import (
    IGNORED_NAMES,
    PYTEST_TEMP_DIR,
    CandidateRepairer,
    CandidateValidator,
    CandidateWorkspace,
    EnvironmentEvolver,
    Generation,
    GenerationStatus,
    PlanNode,
    ValidationResult,
)
from evomesh.git import GitRepository
from evomesh.memory import AgentMemory, MemoryBudget
from evomesh.models import MockProvider

from .fakes import (
    FakeHarness,
    ScriptedValidator,
    StubRepairer,
    UndoingRepairer,
    failing,
    passing,
)

PLAN_REPLY = "1. read the index file\n2. summarise what it lists\n"
CYCLE_REPLY = (
    "RESULT: Indexed 12 papers and found 3 relevant ones.\n"
    "FACT: The papers live under data/papers.\n"
    "STATUS: done\n"
)
# The first model call plans, every later one executes a step.
BDI_REPLIES = [PLAN_REPLY, CYCLE_REPLY]


def settings_for(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        data_path=tmp_path / "state.db",
        generation_path=tmp_path / "generations",
        workspace_path=tmp_path / "workspace",
        runtime=RuntimeSettings(cycle_seconds=3600, stagger_seconds=0),
        **overrides,  # type: ignore[arg-type]
    )


async def test_cycle_advances_the_goal_and_writes_memory_and_context(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider(list(BDI_REPLIES))})
    await environment.start()
    agent = AgentDefinition(
        name="Researcher", purpose="Summarize papers", status=AgentStatus.ACTIVE
    )
    agent.mind.add_goal("Index the paper folder")
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)

    outcome = await environment.cycle_agent("Researcher")

    assert outcome.worked
    assert "Indexed 12 papers" in outcome.summary
    assert outcome.step == "read the index file"  # step 1 of the committed plan

    memory = environment.memory_for(agent)
    assert "The papers live under data/papers." in await memory.read_memory()
    context = await memory.read_context()
    assert "Index the paper folder" in context
    assert "read the index file" in context

    goal = agent.mind.goals[0]
    assert goal.status is GoalStatus.ACTIVE
    assert goal.notes[-1] == "read the index file"
    assert environment.runtime_states()[agent.id].cycles == 1
    await environment.stop()


async def test_a_goal_never_closes_on_its_very_first_cycle(tmp_path: Path) -> None:
    """A 0.5B model answers DONE: yes the first time it reads any goal."""
    reply = "STEP: done\nRESULT: finished\nFACT: NONE\nDONE: yes\n"
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider([reply])})
    await environment.start()
    agent = AgentDefinition(name="Once", purpose="Do a thing", status=AgentStatus.ACTIVE)
    agent.mind.add_goal("Do the thing")
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)

    await environment.cycle_agent("Once")
    assert agent.mind.goals[0].status is not GoalStatus.DONE
    assert agent.mind.goals[0].is_open

    await environment.cycle_agent("Once")
    assert agent.mind.goals[0].status is GoalStatus.DONE
    await environment.stop()


async def test_a_recurring_goal_survives_being_declared_done(tmp_path: Path) -> None:
    reply = "STEP: done\nRESULT: finished\nFACT: NONE\nDONE: yes\n"
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider([reply])})
    await environment.start()
    agent = AgentDefinition(name="Standing", purpose="Watch", status=AgentStatus.ACTIVE)
    standing = agent.mind.add_goal("Keep watching", recurring=True)
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)

    for _ in range(3):
        await environment.cycle_agent("Standing")

    assert standing.status is not GoalStatus.DONE
    assert standing.is_open
    assert environment.runtime_states()[agent.id].goal == "Keep watching"
    await environment.stop()


async def test_a_confirmed_agent_keeps_working_on_its_purpose(tmp_path: Path) -> None:
    """The purpose is an ongoing job, so the agent must not idle after one cycle."""
    from evomesh.architect import ArchitectInterview

    reply = "STEP: read notes\nRESULT: read them\nFACT: NONE\nDONE: yes\n"
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider([reply])})
    await environment.start()
    interview = ArchitectInterview()
    interview.begin("summarize my markdown notes every week")
    definition = interview.confirm()
    await environment.register_agent(definition)
    await environment.start_agent(definition.id, start_delay=3600)

    for _ in range(3):
        await environment.cycle_agent(definition.name)

    assert definition.mind.next_goal() is not None
    assert environment.runtime_states()[definition.id].goal
    await environment.stop()


async def test_a_model_failure_costs_an_attempt_and_shows_up_as_error(tmp_path: Path) -> None:
    class Broken(MockProvider):
        async def generate(
            self,
            prompt: str,
            *,
            system: str = "",
            model: str | None = None,
            num_ctx: int | None = None,
        ) -> str:
            raise RuntimeError("model is down")

    environment = Environment(settings_for(tmp_path), {"ollama": Broken()})
    await environment.start()
    agent = AgentDefinition(name="Fragile", purpose="Try", status=AgentStatus.ACTIVE)
    agent.mind.add_goal("Attempt something")
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)

    outcome = await environment.cycle_agent("Fragile")
    assert outcome.error is not None
    assert agent.mind.goals[0].attempts == 1
    assert environment.runtime_states()[agent.id].phase is AgentPhase.ERROR
    await environment.stop()


async def test_system_agents_boot_running_with_a_goal_each(tmp_path: Path) -> None:
    environment = Environment(
        settings_for(tmp_path, evolution=EvolutionSettings(autonomous=False)),
        {"ollama": MockProvider(list(BDI_REPLIES))},
    )
    await environment.start(start_agent_loops=True)
    states = environment.runtime_states()

    for agent_id in ("architect", "guardian", "evaluator"):
        assert states[agent_id].phase is not AgentPhase.OFFLINE, agent_id
        assert environment.registry.get(agent_id).mind.next_goal() is not None, agent_id
    # autonomous evolution is off, so the Evolver must be visibly parked, not
    # silently labelled active the way it used to be.
    assert states["evolver"].phase is AgentPhase.OFFLINE
    assert states["evolver"].last_error
    await environment.stop()


async def test_guardian_perceives_the_mesh_as_it_is_now_not_as_it_was_at_boot(
    tmp_path: Path,
) -> None:
    """Percepts are gathered per cycle; a staggered boot used to freeze the roster."""
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider(list(BDI_REPLIES))})
    await environment.start(start_agent_loops=True)

    await environment.cycle_agent("guardian")

    degraded = environment.registry.get("guardian").mind.belief("mesh.degraded")
    assert degraded is not None
    assert degraded.statement.startswith("all ")
    assert "guardian" not in degraded.statement  # it never reports itself
    await environment.stop()


async def test_guardian_names_an_agent_that_stops_after_boot(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider(list(BDI_REPLIES))})
    await environment.start(start_agent_loops=True)
    await environment.stop_agent("evaluator")

    await environment.cycle_agent("guardian")

    degraded = environment.registry.get("guardian").mind.belief("mesh.degraded")
    assert degraded is not None
    # The name, never the raw agent id.
    assert "Evaluator" in degraded.statement
    await environment.stop()


async def test_guardian_wants_to_investigate_only_when_the_world_changed(
    tmp_path: Path,
) -> None:
    """Option generation: a new desire arises from a belief change, then clears."""
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider(list(BDI_REPLIES))})
    await environment.start(start_agent_loops=True)
    guardian = environment.registry.get("guardian")
    await environment.cycle_agent("guardian")
    settled = len(guardian.mind.goals)

    await environment.stop_agent("evaluator")
    await environment.cycle_agent("guardian")

    investigation = [
        goal for goal in guardian.mind.goals if goal.description.startswith("Investigate why")
    ]
    assert investigation, "a degraded mesh should generate a desire to investigate"
    assert len(guardian.mind.goals) == settled + 1
    # It outranks the standing sweep, so the Guardian commits to it next.
    assert guardian.mind.next_goal() is investigation[0]

    # Perceiving the same degradation again must not pile up a second goal.
    await environment.cycle_agent("guardian")
    assert len(guardian.mind.goals) == settled + 1

    # Once the mesh recovers the investigation is discharged, not left open.
    await environment.start_agent("evaluator", start_delay=3600)
    await environment.cycle_agent("guardian")
    await environment.cycle_agent("guardian")
    assert investigation[0].status is GoalStatus.DONE
    await environment.stop()


async def test_state_explains_why_an_agent_is_not_running(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    stranded = AgentDefinition(
        name="Stranded", purpose="Nowhere", provider="missing", status=AgentStatus.ACTIVE
    )
    await environment.register_agent(stranded)
    await environment.start_all()

    state = environment.runtime_states()[stranded.id]
    assert state.phase is AgentPhase.OFFLINE
    assert "missing" in (state.last_error or "")
    await environment.stop()


async def test_stopping_an_agent_reports_offline_rather_than_active(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start(start_agent_loops=True)
    await environment.stop_agent("guardian")

    state = environment.runtime_states()["guardian"]
    assert state.phase is AgentPhase.OFFLINE
    assert environment.registry.get("guardian").status is AgentStatus.STOPPED
    console = ConsoleChannel(environment)
    assert "stopped/offline" in await console.route("/agents")
    await environment.stop()


async def test_prompt_carries_memory_and_respects_the_budget(tmp_path: Path) -> None:
    definition = AgentDefinition(name="Budgeted", purpose="Stay small")
    definition.mind.add_goal("Remember things")
    budget = MemoryBudget(memory_chars=300, context_chars=200, inbox_chars=100, prompt_chars=900)
    memory = AgentMemory(tmp_path / "workspace", definition, budget)
    await memory.ensure()
    for index in range(60):
        await memory.remember(f"fact number {index} about the corpus")

    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=budget,
    )
    prompt = await context.build_prompt("do the next step")

    assert len(prompt) <= budget.prompt_chars
    assert "Remember things" in prompt
    assert "fact number 59" in prompt  # the newest memory survives the trim
    assert "fact number 0 " not in prompt


async def test_memory_compaction_keeps_the_newest_entries(tmp_path: Path) -> None:
    definition = AgentDefinition(name="Verbose", purpose="Write a lot")
    memory = AgentMemory(
        tmp_path / "workspace", definition, MemoryBudget(memory_chars=400)
    )
    await memory.ensure()
    for index in range(40):
        await memory.remember(f"observation {index}")

    assert await memory.compact() is True
    text = memory.memory_path.read_text(encoding="utf-8")
    assert "## Summary" in text
    assert "observation 39" in text
    assert "observation 0 " not in text
    assert len(text) < 1200


def test_cycle_reply_parsing_survives_reasoning_and_markdown() -> None:
    reply = parse_cycle_reply(
        "<think>I should check the file first, then decide.</think>\n"
        "**STEP:** open the config\n"
        "**RESULT:** The port is wrong.\n"
        "FACT: the port is 8765\n"
        "DONE: yes"
    )
    assert reply.step == "open the config"
    assert reply.result == "The port is wrong."
    assert reply.fact == "the port is 8765"
    assert reply.done is True


def test_reasoning_is_stripped_when_only_the_closing_tag_comes_back() -> None:
    # Ollama chat templates already hold the opening tag, so a reasoning model
    # answers with the thinking itself and ends it with a bare </think>.
    text = strip_reasoning(
        "The user asks if I am running. I should keep it short.\n"
        "</think>\n\n"
        "Yes, I am operational."
    )
    assert text == "Yes, I am operational."


def test_reasoning_is_stripped_when_the_block_is_never_closed() -> None:
    text = strip_reasoning("Yes, I am operational.\n<think>Now let me second-guess that")
    assert text == "Yes, I am operational."


def test_cycle_reply_survives_an_unopened_reasoning_block() -> None:
    reply = parse_cycle_reply(
        "The goal says to check the port, so I will open the config.\n"
        "</think>\n"
        "STEP: open the config\n"
        "RESULT: The port is wrong.\n"
        "FACT: the port is 8765\n"
        "DONE: no"
    )
    assert reply.step == "open the config"
    assert reply.fact == "the port is 8765"
    assert "goal says" not in reply.result


def test_unformatted_model_output_is_still_treated_as_work() -> None:
    reply = parse_cycle_reply("I looked at the folder and it is empty.")
    assert "folder" in reply.result
    assert reply.done is False


async def test_guardian_reports_degraded_provider_without_a_model(tmp_path: Path) -> None:
    definition = AgentDefinition(name="Guardian", purpose="Watch")
    definition.mind.add_goal("Watch the mesh", recurring=True)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
        services={"provider_health": (False, "Ollama is not running"), "runtime_states": {}},
    )

    outcome = await GuardianBehavior().cycle(context)

    assert "Ollama is not running" in outcome.summary
    assert outcome.worked


class StubValidator:
    def __init__(self, passed: bool = True) -> None:
        self.passed = passed
        self.calls = 0

    async def validate(self, generation: object) -> ValidationResult:
        self.calls += 1
        return ValidationResult(
            passed=self.passed, commands=[{"command": "stub", "exit_code": 0}]
        )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("ACTIVE = True\n", encoding="utf-8")
    return root


async def test_evolver_pipeline_advances_one_stage_per_cycle(
    tmp_path: Path, project: Path
) -> None:
    from evomesh.storage import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    validator = StubValidator()
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"),
        repository,
        MockProvider(),
        validator,  # type: ignore[arg-type]
    )
    definition = AgentDefinition(name="Environment Evolver", purpose="Evolve")
    definition.mind.add_goal("Improve health reporting", recurring=True)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
        services={"evolver": evolver, "harness": FakeHarness([MUTATION])},
    )
    behavior = EvolverBehavior(auto_validate=True)

    plan = await behavior.cycle(context)
    assert "opened candidate generation" in plan.summary
    assert (await evolver.pipeline_state())["stage"] == "propose"

    propose = await behavior.cycle(context)
    assert "src/app.py" in propose.summary.replace("\\", "/")
    generation = evolver.candidate(2)
    assert (generation.path / "src" / "app.py").read_text(encoding="utf-8") == "ACTIVE = False\n"
    # the live tree is untouched
    assert (project / "src" / "app.py").read_text(encoding="utf-8") == "ACTIVE = True\n"

    validated = await behavior.cycle(context)
    assert validator.calls == 1
    assert "passed" in validated.summary

    report = await behavior.cycle(context)
    assert report.phase is AgentPhase.WAITING_HUMAN
    assert (await evolver.pipeline_state())["stage"] == "await-human"

    # It parks instead of opening a second candidate nobody has reviewed.
    parked = await behavior.cycle(context)
    assert parked.phase is AgentPhase.WAITING_HUMAN
    assert len(evolver.workspace.supervisor.candidates()) == 1


async def test_a_plan_is_drafted_reviewed_split_and_worked_item_by_item(
    tmp_path: Path, project: Path
) -> None:
    """The full auto_plan path: draft -> evaluate -> decompose (into three,
    not two, children -- nothing forces a binary split) -> propose/validate
    once per leaf, in the order the leaves were discovered.
    """
    from evomesh.storage import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    validator = StubValidator()
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"),
        repository,
        MockProvider(),
        validator,  # type: ignore[arg-type]
    )
    definition = AgentDefinition(name="Environment Evolver", purpose="Evolve")
    definition.mind.add_goal("Improve health reporting", recurring=True)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    batches = [
        [("docs/evolution/plans/plan.md", "Split health reporting into three parts.")],
        [("docs/evolution/plans/plan.eval.md", "Looks groundable.\nVERDICT: approve\n")],
        [
            (
                "docs/evolution/plans/nodes/root-1.md",
                "- item A :: change alpha\n"
                "- item B :: change beta\n"
                "- item C :: change gamma\n",
            )
        ],
        [("docs/evolution/plans/nodes/root-1.1.md", "LEAF\n")],
        [("docs/evolution/plans/nodes/root-1.2.md", "LEAF\n")],
        [("docs/evolution/plans/nodes/root-1.3.md", "LEAF\n")],
        [("src/app.py", "ACTIVE = False\n")],
        [("src/other.py", "OTHER = 1\n")],
        [("src/third.py", "THIRD = 2\n")],
    ]
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
        services={"evolver": evolver, "harness": FakeHarness(batches)},
    )
    behavior = EvolverBehavior(auto_validate=True, auto_plan=True)

    await behavior.cycle(context)  # plan -> draft
    assert (await evolver.pipeline_state())["stage"] == "draft"

    await behavior.cycle(context)  # draft -> evaluate
    assert (await evolver.pipeline_state())["stage"] == "evaluate"

    await behavior.cycle(context)  # evaluate -> decompose
    state = await evolver.pipeline_state()
    assert state["stage"] == "decompose"
    assert state["plan_queue"] == ["root-1"]
    generation = evolver.candidate(int(state["generation"]))
    root = evolver.current_plan_root(generation)
    assert root is not None
    assert root.approved is True

    await behavior.cycle(context)  # decompose root-1 -> three children, not two
    state = await evolver.pipeline_state()
    assert state["plan_queue"] == ["root-1.1", "root-1.2", "root-1.3"]
    generation = evolver.candidate(int(state["generation"]))
    root_1 = evolver.plan_node(generation, "root-1")
    assert root_1 is not None
    assert root_1.kind == "split"

    await behavior.cycle(context)  # decompose root-1.1 -> leaf
    await behavior.cycle(context)  # decompose root-1.2 -> leaf
    await behavior.cycle(context)  # decompose root-1.3 -> leaf
    state = await evolver.pipeline_state()
    assert state["stage"] == "propose"
    assert state["work_items"] == ["root-1.1", "root-1.2", "root-1.3"]

    for expected_file, remaining in (
        ("src/app.py", ["root-1.2", "root-1.3"]),
        ("src/other.py", ["root-1.3"]),
        ("src/third.py", []),
    ):
        propose = await behavior.cycle(context)
        assert expected_file in propose.summary.replace("\\", "/")
        state = await evolver.pipeline_state()
        assert state["work_items"] == remaining
        assert state["stage"] == "validate"
        validated = await behavior.cycle(context)
        assert "passed" in validated.summary
        state = await evolver.pipeline_state()
        assert state["stage"] == ("propose" if remaining else "report")

    report = await behavior.cycle(context)
    assert report.phase is AgentPhase.WAITING_HUMAN
    generation = evolver.candidate(int((await evolver.pipeline_state())["generation"]))
    leaves = {node.id for node in generation.plan if node.kind == "leaf"}
    assert leaves == {"root-1.1", "root-1.2", "root-1.3"}


async def test_a_stuck_decompose_node_falls_back_to_a_leaf_instead_of_losing_the_queue(
    tmp_path: Path, project: Path
) -> None:
    """Found live: generation 66 split a plan into more than a dozen work
    items, then one decompose job at the end of the queue answered without
    writing its node file -- and the whole generation was discarded, losing
    every sibling already split. A stuck node should fall back to a leaf and
    let the rest of the queue carry on instead.
    """
    from evomesh.storage import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"),
        repository,
        MockProvider(),
        StubValidator(),  # type: ignore[arg-type]
    )
    definition = AgentDefinition(name="Environment Evolver", purpose="Evolve")
    definition.mind.add_goal("Improve health reporting", recurring=True)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    batches = [
        [("docs/evolution/plans/plan.md", "Split health reporting into three parts.")],
        [("docs/evolution/plans/plan.eval.md", "Looks groundable.\nVERDICT: approve\n")],
        [
            (
                "docs/evolution/plans/nodes/root-1.md",
                "- item A :: change alpha\n- item B :: change beta\n",
            )
        ],
        [("docs/evolution/plans/nodes/root-1.1.md", "LEAF\n")],
        NOTHING,  # root-1.2's decompose job answers without writing anything
    ]
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
        services={"evolver": evolver, "harness": FakeHarness(batches)},
    )
    behavior = EvolverBehavior(auto_validate=True, auto_plan=True)

    await behavior.cycle(context)  # plan -> draft
    await behavior.cycle(context)  # draft -> evaluate
    await behavior.cycle(context)  # evaluate -> decompose
    await behavior.cycle(context)  # decompose root-1 -> root-1.1, root-1.2
    await behavior.cycle(context)  # decompose root-1.1 -> leaf
    stuck = await behavior.cycle(context)  # decompose root-1.2 -> answers, writes nothing

    # The old behaviour discarded the generation here (D5's "nothing to
    # validate" path) and every already-split sibling went with it.
    assert stuck.phase is not AgentPhase.WAITING_HUMAN
    state = await evolver.pipeline_state()
    assert state["stage"] == "propose"
    assert state["work_items"] == ["root-1.1", "root-1.2"]
    generation = evolver.candidate(int(state["generation"]))
    stuck_node = evolver.plan_node(generation, "root-1.2")
    assert stuck_node is not None
    assert stuck_node.kind == "leaf"
    assert stuck_node.status == "leaf"


async def test_a_rejected_plan_is_superseded_not_discarded(
    tmp_path: Path, project: Path
) -> None:
    """A reject sends the pipeline back to draft; the rejected root stays on
    the generation, marked superseded, next to the reason it was rejected --
    the whole point of the tree over one end-of-job rationale sentence.
    """
    from evomesh.storage import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"),
        repository,
        MockProvider(),
        StubValidator(),  # type: ignore[arg-type]
    )
    definition = AgentDefinition(name="Environment Evolver", purpose="Evolve")
    definition.mind.add_goal("Improve health reporting", recurring=True)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    batches = [
        [("docs/evolution/plans/plan.md", "v1: too vague on purpose")],
        [
            (
                "docs/evolution/plans/plan.eval.md",
                "Too vague to split.\nVERDICT: reject: too vague\n",
            )
        ],
        [("docs/evolution/plans/plan.md", "v2: names the module and the change")],
        [("docs/evolution/plans/plan.eval.md", "Groundable now.\nVERDICT: approve\n")],
    ]
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
        services={"evolver": evolver, "harness": FakeHarness(batches)},
    )
    behavior = EvolverBehavior(auto_validate=True, auto_plan=True)

    await behavior.cycle(context)  # plan -> draft
    await behavior.cycle(context)  # draft -> evaluate (v1)
    await behavior.cycle(context)  # evaluate -> draft (rejected)
    state = await evolver.pipeline_state()
    assert state["stage"] == "draft"
    assert state["plan_revision"] == 1
    generation = evolver.candidate(int(state["generation"]))
    first = next(node for node in generation.plan if node.id == "root-1")
    assert first.status == "superseded"
    assert first.approved is False
    assert "too vague" in first.eval_reasoning

    await behavior.cycle(context)  # draft (v2) -> evaluate
    await behavior.cycle(context)  # evaluate -> decompose (approved)
    state = await evolver.pipeline_state()
    assert state["stage"] == "decompose"
    generation = evolver.candidate(int(state["generation"]))
    second = evolver.current_plan_root(generation)
    assert second is not None
    assert second.id == "root-2"
    assert second.approved is True


async def test_a_leaf_repair_does_not_disturb_the_rest_of_the_queue(
    tmp_path: Path, project: Path
) -> None:
    """Repair is scoped to whatever validation just reported -- it already
    ignores which leaf produced the failure -- and the remaining work items
    survive the repair loop untouched, in `state`, exactly as before.
    """
    from evomesh.storage import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    validator = ScriptedValidator([failing("uv run pytest", "boom"), passing(), passing()])
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"),
        repository,
        MockProvider(),
        validator,  # type: ignore[arg-type]
    )
    definition = AgentDefinition(name="Environment Evolver", purpose="Evolve")
    definition.mind.add_goal("Improve health reporting", recurring=True)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    generation = await evolver.create_candidate("improve health reporting")
    generation.plan = [
        PlanNode(id="leafA", title="item A", reasoning="change alpha", kind="leaf", status="leaf"),
        PlanNode(id="leafB", title="item B", reasoning="change beta", kind="leaf", status="leaf"),
    ]
    evolver.workspace.supervisor.record_candidate(generation)
    await evolver.set_pipeline_state(
        {
            "stage": "propose",
            "generation": generation.number,
            "objective": "improve health reporting",
            "path": str(generation.path),
            "work_items": ["leafA", "leafB"],
        }
    )
    batches = [MUTATION, REPAIR, [("src/other.py", "OTHER = 1\n")]]
    answers = ["RATIONALE: a", "RATIONALE: fixed it", "RATIONALE: b"]
    harness = FakeHarness(batches, answers=answers)
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
        services={"evolver": evolver, "harness": harness},
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2)

    await behavior.cycle(context)  # propose leafA
    state = await evolver.pipeline_state()
    assert state["work_items"] == ["leafB"]

    validated = await behavior.cycle(context)  # validate: fails
    assert "failed" in validated.summary
    state = await evolver.pipeline_state()
    assert state["stage"] == "repair"
    assert state["work_items"] == ["leafB"]  # untouched by the failure

    await behavior.cycle(context)  # repair
    validated = await behavior.cycle(context)  # validate: passes, leafB still queued
    assert "passed" in validated.summary
    state = await evolver.pipeline_state()
    assert state["stage"] == "propose"
    assert state["work_items"] == ["leafB"]

    await behavior.cycle(context)  # propose leafB
    state = await evolver.pipeline_state()
    assert state["work_items"] == []
    validated = await behavior.cycle(context)  # validate: passes, nothing left
    state = await evolver.pipeline_state()
    assert state["stage"] == "report"


async def test_an_unauthored_work_item_reports_what_already_validated(
    tmp_path: Path, project: Path
) -> None:
    """Found live: generation 80 authored and validated its first work item,
    then the harness answered without writing anything for the second -- and
    the whole generation was discarded, losing the first item's already-
    validated change along with it. The second item failing to author should
    not erase the first one passing.
    """
    from evomesh.storage import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    validator = ScriptedValidator([passing()])
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"),
        repository,
        MockProvider(),
        validator,  # type: ignore[arg-type]
    )
    definition = AgentDefinition(name="Environment Evolver", purpose="Evolve")
    definition.mind.add_goal("Improve health reporting", recurring=True)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    generation = await evolver.create_candidate("improve health reporting")
    generation.plan = [
        PlanNode(id="leafA", title="item A", reasoning="change alpha", kind="leaf", status="leaf"),
        PlanNode(id="leafB", title="item B", reasoning="change beta", kind="leaf", status="leaf"),
    ]
    evolver.workspace.supervisor.record_candidate(generation)
    await evolver.set_pipeline_state(
        {
            "stage": "propose",
            "generation": generation.number,
            "objective": "improve health reporting",
            "path": str(generation.path),
            "work_items": ["leafA", "leafB"],
        }
    )
    batches = [MUTATION, NOTHING]
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
        services={"evolver": evolver, "harness": FakeHarness(batches)},
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2)

    await behavior.cycle(context)  # propose leafA -> writes src/app.py
    state = await evolver.pipeline_state()
    assert state["work_items"] == ["leafB"]

    validated = await behavior.cycle(context)  # validate: passes
    assert "passed" in validated.summary
    state = await evolver.pipeline_state()
    assert state["stage"] == "propose"

    stuck = await behavior.cycle(context)  # propose leafB -> answers, writes nothing
    # The old behaviour discarded the generation here, taking leafA's
    # already-validated change with it.
    assert stuck.phase is not AgentPhase.WAITING_HUMAN
    state = await evolver.pipeline_state()
    assert state["stage"] == "report"
    assert state["passed"] is True
    generation = evolver.candidate(int(state["generation"]))
    assert any(change.path == "src/app.py" for change in generation.changes)

    report = await behavior.cycle(context)
    assert report.phase is AgentPhase.WAITING_HUMAN
    assert "validation passed" in report.summary


async def test_evolution_console_commands_drive_the_pipeline(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)

    assert "pipeline stage: plan" in await console.route("/evolution status")
    started = await console.route('/evolution start "make the console faster"')
    assert "objective set" in started
    evolver = environment.registry.get("evolver")
    assert evolver.mind.next_goal() is not None
    assert evolver.mind.next_goal().description == "make the console faster"  # type: ignore[union-attr]
    await environment.stop()


async def test_a_running_agent_comes_back_running_after_a_restart(tmp_path: Path) -> None:
    """Shutting the mesh down is not a decision to disable anyone's agents."""
    settings = settings_for(tmp_path)
    first = Environment(settings, {"ollama": MockProvider(list(BDI_REPLIES))})
    await first.start(start_agent_loops=True)
    agent = AgentDefinition(name="Worker", purpose="Keep working", status=AgentStatus.ACTIVE)
    agent.mind.add_goal("Do the ongoing job", recurring=True)
    await first.register_agent(agent)
    await first.start_agent(agent.id, start_delay=3600)
    await first.cycle_agent("Worker")
    await first.stop()

    second = Environment(settings, {"ollama": MockProvider(list(BDI_REPLIES))})
    await second.start(start_agent_loops=True)

    restored = second.registry.get("Worker")
    assert restored.status is AgentStatus.ACTIVE
    assert second.runtime_states()[restored.id].phase is not AgentPhase.OFFLINE
    assert restored.mind.next_goal() is not None
    # Its memory file is the same one it was writing before the restart.
    memory = second.memory_for(restored)
    assert "The papers live under data/papers." in await memory.read_memory()
    await second.stop()


async def test_an_agent_a_human_stopped_stays_stopped_across_a_restart(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    first = Environment(settings, {"ollama": MockProvider(list(BDI_REPLIES))})
    await first.start(start_agent_loops=True)
    agent = AgentDefinition(name="Paused", purpose="Wait", status=AgentStatus.ACTIVE)
    await first.register_agent(agent)
    await first.start_agent(agent.id, start_delay=3600)
    await first.stop_agent(agent.id)  # the human asked
    await first.stop()

    second = Environment(settings, {"ollama": MockProvider(list(BDI_REPLIES))})
    await second.start(start_agent_loops=True)

    restored = second.registry.get("Paused")
    assert restored.status is AgentStatus.STOPPED
    assert second.runtime_states()[restored.id].phase is AgentPhase.OFFLINE
    await second.stop()


async def test_changing_a_model_does_not_disable_the_agent(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider(list(BDI_REPLIES))})
    await environment.start(start_agent_loops=True)
    agent = AgentDefinition(name="Swappable", purpose="Work", status=AgentStatus.ACTIVE)
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)

    updated = await environment.configure_agent_model(agent.id, "ollama", "mock-specialist")

    assert updated.status is AgentStatus.ACTIVE
    assert agent.id in environment.runtimes
    assert environment.runtime_states()[agent.id].phase is not AgentPhase.OFFLINE
    await environment.stop()


async def test_a_candidate_never_contains_live_runtime_state(tmp_path: Path) -> None:
    """Promoting a generation must not ship someone's database or agent memory."""
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider(list(BDI_REPLIES))})
    await environment.start()
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "app.py").write_text("ACTIVE = True\n", encoding="utf-8")

    generation = await environment.evolver.create_candidate("improve something")

    assert (generation.path / "src" / "app.py").exists()
    copied = {item.name for item in generation.path.rglob("*")}
    assert "state.db" not in copied
    assert "memory.md" not in copied
    assert "context.md" not in copied
    assert not (generation.path / "generations").exists()
    await environment.stop()


async def test_a_new_candidate_skips_the_leftovers_of_a_discarded_one(
    tmp_path: Path,
) -> None:
    """Discarding keeps the directory for inspection; numbering must step over it."""
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "app.py").write_text("ACTIVE = True\n", encoding="utf-8")
    supervisor = environment.evolver.workspace.supervisor

    first = await environment.evolver.create_candidate("first attempt")
    supervisor.discard(first.number)
    assert first.path.exists(), "a discarded candidate is kept for review"

    second = await environment.evolver.create_candidate("second attempt")

    assert second.number != first.number
    assert second.path != first.path
    assert second.path.exists()
    await environment.stop()


async def test_the_console_reports_a_discard_in_plain_english(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    (tmp_path / "src").mkdir(exist_ok=True)
    await environment.evolver.create_candidate("something")
    console = ConsoleChannel(environment)

    assert "discarded" in await console.route("/evolution discard")
    await environment.stop()


async def test_asking_the_evolver_what_it_does_reaches_the_live_pipeline(
    tmp_path: Path, project: Path
) -> None:
    """The stage in the answer is read when asked, not remembered from a cycle."""
    from evomesh.storage import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"), repository
    )
    await evolver.set_pipeline_state(
        {
            "stage": "validate",
            "generation": 4,
            "objective": "make the console faster",
            "file": "src/evomesh/console.py",
        }
    )
    definition = AgentDefinition(name="Environment Evolver", purpose="Evolve")
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    provider = MockProvider(["Validating generation 4 right now."])
    context = CycleContext(
        definition=definition,
        provider=provider,
        memory=memory,
        budget=MemoryBudget(),
        services={"evolver": evolver},
        work="phase: thinking\ncycles completed: 7",
    )

    answer = await EvolverBehavior().respond(
        context,
        Message(sender_id="human", recipient_id="evolver", content="what are you working on?"),
    )

    prompt = str(provider.calls[-1]["prompt"])
    assert "CURRENT WORK" in prompt
    assert "evolution stage: validate" in prompt
    assert "candidate generation: 4" in prompt
    assert "make the console faster" in prompt
    assert "cycles completed: 7" in prompt  # the runtime's own half is kept
    assert answer == "Validating generation 4 right now."


async def test_an_agent_answers_about_its_work_from_runtime_state(tmp_path: Path) -> None:
    provider = MockProvider(["I am indexing the papers."])
    environment = Environment(settings_for(tmp_path), {"ollama": provider})
    await environment.start()
    agent = AgentDefinition(name="Worker", purpose="Keep working", status=AgentStatus.ACTIVE)
    agent.mind.add_goal("Index the papers", recurring=True)
    await environment.register_agent(agent)
    # A cycle far in the future: the answer must come from state, not from work
    # the agent happens to do while the question is in flight.
    await environment.start_agent(agent.id, start_delay=3600)
    await environment.send_message(
        Message(sender_id="human", recipient_id=agent.id, content="what are you working on?")
    )
    reply = await environment.bus.receive("human", wait_seconds=2)

    prompt = str(provider.calls[-1]["prompt"])
    assert "CURRENT WORK" in prompt
    assert "goal in hand: Index the papers" in prompt
    assert "next cycle in about" in prompt
    assert reply.content == "I am indexing the papers."
    await environment.stop()


async def test_a_cycle_during_validation_returns_at_once(
    tmp_path: Path, project: Path
) -> None:
    """The claim the README had been making and the code had not kept.

    The validate stage awaited the suite inline, and an agent's mailbox and
    cycle share one lock -- so for the whole of a multi-minute run the Evolver
    answered nothing and looked exactly like an agent that had hung.
    """
    import time

    class SlowValidator:
        def __init__(self) -> None:
            self.started = 0

        async def validate(self, generation: Generation) -> ValidationResult:
            self.started += 1
            await asyncio.sleep(3)
            path = generation.path / "validation-result.json"
            result = ValidationResult(
                passed=True, commands=[{"command": "uv run pytest", "exit_code": 0}]
            )
            path.write_text(result.model_dump_json(), encoding="utf-8")
            return result

    validator = SlowValidator()
    evolver, context, _ = await evolving(
        tmp_path, project, [MUTATION], validator, StubRepairer()  # type: ignore[arg-type]
    )
    behavior = EvolverBehavior(auto_validate=True)

    await behavior.cycle(context)  # plan
    await behavior.cycle(context)  # propose

    began = time.monotonic()
    waiting = await behavior.cycle(context)
    elapsed = time.monotonic() - began

    assert elapsed < 1.0, "the tick must not become the validation run"
    assert "generation 2" in waiting.summary
    assert validator.started == 1
    assert (await evolver.pipeline_state())["stage"] == "validate"

    # A later cycle takes the verdict, and the suite ran exactly once.
    await asyncio.sleep(3.2)
    verdict = await behavior.cycle(context)
    assert "passed" in verdict.summary
    assert validator.started == 1


async def test_a_validation_that_outruns_its_budget_is_blocked_not_failed(
    tmp_path: Path, project: Path
) -> None:
    class Endless:
        async def validate(self, generation: Generation) -> ValidationResult:
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    evolver, context, _ = await evolving(
        tmp_path, project, [MUTATION], Endless(), StubRepairer()  # type: ignore[arg-type]
    )
    behavior = EvolverBehavior(auto_validate=True, validate_seconds=1.0)

    await behavior.cycle(context)  # plan
    await behavior.cycle(context)  # propose
    await behavior.cycle(context)  # start the suite
    await asyncio.sleep(1.2)
    blocked = await behavior.cycle(context)

    state = await evolver.pipeline_state()
    assert "blocked by this machine" in blocked.summary
    assert state["passed"] is None, "a suite the host could not finish is not a verdict"
    assert state["stage"] == "report"


async def test_stopping_the_mesh_cancels_a_validation(tmp_path: Path) -> None:
    class Endless:
        async def validate(self, generation: Generation) -> ValidationResult:
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    environment.evolver.validator = Endless()  # type: ignore[assignment]
    await environment.start()
    generation = await environment.evolver.create_candidate("something slow")
    run = environment.evolver.begin_validation(generation)
    await asyncio.sleep(0.05)

    await environment.stop()

    assert run.task.cancelled() or run.task.done()
    assert environment.evolver.validation is None


async def test_a_missing_toolchain_is_blocked_not_failed(tmp_path: Path) -> None:
    """The case string-matching got wrong, and it cost a candidate its verdict.

    `uv` missing is the host's problem. Reported as a failure, the pipeline reads
    it as a verdict and spends the repair budget asking a model to fix somebody's
    PATH -- the exact thing "not validated is not failed" exists to prevent.
    """
    missing = ValidationResult(
        passed=False,
        commands=[
            {
                "command": "uv",
                "exit_code": -1,
                "output": "uv is not on PATH and no .tools/uv/bin/uv.exe was found",
                "blocked": True,
            }
        ],
    )
    busy = ValidationResult(
        passed=False,
        commands=[
            {
                "command": "uv sync",
                "exit_code": 2,
                "output": "failed to remove file ... (os error 32)",
            }
        ],
    )
    real = ValidationResult(
        passed=False,
        commands=[{"command": "uv run pytest", "exit_code": 1, "output": "E assert 1 == 2"}],
    )

    assert missing.environment_blocker(), "a missing toolchain is the host's fault"
    # Found by this test on this machine: uv could not replace a file in the
    # candidate's venv because something else held it open.
    assert busy.environment_blocker() == "os error 32"
    assert real.environment_blocker() is None, "a failing test is the candidate's fault"


def test_subprocesses_leave_no_asyncio_transport_behind() -> None:
    """Why this project runs commands on a thread rather than through asyncio.

    The proactor loop finalises a subprocess transport during a later garbage
    collection, after the loop that owned it has closed, and the unraisable
    ValueError it raises gets attributed to whichever test is running. Candidate
    validation *is* this suite, so roughly one candidate in three failed for a
    reason it had not caused.
    """
    import evomesh.evolution as evolution
    import evomesh.git as git
    import evomesh.skills as skills

    for module in (evolution, git, skills):
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "create_subprocess_exec" not in source, module.__name__


async def test_the_objective_orients_the_model_before_it_looks(tmp_path: Path) -> None:
    """What replaced the JSON contract: an objective, not a format.

    The map still goes first because the rules refer to it, but it is now
    orientation for an agent that can go and read the files, rather than the
    whole of what the model will ever see about the project.
    """
    from evomesh.storage import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    source = tmp_path / "source"
    (source / "src" / "evomesh").mkdir(parents=True)
    (source / "src" / "evomesh" / "__init__.py").write_text(
        "from evomesh.core import thing\n", encoding="utf-8"
    )
    (source / "src" / "evomesh" / "core.py").write_text('"""Core."""\n', encoding="utf-8")
    evolver = EnvironmentEvolver(
        CandidateWorkspace(source, tmp_path / "generations"), repository
    )

    objective = evolver.mutation_objective("flip the flag")

    assert "OBJECTIVE: flip the flag" in objective
    assert "core.py" in objective
    assert "Read a file before you change it" in objective
    # The old contract's JSON envelope is gone, not merely unused.
    assert "relative_path" not in objective


async def test_a_repair_objective_carries_the_real_failure(tmp_path: Path) -> None:
    from evomesh.storage import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    evolver = EnvironmentEvolver(
        CandidateWorkspace(tmp_path / "source", tmp_path / "generations"), repository
    )

    objective = evolver.repair_objective(
        {"command": "uv run pytest", "exit_code": 1, "output": "E assert 1 == 2"},
        ["src/app.py"],
    )

    assert "`uv run pytest` failed with exit code 1" in objective
    assert "E assert 1 == 2" in objective
    assert "already changed: src/app.py" in objective


# -- self-repair ---------------------------------------------------------

RUFF_FIXABLE = (
    "UP017 [*] Use `datetime.UTC` alias\n"
    "  --> src/app.py:1:1\n"
    "Found 1 error.\n"
    "[*] 1 fixable with the `--fix` option.\n"
)
PYTEST_OUTPUT = "E   assert ACTIVE is True\nE   AssertionError\n1 failed in 0.12s\n"

# What a harness job writes, rather than what a model claimed it would.
MUTATION = [("src/app.py", "ACTIVE = False\n")]
REPAIR = [("src/app.py", "ACTIVE = True\n")]
NOTHING: list[tuple[str, str]] = []


async def evolving(
    tmp_path: Path,
    project: Path,
    batches: list[list[tuple[str, str]]],
    validator: ScriptedValidator,
    repairer: CandidateRepairer | None = None,
) -> tuple[EnvironmentEvolver, CycleContext, FakeHarness]:
    from evomesh.storage import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    provider = MockProvider(["the model is not asked to author anything any more"])
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"),
        repository,
        provider,
        validator,  # type: ignore[arg-type]
        repairer,
    )
    definition = AgentDefinition(name="Environment Evolver", purpose="Evolve")
    definition.mind.add_goal("Improve health reporting", recurring=True)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    harness = FakeHarness(list(batches))
    context = CycleContext(
        definition=definition,
        provider=provider,
        memory=memory,
        budget=MemoryBudget(),
        services={"evolver": evolver, "harness": harness},
    )
    return evolver, context, harness


def test_extract_rationale_takes_only_the_marked_sentence() -> None:
    answer = (
        "I read src/app.py and confirmed the flag was unused.\n"
        "RATIONALE: flipped ACTIVE to False so the dead branch is reachable.\n"
        "Done."
    )
    assert (
        _extract_rationale(answer)
        == "flipped ACTIVE to False so the dead branch is reachable."
    )


def test_extract_rationale_falls_back_to_the_whole_answer_when_unmarked() -> None:
    answer = "flipped the flag because it looked wrong."
    assert _extract_rationale(answer) == answer


async def test_a_fixable_lint_failure_is_repaired_without_the_model(
    tmp_path: Path, project: Path
) -> None:
    validator = ScriptedValidator([failing("uv run ruff check .", RUFF_FIXABLE), passing()])
    repairer = StubRepairer()
    evolver, context, harness = await evolving(
        tmp_path, project, [MUTATION], validator, repairer
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2)

    await behavior.cycle(context)
    await behavior.cycle(context)
    failed = await behavior.cycle(context)
    assert "repairing it (attempt 1 of 2)" in failed.summary
    assert (await evolver.pipeline_state())["stage"] == "repair"

    repaired = await behavior.cycle(context)
    assert repairer.calls == 1
    # The linter fixed it, so no second harness job was ever asked for.
    assert len(harness.objectives) == 1
    assert "ruff --fix" in repaired.summary
    # And it did not spend the budget: the budget bounds model repairs, and a
    # mechanical fix costs nothing. Found the first time the loop ran end to
    # end -- the model fixed the real failure, ruff objected to the import order
    # it produced, and the candidate went to a human over a free finding.
    assert "free repair" in repaired.summary
    assert int((await evolver.pipeline_state()).get("repairs", 0)) == 0

    passed = await behavior.cycle(context)
    assert "validation passed" in passed.summary
    assert (await evolver.pipeline_state())["stage"] == "report"

    report = await behavior.cycle(context)
    assert report.phase is AgentPhase.WAITING_HUMAN
    # No repair attempt is counted: the linter fixed it for free.
    assert "validation passed" in report.summary
    # A repaired candidate is promotable, not failed.
    assert evolver.candidate(2).status is not GenerationStatus.FAILED


async def test_a_free_repair_that_undoes_everything_skips_validation(tmp_path: Path) -> None:
    """`ruff --fix` deleting exactly the line the propose stage added is not a
    fixed candidate -- it is the empty candidate from D5, one stage later, and
    reachable only through a real git tree: this is what caught it live."""
    project = await git_project(tmp_path / "project")
    # MUTATION_OBJECTIVE.md and validation-result.json are real content every
    # candidate gets -- gitignored in the actual repo (see .gitignore)
    # precisely so neither makes an otherwise byte-identical candidate look
    # dirty.
    repo = GitRepository(project)
    (project / ".gitignore").write_text(
        "MUTATION_OBJECTIVE.md\nvalidation-result*.json\n", encoding="utf-8"
    )
    await repo.run("add", "-A")
    await repo.run("commit", "-m", "gitignore")
    validator = ScriptedValidator([failing("uv run ruff check .", RUFF_FIXABLE)])
    repairer = UndoingRepairer("src/app.py", "ACTIVE = True\n")
    evolver, context, harness = await evolving(
        tmp_path, project, [MUTATION], validator, repairer
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2)

    for _ in range(3):  # plan, propose, validate (fails)
        await behavior.cycle(context)
    assert (await evolver.pipeline_state())["stage"] == "repair"

    repaired = await behavior.cycle(context)

    assert repairer.calls == 1
    assert "nothing left to validate" in repaired.summary
    assert (await evolver.pipeline_state())["stage"] == "report"
    assert (await evolver.pipeline_state()).get("passed") is None
    # Not a second harness job: the free fix undid it, not the model.
    assert len(harness.objectives) == 1


async def test_auto_promote_discards_a_candidate_repaired_down_to_no_change(
    tmp_path: Path,
) -> None:
    """Unlike a genuinely unvalidated candidate (host blocked the run), one
    repaired back to byte-identical-to-parent has nothing a human could lose
    by discarding it -- auto_promote should not still stop and wait."""
    project = await git_project(tmp_path / "project")
    (project / ".gitignore").write_text(
        "MUTATION_OBJECTIVE.md\nvalidation-result*.json\n", encoding="utf-8"
    )
    repo = GitRepository(project)
    await repo.run("add", "-A")
    await repo.run("commit", "-m", "gitignore")
    validator = ScriptedValidator([failing("uv run ruff check .", RUFF_FIXABLE)])
    repairer = UndoingRepairer("src/app.py", "ACTIVE = True\n")
    evolver, context, harness = await evolving(
        tmp_path, project, [MUTATION], validator, repairer
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2, auto_promote=True)

    for _ in range(3):  # plan, propose, validate (fails)
        await behavior.cycle(context)
    assert (await evolver.pipeline_state())["stage"] == "repair"

    repaired = await behavior.cycle(context)

    assert "nothing left to validate" in repaired.summary
    assert "discarded generation" in repaired.summary
    assert repaired.phase is AgentPhase.ACTING
    assert (await evolver.pipeline_state())["stage"] == "plan"
    assert "2" not in evolver.workspace.supervisor.metadata().get("candidates", {})
    assert len(harness.objectives) == 1


async def test_candidate_changed_nothing_ignores_an_unrelated_ancestor_repository(
    tmp_path: Path,
) -> None:
    """The regression the fix above actually had, caught only by running the
    existing free-repair test rather than the new one: a candidate that is
    not itself a git repository (the copytree fallback in
    CandidateWorkspace.create, or any test using the plain `project` fixture)
    can still sit inside one that is -- this project's own generations/ in
    production, a pytest tmp_path nested in the checkout here. `git -C`
    walks up and answers for that ancestor, not the candidate, and an
    ancestor that happens to be clean must never read as "this candidate
    changed nothing" -- that silently skipped validation for a candidate
    that both propose and repair had genuinely rewritten.
    """
    outer = await git_project(tmp_path / "outer")
    evolver, _, _ = await evolving(
        tmp_path, outer, [MUTATION], ScriptedValidator([passing()]), StubRepairer()
    )
    nested = outer / "nested-candidate"
    nested.mkdir()
    (nested / "app.py").write_text("ACTIVE = False\n", encoding="utf-8")
    generation = Generation(number=99, status=GenerationStatus.CANDIDATE, path=nested)

    assert await evolver.candidate_changed_nothing(generation) is False


async def test_create_falls_back_to_copytree_inside_an_unrelated_ancestor_repository(
    tmp_path: Path,
) -> None:
    """`CandidateWorkspace.create`'s own version of the bug above, and the
    one that actually reached a human's disk: `git -C <repository_root>
    worktree add` does not require `repository_root` to be a repository at
    all -- found live, run against a plain `project` fixture (no `.git` of
    its own) sitting under this project's own .pytest-tmp, which put a real
    worktree and a real branch into *this* repository, based on whatever
    commit this checkout happened to be on, in a directory a later pytest
    cleanup then deleted out from under it. A candidate whose repository_root
    is not genuinely its own top level must take the copytree fallback
    instead of asking git to try at all.
    """
    outer = await git_project(tmp_path / "outer")
    before = (await GitRepository(outer).run("branch")).strip()
    source = outer / "unrelated-project"
    source.mkdir()
    (source / "app.py").write_text("ACTIVE = True\n", encoding="utf-8")
    workspace = CandidateWorkspace(source, tmp_path / "generations")

    candidate = await workspace.create("Improve health reporting")

    assert not (candidate.path / ".git").exists()
    assert (candidate.path / "app.py").read_text(encoding="utf-8") == "ACTIVE = True\n"
    after = (await GitRepository(outer).run("branch")).strip()
    assert after == before, "no new branch should appear in the unrelated ancestor repository"


async def test_a_failure_the_linter_cannot_fix_goes_to_the_model(
    tmp_path: Path, project: Path
) -> None:
    validator = ScriptedValidator([failing("uv run pytest", PYTEST_OUTPUT), passing()])
    repairer = StubRepairer()
    evolver, context, harness = await evolving(
        tmp_path, project, [MUTATION, REPAIR], validator, repairer
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2)

    for _ in range(4):  # plan, propose, validate, repair
        await behavior.cycle(context)

    assert repairer.calls == 0
    objective = harness.objectives[-1]
    # The repair job names the command, its real output, and what this
    # generation already touched -- and the model reads the file itself.
    assert "uv run pytest" in objective
    assert "assert ACTIVE is True" in objective
    assert "already changed: src/app.py" in objective
    generation = evolver.candidate(2)
    assert (generation.path / "src" / "app.py").read_text(encoding="utf-8") == "ACTIVE = True\n"

    passed = await behavior.cycle(context)
    assert "passed after 1 repair" in passed.summary


async def test_a_repair_that_changes_nothing_stops_the_loop(
    tmp_path: Path, project: Path
) -> None:
    # The same failure every time: the repair provably did not move.
    validator = ScriptedValidator([failing("uv run ruff check .", RUFF_FIXABLE)])
    repairer = StubRepairer()
    evolver, context, _ = await evolving(tmp_path, project, [MUTATION], validator, repairer)
    behavior = EvolverBehavior(auto_validate=True, max_repairs=5)

    for _ in range(4):  # plan, propose, validate, repair
        await behavior.cycle(context)

    stalled = await behavior.cycle(context)
    assert "the last repair changed nothing" in stalled.summary
    assert (await evolver.pipeline_state())["stage"] == "report"
    # It stops on evidence, not on the attempt budget.
    assert repairer.calls == 1


async def test_repairs_are_bounded_by_max_repairs(tmp_path: Path, project: Path) -> None:
    """The budget bounds *model* repairs.

    A different failure each time so the stall guard never fires, and not a
    ruff-fixable one: the linter's own fixer is free and deliberately does not
    spend the budget, which is what the next test covers.
    """
    validator = ScriptedValidator(
        [
            failing("uv run pytest", PYTEST_OUTPUT),
            failing("uv run pytest", PYTEST_OUTPUT + "one\n"),
            failing("uv run pytest", PYTEST_OUTPUT + "two\n"),
        ]
    )
    repairer = StubRepairer()
    evolver, context, harness = await evolving(
        tmp_path, project, [MUTATION, REPAIR, REPAIR], validator, repairer
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2)

    for _ in range(7):  # plan, propose, (validate, repair) x 2, validate
        await behavior.cycle(context)

    assert repairer.calls == 0, "no ruff-fixable finding here"
    assert len(harness.objectives) == 3, "one mutation and two model repairs"
    assert (await evolver.pipeline_state())["stage"] == "report"
    report = await behavior.cycle(context)
    assert "validation failed after 2 repair attempts" in report.summary
    assert evolver.candidate(2).status is GenerationStatus.FAILED


async def test_an_unusable_repair_answer_still_reaches_the_human(
    tmp_path: Path, project: Path
) -> None:
    validator = ScriptedValidator([failing("uv run pytest", PYTEST_OUTPUT)])
    evolver, context, _ = await evolving(
        tmp_path, project, [MUTATION, NOTHING], validator, StubRepairer()
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2)

    for _ in range(3):  # plan, propose, validate
        await behavior.cycle(context)
    broken = await behavior.cycle(context)

    # A repair job that changed nothing does not loop and does not validate an
    # untouched tree: the candidate goes to the human exactly as it stands.
    assert "finished without changing a file" in broken.summary
    # Not back to plan: that would strand this candidate and open another.
    assert (await evolver.pipeline_state())["stage"] == "report"
    assert len(evolver.workspace.supervisor.candidates()) == 1


async def test_auto_promote_discards_a_candidate_the_harness_never_touched(
    tmp_path: Path, project: Path
) -> None:
    """Same D5 no-op as the repair-stage case, just caught one stage earlier:
    the harness job itself never wrote a file. Nothing shipped, nothing lost
    -- auto_promote should not still park it in await-human."""
    validator = ScriptedValidator([passing()])
    evolver, context, harness = await evolving(
        tmp_path, project, [NOTHING], validator, StubRepairer()
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2, auto_promote=True)

    await behavior.cycle(context)  # plan
    discarded = await behavior.cycle(context)  # propose: job wrote nothing

    assert "finished without changing a file" in discarded.summary
    assert "discarded generation" in discarded.summary
    assert discarded.phase is AgentPhase.ACTING
    assert (await evolver.pipeline_state())["stage"] == "plan"
    assert "2" not in evolver.workspace.supervisor.metadata().get("candidates", {})
    assert len(harness.objectives) == 1


async def test_max_repairs_zero_keeps_the_single_shot_pipeline(
    tmp_path: Path, project: Path
) -> None:
    validator = ScriptedValidator([failing("uv run ruff check .", RUFF_FIXABLE)])
    repairer = StubRepairer()
    evolver, context, _ = await evolving(tmp_path, project, [MUTATION], validator, repairer)
    behavior = EvolverBehavior(auto_validate=True, max_repairs=0)

    assert STAGE_REPAIR not in behavior._stages()
    for _ in range(3):  # plan, propose, validate
        await behavior.cycle(context)

    assert repairer.calls == 0
    assert (await evolver.pipeline_state())["stage"] == "report"


# -- failures the candidate did not cause --------------------------------

# The real shape of it: pytest's shared temp root is not writable by this user,
# so every tmp_path test errors at fixture setup and nothing in the candidate
# is implicated.
PERMISSION_OUTPUT = (
    "ERROR at setup of test_a_percept_revises_a_belief\n"
    "E   PermissionError: [WinError 5] Access is denied: "
    r"'C:\Users\AI\AppData\Local\Temp\pytest-of-AI'"
    "\n78 errors in 2.56s\n"
)


def test_pytest_is_given_a_temp_directory_inside_the_candidate() -> None:
    command = next(item for item in CandidateValidator.COMMANDS if "pytest" in item)
    assert "--basetemp" in command
    assert command[command.index("--basetemp") + 1] == PYTEST_TEMP_DIR
    # Relative, because every validation command runs with the generation as cwd.
    assert not Path(PYTEST_TEMP_DIR).is_absolute()
    # And never copied into the next candidate.
    assert PYTEST_TEMP_DIR in IGNORED_NAMES


def test_a_real_failure_is_not_mistaken_for_a_host_failure() -> None:
    assert failing("uv run ruff check .", RUFF_FIXABLE).environment_blocker() is None
    assert failing("uv run pytest", PYTEST_OUTPUT).environment_blocker() is None
    assert passing().environment_blocker() is None
    assert failing("uv run pytest", PERMISSION_OUTPUT).environment_blocker() == "PermissionError"


async def test_a_host_failure_is_not_blamed_on_the_candidate(
    tmp_path: Path, project: Path
) -> None:
    validator = ScriptedValidator([failing("uv run pytest", PERMISSION_OUTPUT)])
    repairer = StubRepairer()
    evolver, context, harness = await evolving(
        tmp_path, project, [MUTATION], validator, repairer
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2)

    await behavior.cycle(context)  # plan
    await behavior.cycle(context)  # propose
    validated = await behavior.cycle(context)

    assert "blocked by this machine, not by the candidate" in validated.summary
    assert "PermissionError" in validated.summary
    state = await evolver.pipeline_state()
    assert state["stage"] == "report"
    # Not failed: nothing was learned about the candidate either way.
    assert state["passed"] is None
    assert state["environment"] == "PermissionError"
    # No repair was attempted, mechanically or through the harness.
    assert repairer.calls == 0
    assert len(harness.objectives) == 1
    assert validator.calls == 1

    report = await behavior.cycle(context)
    assert "this machine blocked the run (PermissionError)" in report.summary
    assert evolver.candidate(2).status is GenerationStatus.CANDIDATE


# -- deciding without a human --------------------------------------------


async def test_a_validated_candidate_is_promoted_without_asking(tmp_path: Path) -> None:
    # A real checkout: promotion now lands the change, so it needs somewhere to land.
    project = await git_project(tmp_path / "project")
    validator = ScriptedValidator([passing()])
    evolver, context, _ = await evolving(
        tmp_path, project, [MUTATION], validator, StubRepairer()
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2, auto_promote=True)

    for _ in range(3):  # plan, propose, validate
        await behavior.cycle(context)
    decided = await behavior.cycle(context)

    assert "promoted generation 2 on its own verdict" in decided.summary
    assert decided.phase is not AgentPhase.WAITING_HUMAN
    supervisor = evolver.workspace.supervisor
    assert supervisor.metadata()["active"] == 2
    assert supervisor.metadata()["last_known_good"] == 1
    # Free for the next pass, with the repair counters cleared.
    state = await evolver.pipeline_state()
    assert state == {"stage": "plan"}


async def test_a_failed_candidate_is_discarded_without_asking(
    tmp_path: Path, project: Path
) -> None:
    validator = ScriptedValidator([failing("uv run pytest", PYTEST_OUTPUT)])
    evolver, context, _ = await evolving(
        tmp_path, project, [MUTATION, NOTHING], validator, StubRepairer()
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=0, auto_promote=True)

    for _ in range(3):  # plan, propose, validate
        await behavior.cycle(context)
    decided = await behavior.cycle(context)

    assert "discarded generation 2 on its own verdict" in decided.summary
    supervisor = evolver.workspace.supervisor
    assert supervisor.candidates() == []
    # Discarding must never move the active generation.
    assert supervisor.metadata()["active"] == 1
    assert (await evolver.pipeline_state()) == {"stage": "plan"}


async def test_a_host_failure_still_stops_for_a_human_under_the_policy(
    tmp_path: Path, project: Path
) -> None:
    validator = ScriptedValidator([failing("uv run pytest", PERMISSION_OUTPUT)])
    evolver, context, _ = await evolving(
        tmp_path, project, [MUTATION], validator, StubRepairer()
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2, auto_promote=True)

    for _ in range(3):  # plan, propose, validate
        await behavior.cycle(context)
    parked = await behavior.cycle(context)

    assert parked.phase is AgentPhase.WAITING_HUMAN
    assert (await evolver.pipeline_state())["stage"] == "await-human"
    # Neither promoted nor thrown away: nothing was learned about it.
    assert evolver.workspace.supervisor.metadata()["active"] == 1
    assert [item.number for item in evolver.workspace.supervisor.candidates()] == [2]


async def test_an_unvalidated_candidate_is_never_promoted_by_policy(
    tmp_path: Path, project: Path
) -> None:
    validator = ScriptedValidator([passing()])
    evolver, context, _ = await evolving(
        tmp_path, project, [MUTATION], validator, StubRepairer()
    )
    # Validation is off, so there is no verdict to act on.
    behavior = EvolverBehavior(auto_validate=False, max_repairs=2, auto_promote=True)

    for _ in range(2):  # plan, propose
        await behavior.cycle(context)
    parked = await behavior.cycle(context)

    assert validator.calls == 0
    assert parked.phase is AgentPhase.WAITING_HUMAN
    assert evolver.workspace.supervisor.metadata()["active"] == 1


async def test_the_checklist_says_it_decides_rather_than_hands_over(
    tmp_path: Path, project: Path
) -> None:
    validator = ScriptedValidator([passing()])
    _, context, _ = await evolving(tmp_path, project, [MUTATION], validator, StubRepairer())
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2, auto_promote=True)

    await behavior.cycle(context)

    steps = [step.description for step in context.definition.mind.intentions[0].steps]
    assert steps[-1] == "promote or discard the candidate on its verdict"
    assert "hand the candidate to the human" not in steps


# -- landing a generation in the tree ------------------------------------


def _seed_tree(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text("ACTIVE = True\n", encoding="utf-8")


async def git_project(root: Path) -> Path:
    """A real git checkout, because cherry-pick cannot be faked usefully."""
    _seed_tree(root)
    repository = GitRepository(root)
    await repository.run("init", "-b", "main")
    await repository.run("config", "user.email", "test@example.com")
    await repository.run("config", "user.name", "Test")
    await repository.run("add", "-A")
    await repository.run("commit", "-m", "initial")
    return root


async def test_a_promoted_generation_lands_on_the_checkout(tmp_path: Path) -> None:
    project = await git_project(tmp_path / "project")
    validator = ScriptedValidator([passing()])
    evolver, context, _ = await evolving(
        tmp_path, project, [MUTATION], validator, StubRepairer()
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2, auto_promote=True)

    for _ in range(4):  # plan, propose, validate, decide
        await behavior.cycle(context)

    # The mutation is now in the tree the mesh runs from, not just in a candidate.
    assert (project / "src" / "app.py").read_text(encoding="utf-8") == "ACTIVE = False\n"
    metadata = evolver.workspace.supervisor.metadata()
    assert metadata["active"] == 2
    assert metadata["active_commit"] != metadata["last_known_good_commit"]
    # The running process still executes what it started with.
    assert metadata["restart_required"] is True


async def test_a_generation_is_never_applied_over_uncommitted_work(tmp_path: Path) -> None:
    project = await git_project(tmp_path / "project")
    validator = ScriptedValidator([passing()])
    evolver, context, _ = await evolving(
        tmp_path, project, [MUTATION], validator, StubRepairer()
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2, auto_promote=True)

    for _ in range(3):  # plan, propose, validate
        await behavior.cycle(context)
    # A human is midway through something in the checkout.
    (project / "src" / "human.py").write_text("MINE = 1\n", encoding="utf-8")
    parked = await behavior.cycle(context)

    assert "could not be applied" in parked.summary
    assert "uncommitted changes" in parked.summary
    assert parked.phase is AgentPhase.WAITING_HUMAN
    # Untouched: neither the human's file nor the promotion happened.
    assert (project / "src" / "app.py").read_text(encoding="utf-8") == "ACTIVE = True\n"
    assert (project / "src" / "human.py").exists()
    assert evolver.workspace.supervisor.metadata()["active"] == 1
    assert (await evolver.pipeline_state())["stage"] == "await-human"


async def test_parking_on_a_dirty_tree_retries_once_it_is_clean(tmp_path: Path) -> None:
    project = await git_project(tmp_path / "project")
    validator = ScriptedValidator([passing()])
    evolver, context, _ = await evolving(
        tmp_path, project, [MUTATION], validator, StubRepairer()
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2, auto_promote=True)

    for _ in range(3):  # plan, propose, validate
        await behavior.cycle(context)
    # A human is midway through something in the checkout.
    (project / "src" / "human.py").write_text("MINE = 1\n", encoding="utf-8")
    parked = await behavior.cycle(context)
    assert parked.phase is AgentPhase.WAITING_HUMAN
    assert (await evolver.pipeline_state())["stage"] == "await-human"

    # The human finishes and commits their own work; nobody runs
    # /evolution promote. The next cycle should still land the candidate
    # on its own, because auto_promote parked it for an environmental
    # reason, not because it wanted a human's decision.
    project_repo = GitRepository(project)
    await project_repo.run("add", "-A")
    await project_repo.run("commit", "-m", "human work")
    landed = await behavior.cycle(context)

    assert "promoted generation" in landed.summary
    assert (project / "src" / "app.py").read_text(encoding="utf-8") == "ACTIVE = False\n"
    assert evolver.workspace.supervisor.metadata()["active"] == 2
    assert (await evolver.pipeline_state())["stage"] == "plan"


async def test_reverting_puts_the_tree_back_on_the_replaced_commit(tmp_path: Path) -> None:
    project = await git_project(tmp_path / "project")
    validator = ScriptedValidator([passing()])
    evolver, context, _ = await evolving(
        tmp_path, project, [MUTATION], validator, StubRepairer()
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2, auto_promote=True)

    for _ in range(4):
        await behavior.cycle(context)
    assert (project / "src" / "app.py").read_text(encoding="utf-8") == "ACTIVE = False\n"

    restored = await evolver.revert_tree()

    assert restored is not None
    assert (project / "src" / "app.py").read_text(encoding="utf-8") == "ACTIVE = True\n"


async def test_a_discarded_generation_never_touches_the_tree(tmp_path: Path) -> None:
    project = await git_project(tmp_path / "project")
    validator = ScriptedValidator([failing("uv run pytest", PYTEST_OUTPUT)])
    evolver, context, _ = await evolving(
        tmp_path, project, [MUTATION], validator, StubRepairer()
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=0, auto_promote=True)

    for _ in range(4):
        await behavior.cycle(context)

    assert (project / "src" / "app.py").read_text(encoding="utf-8") == "ACTIVE = True\n"
    metadata = evolver.workspace.supervisor.metadata()
    assert metadata["active"] == 1
    assert not metadata.get("restart_required")


async def test_every_pipeline_stage_leaves_a_line_in_the_log(
    tmp_path: Path, project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A pipeline that is not moving has to say so somewhere a human will look.

    The stage lived only in the database and nothing was written down when it
    changed, so a mesh that produced no generation for nine hours looked
    exactly like one that was busy -- and when the database was wiped, the only
    record of where the pipeline had got to went with it.
    """
    from evomesh.storage import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"),
        repository,
        MockProvider(),
        StubValidator(),  # type: ignore[arg-type]
    )
    definition = AgentDefinition(name="Environment Evolver", purpose="Evolve")
    definition.mind.add_goal("Improve health reporting", recurring=True)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
        services={"evolver": evolver, "harness": FakeHarness([MUTATION])},
    )
    behavior = EvolverBehavior(auto_validate=True)

    with caplog.at_level(logging.INFO, logger="evomesh.behaviors"):
        for _ in range(5):
            await behavior.cycle(context)

    lines = [record.getMessage() for record in caplog.records]
    moves = [line.split(":")[0] for line in lines if line.startswith("Evolution stage ")]
    assert moves == [
        "Evolution stage plan -> propose",
        "Evolution stage propose -> validate",
        "Evolution stage validate -> report",
        "Evolution stage report -> await-human",
    ]
    # The stall is the thing worth seeing, so the parked cycle is logged too.
    assert any(line.startswith("Evolution is holding") for line in lines)
