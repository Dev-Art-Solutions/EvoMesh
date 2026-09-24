"""A Telegram bot as a second console onto the same running mesh."""

from evomesh.telegram import TelegramError


def test_telegram_error_stores_its_message_like_a_runtime_error() -> None:
    error = TelegramError("bot is unreachable")
    assert str(error) == "bot is unreachable"
