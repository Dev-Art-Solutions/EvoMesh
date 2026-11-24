"""Reading mail: IMAP and POP3 sessions, and turning a raw message into fields.

Blocking on purpose -- every call here runs through ``asyncio.to_thread`` from
mail_service.py, the same shape mailer.py's SMTP round trip has. Standard
library only (``imaplib``, ``poplib``, ``email``), no new dependency (rule 16).

What each protocol can do is not the same, and the difference is not hidden:
IMAP has folders, flags, UIDVALIDITY and moves; POP3 has one inbox, a UIDL per
message and nothing else. A POP3 session never deletes (no DELE is ever sent),
so a server keeping mail for other clients keeps it.
"""

from __future__ import annotations

import contextlib
import email
import imaplib
import json
import poplib
import re
import ssl
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from evomesh.config import MailInboundSettings

POP3_FOLDER = "INBOX"
POP3_UIDVALIDITY = "pop3"
SNIPPET_CHARS = 240


class MailError(Exception):
    """A mailbox operation that failed, with a reason fit to show a human."""


@dataclass
class ParsedMessage:
    uid: str
    message_id: str = ""
    thread_key: str = ""
    sender: str = ""
    recipients: list[str] = field(default_factory=list)
    subject: str = ""
    date: str = ""
    flags: list[str] = field(default_factory=list)
    body: str = ""
    snippet: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    attachments: list[dict[str, Any]] = field(default_factory=list)

    def row(
        self, account: str, folder: str, uidvalidity: str, fetched_at: str
    ) -> dict[str, object]:
        return {
            "account": account,
            "folder": folder,
            "uidvalidity": uidvalidity,
            "uid": self.uid,
            "message_id": self.message_id,
            "thread_key": self.thread_key,
            "sender": self.sender,
            "recipients": ", ".join(self.recipients),
            "subject": self.subject,
            "date": self.date,
            "flags": " ".join(self.flags),
            "snippet": self.snippet,
            "body": self.body,
            "headers": json.dumps(self.headers, ensure_ascii=False),
            "attachments": json.dumps(self.attachments, ensure_ascii=False),
            "fetched_at": fetched_at,
        }


class _TextOnly(HTMLParser):
    """HTML to readable text. Scripts, styles and hidden markup are dropped;
    a link keeps its text and its target, so a human-looking label pointing
    somewhere else is visible to whoever reads the result."""

    SKIP = {"script", "style", "head", "title", "noscript", "template"}
    BREAK = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._href: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BREAK:
            self.parts.append("\n")
        if tag == "a":
            self._href.append(dict(attrs).get("href") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "a" and self._href:
            href = self._href.pop()
            if href and href.lower().startswith(("http://", "https://", "mailto:")):
                self.parts.append(f" <{href}>")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextOnly()
    with contextlib.suppress(Exception):
        parser.feed(html)
        parser.close()
    text = "".join(parser.parts)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t\r\f\v]+", " ", text)).strip()


def _ids(value: str) -> list[str]:
    return re.findall(r"<[^<>\s]+>", value or "")


