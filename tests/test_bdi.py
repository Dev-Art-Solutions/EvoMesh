"""What makes these agents BDI rather than a loop with nice field names."""

from datetime import timedelta
from pathlib import Path

from evomesh.bdi import (
    BDIBehavior,
    BDIReasoner,
    Desire,
    PlanLibrary,
    PlanRecipe,
    StepResult,
    parse_plan,
)
from evomesh.cognition import CycleContext, CycleOutcome
from evomesh.config import EvolutionSettings, RuntimeSettings, Settings
from evomesh.contracts import (
    AgentDefinition,
    AgentPhase,
    AgentStatus,
    Belief,
    BeliefChange,
    Goal,
    GoalStatus,
    Intention,
    IntentionStatus,
    MindState,
    PlanStep,
    StepStatus,
    now_utc,
)
from evomesh.environment import Environment
from evomesh.memory import AgentMemory, MemoryBudget
from evomesh.models import MockProvider

PLAN_OF_THREE = "1. open the folder\n2. read every note\n3. write the summary\n"
STEP_DONE = "RESULT: did it.\nFACT: NONE\nSTATUS: done\n"
PLANNING_MARKER = "Break this goal into"


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        data_path=tmp_path / "state.db",
        generation_path=tmp_path / "generations",
        workspace_path=tmp_path / "workspace",
        runtime=RuntimeSettings(cycle_seconds=3600, stagger_seconds=0),
        evolution=EvolutionSettings(autonomous=False),
    )


class ScriptedProvider(MockProvider):
    """Answers a planning prompt with a plan and anything else with a step result."""

    def __init__(self, plan: str = PLAN_OF_THREE, step: str = STEP_DONE) -> None:
        super().__init__()
        self.plan = plan
        self.step = step

    async def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        num_ctx: int | None = None,
    ) -> str:
        self.calls.append(
            {"prompt": prompt, "system": system, "model": model, "num_ctx": num_ctx}
        )
        return self.plan if PLANNING_MARKER in prompt else self.step


def planning_calls(provider: MockProvider) -> int:
    return sum(1 for call in provider.calls if PLANNING_MARKER in str(call["prompt"]))


async def worker(
    tmp_path: Path, provider: MockProvider, goal: str = "Summarize the notes"
) -> tuple[Environment, AgentDefinition]:
    environment = Environment(settings_for(tmp_path), {"ollama": provider})
    await environment.start()
    agent = AgentDefinition(name="Worker", purpose="Work", status=AgentStatus.ACTIVE)
    agent.mind.add_goal(goal)
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)
    return environment, agent


# -- belief revision ----------------------------------------------------


def test_a_percept_revises_a_belief_instead_of_stacking_beside_it() -> None:
    mind = MindState()
    first = mind.revise([Belief(key="provider.ready", statement="no")])
    second = mind.revise([Belief(key="provider.ready", statement="no")])
    third = mind.revise([Belief(key="provider.ready", statement="yes")])

    assert first.added == ("provider.ready",)
    assert not second, "re-perceiving the same fact is not a change"
    assert third.updated == ("provider.ready",)
    assert len(mind.beliefs) == 1
    assert mind.believes("provider.ready", "yes")


def test_the_belief_base_drops_the_least_recently_confirmed() -> None:
    mind = MindState()
    mind.revise([Belief(key=f"k{index}", statement=str(index)) for index in range(6)], keep=6)
    mind.revise([Belief(key="k0", statement="0")], keep=6)  # re-confirm the oldest
    mind.revise([Belief(key="fresh", statement="new")], keep=6)

    keys = {item.key for item in mind.beliefs}
    assert "fresh" in keys
    assert "k0" in keys, "a fact that keeps being re-perceived is still current"
    assert "k1" not in keys


# -- commitment ---------------------------------------------------------


async def test_the_agent_plans_once_and_then_executes_the_plan(tmp_path: Path) -> None:
    """Commitment: one planning call per goal, not one per cycle."""
    provider = ScriptedProvider()
    environment, agent = await worker(tmp_path, provider)

    for _ in range(3):
        await environment.cycle_agent("Worker")

    assert planning_calls(provider) == 1
    intention = agent.mind.intentions[-1]
    assert intention.plan == "model"
    assert [step.description for step in intention.steps] == [
        "open the folder",
        "read every note",
        "write the summary",
    ]
    assert all(step.status is StepStatus.DONE for step in intention.steps)
    await environment.stop()


async def test_one_plan_step_is_executed_per_cycle(tmp_path: Path) -> None:
    provider = ScriptedProvider()
    environment, agent = await worker(tmp_path, provider)

    first = await environment.cycle_agent("Worker")
    assert first.step == "open the folder"
    assert agent.mind.current_intention().cursor == 1  # type: ignore[union-attr]

    second = await environment.cycle_agent("Worker")
    assert second.step == "read every note"
    assert not second.goal_done, "the plan is not finished yet"

    third = await environment.cycle_agent("Worker")
    assert third.step == "write the summary"
    assert third.goal_done, "the goal is met once the plan is exhausted"
    await environment.stop()


