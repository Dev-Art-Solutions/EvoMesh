"""OpenAICompatibleProvider's Gemini thought_signature round-trip.

Found live, 2026-09-23: a real repair job on gemini-3.5-flash-lite 400'd on
its second turn -- "Function call is missing a thought_signature ... required
for tools to work correctly" -- because Gemini's OpenAI-compatible layer
requires that value, minted on the turn that made a tool call, echoed back
on that exact tool_calls entry whenever it is replayed as conversation
history. This project's own _wire() never carried it, so any second native-
tool turn against Gemini was one 400 away from silently downgrading the
whole job to the much weaker text protocol.
"""

from __future__ import annotations

from typing import Any

import pytest

from evomesh.models import ChatMessage, OpenAICompatibleProvider, ToolCall


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    captured: dict[str, Any] = {}
    payload: dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def post(
        self, url: str, *, headers: dict[str, str], json: dict[str, Any]
    ) -> _FakeResponse:
        _FakeAsyncClient.captured = {"url": url, "headers": headers, "json": json}
        return _FakeResponse(_FakeAsyncClient.payload)


async def test_a_returned_thought_signature_is_captured_onto_the_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("evomesh.models.httpx.AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.payload = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "grep", "arguments": '{"pattern": "x"}'},
                            "extra_content": {"google": {"thought_signature": "sig-abc"}},
                        }
                    ],
                }
            }
        ]
    }

    turn = await OpenAICompatibleProvider("http://x", "m").chat([])

    assert turn.tool_calls[0].thought_signature == "sig-abc"


async def test_a_missing_thought_signature_stays_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("evomesh.models.httpx.AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.payload = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "grep", "arguments": "{}"},
                        }
                    ],
                }
            }
        ]
    }

    turn = await OpenAICompatibleProvider("http://x", "m").chat([])

    assert turn.tool_calls[0].thought_signature is None


async def test_a_captured_signature_is_echoed_back_on_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual fix: a ToolCall carrying a thought_signature must reappear
    in the exact same extra_content.google.thought_signature shape when that
    message is re-sent as history on the next turn."""
    monkeypatch.setattr("evomesh.models.httpx.AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.payload = {"choices": [{"message": {"content": "done", "tool_calls": []}}]}
    call = ToolCall(
        name="grep", arguments={"pattern": "x"}, id="call_1", thought_signature="sig-abc"
    )
    history = [
        ChatMessage(role="assistant", content="", tool_calls=[call]),
        ChatMessage(role="tool", content="result", tool_call_id="call_1"),
    ]

    await OpenAICompatibleProvider("http://x", "m").chat(history)

    sent = _FakeAsyncClient.captured["json"]["messages"]
    assistant_message = next(m for m in sent if m["role"] == "assistant")
    assert assistant_message["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": "sig-abc"}
    }


async def test_no_signature_means_no_extra_content_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every other dialect (and a Gemini call with none captured) must send
    exactly what was sent before this existed -- no stray key."""
    monkeypatch.setattr("evomesh.models.httpx.AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.payload = {"choices": [{"message": {"content": "done", "tool_calls": []}}]}
    call = ToolCall(name="grep", arguments={"pattern": "x"}, id="call_1")
    history = [ChatMessage(role="assistant", content="", tool_calls=[call])]

    await OpenAICompatibleProvider("http://x", "m").chat(history)

    sent = _FakeAsyncClient.captured["json"]["messages"]
    assistant_message = next(m for m in sent if m["role"] == "assistant")
    assert "extra_content" not in assistant_message["tool_calls"][0]
