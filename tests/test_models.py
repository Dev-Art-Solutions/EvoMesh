import httpx
import pytest

from evomesh.models import (
    ToolsUnsupportedError,
    _extract_thought_signature,
    _parse_arguments,
    _post_with_retry,
    _tools_are_unsupported,
)


class _FakeClient(httpx.AsyncClient):
    def __init__(self):
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        return httpx.Response(200)


def test_parse_arguments_parses_json_string_arguments():
    out = _parse_arguments('{"name": "greet", "count": 3}')

    assert out == {"name": "greet", "count": 3}


@pytest.mark.asyncio
async def test_post_with_retry_returns_response_once():
    fake = _FakeClient()

    out = await _post_with_retry(
        fake,
        "https://example.com/chat",
        timeout=None,
    )

    assert fake.calls == 1
    assert out.status_code == 200


def test_extract_thought_signature_returns_nested_string():
    item = {
        "extra_content": {
            "google": {
                "thought_signature": "thinking step by step",
            },
        },
    }

    out = _extract_thought_signature(item)

    assert out == "thinking step by step"


def test_tools_are_unsupported_flags_a_400_mentioning_tool():
    request = httpx.Request("POST", "https://example.com")
    response = httpx.Response(400, text="This model does not support tools", request=request)
    exc = httpx.HTTPStatusError("boom", request=request, response=response)

    assert _tools_are_unsupported(exc) is True


def test_tools_unsupported_stores_and_returns_its_message():
    exc = ToolsUnsupportedError("take the text protocol instead")

    assert str(exc) == "take the text protocol instead"


# -- look(): one question about one image -------------------------------------

IMAGE = b"\x89PNG fake bytes"
# Taken once: a second _serve() in one test must not wrap the first.
_REAL_CLIENT = httpx.AsyncClient


def _serve(monkeypatch: pytest.MonkeyPatch, answer) -> list[httpx.Request]:
    """Route every httpx.AsyncClient in evomesh.models through ``answer``."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return seen


async def test_ollama_look_sends_the_image_and_probes_vision_once(monkeypatch):
    import base64
    import json

    from evomesh.models import OllamaProvider

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "vision"]})
        return httpx.Response(200, json={"response": "a chart"})

    seen = _serve(monkeypatch, answer)
    provider = OllamaProvider("http://ollama.invalid", "ornith", num_ctx=8192)
    assert await provider.supports_vision() is True
    assert await provider.supports_vision() is True
    assert await provider.look(IMAGE, "what?", system="sys", model="ornith") == "a chart"

    assert [r.url.path for r in seen] == ["/api/show", "/api/generate"], "probe cached"
    body = json.loads(seen[1].content)
    assert body["images"] == [base64.b64encode(IMAGE).decode()]
    assert body["prompt"] == "what?" and body["system"] == "sys"
    assert body["options"] == {"num_ctx": 8192, "num_predict": 1024}, "output is bounded"
    assert "think" not in body, "only a thinking model is told not to think"


async def test_ollama_look_tells_a_thinking_model_not_to_think(monkeypatch):
    import json

    from evomesh.models import OllamaProvider

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "vision", "thinking"]})
        return httpx.Response(200, json={"response": "Python (programming language)"})

    seen = _serve(monkeypatch, answer)
    provider = OllamaProvider("http://ollama.invalid", "ornith")
    await provider.look(IMAGE, "title?", max_tokens=256)
    body = json.loads(seen[-1].content)
    assert body["think"] is False
    assert body["options"] == {"num_predict": 256}


async def test_ollama_says_a_text_model_cannot_see_and_does_not_guess_when_unsure(monkeypatch):
    from evomesh.models import OllamaProvider

    _serve(monkeypatch, lambda request: httpx.Response(200, json={"capabilities": ["completion"]}))
    assert await OllamaProvider("http://o.invalid", "qwen3").supports_vision() is False

    _serve(monkeypatch, lambda request: httpx.Response(200, json={"modelfile": "..."}))
    assert await OllamaProvider("http://o.invalid", "old").supports_vision() is None

    _serve(monkeypatch, lambda request: httpx.Response(500))
    unreachable = OllamaProvider("http://o.invalid", "qwen3")
    assert await unreachable.supports_vision() is None
    assert unreachable._vision == {}, "an unknown answer is asked again next time"


async def test_openai_and_anthropic_look_use_their_own_image_shapes(monkeypatch):
    import base64
    import json

    from evomesh.models import AnthropicProvider, OpenAICompatibleProvider

    encoded = base64.b64encode(IMAGE).decode()

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "openai"}}]})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "claude"}]})

    seen = _serve(monkeypatch, answer)
    openai = OpenAICompatibleProvider("http://oa.invalid/v1", "gpt", "k")
    assert await openai.look(IMAGE, "what?", system="sys", mime="image/jpeg") == "openai"
    anthropic = AnthropicProvider("http://an.invalid/v1", "claude", "k")
    assert await anthropic.look(IMAGE, "what?", system="sys") == "claude"

    assert json.loads(seen[0].content)["max_tokens"] == 1024
    assert json.loads(seen[1].content)["max_tokens"] == 1024
    parts = json.loads(seen[0].content)["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "what?"}
    assert parts[1]["image_url"]["url"] == f"data:image/jpeg;base64,{encoded}"
    body = json.loads(seen[1].content)
    assert body["system"] == "sys"
    image = body["messages"][0]["content"][0]
    assert image["source"] == {"type": "base64", "media_type": "image/png", "data": encoded}
