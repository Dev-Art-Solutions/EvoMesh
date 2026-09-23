"""AnthropicProvider translates the shared ChatMessage/ChatTurn shape into
Claude's Messages API dialect -- system as a top-level field, tool results
as `tool_result` content blocks on a *user* turn rather than their own
`tool`-role message, and OpenAI-shaped tool schemas into `input_schema`.
These tests exercise that translation directly, plus one true request/
response round trip against a stubbed transport.
"""

from __future__ import annotations

from typing import Any

import pytest

from evomesh.models import AnthropicProvider, ChatMessage, ChatTurn, ToolCall


def test_a_plain_user_turn_becomes_one_text_block() -> None:
    wire = AnthropicProvider._wire_messages([ChatMessage(role="user", content="hello")])
    assert wire == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]


def test_an_assistant_turn_with_only_tool_calls_has_no_text_block() -> None:
    call = ToolCall(name="read", arguments={"path": "a.py"}, id="call_1")
    wire = AnthropicProvider._wire_messages(
        [ChatMessage(role="assistant", content="", tool_calls=[call])]
    )
    assert wire == [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "a.py"}}
            ],
        }
    ]


def test_consecutive_tool_results_merge_into_one_user_turn() -> None:
    """harness.py appends one `tool`-role message per call an assistant turn
    made, back to back. Anthropic expects every tool_use from that turn
    answered together in the single user turn that follows -- sending one
    user turn per result would be a different (and rejected) shape."""
    messages = [
        ChatMessage(role="user", content="go"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(name="read", arguments={"path": "a.py"}, id="call_1"),
                ToolCall(name="read", arguments={"path": "b.py"}, id="call_2"),
            ],
        ),
        ChatMessage(role="tool", content="contents of a.py", tool_call_id="call_1", name="read"),
        ChatMessage(role="tool", content="contents of b.py", tool_call_id="call_2", name="read"),
        ChatMessage(role="assistant", content="done"),
    ]
    wire = AnthropicProvider._wire_messages(messages)

    tool_turn = wire[2]
    assert tool_turn["role"] == "user"
    assert tool_turn["content"] == [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "contents of a.py"},
        {"type": "tool_result", "tool_use_id": "call_2", "content": "contents of b.py"},
    ]
    # And nothing else collapsed with it: the turn before and after are intact.
    assert wire[0]["role"] == "user"
    assert wire[1]["role"] == "assistant"
    assert wire[3] == {"role": "assistant", "content": [{"type": "text", "text": "done"}]}


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


async def test_chat_sends_system_top_level_and_translated_tool_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.payload = {
        "content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "call_9", "name": "read", "input": {"path": "x.py"}},
        ]
    }
    monkeypatch.setattr("evomesh.models.httpx.AsyncClient", _FakeAsyncClient)

    provider = AnthropicProvider(
        "https://api.anthropic.com/v1", "claude-sonnet-5", api_key="sk-ant-test"
    )
    turn = await provider.chat(
        [ChatMessage(role="user", content="find the bug")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ],
        system="You are terse.",
    )

    sent = _FakeAsyncClient.captured
    assert sent["url"] == "https://api.anthropic.com/v1/messages"
    assert sent["headers"]["x-api-key"] == "sk-ant-test"
    assert sent["headers"]["anthropic-version"] == AnthropicProvider.ANTHROPIC_VERSION
    assert sent["json"]["system"] == "You are terse."
    assert sent["json"]["tools"] == [
        {
            "name": "read",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    assert isinstance(turn, ChatTurn)
    assert turn.text == "looking"
    assert turn.tool_calls == [ToolCall(name="read", arguments={"path": "x.py"}, id="call_9")]
