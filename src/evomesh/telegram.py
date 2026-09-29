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
from evomesh.ideas import APPROVE_REACTIONS, IDEAS_AGENT_ID, REJECT_REACTIONS, verdict_of

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
FILE_ROOT = "https://api.telegram.org/file"
# The mesh-wide bot keeps the original, unsuffixed keys so an upgrade never
# loses its offset or allow-list. A per-agent bot's keys are namespaced by
# agent id so two bots polling the same repository never share state.
OFFSET_STATE_KEY = "telegram.offset"
ALLOWED_STATE_KEY = "telegram.allowed_chats"
# Which message carried which idea, so a thumbs-up or a reply on it decides
# that idea alone. Bounded: an idea nobody answered in 500 later ones is old.
IDEA_MESSAGES_STATE_KEY = "telegram.idea_messages"
MAX_IDEA_MESSAGES = 500

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

# The "/" menu Telegram shows beside the input box (setMyCommands). Only
# commands that do something useful with no arguments, or that say their own
# usage when given none -- a tap sends the bare command. Names must be
# lowercase a-z, 0-9 and _, which is why /num-ctx and friends stay in /help.
MENU_COMMANDS: list[tuple[str, str]] = [
    ("status", "Environment and provider health"),
    ("agents", "Who is running -- tap one to talk to it"),
    ("evolution", "Generation and pipeline state"),
    ("ideas", "Ideas waiting for your review"),
    ("improvements", "The backlog steering evolution"),
    ("harness", "Harness queue and last jobs"),
    ("notifications", "What the mesh announced on its own"),
    ("reports", "An agent's last reports"),
    ("wiki", "What an agent has learned"),
    ("chat", "Choose who you are talking to"),
    ("restart", "Restart the mesh into the current tree"),
    ("help", "Every command"),
]
AGENT_MENU_COMMANDS: list[tuple[str, str]] = [
    ("reports", "Its last reports as sent to you"),
    ("wiki", "What it has learned"),
    ("status", "Environment and provider health"),
    ("help", "Every command"),
]
# Callback data is capped at 64 bytes by Telegram; a button that would need
# more is left out rather than sent truncated into a different command.
CALLBACK_LIMIT = 64
# Buttons under /start, /help and /status: the checks a human makes most.
MAIN_BUTTONS: list[list[tuple[str, str]]] = [
    [("📊 Status", "/status"), ("🤖 Agents", "/agents")],
    [("🧬 Evolution", "/evolution status"), ("💡 Ideas", "/ideas")],
    [("🛠 Harness", "/harness status"), ("🔔 Notifications", "/notifications")],
]
AGENT_BUTTONS: list[list[tuple[str, str]]] = [
    [("📝 Reports", "/reports"), ("📚 Wiki", "/wiki"), ("📊 Status", "/status")],
]
MAX_LISTED_BUTTONS = 10


