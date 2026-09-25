from pathlib import Path

from evomesh.contracts import AgentDefinition, Goal, MindState
from evomesh.procedural_learning import ExecutionTrace, ProcedureLearner, goal_signature
from tests.test_bdi import ScriptedProvider, planning_calls, worker


def trace(*, succeeded: bool = True) -> ExecutionTrace:
    return ExecutionTrace(
        goal_type="health-check",
        context_signature="provider=unhealthy",
        plan_name="model",
        steps=["ping provider", "restart provider", "verify health"],
        tools=["provider.health"],
        agents=["guardian"],
        succeeded=succeeded,
        model_calls=0,
    )


def test_one_success_is_not_promoted() -> None:
    mind = MindState()
    learner = ProcedureLearner(minimum_successes=3)
    item = trace()
    assert learner.observe(mind, item) is None
    assert learner.candidate(mind, item.pattern) is None


def test_repeated_clean_successes_promote_without_model_reasoning() -> None:
    mind = MindState()
    learner = ProcedureLearner(minimum_successes=3)
    learned = [learner.observe(mind, trace()) for _ in range(3)]

    procedure = learned[-1]
    assert learned[:2] == [None, None]
    assert procedure is not None
    assert procedure.successes == 3
    assert mind.procedures[procedure.name].steps[1] == "restart provider"


def test_any_failure_prevents_automatic_promotion() -> None:
    mind = MindState()
    learner = ProcedureLearner(minimum_successes=2)
    learner.observe(mind, trace(succeeded=False))
    learner.observe(mind, trace())
    assert learner.observe(mind, trace()) is None
    assert not mind.procedures


def test_human_can_approve_a_successful_trace_early() -> None:
    mind = MindState()
    learner = ProcedureLearner(minimum_successes=3)
    item = trace()
    learner.observe(mind, item)
    procedure = learner.promote(mind, item.pattern, human_approved=True)
    assert procedure is not None and procedure.approved


def test_a_procedure_that_keeps_failing_is_forgotten() -> None:
    mind = MindState()
    learner = ProcedureLearner(minimum_successes=2)
    for _ in range(2):
        learner.observe(mind, trace())
    assert mind.procedures

    learner.observe(mind, trace(succeeded=False))

    assert not mind.procedures


def test_traces_and_procedures_survive_serialization() -> None:
    mind = MindState()
    learner = ProcedureLearner(minimum_successes=2)
    for _ in range(2):
        learner.observe(mind, trace())

    restored = MindState.model_validate(mind.model_dump(mode="json"))

    assert len(restored.execution_traces) == 2
    goal = Goal(description="Provider=Unhealthy", kind="health-check")
    assert goal_signature(goal) == "provider=unhealthy"
    assert learner.match(restored, goal) is not None


async def test_a_plan_that_keeps_working_stops_costing_a_planning_call(tmp_path: Path) -> None:
    """Three identical successful model plans for the same standing goal
    become a procedure; the fourth pass reuses it instead of planning."""
    provider = ScriptedProvider()
    environment, agent = await worker(tmp_path, provider)
    agent.mind.goals[0].recurring = True

    for _ in range(3 * 3 + 1):  # three passes of three steps, then one more
        await environment.cycle_agent("Worker")

    assert planning_calls(provider) == 3
    assert agent.mind.intentions[-1].plan.startswith("learned:")
    stored = next(
        item for item in await environment.repository.load_agents() if item.id == agent.id
    )
    assert isinstance(stored, AgentDefinition)
    assert stored.mind.procedures
    await environment.stop()
