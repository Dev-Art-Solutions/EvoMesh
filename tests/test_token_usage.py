"""Real token counts, as the model server reported them, reach telemetry."""

from __future__ import annotations

import httpx
import pytest

from evomesh.cognitive_services import (
    CognitiveModelService,
    CognitiveServiceType,
    ModelInvocationReason,
)
from evomesh.models import MockProvider, OllamaProvider


async def test_ollama_token_counts_are_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_post(
        self: httpx.AsyncClient, url: str, *, json: dict[str, object]
    ) -> httpx.Response:
        request = httpx.Request("POST", url)
        if url.endswith("/api/generate"):
            body = {"response": "ok", "prompt_eval_count": 812, "eval_count": 37}
        else:
            body = {"message": {"content": "ok"}, "prompt_eval_count": 90, "eval_count": 4}
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    service = CognitiveModelService()
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen3")

    await service.generate(
        provider,
        "hello",
        service=CognitiveServiceType.EXECUTE_STEP,
        reason=ModelInvocationReason.PLAN_STEP_REQUIRES_REASONING,
    )
    await service.chat(
        provider,
        [],
        service=CognitiveServiceType.TOOL_LOOP,
        reason=ModelInvocationReason.TOOL_SELECTION_REQUIRES_MODEL,
    )

    first, second = service.metrics.records
    assert (first.input_tokens, first.output_tokens) == (812, 37)
    assert (second.input_tokens, second.output_tokens) == (90, 4)
    snapshot = service.metrics.snapshot()
    assert snapshot["input_tokens"] == 902
    assert snapshot["calls_with_token_counts"] == 2


async def test_a_server_that_does_not_count_leaves_tokens_unknown() -> None:
    """Never a stale count from the previous call, never a made-up one."""
    service = CognitiveModelService()
    await service.generate(
        MockProvider(),
        "hello",
        service=CognitiveServiceType.EXECUTE_STEP,
        reason=ModelInvocationReason.PLAN_STEP_REQUIRES_REASONING,
    )
    assert service.metrics.records[0].input_tokens is None