class TelegramError(RuntimeError):
    """A Bot API failure; ``retry_after`` is the wait Telegram asked for on a
    429 (its ``parameters.retry_after``), ``None`` when it named none."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


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
        self._idea_messages: dict[str, int] = {}

    @property
    def _offset_key(self) -> str:
        if not self.locked_agent_id:
            return OFFSET_STATE_KEY
        return f"{OFFSET_STATE_KEY}.{self.locked_agent_id}"

    @property
    def _ideas_key(self) -> str:
        if not self.locked_agent_id:
            return IDEA_MESSAGES_STATE_KEY
        return f"{IDEA_MESSAGES_STATE_KEY}.{self.locked_agent_id}"

    @property
    def _takes_ideas(self) -> bool:
        """The shared bot, or the Idea Scout's own -- not another agent's."""
        return not self.locked_agent_id or self.locked_agent_id == IDEAS_AGENT_ID

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
            "/reports [n] - its last reports as sent to you (default 10)\n"
            "/wiki - what it has learned (its knowledge pages)\n"
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
            await self._publish_menu()
            await self._restore()
            if self.settings.announcements:
                await self._register_listener()
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

    async def _register_listener(self) -> None:
        if self._takes_ideas:
            self.environment.idea_notifiers.append(self.announce_idea)
        # Through add_*_notifier, not a bare append: those also deliver what
        # the mesh announced before this bot finished connecting.
        if self.locked_agent_id:
            await self.environment.add_agent_notifier(self.locked_agent_id, self.announce)
        else:
            await self.environment.add_notifier(self.announce)

    def _unregister_listener(self) -> None:
        if self.announce_idea in self.environment.idea_notifiers:
            self.environment.idea_notifiers.remove(self.announce_idea)
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
        stored_ideas = await self.environment.repository.load_state(self._ideas_key)
        if isinstance(stored_ideas, dict):
            self._idea_messages = {
                str(key): int(value)
                for key, value in stored_ideas.items()
                if isinstance(value, int)
            }
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
                        # Reactions and button presses only arrive when
                        # asked for by name.
                        "allowed_updates": ["message", "message_reaction", "callback_query"],
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
                # A 429 names how long to stay away; retrying sooner only
                # extends the flood limit.
                retry_after = exc.retry_after if isinstance(exc, TelegramError) else None
                await asyncio.sleep(max(self._backoff, retry_after or 0))
                self._backoff = min(self._backoff * 2, BACKOFF_MAX_SECONDS)
                continue
            self._backoff = BACKOFF_INTERVAL_SECONDS
            for update in updates if isinstance(updates, list) else []:
                await self._consume(update)

    async def _consume(self, update: dict[str, Any]) -> None:
        self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
        await self.environment.repository.save_state(self._offset_key, self._offset)
        reaction = update.get("message_reaction")
        if isinstance(reaction, dict):
            await self._react(reaction)
            return
        query = update.get("callback_query")
        if isinstance(query, dict):
            await self._press(query)
            return
        message = update.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id", 0))
        if not chat_id:
            return
        attachment = _incoming_attachment(message)
        text = ""
        try:
            if attachment is not None:
                reply = await self._answer_file(chat_id, *attachment)
            else:
                text = str(message.get("text", "")).strip()
                if not text:
                    return
                reply = await self._answer_idea_reply(chat_id, message, text)
                if reply is None:
                    reply = await self._answer(chat_id, text)
        except (KeyError, ValueError, RuntimeError) as exc:
            reply = f"Error: {exc}"
        await self._deliver(chat_id, text, reply)

    async def _deliver(self, chat_id: int, command: str, reply: str) -> None:
        if not reply:
            return
        markup = None
        if chat_id in self._allowed and not reply.startswith("Error:"):
            markup = await self._markup_for(command)
        await self.send(chat_id, reply, markup)
        console = self._consoles.get(chat_id)
        if console is not None:
            await self._send_file_references(chat_id, console.selected_agent, reply)

    # -- menu and buttons ------------------------------------------------

    async def _publish_menu(self) -> None:
        """Fill the "/" menu. A bot without it still works, so a refusal is
        logged, never raised."""
        if not self.locked_agent_id:
            commands = MENU_COMMANDS
        else:
            commands = list(AGENT_MENU_COMMANDS)
            if self.locked_agent_id == IDEAS_AGENT_ID:
                commands.insert(0, ("ideas", "Ideas waiting for your review"))
        payload = {
            "commands": [
                {"command": name, "description": description} for name, description in commands
            ]
        }
        try:
            await self._call("setMyCommands", payload)
        except (httpx.HTTPError, TelegramError) as exc:
            logger.warning("Could not set the Telegram command menu: %s", exc)

    async def _markup_for(self, command: str) -> dict[str, Any] | None:
        """The buttons that belong under the answer to ``command``, if any.

        Every button carries a command as text and a press routes it through
        ``_answer`` exactly as if it had been typed -- one definition of what
        a command means, the allow-list and BLOCKED_COMMANDS included.
        """
        parts = command.split()
        name = _bare_command(parts[0]) if parts else ""
        action = parts[1].lower() if len(parts) > 1 else ""
        rows: list[list[tuple[str, str]]] = []
        if name in {"/start", "/help", "/status"}:
            rows = AGENT_BUTTONS if self.locked_agent_id else MAIN_BUTTONS
        elif name == "/agents" and not self.locked_agent_id:
            agents = self.environment.registry.all()[:MAX_LISTED_BUTTONS]
            rows = [[(f"💬 {agent.name}", f"/chat {agent.id}")] for agent in agents]
        elif name == "/ideas":
            rows = [
                [(f"✅ #{idea.number}", f"idea approve {idea.number}"),
                 (f"❌ #{idea.number}", f"idea reject {idea.number}")]
                for idea in self.environment.ideas.pending()[:MAX_LISTED_BUTTONS]
            ]
        elif name == "/evolution" and action in {"", "status"} and not self.locked_agent_id:
            rows = [[("🔄 Refresh", "/evolution status")]]
            state = await self.environment.evolver.pipeline_state()
            if state.get("stage") == "await-human" and state.get("awaiting"):
                rows.insert(
                    0, [("✅ Promote", "/evolution promote"), ("🗑 Discard", "/evolution discard")]
                )
        return _keyboard(rows)

    async def _press(self, query: dict[str, Any]) -> None:
        """A tapped inline button: its data is a command, or an idea verdict."""
        message = query.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id", 0))
        data = str(query.get("data") or "")
        allowed = chat_id in self._allowed
        # Answered first: until it is, the button spins on the phone, and a
        # command like /evolution status can take a moment.
        try:
            await self._call(
                "answerCallbackQuery",
                {"callback_query_id": query.get("id"),
                 **({} if allowed else {"text": "This chat is not allowed."})},
            )
        except (httpx.HTTPError, TelegramError) as exc:
            logger.warning("Could not answer a Telegram button press: %s", exc)
        if not allowed or not data:
            return
        try:
            if data.startswith("idea "):
                _, verdict, number = data.split()
                reply = await self._decide_idea(chat_id, int(number), verdict, "button")
                if self._idea_for(chat_id, message.get("message_id")) is not None:
                    # An idea's own message: decided, so its buttons go.
                    await self._call(
                        "editMessageReplyMarkup",
                        {"chat_id": chat_id, "message_id": message.get("message_id"),
                         "reply_markup": {"inline_keyboard": []}},
                    )
                await self.send(chat_id, reply)
                return
            reply = await self._answer(chat_id, data)
        except (KeyError, ValueError, RuntimeError, httpx.HTTPError) as exc:
            reply = f"Error: {exc}"
        await self._deliver(chat_id, data, reply)

    # -- ideas ----------------------------------------------------------

    def _idea_for(self, chat_id: int, message_id: object) -> int | None:
        return self._idea_messages.get(f"{chat_id}:{message_id}")

    async def _decide_idea(self, chat_id: int, number: int, verdict: str, why: str) -> str:
        actor = f"human:telegram:{chat_id}"
        ideas = self.environment.ideas
        if verdict == "approve":
            reply = await ideas.approve(number, actor)
        else:
            reply = await ideas.reject(number, actor, why)
        self.environment.wake_ideas()
        return reply

    async def _answer_idea_reply(
        self, chat_id: int, message: dict[str, Any], text: str
    ) -> str | None:
        """A reply to an idea's own message that says yes or no decides that
        idea; any other reply is an ordinary message."""
        replied = message.get("reply_to_message")
        if not isinstance(replied, dict) or chat_id not in self._allowed:
            return None
        number = self._idea_for(chat_id, replied.get("message_id"))
        verdict = verdict_of(text)
        if number is None or verdict is None:
            return None
        return await self._decide_idea(chat_id, number, verdict, text)

    async def _react(self, reaction: dict[str, Any]) -> None:
        """A thumbs-up on an idea's message approves it; a thumbs-down
        rejects it. Only from an allowed chat, and only on an idea."""
        chat_id = int((reaction.get("chat") or {}).get("id", 0))
        if chat_id not in self._allowed:
            return
        number = self._idea_for(chat_id, reaction.get("message_id"))
        if number is None:
            return
        emojis = {
            str(item.get("emoji"))
            for item in reaction.get("new_reaction") or []
            if isinstance(item, dict) and item.get("type") == "emoji"
        }
        if emojis & APPROVE_REACTIONS:
            verdict = "approve"
        elif emojis & REJECT_REACTIONS:
            verdict = "reject"
        else:
            return
        await self.send(chat_id, await self._decide_idea(chat_id, number, verdict, "reaction"))

    async def announce_idea(self, number: int, text: str) -> None:
        """Send an idea and remember which message carries it."""
        buttons = _keyboard(
            [[("✅ Approve", f"idea approve {number}"), ("❌ Reject", f"idea reject {number}")]]
        )
        chunks = _chunks(text)
        for chat_id in sorted(self._allowed):
            for index, chunk in enumerate(chunks):
                payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
                if index == len(chunks) - 1 and buttons:
                    payload["reply_markup"] = buttons
                try:
                    sent = await self._call("sendMessage", payload)
                except (httpx.HTTPError, TelegramError) as exc:
                    logger.warning("Could not send an idea to Telegram chat %s: %s", chat_id, exc)
                    break
                if isinstance(sent, dict) and sent.get("message_id") is not None:
                    self._idea_messages[f"{chat_id}:{sent['message_id']}"] = number
        while len(self._idea_messages) > MAX_IDEA_MESSAGES:
            self._idea_messages.pop(next(iter(self._idea_messages)))
        await self.environment.repository.save_state(self._ideas_key, self._idea_messages)

    # -- routing --------------------------------------------------------

    async def _answer(self, chat_id: int, text: str) -> str:
        if not await self._admit(chat_id):
            logger.info("Telegram chat %s is not on the allow-list", chat_id)
            return (
                f"This chat ({chat_id}) is not allowed to talk to EvoMesh. "
                "Add the id in the Control Center under Telegram."
            )
        first, _, rest = text.partition(" ")
        command = _bare_command(first)
        if command.startswith("/"):
            # A menu tap in a group arrives as /status@botname.
            text = f"{command} {rest}".strip()
        if command in BLOCKED_COMMANDS:
            return "Stopping the mesh is only possible from the Control Center."
        if command == "/start":
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
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            logger.warning("Could not download the file: %s", exc)
            raise TelegramError("downloading the file failed", retry_after=5) from exc
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

    async def send(
        self, chat_id: int, text: str, markup: dict[str, Any] | None = None
    ) -> None:
        chunks = _chunks(text)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if markup and index == len(chunks) - 1:
                payload["reply_markup"] = markup
            try:
                await self._call("sendMessage", payload)
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
            # The error body says why (and, on a 429, for how long) -- found
            # live 2026-09-25: "getUpdates failed with HTTP 429" and nothing
            # else, then a retry 5s later regardless.
            try:
                error = response.json()
            except ValueError:
                error = {}
            if not isinstance(error, dict):
                error = {}
            description = error.get("description") or "no reason given"
            parameters = error.get("parameters")
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            raise TelegramError(
                f"{method} failed with HTTP {response.status_code}: {description}",
                retry_after=float(retry_after) if isinstance(retry_after, int | float) else None,
            )
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


def _bare_command(token: str) -> str:
    """``/Status@evomesh_bot`` -> ``/status``; anything else unchanged but lowered."""
    lowered = token.lower()
    return lowered.split("@", 1)[0] if lowered.startswith("/") else lowered


def _keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any] | None:
    """An inline keyboard, leaving out any button whose data Telegram would
    refuse -- a whole message rejected over one long agent id is worse."""
    keyboard = [
        [
            {"text": label, "callback_data": data}
            for label, data in row
            if len(data.encode("utf-8")) <= CALLBACK_LIMIT
        ]
        for row in rows
    ]
    keyboard = [row for row in keyboard if row]
    return {"inline_keyboard": keyboard} if keyboard else None


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