def parse_message(uid: str, raw: bytes, flags: list[str], max_body_chars: int) -> ParsedMessage:
    message = email.message_from_bytes(raw, policy=policy.default)
    assert isinstance(message, EmailMessage)

    def header(name: str) -> str:
        try:
            value = message.get(name)
        except (ValueError, IndexError):  # malformed header: keep going
            return ""
        return " ".join(str(value).split()) if value is not None else ""

    message_id = (_ids(header("Message-ID")) or [""])[0]
    references = _ids(header("References"))
    in_reply_to = (_ids(header("In-Reply-To")) or [""])[0]
    thread_key = (references[0] if references else in_reply_to) or message_id
    try:
        when = parsedate_to_datetime(header("Date"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        date = when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, IndexError):
        date = ""
    recipients = [address for _, address in getaddresses([header("To"), header("Cc")]) if address]
    body = ""
    try:
        part = message.get_body(preferencelist=("plain", "html"))
        if part is not None:
            content = part.get_content()
            body = html_to_text(content) if part.get_content_type() == "text/html" else str(content)
    except (LookupError, ValueError, KeyError, AssertionError):
        body = ""
    body = body.replace("\r\n", "\n").strip()
    if len(body) > max_body_chars:
        body = (
            body[:max_body_chars] + f"\n[... {len(body) - max_body_chars} more characters not kept]"
        )
    attachments: list[dict[str, Any]] = []
    with contextlib.suppress(Exception):
        for item in message.iter_attachments():
            payload = item.get_payload(decode=True)
            attachments.append(
                {
                    "name": item.get_filename() or "",
                    "content_type": item.get_content_type(),
                    "size": len(payload) if isinstance(payload, bytes) else 0,
                }
            )
    kept = {
        name: header(name)
        for name in ("From", "To", "Cc", "Reply-To", "Date", "Subject", "In-Reply-To", "References")
        if header(name)
    }
    return ParsedMessage(
        uid=uid,
        message_id=message_id,
        thread_key=thread_key,
        sender=header("From"),
        recipients=recipients,
        subject=header("Subject"),
        date=date,
        flags=flags,
        body=body,
        snippet=" ".join(body.split())[:SNIPPET_CHARS],
        headers=kept,
        attachments=attachments,
    )


def _context() -> ssl.SSLContext:
    return ssl.create_default_context()


# -- IMAP ---------------------------------------------------------------------

_LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)$')
_UID = re.compile(rb"UID (\d+)")
_FLAGS = re.compile(rb"FLAGS \(([^)]*)\)")


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ImapSession:
    def __init__(self, settings: MailInboundSettings) -> None:
        self.settings = settings
        host, port, timeout = settings.host, settings.resolved_port(), settings.timeout_seconds
        try:
            if settings.security == "ssl":
                self.conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                    host, port, ssl_context=_context(), timeout=timeout
                )
            else:
                self.conn = imaplib.IMAP4(host, port, timeout=timeout)
                if settings.security == "starttls":
                    self.conn.starttls(ssl_context=_context())
            self.conn.login(settings.username, settings.password)
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise MailError(f"IMAP {host}:{port}: {exc}") from exc
        self.capabilities = {cap.upper() for cap in self.conn.capabilities}

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.logout()

    def folders(self) -> list[str]:
        typ, data = self.conn.list()
        if typ != "OK":
            raise MailError("IMAP LIST failed")
        names: list[str] = []
        for line in data:
            if not isinstance(line, bytes):
                continue
            match = _LIST_LINE.match(line)
            if not match or b"\\Noselect" in match.group("flags"):
                continue
            name = match.group("name").decode("utf-8", "replace").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            names.append(name)
        return names

    def select(self, folder: str, *, readonly: bool) -> str:
        typ, data = self.conn.select(_quote(folder), readonly=readonly)
        if typ != "OK":
            raise MailError(f"IMAP cannot open folder '{folder}': {data!r}")
        _, values = self.conn.response("UIDVALIDITY")
        value = values[0] if values and values[0] else b"0"
        return value.decode() if isinstance(value, bytes) else str(value)

    def uids(self, since_uid: int) -> list[int]:
        criteria = f"UID {since_uid + 1}:*" if since_uid else "ALL"
        typ, data = self.conn.uid("SEARCH", None, criteria)  # type: ignore[arg-type]
        if typ != "OK":
            raise MailError("IMAP UID SEARCH failed")
        found = [int(uid) for uid in (data[0] or b"").split()]
        # "N:*" always matches the highest message even when it is older.
        return sorted(uid for uid in found if uid > since_uid)

    def fetch(self, uids: list[int], *, bodies: bool) -> Iterator[tuple[str, list[str], bytes]]:
        part = "BODY.PEEK[]" if bodies else "BODY.PEEK[HEADER]"
        for start in range(0, len(uids), 50):
            chunk = ",".join(str(uid) for uid in uids[start : start + 50])
            typ, data = self.conn.uid("FETCH", chunk, f"(UID FLAGS {part})")
            if typ != "OK":
                raise MailError("IMAP UID FETCH failed")
            for item in data:
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                meta, raw = item[0], item[1]
                uid = _UID.search(meta)
                if uid is None:
                    continue
                flags = _FLAGS.search(meta)
                yield (
                    uid.group(1).decode(),
                    flags.group(1).decode().split() if flags else [],
                    raw if isinstance(raw, bytes) else b"",
                )

    def flags_for(self, low_uid: int) -> dict[str, list[str]]:
        typ, data = self.conn.uid("FETCH", f"{low_uid}:*", "(UID FLAGS)")
        if typ != "OK":
            return {}
        found: dict[str, list[str]] = {}
        for item in data:
            meta = item[0] if isinstance(item, tuple) else item
            if not isinstance(meta, bytes):
                continue
            uid, flags = _UID.search(meta), _FLAGS.search(meta)
            if uid:
                found[uid.group(1).decode()] = flags.group(1).decode().split() if flags else []
        return found

    def store(self, uid: str, add: bool, flag: str) -> None:
        typ, _ = self.conn.uid("STORE", uid, "+FLAGS" if add else "-FLAGS", f"({flag})")
        if typ != "OK":
            raise MailError(f"IMAP STORE {flag} failed")

    def move(self, uid: str, target: str) -> str:
        """Move, never expunge anything else: without MOVE or UIDPLUS the
        original is copied and flagged deleted, and left for the server."""
        if "MOVE" in self.capabilities:
            typ, data = self.conn.uid("MOVE", uid, _quote(target))
            if typ != "OK":
                raise MailError(f"IMAP MOVE to '{target}' failed: {data!r}")
            return "moved"
        typ, data = self.conn.uid("COPY", uid, _quote(target))
        if typ != "OK":
            raise MailError(f"IMAP COPY to '{target}' failed: {data!r}")
        self.store(uid, True, "\\Deleted")
        if "UIDPLUS" in self.capabilities:
            self.conn.uid("EXPUNGE", uid)
            return "moved"
        return "copied; the original is flagged deleted and the server will remove it"

    def append(self, folder: str, raw: bytes) -> None:
        typ, data = self.conn.append(_quote(folder), "(\\Seen)", None, raw)  # type: ignore[arg-type]
        if typ != "OK":
            raise MailError(f"IMAP APPEND to '{folder}' failed: {data!r}")