async def test_a_finished_plan_is_marked_achieved_and_a_new_one_is_adopted(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider()
    environment, agent = await worker(tmp_path, provider)
    agent.mind.goals[0].recurring = True  # a standing job re-plans after each pass

    for _ in range(4):
        await environment.cycle_agent("Worker")

    statuses = [item.status for item in agent.mind.intentions]
    assert IntentionStatus.ACHIEVED in statuses
    assert agent.mind.current_intention() is not None, "it committed again"
    assert planning_calls(provider) == 2, "one planning call per pass, not per cycle"
    await environment.stop()


# -- reconsideration ----------------------------------------------------


def test_reconsideration_is_triggered_by_the_things_that_matter() -> None:
    reasoner = BDIReasoner()
    mind = MindState()
    goal = mind.add_goal("do the work")
    intention = mind.commit(goal.id, ["a", "b"], context_keys=["world.state"])
    quiet = BeliefChange()

    assert reasoner.reconsider(intention, mind, quiet) is None, "commitment holds"

    changed = BeliefChange(updated=("world.state",))
    assert reasoner.reconsider(intention, mind, changed) is not None

    unrelated = BeliefChange(updated=("something.else",))
    assert reasoner.reconsider(intention, mind, unrelated) is None

    urgent = mind.add_goal("drop everything", priority=1)
    assert "higher-priority" in (reasoner.reconsider(intention, mind, quiet) or "")

    urgent.status = GoalStatus.DONE
    goal.status = GoalStatus.DONE
    assert "no longer open" in (reasoner.reconsider(intention, mind, quiet) or "")

    assert reasoner.reconsider(None, mind, quiet) is not None


async def test_a_higher_priority_goal_takes_the_commitment(tmp_path: Path) -> None:
    provider = ScriptedProvider()
    environment, agent = await worker(tmp_path, provider)
    await environment.cycle_agent("Worker")
    assert agent.mind.current_intention().steps[0].description == "open the folder"  # type: ignore[union-attr]

    urgent = agent.mind.add_goal("Handle the incident", priority=1)
    await environment.cycle_agent("Worker")

    intention = agent.mind.current_intention()
    assert intention is not None
    assert intention.goal_id == urgent.id
    dropped = [item for item in agent.mind.intentions if item.status is IntentionStatus.DROPPED]
    assert dropped, "the previous commitment was abandoned, not silently kept"
    await environment.stop()


# -- a goal's own cadence, independent of the agent's cycle_seconds -------


def test_a_goal_in_cooldown_is_not_open() -> None:
    """interval_seconds is not a cycle counter -- it is a real clock, checked
    against next_attempt_at rather than counted in ticks, so it means the
    same thing regardless of how fast the agent's own cycle_seconds runs."""
    goal = Goal(description="Check example.com", recurring=True, interval_seconds=3600)
    assert goal.is_open, "no cooldown set yet -- due immediately"

    goal.next_attempt_at = now_utc() + timedelta(seconds=3600)
    assert not goal.is_open, "still inside its own interval"

    goal.next_attempt_at = now_utc() - timedelta(seconds=1)
    assert goal.is_open, "the interval has passed"


def test_next_goal_skips_a_cooldown_and_surfaces_other_work() -> None:
    """The reason this exists at all: an agent with one goal on an hourly
    cadence must not go quiet for everything else in between. A lower-
    priority goal that is actually due gets the commitment instead of the
    higher-priority one still in cooldown."""
    mind = MindState()
    hourly = mind.add_goal("Check example.com", priority=1, interval_seconds=3600)
    hourly.next_attempt_at = now_utc() + timedelta(seconds=3600)
    chat_reply = mind.add_goal("Answer what was asked", priority=5)

    assert mind.next_goal() is chat_reply


async def test_a_finished_goal_with_an_interval_waits_before_reopening(
    tmp_path: Path,
) -> None:
    """Exercises the actual wiring in AgentRuntime._apply, not just the Goal
    model in isolation: a real goal_done outcome sets next_attempt_at, and a
    recurring goal stays open (not DONE) while it waits."""
    environment, agent = await worker(tmp_path, ScriptedProvider())
    goal = agent.mind.add_goal(
        "Check example.com", priority=1, recurring=True, interval_seconds=3600
    )
    runtime = environment.runtimes[agent.id]

    await runtime._apply(  # noqa: SLF001 - exercising the wiring directly, not through a full cycle
        CycleOutcome(summary="fetched it", goal_done=True, phase=AgentPhase.IDLE, worked=True),
        goal,
    )

    assert goal.status is not GoalStatus.DONE
    assert goal.next_attempt_at is not None
    assert goal.next_attempt_at > now_utc()
    assert not goal.is_open
    await environment.stop()


def test_a_cron_goal_waits_for_its_first_scheduled_time() -> None:
    """Unlike interval_seconds, a cron schedule is an appointment: adding the
    goal must not make it due right away just because it is new."""
    mind = MindState()
    goal = mind.add_goal("Check example.com", cron_expression="0 * * * *")

    assert goal.next_attempt_at is not None
    assert not goal.is_open
    assert goal.recurring is False, "add_goal itself does not force recurring -- the console does"


async def test_a_finished_cron_goal_reschedules_to_its_next_occurrence(
    tmp_path: Path,
) -> None:
    """Exercises AgentRuntime._apply's cron branch: finishing a cycle moves
    next_attempt_at to the next matching time, not a fixed offset."""
    environment, agent = await worker(tmp_path, ScriptedProvider())
    goal = agent.mind.add_goal(
        "Check example.com", priority=1, recurring=True, cron_expression="0 * * * *"
    )
    runtime = environment.runtimes[agent.id]

    await runtime._apply(  # noqa: SLF001 - exercising the wiring directly, not through a full cycle
        CycleOutcome(summary="fetched it", goal_done=True, phase=AgentPhase.IDLE, worked=True),
        goal,
    )

    assert goal.next_attempt_at is not None
    assert goal.next_attempt_at.minute == 0
    assert goal.next_attempt_at > now_utc()
    assert not goal.is_open
    await environment.stop()


# -- means-ends reasoning -----------------------------------------------


async def test_a_library_plan_is_used_without_calling_the_model(tmp_path: Path) -> None:
    provider = ScriptedProvider()
    environment = Environment(settings_for(tmp_path), {"ollama": provider})
    await environment.start(start_agent_loops=True)

    await environment.cycle_agent("guardian")

    guardian = environment.registry.get("guardian")
    intention = guardian.mind.intentions[-1]
    assert intention.plan in {"health-sweep", "investigate-degradation"}
    assert provider.calls == [], "a deterministic agent must not touch the model"
    await environment.stop()


async def test_a_model_that_is_down_still_yields_a_usable_plan(tmp_path: Path) -> None:
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

    environment, agent = await worker(tmp_path, Broken(), goal="Keep the notes tidy")

    outcome = await environment.cycle_agent("Worker")

    intention = agent.mind.intentions[-1]
    assert intention.plan == "ad-hoc"
    assert intention.steps[0].description == "Keep the notes tidy"
    assert outcome.error is not None, "planning degraded, execution still reported the failure"
    await environment.stop()


def test_plan_parsing_rejects_a_reply_that_is_not_a_plan() -> None:
    assert parse_plan("1. one\n2. two") == ["one", "two"]
    assert parse_plan("- alpha\n- beta") == ["alpha", "beta"]
    # A model that ignores the format and answers in fields has given no plan.
    assert parse_plan("STEP: x\nRESULT: y\nDONE: no") == []
    assert len(parse_plan("\n".join(f"{i}. step" for i in range(9)))) == 4


# -- dropping an impossible intention ------------------------------------


async def test_a_blocked_step_drops_the_intention_and_blocks_the_goal(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(step="RESULT: no access to the folder\nSTATUS: blocked\n")
    environment, agent = await worker(tmp_path, provider)

    outcome = await environment.cycle_agent("Worker")

    assert "impossible" in outcome.summary
    assert agent.mind.intentions[-1].status is IntentionStatus.IMPOSSIBLE
    assert agent.mind.goals[0].status is GoalStatus.BLOCKED
    assert agent.mind.current_intention() is None
    await environment.stop()


# -- option generation ----------------------------------------------------


async def test_a_behavior_can_generate_a_desire_that_becomes_a_goal(
    tmp_path: Path,
) -> None:
    class Ambitious(BDIBehavior):
        name = "ambitious"

        async def perceive(self, context: CycleContext) -> list[Belief]:
            return [Belief(key="disk.full", statement="the disk is full")]

        async def options(
            self, context: CycleContext, change: BeliefChange
        ) -> list[Desire]:
            if "disk.full" in change.keys:
                return [Desire("Free up disk space", priority=1)]
            return []

        def library(self) -> PlanLibrary:
            return PlanLibrary(
                (PlanRecipe(name="tidy", steps=("delete old candidates",)),)
            )

        async def execute(
            self, context: CycleContext, intention: Intention, step: PlanStep
        ) -> StepResult:
            return StepResult(summary=f"ran: {step.description}")

    definition = AgentDefinition(name="Ambitious", purpose="Keep the disk clean")
    definition.mind.add_goal("Routine upkeep", priority=5)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
    )

    outcome = await Ambitious().cycle(context)

    assert any(goal.description == "Free up disk space" for goal in definition.mind.goals)
    # The new desire outranks the routine goal, so that is what it committed to.
    intention = definition.mind.intentions[-1]
    assert definition.mind.goal(intention.goal_id).description == "Free up disk space"
    assert intention.plan == "tidy"
    assert "delete old candidates" in outcome.summary


# -- the prompt carries the mental state ---------------------------------


async def test_beliefs_and_the_committed_plan_reach_the_prompt(tmp_path: Path) -> None:
    definition = AgentDefinition(name="Grounded", purpose="Stay grounded")
    goal = definition.mind.add_goal("Tidy the notes")
    definition.mind.revise([Belief(key="notes.count", statement="there are 12 notes")])
    definition.mind.commit(goal.id, ["open the folder", "sort the notes"], plan="model")
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
    )

    prompt = await context.build_prompt("do the next step")

    assert "there are 12 notes" in prompt
    assert "open the folder" in prompt
    assert "You are on: open the folder" in prompt


