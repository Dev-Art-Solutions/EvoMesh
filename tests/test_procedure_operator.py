"""Operator controls for typed procedures (closure plan v2 21, AC-22),
through the console a human actually types into."""

from __future__ import annotations

from pathlib import Path

from evomesh.console import ConsoleChannel
from evomesh.contracts import GoalStatus
from evomesh.procedure_runtime import AdmissionStatus, Selection
from tests.procedure_fixtures import BRANCHING
from tests.test_procedure_runtime_wiring import _mesh, _snapshot_goal

W1 = "local_json_snapshot@1"


async def test_list_show_and_validate_explain_without_running_anything(tmp_path: Path) -> None:
    environment, _, _, work = await _mesh(tmp_path)
    console = ConsoleChannel(environment)

    listed = await console.route("/typed")
    shown = await console.route(f"/typed show {W1}")
    validated = await console.route(f"/typed validate {W1}")

    assert "Typed execution is enabled." in listed
    assert f"{W1} promoted source=shipped by operator:iliya" in listed
    digest = environment.procedures.registry.definitions[W1].digest()
    assert f"digest: {digest}" in shown and "read_source[tool]" in shown
    assert "is valid" in validated and "Nothing was run" in validated
    assert not (work / "out").exists()
    await environment.stop()


async def test_approval_is_bound_to_the_typed_digest(tmp_path: Path) -> None:
    environment, _, _, _ = await _mesh(tmp_path)
    console = ConsoleChannel(environment)
    registry = environment.procedures.registry
    admission = await registry.register(BRANCHING, source="authored", owner="human")
    digest = registry.definitions[admission.key].digest()

    short = await console.route(f"/typed approve {admission.key} {digest[:6]}")
    wrong = await console.route(f"/typed approve {admission.key} {'0' * 16}")
    right = await console.route(f"/typed approve {admission.key} {digest[:16]}")

    assert short.startswith("Refused") and wrong.startswith("Refused")
    assert "promoted by operator:console" in right
    assert registry.admissions[admission.key].status is AdmissionStatus.PROMOTED
    await environment.stop()


async def test_a_learned_candidate_cannot_be_approved_before_validation(tmp_path: Path) -> None:
    environment, _, _, _ = await _mesh(tmp_path)
    console = ConsoleChannel(environment)
    registry = environment.procedures.registry
    admission = await registry.register(BRANCHING, source="learned", owner="learning")
    digest = registry.definitions[admission.key].digest()

    reply = await console.route(f"/typed approve {admission.key} {digest[:16]}")

    assert reply.startswith("Refused: NOT_VALIDATED")
    await environment.stop()


async def test_degrade_stops_selection_and_explain_says_why(tmp_path: Path) -> None:
    environment, agent, _, _ = await _mesh(tmp_path)
    console = ConsoleChannel(environment)
    goal = agent.mind.add_goal(
        "Snapshot",
        kind="local_json_snapshot",
        parameters={"source": "source.json", "destination": "out/s.json"},
    )

    before = await console.route(f"/typed explain {agent.name} {goal.id}")
    degraded = await console.route(f"/typed degrade {W1} wrong output seen in review")
    after = await console.route(f"/typed explain {agent.name} {goal.id}")

    assert f"match -> {W1}" in before
    assert "degraded" in degraded
    assert Selection.NO_MATCH.value in after
    await environment.stop()


async def test_executions_can_be_paused_resumed_and_cancelled(tmp_path: Path) -> None:
    environment, agent, _, work = await _mesh(tmp_path)
    console = ConsoleChannel(environment)
    goal_id = _snapshot_goal(agent)
    await environment.cycle_agent(agent.name)  # the read
    (execution,) = [
        line.split()[0]
        for line in (await console.route("/typed executions")).splitlines()
    ]

    paused = await console.route(f"/typed pause {execution}")
    await environment.cycle_agent(agent.name)
    held = await console.route(f"/typed execution {execution}")
    resumed = await console.route(f"/typed resume {execution}")
    cancelled = await console.route(f"/typed cancel {execution}")
    listed = await console.route(f"/typed executions {agent.name}")

    assert paused.endswith("is PAUSED.")
    assert "PAUSED" in held and "write_snapshot" not in held.split("done")[1].split("\n")[0]
    assert resumed.endswith("is RUNNING.")
    assert cancelled.endswith("is CANCELLED.")
    assert listed == "No open typed executions."
    assert not (work / "out" / "snapshot.json").exists()
    assert agent.mind.goal(goal_id).status is not GoalStatus.DONE
    await environment.stop()


async def test_reconcile_refuses_what_is_not_reconciling(tmp_path: Path) -> None:
    environment, agent, _, _ = await _mesh(tmp_path)
    console = ConsoleChannel(environment)
    _snapshot_goal(agent)
    await environment.cycle_agent(agent.name)
    (execution,) = [
        line.split()[0] for line in (await console.route("/typed executions")).splitlines()
    ]

    reply = await console.route(f"/typed reconcile {execution} not_applied")
    bad = await console.route(f"/typed reconcile {execution} shrug")

    assert "NOT_RECONCILING" in reply
    assert bad.startswith("Refused: UNKNOWN_DECISION")
    await environment.stop()


async def test_the_emergency_switch(tmp_path: Path) -> None:
    environment, _, _, _ = await _mesh(tmp_path)
    console = ConsoleChannel(environment)

    off = await console.route("/typed disable")
    listed = await console.route("/typed list")
    on = await console.route("/typed enable")

    assert "disabled" in off and "Typed execution is DISABLED." in listed
    assert on == "Typed execution is enabled."
    assert environment.procedures.registry.enabled is True
    await environment.stop()


async def test_improvements_verify_needs_a_verifying_item(tmp_path: Path) -> None:
    environment, _, _, _ = await _mesh(tmp_path)
    console = ConsoleChannel(environment)

    missing = await console.route("/improvements verify nope checked it")

    assert missing == "No improvement nope."
    await environment.stop()
