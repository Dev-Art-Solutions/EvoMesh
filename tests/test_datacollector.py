"""The Data Collector outbox: nothing acknowledged is lost, nothing is sent
twice by this side, and the receiver is a real HTTP exchange (httpx's
MockTransport) rather than a mocked client method."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from evomesh.config import DataCollectorSettings, Settings
from evomesh.console import ConsoleChannel
from evomesh.contracts import AgentDefinition, DataCollectorGrant
from evomesh.datacollector import (
    DataCollector,
    EventRefused,
    build_dc_tools,
    build_event,
    canonical_url,
    key_for,
)
from evomesh.environment import Environment
from evomesh.harness_tools import ToolContext, ToolDenied
from evomesh.models import MockProvider
from evomesh.storage import SchemaTooNewError, SQLiteRepository


def settings(**overrides: Any) -> DataCollectorSettings:
    values: dict[str, Any] = {
        "enabled": True,
        "api_base_url": "https://dc.test/api/v1",
        "api_key": "k-test",
        "collections": {"news_article": "col-news", "business": "col-biz"},
        "min_backoff_seconds": 1,
        "max_attempts": 3,
    }
    values.update(overrides)
    return DataCollectorSettings.model_validate(values)


class Receiver:
    """A Data Collector stand-in: stores by Idempotency-Key the way the real
    one does for 24 h, and can be told to fail."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.requests = 0
        self.mode = "ok"
        self.batch_plan: list[dict[str, Any]] | None = None
        self.keys: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        self.keys.append(request.headers.get("X-API-Key", ""))
        if self.mode == "down":
            raise httpx.ConnectError("offline", request=request)
        if self.mode == "500":
            return httpx.Response(503, headers={"Retry-After": "2"})
        if self.mode == "401":
            return httpx.Response(401, json={"error": "bad key"})
        body = json.loads(request.content)
        if request.url.path.endswith("/upsert-batch"):
            assert self.batch_plan is not None
            return httpx.Response(207, json={"results": self.batch_plan})
        if self.mode == "400":
            return httpx.Response(422, json={"error": "schema"})
        key = request.headers["Idempotency-Key"]
        replay = key in self.records
        self.records.setdefault(key, body["data"])
        return httpx.Response(200 if replay else 201, json={"id": f"r-{len(self.records)}"})


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncIterator[SQLiteRepository]:
    repo = SQLiteRepository(tmp_path / "dc.db")
    await repo.initialize()
    yield repo


def collector(repository: SQLiteRepository, receiver: Receiver, **overrides: Any) -> DataCollector:
    return DataCollector(
        settings(**overrides), repository, transport=httpx.MockTransport(receiver.handle)
    )


def article(n: int, agent: str = "news-watcher") -> dict[str, Any]:
    link = f"https://example.com/a/{n}"
    return build_event(
        agent_id=agent,
        entity_type="news_article",
        source_key=key_for("news_article", link),
        payload={"headline": f"H{n}", "canonical_url": link},
        source_url=link,
    )


def test_an_envelope_refuses_credentials_and_bad_identity() -> None:
    with pytest.raises(EventRefused, match="payload.auth.api_key"):
        build_event(
            agent_id="a",
            entity_type="business",
            source_key="b:1",
            payload={"name": "x", "auth": {"api_key": "k"}},
        )
    with pytest.raises(EventRefused, match="lower_snake_case"):
        build_event(agent_id="a", entity_type="Bad Type", source_key="k", payload={"a": 1})
    with pytest.raises(EventRefused, match="source_key"):
        build_event(agent_id="a", entity_type="business", source_key=" ", payload={"a": 1})
    event = article(1)
    assert event["content_hash"] == article(1)["content_hash"]
    assert event["event_id"] != article(1)["event_id"]


def test_canonical_url_drops_tracking_and_fragments() -> None:
    assert canonical_url("https://x.com/a/?utm_source=t&id=3#top") == "https://x.com/a?id=3"
    assert canonical_url("javascript:alert(1)") == ""


