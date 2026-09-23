"""_post_with_retry -- the one place every provider's HTTP layer retries a
503 before handing the response back to the caller's own raise_for_status()/
except handling. See models.py's own docstring on the incident this exists
for: pointing a system agent at Gemini's OpenAI-compatible endpoint, roughly
half of the real harness/propose calls came back 503 ("high demand...
temporary") within minutes, discarding a whole harness job each time, while
every manually-replicated request of the same shape succeeded on the first
try.
"""

from __future__ import annotations

from typing import Any

import pytest

from evomesh.models import _post_with_retry


async def _no_sleep(*args: Any, **kwargs: Any) -> None:
    return None


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    def __init__(self, statuses: list[int]) -> None:
        self._statuses = list(statuses)
        self.calls = 0

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls += 1
        return _FakeResponse(self._statuses.pop(0))


async def test_a_503_is_retried_until_it_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("evomesh.models.asyncio.sleep", _no_sleep)
    client = _FakeClient([503, 200])

    response = await _post_with_retry(client, "http://x")  # type: ignore[arg-type]

    assert response.status_code == 200
    assert client.calls == 2


async def test_a_non_503_returns_immediately_with_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("evomesh.models.asyncio.sleep", _no_sleep)
    client = _FakeClient([400])

    response = await _post_with_retry(client, "http://x")  # type: ignore[arg-type]

    assert response.status_code == 400
    assert client.calls == 1


async def test_persistent_503_gives_up_after_three_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two retries on top of the first attempt -- three total -- rather than
    forever, so a real, sustained outage still surfaces as ModelUnavailableError
    to the caller instead of hanging a harness job indefinitely."""
    monkeypatch.setattr("evomesh.models.asyncio.sleep", _no_sleep)
    client = _FakeClient([503, 503, 503])

    response = await _post_with_retry(client, "http://x")  # type: ignore[arg-type]

    assert response.status_code == 503
    assert client.calls == 3


async def test_retries_pass_through_the_same_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retried request must be the same request -- same headers, same
    body -- not a bare re-POST that silently drops auth or the payload."""
    monkeypatch.setattr("evomesh.models.asyncio.sleep", _no_sleep)
    seen: list[dict[str, Any]] = []

    class _RecordingClient:
        def __init__(self) -> None:
            self._statuses = [503, 200]

        async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
            seen.append(kwargs)
            return _FakeResponse(self._statuses.pop(0))

    await _post_with_retry(
        _RecordingClient(),  # type: ignore[arg-type]
        "http://x",
        headers={"Authorization": "Bearer key"},
        json={"model": "m"},
    )

    assert seen == [
        {"headers": {"Authorization": "Bearer key"}, "json": {"model": "m"}},
        {"headers": {"Authorization": "Bearer key"}, "json": {"model": "m"}},
    ]
