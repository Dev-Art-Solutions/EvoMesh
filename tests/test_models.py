import httpx
import pytest

from evomesh.models import _parse_arguments, _post_with_retry


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