async def test_offline_restart_online_loses_nothing_and_duplicates_nothing(
    repository: SQLiteRepository,
) -> None:
    receiver = Receiver()
    receiver.mode = "down"
    first = collector(repository, receiver)
    events = [article(n) for n in range(600)] + [
        build_event(
            agent_id="lead-researcher",
            entity_type="business",
            source_key=f"business:maps:{n}",
            payload={"name": f"B{n}"},
        )
        for n in range(400)
    ]
    for event in events:
        assert await first.record(event) == "queued"
    assert await first.record(article(3)) == "duplicate"
    first.clock = lambda: 0.0
    await first.sync_once()
    assert (await repository.dc_counts())["retry"] == 200  # one round, offline

    # "Reboot": a new collector over the same database, network back.
    receiver.mode = "ok"
    second = collector(repository, receiver)
    second.clock = lambda: 1e12
    for _ in range(10):
        await second.sync_once()
    counts = await repository.dc_counts()
    assert counts == {"acknowledged": 1000}
    assert len(receiver.records) == 1000
    assert set(receiver.keys) == {"k-test"}


async def test_failures_retry_with_backoff_and_then_dead_letter(
    repository: SQLiteRepository,
) -> None:
    receiver = Receiver()
    dc = collector(repository, receiver)
    now = [1000.0]
    dc.clock = lambda: now[0]
    await dc.record(article(1))
    receiver.mode = "500"
    await dc.sync_once()
    row = (await repository.dc_rows(("retry",)))[0]
    assert float(str(row["next_attempt_at"])) >= 1002.0  # Retry-After honoured
    assert (await dc.sync_once()).retried == 0  # not due yet
    for _ in range(3):
        now[0] += 10_000
        await dc.sync_once()
    assert (await repository.dc_counts()) == {"dead_letter": 1}
    assert "gave up" in await dc.dead_text()
    receiver.mode = "ok"
    assert await dc.requeue("all") == 1
    await dc.sync_once()
    assert (await repository.dc_counts()) == {"acknowledged": 1}


async def test_a_rejected_key_keeps_the_events_and_tells_a_human(
    repository: SQLiteRepository,
) -> None:
    receiver = Receiver()
    receiver.mode = "401"
    dc = collector(repository, receiver)
    told: list[str] = []
    dc.announce = told.append
    await dc.record(article(1))
    report = await dc.sync_once()
    assert report.error.startswith("credential rejected")
    assert (await repository.dc_counts()) == {"retry": 1}
    assert told and "API key" in told[0]


async def test_a_schema_rejection_is_a_dead_letter_not_a_retry_storm(
    repository: SQLiteRepository,
) -> None:
    receiver = Receiver()
    receiver.mode = "400"
    dc = collector(repository, receiver)
    await dc.record(article(1))
    await dc.sync_once()
    assert (await repository.dc_counts()) == {"dead_letter": 1}
    assert receiver.requests == 1


async def test_a_partial_batch_acks_each_success_and_retries_only_failures(
    repository: SQLiteRepository,
) -> None:
    receiver = Receiver()
    receiver.batch_plan = [
        {"index": 0, "status": "created", "id": "r1"},
        {"index": 1, "status": "stale"},
        {"index": 2, "status": "error", "code": "rate_limited", "message": "slow down"},
        {"index": 3, "status": "error", "code": "validation_failed", "message": "bad"},
    ]
    dc = collector(repository, receiver, upsert_batch=True)
    for n in range(4):
        await dc.record(article(n))
    report = await dc.sync_once()
    assert (report.acknowledged, report.retried, report.dead) == (2, 1, 1)
    assert receiver.requests == 1


async def test_delivery_rechecks_policy_and_quarantines(repository: SQLiteRepository) -> None:
    receiver = Receiver()
    dc = collector(repository, receiver)
    dc.policy = lambda row: "grant revoked" if row["agent_id"] == "gone" else ""
    await dc.record(article(1, agent="gone"))
    await dc.record(article(2))
    report = await dc.sync_once()
    assert report.quarantined == 1 and report.acknowledged == 1
    assert len(receiver.records) == 1


