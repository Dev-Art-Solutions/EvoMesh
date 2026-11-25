"""The operator's Data Collector: a durable outbox, its sync, and the tools.

An event is written to SQLite (``dc_outbox``) before ``record`` returns, and
leaves the outbox only on an acknowledgement or a human's cancel. Delivery is
at-least-once. The receiver makes it effectively-once, either by
``source_key`` (contract v1's ``upsert-batch``) or, until it has that, by the
``Idempotency-Key`` it remembers for 24 hours. See
docs/integrations/data-collector-contract.md.

Agents never hold the credential. ``dc_record_event`` and ``dc_query`` are
built per agent from its ``DataCollectorGrant``; the agent id, timestamps and
hashes are filled in here, never taken from a tool argument. Nothing here
blocks an agent on the network: ``record`` is a local insert, and the sync
runs as its own task.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from glob import glob
from pathlib import Path
from typing import Any

import httpx

from evomesh.config import DataCollectorSettings
from evomesh.contracts import AgentDefinition, DataCollectorGrant
from evomesh.harness_tools import Tool, ToolDenied
from evomesh.storage import SQLiteRepository

logger = logging.getLogger(__name__)

CONTRACT_VERSION = 1
PRIVACY_CLASSES = ("public", "business_contact", "personal", "restricted")
# Keys that never leave the machine, at any depth of a payload or metadata.
SECRET_KEY = re.compile(
    r"pass(word|wd)?$|secret|token|cookie|authori[sz]ation|api[_-]?key|otp|session[_-]?id",
    re.IGNORECASE,
)
ENTITY_TYPE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
MAX_PAYLOAD_BYTES = 256 * 1024
OPEN_STATES = ("pending", "retry")
ACK_OUTCOMES = {"created", "updated", "unchanged", "stale"}
RETRYABLE_CODES = {"rate_limited", "unavailable"}


class EventRefused(Exception):
    """An event that was never queued, with the reason a caller can act on."""


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(payload: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def key_for(entity_type: str, *parts: str) -> str:
    """A stable source_key from canonical identity fields, never from time."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{entity_type}:sha256:{digest}"


