import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from evomesh.config import EmailAccountSettings, Settings, load_settings
from evomesh.console import ConsoleChannel
from evomesh.contracts import AgentDefinition
from evomesh.environment import Environment
from evomesh.harness_tools import ToolContext, ToolDenied
from evomesh.mailer import (
    EmailRefused,
    Mailer,
    build_send_email_tool,
    parse_recipients,
    recipient_allowed,
)
from evomesh.models import MockProvider


class FakeSmtp:
    """Just enough SMTP (no TLS, no auth) to accept one message per
    connection and keep what arrived -- a real socket round trip through
    smtplib, not a mock of it."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.recipients: list[list[str]] = []
        self.server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        assert self.server is not None
        return self.server.sockets[0].getsockname()[1]

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def say(line: str) -> None:
            writer.write(f"{line}\r\n".encode())
            await writer.drain()

        await say("220 fake ESMTP")
        rcpts: list[str] = []
        while True:
            raw = await reader.readline()
            if not raw:
                break
            line = raw.decode().strip()
            verb = line.split(" ", 1)[0].upper()
            if verb in {"EHLO", "HELO"}:
                await say("250 fake")
            elif verb == "MAIL":
                await say("250 ok")
            elif verb == "RCPT":
                rcpts.append(line.split(":", 1)[1].strip(" <>"))
                await say("250 ok")
            elif verb == "DATA":
                await say("354 go")
                body: list[str] = []
                while True:
                    data = (await reader.readline()).decode()
                    if data.rstrip("\r\n") == ".":
                        break
                    body.append(data)
                self.messages.append("".join(body))
                self.recipients.append(rcpts)
                rcpts = []
                await say("250 queued")
            elif verb == "QUIT":
                await say("221 bye")
                break
            else:
                await say("250 ok")
        writer.close()


@pytest.fixture
async def smtp() -> AsyncIterator[FakeSmtp]:
    fake = FakeSmtp()
    fake.server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
    try:
        yield fake
    finally:
        fake.server.close()
        await fake.server.wait_closed()


def account(port: int, **overrides: object) -> EmailAccountSettings:
    values: dict[str, object] = {
        "host": "127.0.0.1",
        "port": port,
        "security": "none",
        "from_address": "mesh@example.com",
        "from_name": "EvoMesh",
    }
    values.update(overrides)
    return EmailAccountSettings.model_validate(values)


def test_parse_recipients() -> None:
    assert parse_recipients("a@x.com, B <b@y.org>") == ["a@x.com", "b@y.org"]
    with pytest.raises(EmailRefused):
        parse_recipients("not-an-address")


def test_recipient_allow_list() -> None:
    assert recipient_allowed("a@x.com", [])
    assert recipient_allowed("A@X.com", ["@x.com"])
    assert recipient_allowed("a@x.com", ["a@x.com"])
    assert not recipient_allowed("a@evilx.com", ["@x.com", "b@x.com"])


async def test_send_delivers_and_audits(smtp: FakeSmtp, tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    mailer = Mailer({"alerts": account(smtp.port)}, audit_path=audit)
    record = await mailer.send("alerts", "ops@example.com", "Gold alert", "XAUUSD up", agent="A")
    assert record.status == "sent"
    assert smtp.recipients == [["ops@example.com"]]
    assert "Subject: Gold alert" in smtp.messages[0]
    assert "XAUUSD up" in smtp.messages[0]
    assert "From: EvoMesh <mesh@example.com>" in smtp.messages[0]
    line = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert line["status"] == "sent" and line["agent"] == "A"


async def test_send_refusals_are_audited_and_never_attempted(tmp_path: Path) -> None:
    delivered: list[object] = []
    mailer = Mailer(
        {
            "alerts": account(
                1, allowed_recipients=["@example.com"], max_per_hour=1, max_recipients=2
            )
        },
        audit_path=tmp_path / "audit.jsonl",
        deliver=lambda acct, message: delivered.append(message),
    )
    with pytest.raises(EmailRefused, match="may not mail"):
        await mailer.send("alerts", "x@other.org", "s", "b")
    with pytest.raises(EmailRefused, match="no email account"):
        await mailer.send("nope", "a@example.com", "s", "b")
    with pytest.raises(EmailRefused, match="recipients"):
        await mailer.send("alerts", "a@example.com,b@example.com,c@example.com", "s", "b")
    with pytest.raises(EmailRefused, match="subject"):
        await mailer.send("alerts", "a@example.com", " \r\n ", "b")
    await mailer.send("alerts", "a@example.com", "one", "b")
    with pytest.raises(EmailRefused, match="last hour"):
        await mailer.send("alerts", "a@example.com", "two", "b")
    assert len(delivered) == 1
    statuses = [record.status for record in mailer.recent()]
    assert statuses == ["refused", "refused", "refused", "refused", "sent", "refused"]


async def test_smtp_failure_is_a_runtime_error() -> None:
    mailer = Mailer({"dead": account(1, timeout_seconds=2)})
    with pytest.raises(RuntimeError, match="failed"):
        await mailer.send("dead", "a@example.com", "s", "b")
    assert mailer.recent()[-1].status == "failed"


async def test_tool_is_limited_to_granted_accounts(smtp: FakeSmtp) -> None:
    mailer = Mailer({"alerts": account(smtp.port), "billing": account(smtp.port)})
    assert build_send_email_tool(mailer, [], "A") is None
    assert build_send_email_tool(mailer, ["ghost"], "A") is None
    tool = build_send_email_tool(mailer, ["alerts"], "A")
    assert tool is not None
    assert tool.parameters["properties"]["account"]["enum"] == ["alerts"]
    context = ToolContext(root=Path("."))
    result = await tool.run(context, {"to": "a@example.com", "subject": "hi", "body": "b"})
    assert "sent to a@example.com" in result
    with pytest.raises(ToolDenied, match="one of alerts"):
        await tool.run(
            context, {"account": "billing", "to": "a@example.com", "subject": "x", "body": "y"}
        )


def test_password_ref_resolves_from_secrets(tmp_path: Path) -> None:
    (tmp_path / "evomesh.yaml").write_text(
        "email:\n  accounts:\n    alerts:\n      host: smtp.example.com\n"
        "      from_address: a@example.com\n      username: a\n      password_ref: mail\n",
        encoding="utf-8",
    )
    (tmp_path / "evomesh.secrets.yaml").write_text("mail: s3cret\n", encoding="utf-8")
    settings = load_settings(tmp_path / "evomesh.yaml")
    assert settings.email.accounts["alerts"].password == "s3cret"
    (tmp_path / "evomesh.secrets.yaml").write_text("other: x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="password_ref"):
        load_settings(tmp_path / "evomesh.yaml")


async def test_email_commands_grant_and_offer_the_tool(smtp: FakeSmtp, tmp_path: Path) -> None:
    settings = Settings(
        data_path=tmp_path / "data.db",
        generation_path=tmp_path / "generations",
        workspace_path=tmp_path / "workspace",
    )
    settings.email.accounts["alerts"] = account(smtp.port)
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    try:
        agent = AgentDefinition(name="Analyst", purpose="p", model_name="mock-model")
        await environment.register_agent(agent)
        console = ConsoleChannel(environment)
        assert "agents: none" in await console.route("/email accounts")
        assert "No email account" in await console.route("/email grant Analyst ghost")
        assert "may now send" in await console.route("/email grant Analyst alerts")
        assert "agents: Analyst" in await console.route("/email accounts")
        names = {tool.name for tool in environment.builtin_tools_for(agent)}
        assert "send_email" in names
        assert "Sent a test" in await console.route("/email test alerts ops@example.com")
        assert "sent console via alerts" in await console.route("/email log")
        assert "no longer" in await console.route("/email revoke Analyst alerts")
        names = {tool.name for tool in environment.builtin_tools_for(agent)}
        assert "send_email" not in names
    finally:
        await environment.stop()


async def test_stale_grants_are_named_and_can_be_revoked(tmp_path: Path) -> None:
    settings = Settings(
        data_path=tmp_path / "data.db",
        generation_path=tmp_path / "generations",
        workspace_path=tmp_path / "workspace",
    )
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    try:
        agent = AgentDefinition(
            name="Analyst",
            purpose="p",
            model_name="mock-model",
            email_accounts=["gone"],
            mcp=["vanished"],
        )
        await environment.register_agent(agent)
        problems = environment.stale_grants()
        assert any("'gone'" in problem for problem in problems)
        assert any("'vanished'" in problem for problem in problems)
        console = ConsoleChannel(environment)
        assert "no longer" in await console.route("/email revoke Analyst gone")
        assert "none" in await console.route("/mcp revoke Analyst vanished")
        assert environment.stale_grants() == []
    finally:
        await environment.stop()
