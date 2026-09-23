import httpx
import pytest

from evomesh.models import (
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
