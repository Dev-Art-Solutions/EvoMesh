"""OllamaProvider's `format` field -- Ollama's own grammar-constrained
decoding, used by the harness's text-protocol fallback (structured_fallback,
see harness.py) to make a malformed tool-call envelope structurally
impossible. These tests exercise the HTTP body directly against a stubbed
transport, mirroring test_anthropic_provider.py's fake-client pattern.
"""

from __future__ import annotations

from typing import Any

import pytest

from evomesh.models import OllamaProvider


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

    async def post(self, url: str, *, json: dict[str, Any]) -> _FakeResponse:
        _FakeAsyncClient.captured = {"url": url, "json": json}
        return _FakeResponse(_FakeAsyncClient.payload)


SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": []}


async def test_generate_sends_format_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("evomesh.models.httpx.AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.payload = {"response": "ok"}

    await OllamaProvider("http://localhost:11434", "ornith").generate(
        "hello", format=SCHEMA
    )

    assert _FakeAsyncClient.captured["json"]["format"] == SCHEMA


async def test_generate_omits_format_when_not_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("evomesh.models.httpx.AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.payload = {"response": "ok"}

    await OllamaProvider("http://localhost:11434", "ornith").generate("hello")

    assert "format" not in _FakeAsyncClient.captured["json"]


async def test_chat_never_sends_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """The native-tools path (chat()) must never gain a `format` key --
    structured_fallback only ever touches generate(), the text-protocol
    fallback's own call."""
    monkeypatch.setattr("evomesh.models.httpx.AsyncClient", _FakeAsyncClient)
    provider = OllamaProvider("http://localhost:11434", "ornith")

    _FakeAsyncClient.payload = {"response": "ok"}
    await provider.generate("hello", format=SCHEMA)
    assert "format" in _FakeAsyncClient.captured["json"]

    _FakeAsyncClient.payload = {"message": {"content": "hi"}}
    await provider.chat([])

    assert "format" not in _FakeAsyncClient.captured["json"]
