"""A Telegram bot as a second console onto the same running mesh.

Everything typed into the chat goes through the same :class:`ConsoleChannel`
router the desktop Control Center talks to, so there is exactly one definition
of what a command means. The bot adds three things the console does not have:
an allow-list, one conversation state per chat, and announcements the mesh
sends on its own -- a promoted generation, an imminent restart -- which is the
part a human actually wants on their phone.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from evomesh.config import TelegramSettings
from evomesh.console import ConsoleChannel
from evomesh.environment import Environment

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
OFFSET_STATE_KEY = "telegram.offset"
ALLOWED_STATE_KEY = "telegram.allowed_chats"

# Telegram rejects anything longer than 4096 characters outright, and an agent's
# answer routinely runs past that. Chunk below the limit rather than truncating:
# a status listing cut in half is worse than one that arrives in two messages.
MESSAGE_LIMIT = 3800

WELCOME = (
    "EvoMesh is connected.\n\n"
    "Send a message to talk to the selected agent, or use a command:\n"
    "/status - environment and provider health\n"
    "/agents - who is running\n"
    "/evolution status - the generation and pipeline state\n"
    "/chat <agent> - choose who you are talking to\n"
    "/help - every command\n\n"
    "Stopping the mesh is deliberately not possible from here."
)

# Shutting the mesh down from a phone would leave nothing running to be asked to
# start it again, and the control port only listens on localhost.
BLOCKED_COMMANDS = {"/exit"}


class TelegramError(RuntimeError):
    pass


class TelegramChannel:
    """Long-polls Telegram and routes each message through the console."""

    def __init__(
        self,
        environment: Environment,
        settings: TelegramSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.environment = environment
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._consoles: dict[int, ConsoleChannel] = {}
        self._allowed: set[int] = {int(item) for item in settings.allowed_chat_ids}
        self._offset = 0
        self._running = False
        self.identity = ""

    @property
    def configured(self) -> bool:
        return self.settings.enabled and bool(self.settings.token.strip())

    @property
    def running(self) -> bool:
        """Whether the poller is actually connected, not merely configured."""
        return self._running

    @property
    def allowed_chats(self) -> list[int]:
        """Every chat that may talk to the mesh, adopted ones included."""
        return sorted(self._allowed)

    async def allow(self, chat_id: int) -> bool:
        """Let a chat in, and remember it across restarts.

        Persisted rather than written back into evomesh.yaml: a chat adopted at
        runtime is live state, and rewriting a human's config file from inside
        the mesh would be a surprise nobody asked for.
        """
        if chat_id in self._allowed:
            return False
        self._allowed.add(chat_id)
        await self._persist_allowed()
        return True

    async def revoke(self, chat_id: int) -> bool:
        if chat_id not in self._allowed:
            return False
        self._allowed.discard(chat_id)
        self._consoles.pop(chat_id, None)
        await self._persist_allowed()
        return True

    async def check(self) -> tuple[bool, str]:
        """Ask Telegram who this token belongs to. Used by /telegram test."""
        if not self.settings.token.strip():
            return False, "no bot token is configured"
        opened = self._client is None
        if opened:
            self._client = httpx.AsyncClient(timeout=10)
        try:
            me = await self._call("getMe", {})
            return True, f"@{me.get('username', '?')}"
        except (httpx.HTTPError, TelegramError) as exc:
            return False, str(exc)
        finally:
            if opened and self._client is not None:
                await self._client.aclose()
                self._client = None

    def stop(self) -> None:
        self._running = False

    # -- lifecycle ------------------------------------------------------

    async def run(self) -> None:
        if not self.configured:
            return
        client = self._client or httpx.AsyncClient(
            # Comfortably longer than the long-poll window, or every idle poll
            # would surface as a timeout error in the log.
            timeout=self.settings.poll_timeout_seconds + 15
        )
        self._client = client
        self._running = True
        try:
            me = await self._call("getMe", {})
            self.identity = f"@{me.get('username', '?')}"
            logger.info("Telegram connected as %s", self.identity)
            await self._restore()
            if self.settings.announcements:
                self.environment.notifiers.append(self.announce)
            await self._poll()
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, TelegramError) as exc:
            # A bad token or no network is a configuration problem, not a reason
            # to take the mesh down with it.
            logger.warning("Telegram is not available: %s", exc)
        finally:
            self._running = False
            if self.announce in self.environment.notifiers:
                self.environment.notifiers.remove(self.announce)
            if self._owns_client:
                await client.aclose()

    async def _restore(self) -> None:
        stored_offset = await self.environment.repository.load_state(OFFSET_STATE_KEY)
        if isinstance(stored_offset, int):
            # Picking up where the last process stopped is what keeps an
            # automatic restart from replaying the commands that caused it.
            self._offset = stored_offset
        stored_chats = await self.environment.repository.load_state(ALLOWED_STATE_KEY)
        if isinstance(stored_chats, list):
            self._allowed |= {int(item) for item in stored_chats if isinstance(item, int | str)}

    async def _poll(self) -> None:
        while self._running:
            try:
                updates = await self._call(
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": self.settings.poll_timeout_seconds,
                        "allowed_updates": ["message"],
                    },
                )
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, TelegramError) as exc:
                logger.warning("Telegram poll failed, retrying: %s", exc)
                await asyncio.sleep(5)
                continue
            for update in updates if isinstance(updates, list) else []:
                await self._consume(update)

    async def _consume(self, update: dict[str, Any]) -> None:
        self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
        await self.environment.repository.save_state(OFFSET_STATE_KEY, self._offset)
        message = update.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id", 0))
        text = str(message.get("text", "")).strip()
        if not chat_id or not text:
            return
        try:
            reply = await self._answer(chat_id, text)
        except (KeyError, ValueError, RuntimeError) as exc:
            reply = f"Error: {exc}"
        if reply:
            await self.send(chat_id, reply)

    # -- routing --------------------------------------------------------

    async def _answer(self, chat_id: int, text: str) -> str:
        if not await self._admit(chat_id):
            logger.info("Telegram chat %s is not on the allow-list", chat_id)
            return (
                f"This chat ({chat_id}) is not allowed to talk to EvoMesh. "
                "Add the id in the Control Center under Telegram."
            )
        if text.split()[0].lower() in BLOCKED_COMMANDS:
            return "Stopping the mesh is only possible from the Control Center."
        command = text.split()[0].lower()
        if command in {"/start", "/start@evomesh"}:
            return WELCOME
        console = self._consoles.get(chat_id)
        if console is None:
            console = ConsoleChannel(self.environment)
            self._consoles[chat_id] = console
        return await console.route(text)

    async def _admit(self, chat_id: int) -> bool:
        """Let a known chat in, and let the very first one claim the bot.

        A chat id cannot be looked up anywhere -- Telegram only reveals it once
        someone writes to the bot -- so an empty allow-list would leave the
        integration unusable until a human went hunting for the number.
        """
        if chat_id in self._allowed:
            return True
        if self._allowed or not self.settings.adopt_first_chat:
            return False
        await self.allow(chat_id)
        logger.info("Telegram chat %s adopted this bot", chat_id)
        return True

    async def _persist_allowed(self) -> None:
        await self.environment.repository.save_state(
            ALLOWED_STATE_KEY, sorted(self._allowed)
        )

    # -- sending --------------------------------------------------------

    async def announce(self, text: str) -> None:
        for chat_id in sorted(self._allowed):
            await self.send(chat_id, text)

    async def send(self, chat_id: int, text: str) -> None:
        for chunk in _chunks(text):
            try:
                await self._call("sendMessage", {"chat_id": chat_id, "text": chunk})
            except (httpx.HTTPError, TelegramError) as exc:
                logger.warning("Could not send to Telegram chat %s: %s", chat_id, exc)
                return

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        if self._client is None:
            raise TelegramError("the Telegram client is not open")
        response = await self._client.post(
            f"{API_ROOT}/bot{self.settings.token.strip()}/{method}", json=payload
        )
        if response.status_code >= 400:
            raise TelegramError(f"{method} failed with HTTP {response.status_code}")
        body = response.json()
        if not body.get("ok"):
            raise TelegramError(f"{method} was refused: {body.get('description', 'no reason')}")
        return body.get("result")


def _chunks(text: str) -> list[str]:
    """Split on line boundaries where possible, so output stays readable."""
    remaining = text.strip() or "(no output)"
    parts: list[str] = []
    while len(remaining) > MESSAGE_LIMIT:
        cut = remaining.rfind("\n", 0, MESSAGE_LIMIT)
        if cut <= 0:
            cut = MESSAGE_LIMIT
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    parts.append(remaining)
    return parts