async def test_the_console_shows_beliefs_and_intentions(tmp_path: Path) -> None:
    from evomesh.console import ConsoleChannel

    environment = Environment(settings_for(tmp_path), {"ollama": ScriptedProvider()})
    await environment.start(start_agent_loops=True)
    await environment.cycle_agent("guardian")
    console = ConsoleChannel(environment)

    beliefs = await console.route("/beliefs guardian")
    intentions = await console.route("/intentions guardian")

    assert "provider.ready" in beliefs
    assert "mesh.degraded" in beliefs
    assert "plan '" in intentions
    assert "[x]" in intentions or "[ ]" in intentions
    await environment.stop()


async def test_the_evolver_keeps_one_commitment_across_the_whole_pipeline(
    tmp_path: Path,
) -> None:
    """A plan that advances its own state must not treat that as a reason to re-plan."""
    from evomesh.behaviors import EvolverBehavior
    from evomesh.evolution import CandidateWorkspace, EnvironmentEvolver, ValidationResult
    from evomesh.storage import SQLiteRepository

    from .fakes import FakeHarness

    class StubValidator:
        async def validate(self, generation: object) -> ValidationResult:
            return ValidationResult(passed=True, commands=[{"command": "stub", "exit_code": 0}])

    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("ACTIVE = True\n", encoding="utf-8")
    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    mutation = '{"relative_path": "src/app.py", "content": "X = 1\\n", "rationale": "flip"}'
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"),
        repository,
        MockProvider([mutation]),
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
        services={"evolver": evolver, "harness": FakeHarness([[("src/app.py", "X = 1\n")]])},
    )
    behavior = EvolverBehavior(auto_validate=True)

    for _ in range(4):
        await behavior.cycle(context)

    committed = definition.mind.intentions
    assert len(committed) == 1, "one plan carried the whole pipeline, not one per stage"
    intention = committed[0]
    assert intention.plan == "evolve-generation"
    # Repair is the fourth step and nothing broke, so it is the one box the
    # checklist honestly leaves unticked.
    assert [step.status for step in intention.steps] == [
        StepStatus.DONE,
        StepStatus.DONE,
        StepStatus.DONE,
        StepStatus.PENDING,
        StepStatus.DONE,
    ]
    assert (await evolver.pipeline_state())["stage"] == "await-human"


