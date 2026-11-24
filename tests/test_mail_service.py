"""Reading mail, drafts and approval -- against fake IMAP/POP3 sessions and a
real SMTP round trip (test_mailer.FakeSmtp)."""

import json
import smtplib
from collections.abc import AsyncIterator
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from evomesh.config import EmailAccountSettings, MailInboundSettings, Settings
from evomesh.console import ConsoleChannel
from evomesh.contracts import AgentDefinition
from evomesh.environment import Environment
from evomesh.harness_tools import Tool, ToolContext, ToolDenied
from evomesh.mail_client import ImapSession, MailError, Pop3Session, html_to_text, parse_message
from evomesh.mail_service import MailService, build_mail_tools
from evomesh.mailer import Mailer
from evomesh.manager import classify
from evomesh.models import MockProvider
from evomesh.storage import SQLiteRepository
from tests.test_mailer import FakeSmtp, smtp  # noqa: F401 - the fixture

CONTEXT = ToolContext(root=Path("."))


def raw_message(
    uid: int,
    *,
    subject: str = "Hello",
    sender: str = "Client <client@example.org>",
    body: str = "Plain body",
    html: str = "",
    references: str = "",
    attachment: bool = False,
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "support@example.com, other@example.org"
    message["Subject"] = subject
    message["Date"] = f"Mon, 2{uid % 10} Nov 2026 10:00:00 +0000"
    message["Message-ID"] = f"<m{uid}@example.org>"
    if references:
        message["References"] = references
        message["In-Reply-To"] = references.split()[-1]
    if html:
        message.set_content(body)
        message.add_alternative(html, subtype="html")
    else:
        message.set_content(body)
    if attachment:
        message.add_attachment(
            b"%PDF-1.4 data", maintype="application", subtype="pdf", filename="offer.pdf"
        )
    return message.as_bytes()


class Mailbox:
    """What the fake server holds, shared across sessions like a real one."""

    def __init__(self) -> None:
        self.uidvalidity = "100"
        self.folders: dict[str, dict[int, tuple[list[str], bytes]]] = {"INBOX": {}, "Archive": {}}
        self.next_uid = 1
        self.logins = 0

    def deliver(self, raw: bytes, folder: str = "INBOX") -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.folders[folder][uid] = ([], raw)
        return uid


class FakeImap(ImapSession):
    def __init__(self, settings: MailInboundSettings, box: Mailbox) -> None:  # noqa: D107
        self.settings = settings
        self.box = box
        self.capabilities = {"IMAP4REV1", "MOVE"}
        self.folder = "INBOX"
        box.logins += 1

    def close(self) -> None:
        pass

    def folders(self) -> list[str]:
        return list(self.box.folders)

    def select(self, folder: str, *, readonly: bool) -> str:
        if folder not in self.box.folders:
            raise MailError(f"no folder {folder}")
        self.folder = folder
        return self.box.uidvalidity

    def uids(self, since_uid: int) -> list[int]:
        return sorted(uid for uid in self.box.folders[self.folder] if uid > since_uid)

    def fetch(self, uids: list[int], *, bodies: bool):  # type: ignore[override]
        for uid in uids:
            flags, raw = self.box.folders[self.folder][uid]
            yield str(uid), list(flags), raw if bodies else raw.split(b"\n\n", 1)[0]

    def flags_for(self, low_uid: int) -> dict[str, list[str]]:
        return {
            str(uid): list(flags)
            for uid, (flags, _) in self.box.folders[self.folder].items()
            if uid >= low_uid
        }

    def store(self, uid: str, add: bool, flag: str) -> None:
        flags = self.box.folders[self.folder][int(uid)][0]
        if add and flag not in flags:
            flags.append(flag)
        if not add and flag in flags:
            flags.remove(flag)

    def move(self, uid: str, target: str) -> str:
        self.box.folders[target][int(uid)] = self.box.folders[self.folder].pop(int(uid))
        return "moved"

    def append(self, folder: str, raw: bytes) -> None:
        self.box.deliver(raw, folder)


class FakePop3(Pop3Session):
    def __init__(self, settings: MailInboundSettings, messages: dict[str, bytes]) -> None:  # noqa: D107
        self.settings = settings
        self.messages = messages

    def close(self) -> None:
        pass

    def listing(self) -> dict[str, int]:
        return {uid: index + 1 for index, uid in enumerate(self.messages)}

    def fetch(self, number: int, *, bodies: bool) -> bytes:
        return list(self.messages.values())[number - 1]


def mail_account(port: int, protocol: str = "imap", **incoming: Any) -> EmailAccountSettings:
    return EmailAccountSettings.model_validate(
        {
            "host": "127.0.0.1",
            "port": port,
            "security": "none",
            "from_address": "support@example.com",
            "incoming": {
                "protocol": protocol,
                "host": "mail.example.com",
                "sync_interval_seconds": 0,
                **incoming,
            },
        }
    )


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncIterator[SQLiteRepository]:
    repo = SQLiteRepository(tmp_path / "mail.db")
    await repo.initialize()
    yield repo


def service_for(
    repository: SQLiteRepository, accounts: dict[str, EmailAccountSettings], factory: Any
) -> tuple[MailService, list[str]]:
    said: list[str] = []
    service = MailService(
        accounts, repository, Mailer(accounts), announce=said.append, session_factory=factory
    )
    return service, said


def tool(tools: list[Tool], name: str) -> Tool:
    return next(item for item in tools if item.name == name)


def test_parse_message_prefers_text_strips_html_and_lists_attachments() -> None:
    html = '<p>Hi<script>steal()</script> <a href="https://evil.example/x">bank.com</a></p>'
    parsed = parse_message(
        "7",
        raw_message(
            7, subject="Пакет =?x", body="", html=html, references="<a@x> <b@x>", attachment=True
        ),
        ["\\Seen"],
        50_000,
    )
    assert parsed.thread_key == "<a@x>"
    assert parsed.subject == "Пакет =?x"
    assert parsed.attachments == [
        {"name": "offer.pdf", "content_type": "application/pdf", "size": 13}
    ]
    assert parsed.date.startswith("2026-11-27T10:00:00")
    assert "steal" not in html_to_text(html)
    assert "<https://evil.example/x>" in html_to_text(html)


def test_a_malformed_message_still_parses() -> None:
    parsed = parse_message(
        "1", b"Subject: =?broken\r\nDate: not a date\r\n\r\n\xff\xfe body", [], 100
    )
    assert parsed.date == ""
    assert parsed.uid == "1"


async def test_imap_sync_is_incremental_survives_restart_and_uidvalidity_reset(
    repository: SQLiteRepository,
) -> None:
    box = Mailbox()
    for uid in range(1, 4):
        box.deliver(raw_message(uid))
    accounts = {"support": mail_account(25)}
    service, _ = service_for(repository, accounts, lambda s: FakeImap(s, box))
    assert "3 new" in await service.sync("support")
    box.deliver(raw_message(4))
    box.folders["INBOX"][1][0].append("\\Seen")
    del box.folders["INBOX"][2]

    restarted, _ = service_for(repository, accounts, lambda s: FakeImap(s, box))
    assert "1 new" in await restarted.sync("support")
    rows = await restarted.search(["support"], limit=25)
    assert sorted(str(row["uid"]) for row in rows) == ["1", "3", "4"]
    assert "\\Seen" in next(str(r["flags"]) for r in rows if r["uid"] == "1")
    assert [str(r["uid"]) for r in await restarted.search(["support"], unread_only=True)] == [
        "4",
        "3",
    ]

    box.uidvalidity = "200"
    result = await restarted.sync("support")
    assert "UIDVALIDITY changed" in result
    rows = await restarted.search(["support"], limit=25)
    assert {str(row["uidvalidity"]) for row in rows} == {"200"}
    assert len(rows) == 3


async def test_pop3_reads_the_inbox_without_duplicates_and_refuses_to_manage(
    repository: SQLiteRepository,
) -> None:
    messages = {"uidl-a": raw_message(1), "uidl-b": raw_message(2)}
    accounts = {"home": mail_account(25, protocol="pop3")}
    service, _ = service_for(repository, accounts, lambda s: FakePop3(s, messages))
    assert "2 new" in await service.sync("home")
    assert "0 new" in await service.sync("home")
    rows = await service.search(["home"])
    with pytest.raises(MailError, match="POP3"):
        await service.change(f"home|INBOX|pop3|{rows[0]['uid']}", "read")


async def test_grants_decide_which_tools_and_which_accounts(repository: SQLiteRepository) -> None:
    box = Mailbox()
    box.deliver(raw_message(1))
    accounts = {"support": mail_account(25), "sales": mail_account(25)}
    service, _ = service_for(repository, accounts, lambda s: FakeImap(s, box))
    nobody = AgentDefinition(name="Nobody", purpose="p")
    assert build_mail_tools(service, nobody) == []

    reader = AgentDefinition(name="Reader", purpose="p", mail_read=["support"])
    tools = build_mail_tools(service, reader)
    names = {item.name for item in tools}
    assert {"mail_search", "mail_read"} <= names
    assert not names & {"send_email", "mail_draft", "mail_manage"}
    listing = await tool(tools, "mail_search").run(CONTEXT, {})
    ref = listing.split("ref: ")[1].split()[0]
    text = await tool(tools, "mail_read").run(CONTEXT, {"ref": ref})
    assert "UNTRUSTED EMAIL CONTENT" in text and "Plain body" in text
    with pytest.raises(ToolDenied, match="may read support"):
        await tool(tools, "mail_read").run(CONTEXT, {"ref": ref.replace("support", "sales", 1)})


async def test_every_send_waits_for_approval_and_happens_once(
    repository: SQLiteRepository,
    smtp: FakeSmtp,  # noqa: F811
) -> None:
    box = Mailbox()
    box.deliver(raw_message(1, references="<root@example.org>"))
    accounts = {"support": mail_account(smtp.port, sent_folder="Archive")}
    service, said = service_for(repository, accounts, lambda s: FakeImap(s, box))
    agent = AgentDefinition(
        name="Assistant", purpose="p", mail_read=["support"], email_accounts=["support"]
    )
    tools = build_mail_tools(service, agent)

    answer = await tool(tools, "send_email").run(
        CONTEXT, {"to": "a@example.org", "subject": "Hi", "body": "Text"}
    )
    assert "waiting for a human's approval" in answer
    assert smtp.messages == []
    assert "/email approve" in said[-1]

    listing = await tool(tools, "mail_search").run(CONTEXT, {})
    ref = listing.split("ref: ")[1].split()[0]
    drafted = await tool(tools, "mail_reply").run(
        CONTEXT, {"ref": ref, "body": "Thanks", "reply_all": True}
    )
    reply_id = drafted.split("draft ")[1].split()[0]
    await tool(tools, "mail_send_draft").run(CONTEXT, {"draft_id": reply_id})
    # An edit after the request needs a fresh approval.
    await tool(tools, "mail_draft").run(CONTEXT, {"draft_id": reply_id, "body": "Thanks!"})
    assert "not waiting" in await service.approve(reply_id)
    await tool(tools, "mail_send_draft").run(CONTEXT, {"draft_id": reply_id})

    assert "Sent draft" in await service.approve(reply_id)
    assert "is sent" in await service.approve(reply_id)
    assert len(smtp.messages) == 1
    sent = smtp.messages[0]
    assert "In-Reply-To: <m1@example.org>" in sent
    assert "References: <root@example.org> <m1@example.org>" in sent
    assert "Cc: other@example.org" in sent
    assert len(box.folders["Archive"]) == 1  # filed in sent_folder

    first = answer.split("draft ")[1].split()[0]
    assert "denied" in await service.deny(first)
    assert len(smtp.messages) == 1


async def test_an_uncertain_send_is_never_retried_and_survives_a_restart(
    repository: SQLiteRepository,
) -> None:
    accounts = {"support": mail_account(25)}

    def drop_mid_send(account: EmailAccountSettings, message: EmailMessage) -> None:
        raise smtplib.SMTPServerDisconnected("connection lost during DATA")

    service, said = service_for(repository, accounts, lambda s: FakeImap(s, Mailbox()))
    service.mailer.deliver = drop_mid_send
    agent = AgentDefinition(name="A", purpose="p", email_accounts=["support"])
    answer = await tool(build_mail_tools(service, agent), "send_email").run(
        CONTEXT, {"to": "a@example.org", "subject": "Hi", "body": "x"}
    )
    draft_id = answer.split("draft ")[1].split()[0]
    assert "may have gone out" in await service.approve(draft_id)
    assert "is uncertain" in await service.approve(draft_id)
    assert "will not be resent" in said[-1]

    draft = await service.draft(draft_id)
    draft.state = "sending"  # as if the process died mid-send
    await service.save(draft)
    restarted, _ = service_for(repository, accounts, lambda s: FakeImap(s, Mailbox()))
    assert "may or may not have been sent" in await restarted.unsettled_report()
    assert (await restarted.draft(draft_id)).state == "uncertain"


async def test_the_hourly_cap_is_read_from_the_database(
    repository: SQLiteRepository,
    smtp: FakeSmtp,  # noqa: F811
) -> None:
    accounts = {"support": mail_account(smtp.port, sync_interval_seconds=0)}
    accounts["support"].max_per_hour = 1
    agent = AgentDefinition(name="A", purpose="p", email_accounts=["support"])
    service, _ = service_for(repository, accounts, lambda s: FakeImap(s, Mailbox()))
    for _ in range(2):
        await tool(build_mail_tools(service, agent), "send_email").run(
            CONTEXT, {"to": "a@example.org", "subject": "Hi", "body": "x"}
        )
    pending = [
        json.loads(str(r["payload"]))["id"] for r in await repository.draft_rows(("pending",))
    ]
    assert "Sent" in await service.approve(pending[0])
    restarted, _ = service_for(repository, accounts, lambda s: FakeImap(s, Mailbox()))
    with pytest.raises(MailError, match="max_per_hour"):
        await restarted.approve(pending[1])


async def test_manage_moves_and_flags_on_the_server(repository: SQLiteRepository) -> None:
    box = Mailbox()
    box.deliver(raw_message(1))
    service, _ = service_for(repository, {"support": mail_account(25)}, lambda s: FakeImap(s, box))
    agent = AgentDefinition(name="A", purpose="p", mail_read=["support"], mail_manage=["support"])
    tools = build_mail_tools(service, agent)
    ref = (await tool(tools, "mail_search").run(CONTEXT, {})).split("ref: ")[1].split()[0]
    await tool(tools, "mail_manage").run(CONTEXT, {"ref": ref, "action": "read"})
    assert box.folders["INBOX"][1][0] == ["\\Seen"]
    await tool(tools, "mail_manage").run(CONTEXT, {"ref": ref, "action": "archive"})
    assert 1 in box.folders["Archive"] and not box.folders["INBOX"]


def test_the_manager_can_never_approve_an_email() -> None:
    assert classify("/email approve ab12cd34") == "forbidden"
    assert classify("/email deny ab12cd34") == "forbidden"
    assert classify("/email pending") == "read"
    assert classify("/email grant Assistant support read") == "approve"


async def test_console_grants_levels_and_lists_pending(tmp_path: Path) -> None:
    settings = Settings(
        data_path=tmp_path / "data.db",
        generation_path=tmp_path / "generations",
        workspace_path=tmp_path / "workspace",
    )
    settings.email.accounts["support"] = mail_account(25)
    settings.email.accounts["outbound"] = EmailAccountSettings(
        host="127.0.0.1", from_address="o@example.com"
    )
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    try:
        agent = AgentDefinition(name="Assistant", purpose="p", model_name="mock-model")
        await environment.register_agent(agent)
        console = ConsoleChannel(environment)
        assert "nothing to read" in await console.route("/email grant Assistant outbound read")
        assert "may now manage" in await console.route("/email grant Assistant support manage")
        assert agent.mail_read == ["support"] and agent.mail_manage == ["support"]
        assert "No email is waiting" in await console.route("/email pending")
        assert "no longer read, manage" in await console.route(
            "/email revoke Assistant support read"
        )
        assert agent.mail_read == [] and agent.mail_manage == []
    finally:
        await environment.stop()
