from evomesh.contracts import MindState
from evomesh.procedural_learning import ExecutionTrace, ProcedureLearner


def trace(*, succeeded: bool = True) -> ExecutionTrace:
    return ExecutionTrace(
        goal_type="health-check",
        context_signature="provider=unhealthy",
        plan_name="recover-provider",
        steps=["ping provider", "restart provider", "verify health"],
        tools=["provider.health"],
        agents=["guardian"],
        succeeded=succeeded,
        model_calls=0,
    )


def test_one_success_is_not_promoted() -> None:
    learner = ProcedureLearner(minimum_successes=3)
    item = trace()
    learner.record(item)
    assert learner.candidate(item.pattern) is None


def test_repeated_clean_successes_promote_without_model_reasoning() -> None:
    learner = ProcedureLearner(minimum_successes=3)
    items = [trace() for _ in range(3)]
    for item in items:
        learner.record(item)
    mind = MindState()

    procedure = learner.promote(mind, items[0].pattern)

    assert procedure is not None
    assert procedure.successes == 3
    assert mind.procedures[procedure.name].steps[1] == "restart provider"


def test_any_failure_prevents_automatic_promotion() -> None:
    learner = ProcedureLearner(minimum_successes=2)
    items = [trace(), trace(), trace(succeeded=False)]
    for item in items:
        learner.record(item)
    assert learner.candidate(items[0].pattern) is None


def test_human_can_approve_a_successful_trace_early() -> None:
    learner = ProcedureLearner(minimum_successes=3)
    item = trace()
    learner.record(item)
    mind = MindState()
    assert learner.promote(mind, item.pattern, human_approved=True) is not None
