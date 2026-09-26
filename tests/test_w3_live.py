"""W3 through the running mesh (closure audit 9739188, E01).

test_w3_improvement.py proves the candidate and validation half with a
scripted harness. This joins the boundaries it leaves out: the real
Environment's capability routing with two eligible, differently identified
code agents, the real harness queue and worker running the tool loop under
the selected agent's identity and a grant scoped to the candidate, a separate
read-only review job, the retained acceptance check bound to the same
candidate revision, and the post-change probe -- one trace, nothing inline.

The model is scripted (plan: a scripted provider is acceptable for
orchestration); every other component is the production one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evomesh.behaviors import EvolverBehavior
from evomesh.config import HarnessSettings
from evomesh.contracts import AgentDefinition, AgentStatus
from evomesh.environment import Environment
from evomesh.evolution import CandidateWorkspace, EnvironmentEvolver
from evomesh.harness_queue import HarnessGateway
from evomesh.improvements import CODE_EDIT_CAPABILITY, ImprovementStatus, ReviewVerdict
from evomesh.models import ChatTurn, MockProvider, ToolCall
from tests.test_bdi import settings_for
from tests.test_cycles import git_project
from tests.test_w3_improvement import (
    DEFECT,
    FIXED,
    AcceptanceValidator,
    _acceptance,
    _fixture,
    _real_baseline,
)


async def test_w3_live_the_routed_agent_runs_the_real_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _fixture(root)
    project = await git_project(root)
    provider = MockProvider(
        ["the pipeline asks the model nothing outside a job"],
        turns=[
            ChatTurn(
                tool_calls=[
                    ToolCall(
                        name="edit",
                        arguments={
                            "path": "src/evomesh/pricing.py",
                            "old": "return sum(items) + 1",
                            "new": "return sum(items)",
                        },
                    )
                ]
            ),
            ChatTurn(text="RATIONALE: total() added a stray 1; it now returns the plain sum"),
            ChatTurn(text="Read the diff against the objective.\nVERDICT: COMPLETE"),
        ],
    )
    settings = settings_for(tmp_path)
    settings.harness = HarnessSettings(enabled=True, allow_write=True)
    settings.evolution.review = True
    environment = Environment(settings, {"ollama": provider})
    await environment.start()
    validator = AcceptanceValidator()
    # The fixture project is what this mesh evolves: its evolver, pointed there.
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"),
        environment.repository,
        provider,
        validator,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(evolver, "baseline", _real_baseline(evolver, project))
    environment.evolver = evolver
    # Two eligible code agents, differently identified. Only the one that
    # owns the candidate pipeline can carry a generation out, so routing
    # must pick it deterministically -- never "whichever bid first".
    owner = AgentDefinition(
        name="Pipeline Owner",
        purpose="Runs the candidate pipeline",
        status=AgentStatus.ACTIVE,
        capabilities=[CODE_EDIT_CAPABILITY],
    )
    owner.mind.add_goal("Improve the fixture", recurring=True)
    other = AgentDefinition(
        name="Other Coder",
        purpose="Also edits code",
        status=AgentStatus.ACTIVE,
        capabilities=[CODE_EDIT_CAPABILITY],
    )
    environment.behaviors[owner.id] = EvolverBehavior(
        auto_validate=True,
        max_repairs=0,
        auto_promote=True,
        review=True,
        baseline_tests=True,
        test_backlog=False,
        scout_when_idle=False,
    )
    for agent in (owner, other):
        await environment.register_agent(agent)
        await environment.start_agent(agent.id, start_delay=3600)
    harness = environment.harness
    assert isinstance(harness, HarnessGateway), "the real queue, not a fake"
    target = project / "src" / "evomesh" / "pricing.py"
    assert target.read_text(encoding="utf-8") == DEFECT
    assert _acceptance(project)[0] is False, "the retained check fails on the live tree first"

    # The owner's own runtime cycles, as it would on the mesh.
    for _ in range(400):
        await environment.cycle_agent(owner.name)
        if target.read_text(encoding="utf-8") == FIXED:
            break
        await asyncio.sleep(0.02)
    assert target.read_text(encoding="utf-8") == FIXED, "the fix never landed"

    plane = environment.improvements
    item = next(iter(plane.backlog.items.values()))
    work = plane.backlog.work_items[item.work_item_ids[0]]
    # Routing: both bid, the pipeline owner was selected, by identity.
    bids = environment.contract_net.bids(work, states=environment.runtime_states())
    assert {bid.agent_id for bid in bids} >= {owner.id, other.id}
    assert work.assigned_agent_id == owner.id != other.id
    assert work.inputs["handle"]["assignee"] == owner.id
    # The jobs that ran are the queue's, under that identity, in that scope.
    jobs = sorted(harness.queue.jobs.values(), key=lambda job: job.number)
    generation_path = Path(work.inputs["handle"]["workspace"])
    assert [job.agent_id for job in jobs] == [owner.id, owner.id]
    assert all(job.root == generation_path for job in jobs)
    implement, review = jobs
    assert implement.allow_write is True
    assert review.allow_write is False, "the review ran read-only"
    assert "VERDICT" in review.objective and "VERDICT" not in implement.objective
    grants = await environment.repository.load_grants(owner.id)
    assert any(Path(grant.path) == generation_path for grant in grants)
    assert not any(Path(grant.path) == project for grant in grants), "no grant on the live tree"
    assert not await environment.repository.load_grants(other.id), "the other coder got nothing"
    # The model's turns were the job's: the edit came from the tool loop.
    assert len(provider.chats) >= 3
    # Review and acceptance bound to the final candidate revision.
    assert validator.runs == [True]
    assert item.review_verdict is ReviewVerdict.COMPLETE
    assert item.review_revision and item.review_revision == item.validation_revision

    # The post-change probe: the retained check against the promoted tree.
    await environment.cycle_agent(owner.name)
    assert _acceptance(project)[0] is True
    assert item.status is ImprovementStatus.VERIFIED, (item.status, item.inconclusive_reason)
    assert item.verification is not None and item.verification.observers == ["baseline_suite"]
    await environment.stop()
