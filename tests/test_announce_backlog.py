"""Announcements made before any channel is listening reach the first one
that registers. Found live 2026-09-27: every watcher's first tick runs inside
Environment.start(), before __main__ creates the Telegram channel, so the
news a restart found went to nobody -- and was marked reported."""

from __future__ import annotations

from pathlib import Path

import httpx

from evomesh.contracts import TelegramSettings
from evomesh.environment import UNDELIVERED_LIMIT, Environment
from evomesh.models import ChatTurn
from evomesh.telegram import TelegramChannel
from tests.test_ideas import FakeTelegram, _mesh  # pyright: ignore[reportPrivateUsage]


def _channel(environment: Environment, fake: FakeTelegram) -> TelegramChannel:
    return TelegramChannel(
        environment,
        TelegramSettings(enabled=True, token="t", allowed_chat_ids=[42]),
        httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )


async def test_news_announced_before_telegram_connects_is_delivered_when_it_does(
    tmp_path: Path,
) -> None:
    environment, _, _ = await _mesh(tmp_path, [ChatTurn(text="nothing")])
    await environment.announce("Gold hits a record (https://example.com/gold)")

    fake = FakeTelegram()
    channel = _channel(environment, fake)
    await channel._register_listener()  # pyright: ignore[reportPrivateUsage]

    assert [item["text"] for item in fake.sent] == ["Gold hits a record (https://example.com/gold)"]
    # Delivered once: a second channel does not get the same backlog again.
    late = FakeTelegram()
    await _channel(environment, late)._register_listener()  # pyright: ignore[reportPrivateUsage]
    assert late.sent == []
    await environment.stop()


async def test_the_backlog_is_bounded_and_skipped_once_someone_listens(
    tmp_path: Path,
) -> None:
    environment, _, _ = await _mesh(tmp_path, [ChatTurn(text="nothing")])
    for number in range(UNDELIVERED_LIMIT + 5):
        await environment.announce(f"item {number}")

    fake = FakeTelegram()
    await _channel(environment, fake)._register_listener()  # pyright: ignore[reportPrivateUsage]
    assert len(fake.sent) == UNDELIVERED_LIMIT
    assert fake.sent[0]["text"] == "item 5", "the oldest are the ones dropped"

    await environment.announce("live")
    assert fake.sent[-1]["text"] == "live"
    assert len(fake.sent) == UNDELIVERED_LIMIT + 1
    await environment.stop()