async def test_an_unmapped_type_waits_and_a_full_outbox_refuses(
    repository: SQLiteRepository,
) -> None:
    dc = collector(repository, Receiver(), collections={}, max_outbox_rows=2)
    await dc.record(article(1))
    await dc.record(article(2))
    with pytest.raises(EventRefused, match="outbox holds 2"):
        await dc.record(article(3))
    assert (await dc.sync_once()).unroutable == 2
    assert (await repository.dc_counts()) == {"pending": 2}


async def test_the_news_cache_is_imported_once_by_canonical_link(
    repository: SQLiteRepository, tmp_path: Path
) -> None:
    cache = tmp_path / "scripts" / ".news_cache.jsonl"
    cache.parent.mkdir()
    lines = [
        {"title": "Gold rises", "link": "https://n.example/gold?utm_source=x", "fetched_at": 1.7e9},
        {"title": "Gold rises", "link": "https://n.example/gold", "fetched_at": 1.7e9},
        {"title": "Oil falls", "link": "https://n.example/oil", "source": "https://n.example/rss"},
    ]
    cache.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\nnot json\n", encoding="utf-8"
    )
    dc = collector(repository, Receiver())
    assert await dc.import_news(tmp_path, "NewsWatcher", dry_run=True) == (2, 0)
    assert await dc.import_news(tmp_path, "NewsWatcher") == (2, 0)
    assert await dc.import_news(tmp_path, "NewsWatcher") == (0, 0)  # remembered
    assert await dc.import_news(tmp_path, "NewsWatcher", replay=True) == (0, 2)
    assert (await repository.dc_counts()) == {"pending": 2}


async def test_tools_follow_the_grant_and_the_agent_cannot_forge_itself(
    repository: SQLiteRepository,
) -> None:
    dc = collector(repository, Receiver())
    plain = AgentDefinition(name="Plain", purpose="p")
    assert build_dc_tools(dc, plain) == []
    writer = AgentDefinition(
        name="Writer", purpose="p", data_collector=DataCollectorGrant(write=["news_article"])
    )
    tools = {tool.name: tool for tool in build_dc_tools(dc, writer)}
    assert set(tools) == {"dc_record_event"}
    context = ToolContext(root=Path("."))
    answer = await tools["dc_record_event"].run(
        context,
        {
            "entity_type": "news_article",
            "source_key": "news_article:x",
            "payload": {"headline": "x", "source_agent_id": "Someone Else"},
        },
    )
    assert "queued" in answer
    row = (await repository.dc_rows(("pending",)))[0]
    assert row["agent_id"] == "Writer"
    with pytest.raises(ToolDenied, match="not 'business'"):
        await tools["dc_record_event"].run(
            context, {"entity_type": "business", "source_key": "b", "payload": {"a": 1}}
        )


async def test_a_newer_database_is_refused_and_an_upgrade_keeps_a_copy(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE schema_version(version INTEGER PRIMARY KEY);"
            "INSERT INTO schema_version VALUES (1);"
            "CREATE TABLE agents(id TEXT PRIMARY KEY, definition TEXT NOT NULL);"
        )
    await SQLiteRepository(path).initialize()
    assert list((tmp_path / "backups").glob("old-pre-v*.db"))
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO schema_version VALUES (99)")
    with pytest.raises(SchemaTooNewError, match="schema version 99"):
        await SQLiteRepository(path).initialize()


async def test_console_dc_commands(tmp_path: Path) -> None:
    config = Settings(
        data_path=tmp_path / "data.db",
        generation_path=tmp_path / "generations",
        workspace_path=tmp_path / "workspace",
    )
    environment = Environment(config, {"ollama": MockProvider()})
    await environment.start()
    try:
        agent = AgentDefinition(name="Scout", purpose="p", model_name="mock-model")
        await environment.register_agent(agent)
        console = ConsoleChannel(environment)
        assert "disabled" in await console.route("/dc status")
        assert "may write business, contact" in await console.route(
            "/dc grant Scout write business,contact"
        )
        assert agent.data_collector is not None
        assert "no Data Collector access" in await console.route("/dc revoke Scout")
        assert agent.data_collector is None
    finally:
        await environment.stop()
