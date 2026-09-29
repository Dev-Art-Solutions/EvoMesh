"""Outgoing email over SMTP, from named accounts an agent is granted.

Accounts live in ``evomesh.yaml`` under ``email.accounts`` (passwords by
``password_ref`` into evomesh.secrets.yaml). An agent can send from exactly
the accounts in its ``AgentDefinition.email_accounts`` -- empty, the
default, means the ``send_email`` tool is not even offered, the same "an
unusable tool in the schema is a tool a model will try" reasoning the
harness applies to ``shell`` and ``fetch``.

Standard library only (``smtplib``, ``email``), run in a worker thread --
no new runtime dependency (rule 16). Every attempt, sent or refused, is
appended to an audit file (``email-audit.jsonl`` beside the database) so a
human can see what the mesh mailed, and to whom, after the fact.
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import ssl
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid
from pathlib import Path
from typing import Any

from evomesh.config import EmailAccountSettings
from evomesh.contracts import now_utc
from evomesh.harness_tools import Tool, ToolDenied

logger = logging.getLogger(__name__)

HOUR = 3600.0


class EmailRefused(Exception):
    """A send the mailer would not attempt: unknown account, not granted,
    a recipient outside the allow-list, over the rate limit, a malformed
    header. The message is written for the model to read and act on."""


@dataclass
class SendRecord:
    at: str
    agent: str
    account: str
    to: list[str]
    subject: str
    status: str
    detail: str = ""

    def as_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)


def parse_recipients(raw: str | list[str]) -> list[str]:
    values = [raw] if isinstance(raw, str) else [str(item) for item in raw]
    addresses = [address.strip() for _, address in getaddresses(values) if address.strip()]
    for address in addresses:
        local, _, domain = address.rpartition("@")
        if not local or "." not in domain or any(ch in address for ch in " \r\n,;<>"):
            raise EmailRefused(f"'{address}' is not an email address")
    return addresses


def recipient_allowed(address: str, allowed: list[str]) -> bool:
    if not allowed:
        return True
    lowered = address.lower()
    for entry in allowed:
        rule = entry.strip().lower()
        if rule.startswith("@") and lowered.endswith(rule):
            return True
        if lowered == rule:
            return True
    return False


def _deliver(account: EmailAccountSettings, message: EmailMessage) -> None:
    """Blocking SMTP round trip -- always called through asyncio.to_thread."""
    timeout = account.timeout_seconds
    if account.security == "ssl":
        client: smtplib.SMTP = smtplib.SMTP_SSL(
            account.host, account.port, timeout=timeout, context=ssl.create_default_context()
        )
    else:
        client = smtplib.SMTP(account.host, account.port, timeout=timeout)
    with client:
        client.ehlo()
        if account.security == "starttls":
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if account.username:
            client.login(account.username, account.password)
        client.send_message(message)


@dataclass
class Mailer:
    """Mesh-wide, owned by Environment.mailer."""

    accounts: dict[str, EmailAccountSettings]
    audit_path: Path | None = None
    deliver: Callable[[EmailAccountSettings, EmailMessage], None] = _deliver
    _sent: dict[str, deque[float]] = field(default_factory=dict)
    _recent: deque[SendRecord] = field(default_factory=lambda: deque(maxlen=200))

    def names(self) -> list[str]:
        return sorted(self.accounts)

    def recent(self, limit: int = 20) -> list[SendRecord]:
        return list(self._recent)[-limit:]

    def _check_rate(self, name: str, account: EmailAccountSettings) -> None:
        window = self._sent.setdefault(name, deque())
        now = time.monotonic()
        while window and now - window[0] > HOUR:
            window.popleft()
        if len(window) >= account.max_per_hour:
            wait = int(HOUR - (now - window[0])) + 1
            raise EmailRefused(
                f"account '{name}' has sent {account.max_per_hour} emails in the last hour; "
                f"try again in {wait}s"
            )

    def build(
        self, name: str, to: list[str], subject: str, body: str
    ) -> tuple[EmailAccountSettings, EmailMessage]:
        account = self.accounts.get(name)
        if account is None:
            known = ", ".join(self.names()) or "none configured"
            raise EmailRefused(f"no email account '{name}' (accounts: {known})")
        if not to:
            raise EmailRefused("no recipient given")
        if len(to) > account.max_recipients:
            raise EmailRefused(
                f"{len(to)} recipients, over this account's {account.max_recipients}"
            )
        allowed = account.allowed_recipients
        refused = [address for address in to if not recipient_allowed(address, allowed)]
        if refused:
            raise EmailRefused(
                f"account '{name}' may not mail {', '.join(refused)} "
                f"(allowed: {', '.join(account.allowed_recipients)})"
            )
        subject = " ".join(subject.split())
        if not subject:
            raise EmailRefused("the subject is empty")
        if len(body) > account.max_body_chars:
            raise EmailRefused(f"body is {len(body)} chars, over {account.max_body_chars}")
        message = EmailMessage()
        message["From"] = formataddr((account.from_name, account.from_address))
        message["To"] = ", ".join(to)
        message["Subject"] = subject[:250]
        message["Message-ID"] = make_msgid(domain=account.from_address.rpartition("@")[2] or None)
        message["X-Mailer"] = "EvoMesh"
        message.set_content(body)
        return account, message

    async def send(
        self, name: str, to: str | list[str], subject: str, body: str, *, agent: str = "console"
    ) -> SendRecord:
        """Validate, rate-limit, deliver, audit. Raises EmailRefused for a
        send never attempted and RuntimeError for one the server failed."""
        recipients: list[str] = []
        try:
            recipients = parse_recipients(to)
            account, message = self.build(name, recipients, subject, body)
            self._check_rate(name, account)
        except EmailRefused as exc:
            await self._audit(
                SendRecord(_now(), agent, name, recipients, subject, "refused", str(exc))
            )
            raise
        # Counted before delivery: a server that accepts and then errors
        # still may have sent it, and the limit is there to stop floods.
        self._sent.setdefault(name, deque()).append(time.monotonic())
        try:
            await asyncio.to_thread(self.deliver, account, message)
        except (smtplib.SMTPException, OSError) as exc:
            record = SendRecord(_now(), agent, name, recipients, subject, "failed", str(exc))
            await self._audit(record)
            raise RuntimeError(f"SMTP send through '{name}' failed: {exc}") from exc
        record = SendRecord(_now(), agent, name, recipients, subject, "sent")
        await self._audit(record)
        logger.info("email: %s sent '%s' to %s via %s", agent, subject, recipients, name)
        return record

    async def _audit(self, record: SendRecord) -> None:
        self._recent.append(record)
        if self.audit_path is None:
            return

        def write() -> None:
            assert self.audit_path is not None
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(record.as_json() + "\n")

        try:
            await asyncio.to_thread(write)
        except OSError:
            logger.warning("email: could not write the audit log", exc_info=True)


def _now() -> str:
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


def build_send_email_tool(mailer: Mailer, accounts: list[str], agent: str) -> Tool | None:
    """The ``send_email`` harness tool, limited to ``accounts`` that exist.
    None when there are none -- the tool is then not offered at all."""
    usable = [name for name in accounts if name in mailer.accounts]
    if not usable:
        return None

    async def run(context: Any, args: dict[str, Any]) -> str:
        del context
        account = str(args.get("account") or (usable[0] if len(usable) == 1 else ""))
        if account not in usable:
            raise ToolDenied(f"DENIED: account must be one of {', '.join(usable)}")
        try:
            record = await mailer.send(
                account,
                args.get("to") or "",
                str(args.get("subject") or ""),
                str(args.get("body") or ""),
                agent=agent,
            )
        except (EmailRefused, RuntimeError) as exc:
            raise ToolDenied(f"DENIED: {exc}") from exc
        return f"sent to {', '.join(record.to)} from '{account}'"

    return Tool(
        name="send_email",
        description=(
            "Send a plain-text email. Only when your goal or the human asks for an email; "
            "never to an address you guessed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "account": {"type": "string", "enum": usable},
                "to": {"type": "string", "description": "Comma-separated addresses"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
        run=run,
    )