async def test_a_waiting_evolver_keeps_its_commitment_instead_of_re_adopting(
    tmp_path: Path,
) -> None:
    """Parked on a human decision, it must not burn one plan per cycle."""
    from evomesh.behaviors import EvolverBehavior
    from evomesh.evolution import CandidateWorkspace, EnvironmentEvolver
    from evomesh.storage import SQLiteRepository

    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("ACTIVE = True\n", encoding="utf-8")
    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"), repository, MockProvider()
    )
    await evolver.set_pipeline_state({"stage": "await-human", "generation": 2})
    definition = AgentDefinition(name="Environment Evolver", purpose="Evolve")
    definition.mind.add_goal("Improve EvoMesh", recurring=True)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
        services={"evolver": evolver},
    )
    behavior = EvolverBehavior(auto_validate=False)

    outcome = await behavior.cycle(context)
    for _ in range(3):
        outcome = await behavior.cycle(context)

    assert "waiting for a human" in outcome.summary
    assert len(definition.mind.intentions) == 1, "one held commitment, not four"
    intention = definition.mind.intentions[0]
    assert intention.status is IntentionStatus.ACTIVE
    assert intention.cursor == 0, "a held step is not consumed"
    # With validation off, the plan never advertises a step that will not run.
    assert "validate the candidate" not in [step.description for step in intention.steps]
