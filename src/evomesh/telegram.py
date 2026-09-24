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
import tempfile
from pathlib import Path
from typing import Any

import httpx

from evomesh.cognition import extract_file_references
from evomesh.console import MAX_ATTACHMENT_BYTES, ConsoleChannel
from evomesh.contracts import TelegramSettings
from evomesh.environment import Environment

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
FILE_ROOT = "https://api.telegram.org/file"
# The mesh-wide bot keeps the original, unsuffixed keys so an upgrade never
# loses its offset or allow-list. A per-agent bot's keys are namespaced by
# agent id so two bots polling the same repository never share state.
OFFSET_STATE_KEY = "telegram.offset"
ALLOWED_STATE_KEY = "telegram.allowed_chats"

# Telegram rejects anything longer than 4096 characters outright, and an agent's
# answer routinely runs past that. Chunk below the limit rather than truncating:
# a status listing cut in half is worse than one that arrives in two messages.
MESSAGE_LIMIT = 3800

# Base backoff interval and its ceiling for consecutive poll failures, which
# double each time in a row (5s, 10s, 20s ...) until one succeeds again.
BACKOFF_INTERVAL_SECONDS = 5
BACKOFF_MAX_SECONDS = 120

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
        *,
        locked_agent_id: str | None = None,
        locked_agent_name: str | None = None,
    ) -> None:
        self.environment = environment
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._consoles: dict[int, ConsoleChannel] = {}
        self._allowed: set[int] = {int(item) for item in settings.allowed_chat_ids}
        self._offset = 0
        self._running = False
        # Exponential backoff for consecutive poll failures, reset after a success.
        self._backoff = BACKOFF_INTERVAL_SECONDS
        self.identity = ""
        # None: the mesh-wide bot, talking to whichever agent /chat selected.
        # Set: a private bot for exactly one agent -- no /chat, no switching.
        self.locked_agent_id = locked_agent_id
        self.locked_agent_name = locked_agent_name or locked_agent_id or ""

    @property
    def _offset_key(self) -> str:
        if not self.locked_agent_id:
            return OFFSET_STATE_KEY
        return f"{OFFSET_STATE_KEY}.{self.locked_agent_id}"

    @property
    def _allowed_key(self) -> str:
        if not self.locked_agent_id:
            return ALLOWED_STATE_KEY
        return f"{ALLOWED_STATE_KEY}.{self.locked_agent_id}"

    def _welcome(self) -> str:
        if not self.locked_agent_id:
            return WELCOME
        return (
            f"This is {self.locked_agent_name}'s private line.\n\n"
            "Just send a message -- everything you type goes straight to this "
            "agent, no other agent can be reached from here.\n"
            "/status - environment and provider health\n"
            "/help - every command\n\n"
            "Stopping the mesh is deliberately not possible from here."
        )

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
                self._register_listener()
            await self._poll()
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, TelegramError) as exc:
            # A bad token or no network is a configuration problem, not a reason
            # to take the mesh down with it.
            logger.warning("Telegram is not available: %s", exc)
        finally:
            self._running = False
            self._unregister_listener()
            if self._owns_client:
                await client.aclose()

    def _register_listener(self) -> None:
        if self.locked_agent_id:
            self.environment.agent_notifiers.setdefault(self.locked_agent_id, []).append(
                self.announce
            )
        else:
            self.environment.notifiers.append(self.announce)

    def _unregister_listener(self) -> None:
        if self.locked_agent_id:
            listeners = self.environment.agent_notifiers.get(self.locked_agent_id, [])
            if self.announce in listeners:
                listeners.remove(self.announce)
        elif self.announce in self.environment.notifiers:
            self.environment.notifiers.remove(self.announce)

    async def _restore(self) -> None:
        stored_offset = await self.environment.repository.load_state(self._offset_key)
        if isinstance(stored_offset, int):
            # Picking up where the last process stopped is what keeps an
            # automatic restart from replaying the commands that caused it.
            self._offset = stored_offset
        stored_chats = await self.environment.repository.load_state(self._allowed_key)
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
                logger.warning(
                    "Telegram poll failed, retrying in %.0fs: %s: %s",
                    self._backoff,
                    type(exc).__name__,
                    exc,
                )
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, BACKOFF_MAX_SECONDS)
                continue
            self._backoff = BACKOFF_INTERVAL_SECONDS
            for update in updates if isinstance(updates, list) else []:
                await self._consume(update)

    async def _consume(self, update: dict[str, Any]) -> None:
        self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
        await self.environment.repository.save_state(self._offset_key, self._offset)
        message = update.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id", 0))
        if not chat_id:
            return
        attachment = _incoming_attachment(message)
        try:
            if attachment is not None:
                reply = await self._answer_file(chat_id, *attachment)
            else:
                text = str(message.get("text", "")).strip()
                if not text:
                    return
                reply = await self._answer(chat_id, text)
        except (KeyError, ValueError, RuntimeError) as exc:
            reply = f"Error: {exc}"
        if reply:
            await self.send(chat_id, reply)
            console = self._consoles.get(chat_id)
            if console is not None:
                await self._send_file_references(chat_id, console.selected_agent, reply)

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
            return self._welcome()
        if self.locked_agent_id and command in {"/chat"}:
            return f"This bot only talks to {self.locked_agent_name}."
        return await self._console_for(chat_id).route(text)

    async def _answer_file(self, chat_id: int, file_id: str, suggested_name: str) -> str:
        """A document or photo arrived -- download it and hand it to the
        selected agent through the exact same reactive round trip a human
        typing /attach in the Control Center goes through.

        ``ConsoleChannel.attach`` takes a real ``Path`` rather than command
        text on purpose (see its own docstring): the file already exists on
        this machine's disk once downloaded, no need to round-trip a path
        through the text command parser a second time.
        """
        if not await self._admit(chat_id):
            logger.info("Telegram chat %s is not on the allow-list", chat_id)
            return (
                f"This chat ({chat_id}) is not allowed to talk to EvoMesh. "
                "Add the id in the Control Center under Telegram."
            )
        with tempfile.TemporaryDirectory(prefix="evomesh-telegram-") as scratch:
            local_path = await self._download(file_id, suggested_name, Path(scratch))
            return await self._console_for(chat_id).attach(local_path)

    def _console_for(self, chat_id: int) -> ConsoleChannel:
        console = self._consoles.get(chat_id)
        if console is None:
            console = ConsoleChannel(self.environment, locked_agent_id=self.locked_agent_id)
            self._consoles[chat_id] = console
        return console

    async def _download(self, file_id: str, suggested_name: str, scratch: Path) -> Path:
        info = await self._call("getFile", {"file_id": file_id})
        file_path = str(info.get("file_path") or "")
        if not file_path:
            raise TelegramError("Telegram did not return a file_path for this file")
        size = int(info.get("file_size") or 0)
        if size > MAX_ATTACHMENT_BYTES:
            limit_mb = MAX_ATTACHMENT_BYTES // (1024 * 1024)
            raise ValueError(
                f"that file is too large ({size // (1024 * 1024)} MB, limit {limit_mb} MB)"
            )
        if self._client is None:
            raise TelegramError("the Telegram client is not open")
        url = f"{FILE_ROOT}/bot{self.settings.token.strip()}/{file_path}"
        response = await self._client.get(url)
        if response.status_code >= 400:
            raise TelegramError(f"downloading the file failed with HTTP {response.status_code}")
        destination = scratch / (suggested_name or Path(file_path).name or "attachment")
        destination.write_bytes(response.content)
        return destination

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
            self._allowed_key, sorted(self._allowed)
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

    async def _send_file_references(self, chat_id: int, agent_id: str, text: str) -> None:
        """Upload whatever ``FILE:`` lines an agent's own reply named --
        the same convention the Control Center's chat panel renders as a
        clickable link, sent here as a real Telegram document instead.

        Resolved against *that* agent's own ``default_harness_root``, the
        same rule ``ConsoleChannel.attach`` uses for the human-to-agent
        direction, so the two directions agree on what "the agent's
        workspace" means without a second definition of it.
        """
        references = extract_file_references(text)
        if not references or not agent_id:
            return
        try:
            agent = self.environment.registry.get(agent_id)
        except KeyError:
            return
        base = self.environment.default_harness_root(agent)
        for relative in references:
            path = Path(relative)
            if not path.is_absolute():
                path = base / path
            if path.is_file():
                await self._send_document(chat_id, path)

    async def _send_document(self, chat_id: int, path: Path) -> None:
        if self._client is None:
            return
        try:
            data = await asyncio.to_thread(path.read_bytes)
            response = await self._client.post(
                f"{API_ROOT}/bot{self.settings.token.strip()}/sendDocument",
                data={"chat_id": chat_id},
                files={"document": (path.name, data)},
            )
            if response.status_code >= 400:
                logger.warning(
                    "Could not send file %s to Telegram chat %s: HTTP %s",
                    path, chat_id, response.status_code,
                )
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("Could not send file %s to Telegram chat %s: %s", path, chat_id, exc)

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


def _incoming_attachment(message: dict[str, Any]) -> tuple[str, str] | None:
    """``(file_id, suggested filename)`` for a document or photo on this
    message, or ``None`` when it carries neither.

    A photo arrives as a list of ``PhotoSize`` entries, smallest first and
    with no filename of its own (Telegram generates the thumbnails, not the
    sender) -- the last entry is the largest actually-uploaded size.
    """
    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        name = str(document.get("file_name") or "") or f"{document['file_id']}.bin"
        return str(document["file_id"]), name
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        largest = photos[-1]
        if isinstance(largest, dict) and largest.get("file_id"):
            return str(largest["file_id"]), f"{largest['file_id']}.jpg"
    return None


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
