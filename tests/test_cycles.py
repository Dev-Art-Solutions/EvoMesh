from pathlib import Path

import pytest

from evomesh.behaviors import STAGE_REPAIR, EvolverBehavior, GuardianBehavior
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
    ValidationResult,
)
from evomesh.memory import AgentMemory, MemoryBudget
from evomesh.models import MockProvider

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
        async def generate(self, prompt: str, *, system: str = "", model: str | None = None) -> str:
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
    mutation = (
        '{"relative_path": "src/app.py", "content": "ACTIVE = False\\n", '
        '"rationale": "flip the flag"}'
    )
    validator = StubValidator()
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"),
        repository,
        MockProvider([mutation]),
        validator,  # type: ignore[arg-type]
    )
    definition = AgentDefinition(name="Environment Evolver", purpose="Evolve")
    definition.mind.add_goal("Improve health reporting", recurring=True)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    context = CycleContext(
        definition=definition,
        provider=MockProvider([mutation]),
        memory=memory,
        budget=MemoryBudget(),
        services={"evolver": evolver},
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


async def test_a_mutation_survives_reasoning_and_a_first_bad_answer(tmp_path: Path) -> None:
    """Local models bury the object in reasoning, then get it right when told why."""
    from evomesh.storage import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    good = (
        '{"relative_path": "src/app.py", "content": "ACTIVE = False\\n", '
        '"rationale": "flip the flag"}'
    )
    provider = MockProvider(
        [
            "I should change the flag, but I will not answer in JSON.\n</think>\nSure thing!",
            f"Reasoning about the file first.\n</think>\n{good}",
        ]
    )
    evolver = EnvironmentEvolver(
        CandidateWorkspace(tmp_path / "source", tmp_path / "generations"),
        repository,
        provider,
    )

    mutation = await evolver.propose_mutation("flip the flag", model="qwen3:14b")

    assert mutation.relative_path == Path("src/app.py")
    assert len(provider.calls) == 2
    assert provider.calls[-1]["model"] == "qwen3:14b"
    assert "could not be used" in str(provider.calls[-1]["prompt"])


async def test_an_unusable_mutation_names_what_was_wrong(tmp_path: Path) -> None:
    from evomesh.storage import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    evolver = EnvironmentEvolver(
        CandidateWorkspace(tmp_path / "source", tmp_path / "generations"),
        repository,
        MockProvider(["no object here at all"]),
    )

    with pytest.raises(ValueError, match="no JSON object was found"):
        await evolver.propose_mutation("flip the flag")


# -- self-repair ---------------------------------------------------------

RUFF_FIXABLE = (
    "UP017 [*] Use `datetime.UTC` alias\n"
    "  --> src/app.py:1:1\n"
    "Found 1 error.\n"
    "[*] 1 fixable with the `--fix` option.\n"
)
PYTEST_OUTPUT = "E   assert ACTIVE is True\nE   AssertionError\n1 failed in 0.12s\n"

MUTATION = '{"relative_path": "src/app.py", "content": "ACTIVE = False\\n", "rationale": "flip"}'
REPAIR = (
    '{"relative_path": "src/app.py", "content": "ACTIVE = True\\n", '
    '"rationale": "restore the flag the suite expects"}'
)


def failing(command: str, output: str) -> ValidationResult:
    return ValidationResult(
        passed=False,
        commands=[
            {"command": "uv sync", "exit_code": 0, "output": ""},
            {"command": command, "exit_code": 1, "output": output},
        ],
    )


def passing() -> ValidationResult:
    return ValidationResult(
        passed=True, commands=[{"command": "uv run pytest", "exit_code": 0, "output": "ok"}]
    )


class ScriptedValidator:
    """Writes a real validation-result.json: the repair stage reads it back."""

    def __init__(self, results: list[ValidationResult]) -> None:
        self.results = results
        self.calls = 0

    async def validate(self, generation: Generation) -> ValidationResult:
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        path = generation.path / "validation-result.json"
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result


class StubRepairer(CandidateRepairer):
    """The real predicate, a stubbed subprocess."""

    def __init__(self) -> None:
        self.calls = 0

    async def autofix(self, generation: Generation) -> dict[str, object]:
        self.calls += 1
        return {
            "command": " ".join(self.AUTOFIX),
            "exit_code": 0,
            "output": "Found 1 error (1 fixed, 0 remaining).\n",
        }


async def evolving(
    tmp_path: Path,
    project: Path,
    replies: list[str],
    validator: ScriptedValidator,
    repairer: CandidateRepairer | None = None,
) -> tuple[EnvironmentEvolver, CycleContext, MockProvider]:
    from evomesh.storage import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    provider = MockProvider(list(replies))
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
    context = CycleContext(
        definition=definition,
        provider=provider,
        memory=memory,
        budget=MemoryBudget(),
        services={"evolver": evolver},
    )
    return evolver, context, provider


async def test_a_fixable_lint_failure_is_repaired_without_the_model(
    tmp_path: Path, project: Path
) -> None:
    validator = ScriptedValidator([failing("uv run ruff check .", RUFF_FIXABLE), passing()])
    repairer = StubRepairer()
    evolver, context, provider = await evolving(
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
    # The linter fixed it, so the mutation is still the only model call.
    assert len(provider.calls) == 1
    assert "ruff --fix" in repaired.summary

    passed = await behavior.cycle(context)
    assert "passed after 1 repair" in passed.summary
    assert (await evolver.pipeline_state())["stage"] == "report"

    report = await behavior.cycle(context)
    assert report.phase is AgentPhase.WAITING_HUMAN
    assert "validation passed after 1 repair attempt" in report.summary
    # A repaired candidate is promotable, not failed.
    assert evolver.candidate(2).status is not GenerationStatus.FAILED


async def test_a_failure_the_linter_cannot_fix_goes_to_the_model(
    tmp_path: Path, project: Path
) -> None:
    validator = ScriptedValidator([failing("uv run pytest", PYTEST_OUTPUT), passing()])
    repairer = StubRepairer()
    evolver, context, provider = await evolving(
        tmp_path, project, [MUTATION, REPAIR], validator, repairer
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2)

    for _ in range(4):  # plan, propose, validate, repair
        await behavior.cycle(context)

    assert repairer.calls == 0
    prompt = str(provider.calls[-1]["prompt"])
    # The model is shown the command, the real output, and the file it wrote.
    assert "uv run pytest" in prompt
    assert "assert ACTIVE is True" in prompt
    assert "ACTIVE = False" in prompt
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
    # A different failure each time, so the stall guard never fires.
    validator = ScriptedValidator(
        [
            failing("uv run ruff check .", RUFF_FIXABLE),
            failing("uv run ruff check .", RUFF_FIXABLE + "one\n"),
            failing("uv run ruff check .", RUFF_FIXABLE + "two\n"),
        ]
    )
    repairer = StubRepairer()
    evolver, context, _ = await evolving(tmp_path, project, [MUTATION], validator, repairer)
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2)

    for _ in range(7):  # plan, propose, (validate, repair) x 2, validate
        await behavior.cycle(context)

    assert repairer.calls == 2
    assert (await evolver.pipeline_state())["stage"] == "report"
    report = await behavior.cycle(context)
    assert "validation failed after 2 repair attempts" in report.summary
    assert evolver.candidate(2).status is GenerationStatus.FAILED


async def test_an_unusable_repair_answer_still_reaches_the_human(
    tmp_path: Path, project: Path
) -> None:
    validator = ScriptedValidator([failing("uv run pytest", PYTEST_OUTPUT)])
    evolver, context, _ = await evolving(
        tmp_path, project, [MUTATION, "I would rather not."], validator, StubRepairer()
    )
    behavior = EvolverBehavior(auto_validate=True, max_repairs=2)

    for _ in range(3):  # plan, propose, validate
        await behavior.cycle(context)
    broken = await behavior.cycle(context)

    assert "no repair could be authored" in broken.summary
    # Not back to plan: that would strand this candidate and open another.
    assert (await evolver.pipeline_state())["stage"] == "report"
    assert len(evolver.workspace.supervisor.candidates()) == 1


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
    evolver, context, provider = await evolving(
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
    # No repair was attempted, mechanically or with the model.
    assert repairer.calls == 0
    assert len(provider.calls) == 1
    assert validator.calls == 1

    report = await behavior.cycle(context)
    assert "this machine blocked the run (PermissionError)" in report.summary
    assert evolver.candidate(2).status is GenerationStatus.CANDIDATE


# -- deciding without a human --------------------------------------------


async def test_a_validated_candidate_is_promoted_without_asking(
    tmp_path: Path, project: Path
) -> None:
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
        tmp_path, project, [MUTATION, "not a mutation"], validator, StubRepairer()
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
