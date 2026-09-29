"""A Telegram bot as a second console onto the same running mesh."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from evomesh.codebase import open_improvements
from evomesh.contracts import TelegramSettings
from evomesh.ideas import IDEAS_AGENT_ID
from evomesh.telegram import MENU_COMMANDS, TelegramChannel, TelegramError
from tests.test_ideas import TOTAL, FakeTelegram, _telegram  # pyright: ignore[reportPrivateUsage]


def test_telegram_error_stores_its_message_like_a_runtime_error() -> None:
    error = TelegramError("bot is unreachable")
    assert str(error) == "bot is unreachable"


def _buttons(message: dict[str, Any]) -> list[str]:
    markup = message.get("reply_markup") or {}
    return [button["callback_data"] for row in markup["inline_keyboard"] for button in row]


def _press(chat_id: int, data: str, message_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": 9,
        "callback_query": {
            "id": "q1",
            "data": data,
            "message": {"chat": {"id": chat_id}, "message_id": message_id},
        },
    }


async def test_the_menu_is_published_and_a_private_bot_gets_its_own(tmp_path: Path) -> None:
    environment, channel, fake, _ = await _telegram(tmp_path)
    await channel._publish_menu()  # pyright: ignore[reportPrivateUsage]
    own_api = FakeTelegram()
    own = TelegramChannel(
        environment,
        TelegramSettings(enabled=True, token="own", allowed_chat_ids=[42]),
        httpx.AsyncClient(transport=httpx.MockTransport(own_api.handler)),
        locked_agent_id=IDEAS_AGENT_ID,
        locked_agent_name="Idea Scout",
    )
    await own._publish_menu()  # pyright: ignore[reportPrivateUsage]

    def menu(api: FakeTelegram) -> list[str]:
        return [
            item["command"]
            for method, body in api.calls
            if method == "setMyCommands"
            for item in body["commands"]
        ]

    shared, private = menu(fake), menu(own_api)
    assert shared == [name for name, _ in MENU_COMMANDS]
    assert "chat" not in private and {"ideas", "reports", "wiki"} <= set(private)
    # Every menu entry must be a command the console actually has.
    console = channel._console_for(42)  # pyright: ignore[reportPrivateUsage]
    for name in shared:
        assert hasattr(console, f"_command_{name}"), name
    await environment.stop()


async def test_start_carries_buttons_and_a_press_runs_that_command(tmp_path: Path) -> None:
    environment, channel, fake, _ = await _telegram(tmp_path)
    await channel._consume(  # pyright: ignore[reportPrivateUsage]
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/start"}}
    )
    assert "/agents" in _buttons(fake.sent[-1])

    await channel._consume(_press(42, "/agents"))  # pyright: ignore[reportPrivateUsage]

    assert any(method == "answerCallbackQuery" for method, _ in fake.calls)
    agent_buttons = _buttons(fake.sent[-1])
    assert agent_buttons and all(data.startswith("/chat ") for data in agent_buttons)
    await channel._consume(_press(42, agent_buttons[0]))  # pyright: ignore[reportPrivateUsage]
    assert fake.sent[-1]["text"].startswith("Talking to ")
    await environment.stop()


async def test_a_command_addressed_to_the_bot_by_name_still_runs(tmp_path: Path) -> None:
    environment, channel, fake, _ = await _telegram(tmp_path)
    await channel._consume(  # pyright: ignore[reportPrivateUsage]
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/Agents@evomesh_bot"}}
    )
    assert "Unknown command" not in fake.sent[-1]["text"]
    await environment.stop()


async def test_an_idea_arrives_with_buttons_and_approve_moves_it(tmp_path: Path) -> None:
    environment, channel, fake, project = await _telegram(tmp_path)
    idea = await environment.ideas.add(TOTAL, "Idea Scout")
    assert idea is not None
    await environment.announce_idea(idea)
    announced = fake.sent[-1]
    assert _buttons(announced) == [f"idea approve {idea.number}", f"idea reject {idea.number}"]

    await channel._consume(  # pyright: ignore[reportPrivateUsage]
        _press(42, f"idea approve {idea.number}", announced["message_id"])
    )

    assert [item.title for item in open_improvements(project)] == [TOTAL.title]
    cleared = [body for method, body in fake.calls if method == "editMessageReplyMarkup"]
    assert cleared and cleared[-1]["message_id"] == announced["message_id"]
    await environment.stop()


async def test_a_stranger_pressing_a_button_changes_nothing(tmp_path: Path) -> None:
    environment, channel, fake, project = await _telegram(tmp_path)
    idea = await environment.ideas.add(TOTAL, "Idea Scout")
    assert idea is not None
    await environment.announce_idea(idea)
    sent_before = len(fake.sent)

    await channel._consume(_press(7, f"idea approve {idea.number}"))  # pyright: ignore[reportPrivateUsage]
    await channel._consume(_press(7, "/restart"))  # pyright: ignore[reportPrivateUsage]

    assert open_improvements(project) == []
    assert len(fake.sent) == sent_before
    assert not environment.restart_requested.is_set()
    await environment.stop()


async def test_ideas_listing_offers_a_verdict_per_idea(tmp_path: Path) -> None:
    environment, channel, fake, _ = await _telegram(tmp_path)
    idea = await environment.ideas.add(TOTAL, "Idea Scout")
    assert idea is not None
    await channel._consume(  # pyright: ignore[reportPrivateUsage]
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/ideas"}}
    )
    assert f"idea reject {idea.number}" in _buttons(fake.sent[-1])
    await environment.stop()