# -- POP3 -------------------------------------------------------------------


class Pop3Session:
    def __init__(self, settings: MailInboundSettings) -> None:
        self.settings = settings
        host, port, timeout = settings.host, settings.resolved_port(), settings.timeout_seconds
        try:
            if settings.security == "ssl":
                self.conn: poplib.POP3 = poplib.POP3_SSL(
                    host, port, timeout=timeout, context=_context()
                )
            else:
                self.conn = poplib.POP3(host, port, timeout=timeout)
                if settings.security == "starttls":
                    self.conn.stls(context=_context())
            self.conn.user(settings.username)
            self.conn.pass_(settings.password)
        except (poplib.error_proto, OSError, ssl.SSLError) as exc:
            raise MailError(f"POP3 {host}:{port}: {exc}") from exc

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.quit()

    def listing(self) -> dict[str, int]:
        """UIDL -> message number in this session."""
        _, lines, _ = self.conn.uidl()  # type: ignore[misc]
        found: dict[str, int] = {}
        for line in lines:
            number, _, uid = line.decode("utf-8", "replace").partition(" ")
            if uid.strip():
                found[uid.strip()] = int(number)
        return found

    def fetch(self, number: int, *, bodies: bool) -> bytes:
        _, lines, _ = self.conn.retr(number) if bodies else self.conn.top(number, 0)  # type: ignore[misc]
        return b"\r\n".join(lines)


def open_session(settings: MailInboundSettings) -> ImapSession | Pop3Session:
    return ImapSession(settings) if settings.protocol == "imap" else Pop3Session(settings)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
