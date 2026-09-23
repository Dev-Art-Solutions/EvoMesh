"""_post_with_retry -- the one place every provider's HTTP layer retries a
503 or a network-layer transport failure before handing the response (or
re-raising the final exception) back to the caller's own
raise_for_status()/except handling. See models.py's own docstring on the
two incidents this exists for: pointing a system agent at Gemini's
OpenAI-compatible endpoint, roughly half of the real harness/propose calls
came back 503 ("high demand... temporary") within minutes, discarding a
whole harness job each time, while every manually-replicated request of the
same shape succeeded on the first try -- and separately, a real repair job
whose request ended the whole job with a raw httpx.ReadError after 847
seconds, which the 503-only retry never touched since there was no response
to check a status code on at all.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from evomesh.models import _post_with_retry


async def _no_sleep(*args: Any, **kwargs: Any) -> None:
    return None


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    """Each entry in ``outcomes`` is either an int status code, or an
    ``httpx.TransportError`` instance to raise instead of returning."""

    def __init__(self, outcomes: list[int | httpx.TransportError]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, httpx.TransportError):
            raise outcome
        return _FakeResponse(outcome)


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


async def test_a_transport_error_is_retried_until_it_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("evomesh.models.asyncio.sleep", _no_sleep)
    client = _FakeClient([httpx.ReadError("connection dropped"), 200])

    response = await _post_with_retry(client, "http://x")  # type: ignore[arg-type]

    assert response.status_code == 200
    assert client.calls == 2


async def test_a_persistent_transport_error_is_reraised_after_three_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three attempts, then the real exception surfaces -- a sustained
    outage must still reach the caller as ModelUnavailableError, not hang
    the harness job forever waiting on a connection that never recovers."""
    monkeypatch.setattr("evomesh.models.asyncio.sleep", _no_sleep)
    client = _FakeClient(
        [
            httpx.ReadError("connection dropped"),
            httpx.ReadError("connection dropped"),
            httpx.ReadError("connection dropped"),
        ]
    )

    with pytest.raises(httpx.ReadError):
        await _post_with_retry(client, "http://x")  # type: ignore[arg-type]

    assert client.calls == 3


async def test_a_transport_error_and_a_503_share_the_same_attempt_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two failure modes are not separately budgeted -- three total
    attempts covers any mix of the two, matching the single incident that
    could plausibly hit both in the same job."""
    monkeypatch.setattr("evomesh.models.asyncio.sleep", _no_sleep)
    client = _FakeClient([httpx.ReadError("connection dropped"), 503, 200])

    response = await _post_with_retry(client, "http://x")  # type: ignore[arg-type]

    assert response.status_code == 200
    assert client.calls == 3
