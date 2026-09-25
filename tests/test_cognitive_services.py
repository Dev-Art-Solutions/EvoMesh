from __future__ import annotations

from pathlib import Path

import pytest

from evomesh.cognition import CycleContext
from evomesh.cognitive_services import (
    CognitiveMetrics,
    CognitiveModelService,
    CognitiveServiceType,
    ContextAssembler,
    ModelCallStatus,
    ModelInvocationReason,
    TaskPacket,
)
from evomesh.contracts import AgentDefinition, Belief
from evomesh.memory import AgentMemory, MemoryBudget
from evomesh.models import ChatMessage, ChatTurn, MockProvider, ToolCall


async def test_generate_records_the_reason_identity_sizes_and_result() -> None:
    provider = MockProvider(["done"])
    cognition = CognitiveModelService()

    answer = await cognition.generate(
        provider,
        "make a plan",
        service=CognitiveServiceType.CREATE_NOVEL_PLAN,
        reason=ModelInvocationReason.NO_PLAN_MATCH,
        provider_name="mock",
        agent_id="agent-1",
        goal_id="goal-1",
        model="small",
        system="planner",
    )

    assert answer == "done"
    call = cognition.metrics.records[0]
    assert call.service is CognitiveServiceType.CREATE_NOVEL_PLAN
    assert call.reason is ModelInvocationReason.NO_PLAN_MATCH
    assert call.provider == "mock"
    assert call.agent_id == "agent-1"
    assert call.goal_id == "goal-1"
    assert call.input_chars >= len("make a plan") + len("planner")
    assert call.output_chars == len("done")
    assert call.status is ModelCallStatus.SUCCEEDED


async def test_failed_call_is_recorded_and_the_original_error_is_raised() -> None:
    class Broken(MockProvider):
        async def generate(self, prompt: str, **kwargs: object) -> str:
            raise RuntimeError("provider offline")

    cognition = CognitiveModelService()

    with pytest.raises(RuntimeError, match="provider offline"):
        await cognition.generate(
            Broken(),
            "task",
            service=CognitiveServiceType.SYNTHESIZE_EVIDENCE,
            reason=ModelInvocationReason.SYNTHESIS_REQUIRED,
        )

    call = cognition.metrics.records[0]
    assert call.status is ModelCallStatus.FAILED
    assert call.error == "provider offline"
    assert call.output_chars == 0


async def test_chat_records_tool_call_output_size() -> None:
    provider = MockProvider(
        turns=[
            ChatTurn(
                text="",
                tool_calls=[ToolCall(id="1", name="read", arguments={"path": "a.py"})],
            )
        ]
    )
    cognition = CognitiveModelService()

    await cognition.chat(
        provider,
        [ChatMessage(role="user", content="inspect")],
        service=CognitiveServiceType.TOOL_LOOP,
        reason=ModelInvocationReason.TOOL_SELECTION_REQUIRES_MODEL,
    )

    assert cognition.metrics.records[0].output_chars > 0


def test_metrics_history_is_bounded_and_snapshot_is_structured() -> None:
    metrics = CognitiveMetrics(max_records=2)
    service = CognitiveModelService(metrics)
    provider = MockProvider(["ok"])

    async def make_calls() -> None:
        for _ in range(3):
            await service.generate(
                provider,
                "x",
                service=CognitiveServiceType.DIRECT_INFERENCE,
                reason=ModelInvocationReason.EXPLICIT_INFERENCE_REQUEST,
            )

    import asyncio

    asyncio.run(make_calls())
    snapshot = metrics.snapshot()
    assert len(metrics.records) == 2
    assert snapshot["calls"] == 2
    assert snapshot["by_service"] == {"direct_inference": 2}


def test_task_packet_is_labelled_and_hard_budgeted() -> None:
    packet = TaskPacket(
        role="Researcher",
        operation=CognitiveServiceType.SYNTHESIZE_EVIDENCE,
        task="Compare the evidence",
        goal="Choose the relevant differences",
        beliefs="price.a = 1\nprice.b = 2",
        artifacts=("a.json", "b.json"),
        output_contract='{"differences": []}',
    )

    rendered = ContextAssembler(160).assemble(packet)

    assert rendered.startswith("GOAL: Choose the relevant differences")
    assert len(rendered) <= 160


async def test_cycle_context_records_narrow_service_and_filters_beliefs(
    tmp_path: Path,
) -> None:
    provider = MockProvider(["answer"])
    definition = AgentDefinition(name="Worker", purpose="Work")
    definition.mind.revise(
        [
            Belief(key="relevant", statement="keep this"),
            Belief(key="unrelated", statement="drop this"),
        ]
    )
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    context = CycleContext(
        definition=definition,
        provider=provider,
        memory=memory,
        budget=MemoryBudget(),
    )

    await context.think(
        "Interpret it",
        service=CognitiveServiceType.INTERPRET_UNSTRUCTURED_INPUT,
        reason=ModelInvocationReason.AMBIGUOUS_INPUT,
        relevant_belief_keys=("relevant",),
    )

    prompt = str(provider.calls[0]["prompt"])
    assert "keep this" in prompt
    assert "drop this" not in prompt
    record = context.cognitive.metrics.records[0]
    assert record.service is CognitiveServiceType.INTERPRET_UNSTRUCTURED_INPUT
    assert record.agent_id == definition.id