def secret_paths(value: object, path: str = "") -> list[str]:
    if isinstance(value, dict):
        found: list[str] = []
        for key, inner in value.items():
            where = f"{path}.{key}" if path else str(key)
            if SECRET_KEY.search(str(key)):
                found.append(where)
            found.extend(secret_paths(inner, where))
        return found
    if isinstance(value, list):
        return [
            hit
            for index, inner in enumerate(value)
            for hit in secret_paths(inner, f"{path}[{index}]")
        ]
    return []


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_event(
    *,
    agent_id: str,
    entity_type: str,
    source_key: str,
    payload: dict[str, Any],
    event_type: str = "",
    source_system: str = "evomesh",
    source_job_id: str | None = None,
    observed_at: str | None = None,
    source_url: str = "",
    source_method: str = "",
    privacy_class: str = "public",
    metadata: dict[str, Any] | None = None,
    artifact_refs: list[str] | None = None,
) -> dict[str, Any]:
    """One contract-v1 envelope, or EventRefused saying why not."""
    if not ENTITY_TYPE.match(entity_type):
        raise EventRefused(f"entity_type '{entity_type}' must be lower_snake_case, 2-64 chars")
    source_key = source_key.strip()
    if not source_key or len(source_key) > 512:
        raise EventRefused("source_key must be 1-512 characters (a stable identity, not a time)")
    if privacy_class not in PRIVACY_CLASSES:
        raise EventRefused(f"privacy_class must be one of {', '.join(PRIVACY_CLASSES)}")
    if not isinstance(payload, dict) or not payload:
        raise EventRefused("payload must be a non-empty object")
    metadata = dict(metadata or {})
    leaked = secret_paths(payload, "payload") + secret_paths(metadata, "metadata")
    if leaked:
        raise EventRefused(
            "refused: these fields look like credentials and never leave this machine: "
            + ", ".join(leaked[:5])
        )
    if len(canonical_json(payload).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise EventRefused(
            f"payload is over {MAX_PAYLOAD_BYTES // 1024} KB; store a file as an artifact"
        )
    now = _now_iso()
    return {
        "contract_version": CONTRACT_VERSION,
        "event_id": str(uuid.uuid4()),
        "event_type": event_type or f"{entity_type}.observed",
        "source_system": source_system,
        "source_agent_id": agent_id,
        "source_job_id": source_job_id,
        "entity_type": entity_type,
        "source_key": source_key,
        "observed_at": observed_at or now,
        "collected_at": now,
        "source_url": source_url,
        "source_method": source_method,
        "content_hash": content_hash(payload),
        "privacy_class": privacy_class,
        "payload": payload,
        "metadata": metadata,
        "artifact_refs": list(artifact_refs or []),
    }


ENVELOPE_FIELD = "dc_event"


def flat_record(envelope: dict[str, Any]) -> dict[str, Any]:
    """The record a plain create stores: the payload's fields at the top,
    where the collector's dashboard and filters look, and the rest of the
    envelope under one key. upsert-batch still sends the envelope itself."""
    rest = {key: value for key, value in envelope.items() if key != "payload"}
    return {**envelope.get("payload", {}), ENVELOPE_FIELD: rest}


def flat_filter(value: Any) -> Any:
    """A filter written against the envelope ("payload.feed") addresses the
    flat record ("feed"); any other envelope field lives under dc_event."""
    if isinstance(value, list):
        return [flat_filter(inner) for inner in value]
    if not isinstance(value, dict):
        return value
    out = {key: flat_filter(inner) for key, inner in value.items()}
    field_name = out.get("field")
    if isinstance(field_name, str):
        if field_name.startswith("payload."):
            out["field"] = field_name.removeprefix("payload.")
        elif field_name.split(".")[0] in ENVELOPE_KEYS:
            out["field"] = f"{ENVELOPE_FIELD}.{field_name}"
    return out


ENVELOPE_KEYS = frozenset(
    {
        "contract_version", "event_id", "event_type", "source_system", "source_agent_id",
        "source_job_id", "entity_type", "source_key", "observed_at", "collected_at",
        "source_url", "source_method", "content_hash", "privacy_class", "metadata",
        "artifact_refs",
    }
)


@dataclass
class SyncReport:
    acknowledged: int = 0
    retried: int = 0
    dead: int = 0
    unroutable: int = 0
    quarantined: int = 0
    error: str = ""

    def line(self) -> str:
        text = (
            f"acknowledged {self.acknowledged}, retry {self.retried}, "
            f"dead-letter {self.dead}, no collection {self.unroutable}, "
            f"quarantined {self.quarantined}"
        )
        return f"{text} ({self.error})" if self.error else text


@dataclass
class DataCollector:
    """Mesh-wide, owned by Environment.data_collector."""

    settings: DataCollectorSettings
    repository: SQLiteRepository
    transport: httpx.AsyncBaseTransport | None = None
    clock: Callable[[], float] = time.time
    paused: bool = False
    last_sync_at: str = ""
    last_error: str = ""
    # Set by the environment: a dead letter or a rejected credential is
    # something a human has to hear about, not find in a log.
    announce: Callable[[str], None] | None = None
    # Re-checked at delivery: a non-empty reason quarantines the row (a
    # revoked grant, a mail account opted out) instead of sending it.
    policy: Callable[[dict[str, object]], str] | None = None
    # Deterministic producers run before each sync round (news cache import).
    producers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task[None] | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Links this process already offered to the outbox, so the news import
    # that runs every round does not re-insert a 5000-line cache each time.
    _imported: set[str] = field(default_factory=set)

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(s.enabled and s.api_base_url and s.api_key)

    # -- recording ----------------------------------------------------------

    async def record(self, event: dict[str, Any]) -> str:
        """Queue one envelope. 'queued', or 'duplicate' for an observation the
        outbox already holds. Raises EventRefused when the outbox is full."""
        counts = await self.repository.dc_counts()
        open_rows = sum(counts.get(state, 0) for state in OPEN_STATES)
        if open_rows >= self.settings.max_outbox_rows:
            raise EventRefused(
                f"the Data Collector outbox holds {open_rows} unsent events "
                f"(max_outbox_rows {self.settings.max_outbox_rows}); nothing new is "
                "accepted until it drains -- see /dc status"
            )
        stored = await self.repository.dc_enqueue(
            {
                "event_id": event["event_id"],
                "source_key": event["source_key"],
                "content_hash": event["content_hash"],
                "entity_type": event["entity_type"],
                "agent_id": event["source_agent_id"],
                "payload": canonical_json(event),
                "state": "pending",
                "created_at": event["collected_at"],
            }
        )
        if stored:
            self._wake.set()
        return "queued" if stored else "duplicate"

    # -- the sync lane --------------------------------------------------------

    def start(self) -> None:
        if self._task is None and self.settings.enabled:
            self._task = asyncio.create_task(self._run(), name="data-collector-sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.settings.sync_interval_seconds)
            self._wake.clear()
            for produce in self.producers:
                try:
                    await produce()
                except Exception:  # noqa: BLE001 - a producer fault never stops the sync
                    logger.exception("data collector: a producer failed")
            if self.paused or not self.configured:
                continue
            try:
                await self.sync_once()
            except Exception:  # noqa: BLE001 - the lane outlives any one bad round
                logger.exception("data collector: sync round failed")

    def wake(self) -> None:
        self._wake.set()

    async def sync_once(self) -> SyncReport:
        report = SyncReport()
        if not self.configured:
            report.error = "not configured (data_collector.enabled, api_base_url, credential_ref)"
            return report
        async with self._lock:
            rows = await self.repository.dc_rows(
                OPEN_STATES, due_before=self.clock(), limit=self.settings.max_batch_records * 4
            )
            by_collection: dict[str, list[dict[str, object]]] = {}
            for row in rows:
                reason = self.policy(row) if self.policy is not None else ""
                if reason:
                    report.quarantined += 1
                    await self.repository.dc_update(
                        str(row["event_id"]), state="quarantined", last_error=reason[:500]
                    )
                    continue
                collection = self.settings.collections.get(str(row["entity_type"]), "")
                if not collection:
                    report.unroutable += 1
                    await self.repository.dc_update(
                        str(row["event_id"]),
                        last_error=f"no collection for entity_type '{row['entity_type']}' "
                        "in data_collector.collections",
                        next_attempt_at=self.clock() + self.settings.sync_interval_seconds,
                    )
                    continue
                by_collection.setdefault(collection, []).append(row)
            async with self._client() as client:
                for collection, batch in by_collection.items():
                    for start in range(0, len(batch), self.settings.max_batch_records):
                        chunk = batch[start : start + self.settings.max_batch_records]
                        if self.settings.upsert_batch:
                            await self._send_batch(client, collection, chunk, report)
                        else:
                            for row in chunk:
                                await self._send_one(client, collection, row, report)
                        if report.error.startswith("credential"):
                            break
        self.last_sync_at = _now_iso()
        self.last_error = report.error
        return report

    def _client(self) -> httpx.AsyncClient:
        s = self.settings
        return httpx.AsyncClient(
            base_url=s.api_base_url.rstrip("/") + "/",
            headers={"X-API-Key": s.api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(s.request_timeout_seconds, connect=s.connect_timeout_seconds),
            verify=s.verify_tls,
            transport=self.transport,
            follow_redirects=False,
        )

    async def _send_one(
        self,
        client: httpx.AsyncClient,
        collection: str,
        row: dict[str, object],
        report: SyncReport,
    ) -> None:
        envelope = json.loads(str(row["payload"]))
        try:
            response = await client.post(
                f"collections/{collection}/records",
                json={"data": flat_record(envelope)},
                headers={"Idempotency-Key": str(row["event_id"])},
            )
        except httpx.HTTPError as exc:
            await self._retry(row, f"network: {exc.__class__.__name__}: {exc}", None, report)
            return
        await self._settle(row, response, report)

    async def _send_batch(
        self,
        client: httpx.AsyncClient,
        collection: str,
        rows: list[dict[str, object]],
        report: SyncReport,
    ) -> None:
        items = [json.loads(str(row["payload"])) for row in rows]
        try:
            response = await client.post(
                f"collections/{collection}/records/upsert-batch", json={"items": items}
            )
        except httpx.HTTPError as exc:
            for row in rows:
                await self._retry(row, f"network: {exc.__class__.__name__}: {exc}", None, report)
            return
        if response.status_code not in (200, 207):
            for row in rows:
                await self._settle(row, response, report)
            return
        try:
            results = {int(item["index"]): item for item in response.json().get("results", [])}
        except (ValueError, KeyError, TypeError, AttributeError):
            for row in rows:
                await self._retry(
                    row, "receiver answered a batch with no per-item results", None, report
                )
            return
        for index, row in enumerate(rows):
            item = results.get(index)
            if item is None:
                await self._retry(row, "no result for this item in the batch answer", None, report)
            elif item.get("status") in ACK_OUTCOMES:
                await self._ack(row, f"{item.get('status')}:{item.get('id', '')}", report)
            elif item.get("code") in RETRYABLE_CODES:
                await self._retry(
                    row, f"{item.get('code')}: {item.get('message', '')}", None, report
                )
            else:
                await self._dead(
                    row, f"{item.get('code', 'error')}: {item.get('message', '')}", report
                )

    async def _settle(
        self, row: dict[str, object], response: httpx.Response, report: SyncReport
    ) -> None:
        status = response.status_code
        if status in (200, 201):
            try:
                receipt = str(response.json().get("id", ""))
            except ValueError:
                receipt = ""
            await self._ack(row, f"{status}:{receipt}", report)
        elif status in (401, 403):
            # Not the event's fault: keep it, stop this round, tell a human.
            report.error = f"credential rejected ({status}); events kept, sync retries later"
            await self._retry(row, report.error, None, report)
            if self.last_error != report.error and self.announce is not None:
                self.announce(
                    f"Data Collector refused the API key ({status}). Nothing is lost; "
                    "fix data_collector.credential_ref and /dc sync."
                )
        elif status == 429 or status >= 500:
            await self._retry(row, f"HTTP {status}", response.headers.get("Retry-After"), report)
        else:
            await self._dead(row, f"HTTP {status}: {response.text[:300]}", report)

    async def _ack(self, row: dict[str, object], receipt: str, report: SyncReport) -> None:
        await self.repository.dc_update(
            str(row["event_id"]),
            state="acknowledged",
            acked_at=_now_iso(),
            receipt=receipt,
            last_error="",
        )
        report.acknowledged += 1

    async def _retry(
        self, row: dict[str, object], error: str, retry_after: str | None, report: SyncReport
    ) -> None:
        attempts = int(row["attempts"]) + 1  # type: ignore[arg-type]
        if attempts >= self.settings.max_attempts:
            await self._dead(row, f"gave up after {attempts} attempts: {error}", report)
            return
        delay = min(
            self.settings.max_backoff_seconds,
            self.settings.min_backoff_seconds * 2 ** (attempts - 1),
        ) * random.uniform(0.5, 1.0)  # noqa: S311 - jitter, not security
        if retry_after and retry_after.strip().isdigit():
            delay = max(delay, float(retry_after))
        await self.repository.dc_update(
            str(row["event_id"]),
            state="retry",
            attempts=attempts,
            next_attempt_at=self.clock() + delay,
            last_error=error[:500],
        )
        report.retried += 1

    async def _dead(self, row: dict[str, object], error: str, report: SyncReport) -> None:
        await self.repository.dc_update(
            str(row["event_id"]), state="dead_letter", last_error=error[:500]
        )
        report.dead += 1
        if self.announce is not None:
            self.announce(
                f"Data Collector dead letter {str(row['event_id'])[:8]} "
                f"({row['entity_type']}): {error[:200]}. /dc dead to see, /dc retry to resend."
            )

    # -- operator commands --------------------------------------------------

    async def status_text(self) -> str:
        s = self.settings
        counts = await self.repository.dc_counts()
        shown = ", ".join(f"{state} {count}" for state, count in sorted(counts.items())) or "empty"
        state = (
            "disabled (data_collector.enabled: false)"
            if not s.enabled
            else "not configured (api_base_url / credential_ref missing)"
            if not self.configured
            else "paused"
            if self.paused
            else "running"
        )
        lines = [
            f"Data Collector: {state}",
            f"  target: {s.api_base_url or '-'}  workspace {s.workspace_id or '-'}  "
            f"project {s.project_id or '-'}",
            "  delivery: "
            + (
                "upsert-batch by source_key"
                if s.upsert_batch
                else "one create per event with Idempotency-Key (24 h)"
            ),
            f"  outbox: {shown}",
            "  collections: "
            + (", ".join(f"{k}->{v}" for k, v in sorted(s.collections.items())) or "none mapped"),
            f"  last sync: {self.last_sync_at or 'never'}"
            + (f"  last error: {self.last_error}" if self.last_error else ""),
        ]
        return "\n".join(lines)

    async def dead_text(self, limit: int = 10) -> str:
        rows = await self.repository.dc_rows(("dead_letter",), limit=limit)
        if not rows:
            return "No dead letters."
        return "\n".join(
            f"{str(row['event_id'])[:8]}  {row['entity_type']}  {row['agent_id']}  "
            f"attempts {row['attempts']}  {row['last_error']}"
            for row in rows
        )

    async def requeue(self, prefix: str) -> int:
        rows = await self.repository.dc_rows(("dead_letter",), limit=10_000)
        chosen = [row for row in rows if prefix == "all" or str(row["event_id"]).startswith(prefix)]
        for row in chosen:
            await self.repository.dc_update(
                str(row["event_id"]), state="retry", attempts=0, next_attempt_at=0, last_error=""
            )
        if chosen:
            self._wake.set()
        return len(chosen)

    async def cancel(self, prefix: str) -> int:
        rows = await self.repository.dc_rows(OPEN_STATES + ("dead_letter",), limit=10_000)
        chosen = [row for row in rows if str(row["event_id"]).startswith(prefix)] if prefix else []
        for row in chosen:
            await self.repository.dc_update(str(row["event_id"]), state="cancelled")
        return len(chosen)

    # -- reading --------------------------------------------------------------

    async def query(
        self, entity_type: str, filter_: dict[str, Any] | None, limit: int
    ) -> dict[str, Any]:
        if not self.configured:
            raise EventRefused("the Data Collector is not configured")
        collection = self.settings.collections.get(entity_type)
        if not collection:
            raise EventRefused(f"no collection mapped for '{entity_type}'")
        body: dict[str, Any] = {"limit": max(1, min(50, limit))}
        if filter_:
            body["filter"] = flat_filter(filter_)
        async with self._client() as client:
            try:
                response = await client.post(f"collections/{collection}/records/search", json=body)
            except httpx.HTTPError as exc:
                raise EventRefused(f"Data Collector unreachable: {exc.__class__.__name__}") from exc
        if response.status_code != 200:
            raise EventRefused(
                f"Data Collector answered HTTP {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    # -- producers ------------------------------------------------------------

    async def import_news(
        self,
        root: Path,
        agent_id: str,
        *,
        dry_run: bool = False,
        globs: Iterable[str] = (),
        replay: bool = False,
    ) -> tuple[int, int]:
        """Every cached article as a news_article event: (new, already held).
        Keyed by the canonical link, so a replay of the same cache, or the
        next poll seeing the same article, is a duplicate and not a record."""
        patterns = list(globs) or list(NEWS_CACHE_GLOBS)
        new = held = 0
        seen: set[str] = set()
        for pattern in patterns:
            for path in sorted(glob(str(root / pattern))):
                for entry in _read_jsonl(Path(path)):
                    link = canonical_url(str(entry.get("link") or ""))
                    title = str(entry.get("title") or "").strip()
                    if not link or not title or link in seen:
                        continue
                    seen.add(link)
                    if link in self._imported and not (dry_run or replay):
                        continue
                    event = news_event(agent_id, entry, link, title)
                    if dry_run:
                        new += 1
                        continue
                    outcome = await self.record(event)
                    self._imported.add(link)
                    if outcome == "queued":
                        new += 1
                    else:
                        held += 1
        return new, held


NEWS_CACHE_GLOBS = (
    "scripts/.news_cache.jsonl",
    "workspace/agents/*/playground/scripts/.news_cache.jsonl",
    "agent-templates/*/scripts/.news_cache.jsonl",
)


def canonical_url(url: str) -> str:
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        return ""
    url = url.split("#", 1)[0]
    base, _, query = url.partition("?")
    kept = [
        part
        for part in query.split("&")
        if part and not part.lower().startswith(("utm_", "fbclid", "gclid"))
    ]
    return base.rstrip("/") + ("?" + "&".join(kept) if kept else "")


def news_event(agent_id: str, entry: dict[str, Any], link: str, title: str) -> dict[str, Any]:
    fetched = entry.get("fetched_at")
    first_seen = (
        datetime.fromtimestamp(float(fetched), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(fetched, int | float)
        else None
    )
    return build_event(
        agent_id=agent_id,
        entity_type="news_article",
        source_key=key_for("news_article", link),
        payload={
            "headline": title,
            "canonical_url": link,
            "published": str(entry.get("published") or ""),
            "feed": str(entry.get("source") or ""),
            "first_seen_at": first_seen or "",
        },
        observed_at=first_seen,
        source_url=link,
        source_method="rss" if entry.get("source") else "page",
        metadata={"producer": "news_fetch cache", "fetch_status": "success"},
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines:
        with contextlib.suppress(ValueError):
            value = json.loads(line)
            if isinstance(value, dict):
                entries.append(value)
    return entries


def build_dc_tools(collector: DataCollector, definition: AgentDefinition) -> list[Tool]:
    """dc_record_event / dc_query for what the agent's grant allows; none
    when it has no grant or the mesh has no Data Collector enabled."""
    grant: DataCollectorGrant | None = definition.data_collector
    if grant is None or not collector.settings.enabled:
        return []
    tools: list[Tool] = []
    agent = definition.name

    if grant.write:

        async def record(context: Any, args: dict[str, Any]) -> str:
            del context
            entity_type = str(args.get("entity_type") or "")
            if not grant.allows("write", entity_type):
                raise ToolDenied(
                    f"DENIED: you may record {', '.join(grant.write)}, not '{entity_type}'"
                )
            payload = args.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError as exc:
                    raise ToolDenied("DENIED: payload must be a JSON object") from exc
            try:
                event = build_event(
                    agent_id=agent,
                    entity_type=entity_type,
                    source_key=str(args.get("source_key") or ""),
                    payload=payload if isinstance(payload, dict) else {},
                    event_type=str(args.get("event_type") or ""),
                    source_url=str(args.get("source_url") or ""),
                    source_method=str(args.get("source_method") or ""),
                    observed_at=str(args.get("observed_at") or "") or None,
                    privacy_class=str(args.get("privacy_class") or "public"),
                )
                outcome = await collector.record(event)
            except EventRefused as exc:
                raise ToolDenied(f"DENIED: {exc}") from exc
            if outcome == "duplicate":
                return f"already recorded (same {entity_type}, same content); nothing new queued"
            return f"queued {entity_type} {event['event_id'][:8]} for the Data Collector"

        tools.append(
            Tool(
                name="dc_record_event",
                description=(
                    "Save one observed record to the Data Collector. source_key is the record's "
                    "stable identity (e.g. its canonical URL), never a time. Never include "
                    "passwords, tokens or cookies."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "entity_type": {"type": "string", "enum": sorted(grant.write)}
                        if "*" not in grant.write
                        else {"type": "string"},
                        "source_key": {"type": "string"},
                        "payload": {"type": "object"},
                        "source_url": {"type": "string"},
                        "observed_at": {"type": "string", "description": "ISO-8601 UTC"},
                        "privacy_class": {"type": "string", "enum": list(PRIVACY_CLASSES)},
                    },
                    "required": ["entity_type", "source_key", "payload"],
                },
                run=record,
            )
        )

    if grant.read:

        async def query(context: Any, args: dict[str, Any]) -> str:
            del context
            entity_type = str(args.get("entity_type") or "")
            if not grant.allows("read", entity_type):
                raise ToolDenied(
                    f"DENIED: you may query {', '.join(grant.read)}, not '{entity_type}'"
                )
            raw_filter = args.get("filter")
            if isinstance(raw_filter, str) and raw_filter.strip():
                try:
                    raw_filter = json.loads(raw_filter)
                except ValueError as exc:
                    raise ToolDenied("DENIED: filter must be a JSON object") from exc
            try:
                answer = await collector.query(
                    entity_type,
                    raw_filter if isinstance(raw_filter, dict) else None,
                    int(args.get("limit") or 10),
                )
            except EventRefused as exc:
                raise ToolDenied(f"DENIED: {exc}") from exc
            return canonical_json(answer)[:6000]

        tools.append(
            Tool(
                name="dc_query",
                description=(
                    "Search the Data Collector's records of one entity type. filter uses the "
                    'collector\'s DSL, e.g. {"field": "payload.feed", "op": "eq", "value": "..."}.'
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "entity_type": {"type": "string"},
                        "filter": {"type": "object"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["entity_type"],
                },
                run=query,
            )
        )
    return tools
