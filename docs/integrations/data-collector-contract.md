# Data Collector ingestion contract: proposal v1

Status: **proposal for review in PR 00**. Nothing here is implemented in any of the three repos.
The same document (and its fixtures, added in PR 01) is committed to Data-Collector and
EvoMesh-LeadCollector under the same `contract_version`. Producers: EvoMesh (PR 02/03/10) and
LeadCollector (PR 04). Receiver: Data-Collector (PR 01).

## 1. Why the receiver must change first

At `iovigi/Data-Collector@34c5a28`, `POST /collections/{cid}/records/batch` inserts every item
unconditionally (`RecordService.batch_create`). There is also no unique producer key on `records`.
Both producers deliver at-least-once: a lost response, a retry after a 207 or a replay after a
restart sends items DC already holds. Today each of those becomes a duplicate. The single-record
`Idempotency-Key` cache expires after 24 h, which is shorter than a realistic offline window.
**Effectively-once outcome needs identity stored on the record, not a cache.**

## 2. Envelope (one per item)

```json
{
  "contract_version": 1,
  "event_id": "0b6f3c1e-6d3a-4b8e-9a51-2f8d1d5e7c10",
  "event_type": "news.observed",
  "source_system": "evomesh",
  "source_agent_id": "news-watcher",
  "source_job_id": null,
  "entity_type": "news_article",
  "source_key": "news_article:sha256:9f2c...e1",
  "observed_at": "2026-11-24T08:00:00Z",
  "collected_at": "2026-11-24T08:00:01Z",
  "source_url": "https://example.com/markets/gold-rises",
  "source_method": "rss",
  "content_hash": "sha256:4be1...07",
  "privacy_class": "public",
  "payload": {"headline": "Gold rises", "language": "en"},
  "metadata": {"producer_version": "0.10.0", "fetch_status": "success"},
  "artifact_refs": []
}
```

The producer fills these fields, never the model. `source_agent_id` is set by trusted code from
the calling agent, and a tool argument cannot override it. There is no `workspace_id` in the
envelope: the receiver derives it from the collection and the credential.

### Field rules

| Field | Rule |
|---|---|
| `source_key` | **Stable identity of the entity**, not of the observation. Built by the producer from canonical fields, e.g. `news_article:` + sha256(canonical URL); `business:` + provider + place id. It is never derived from time |
| `event_id` | Unique per observation; used for `source_observations` and tracing, **not** for dedupe |
| `observed_at` | When the source showed it. The receiver uses it to refuse stale overwrites |
| `content_hash` | sha256 of the canonical JSON of `payload` (sorted keys, UTF-8). An equal hash makes the item `unchanged` |
| `privacy_class` | `public` \| `business_contact` \| `personal` \| `restricted`. Mail content is always `restricted` |
| Unknown values | `{"state": "not_checked"\|"not_detected"\|"unavailable"\|"verified_present"\|"verified_absent", "value": ...}`. **A missing field is never treated as `verified_absent`** |
| Forbidden anywhere | passwords, tokens, cookies, session dumps, OTP codes. Producers drop them; the receiver rejects payloads matching its secret detectors |

## 3. Receiver semantics (PR 01)

New endpoint, so the existing batch keeps its behaviour for existing clients:

`POST /api/v1/collections/{cid}/records/upsert-batch`, with at most `max_batch_records` items.

Per item, inside a unique index on `(workspace_id, collection_id, source_key)`:

| Outcome | When |
|---|---|
| `created` | no record with that `source_key` |
| `updated` | exists, `observed_at` newer, `content_hash` differs; `version` +1 |
| `unchanged` | `content_hash` equal. Last-seen time is bumped; no version change |
| `stale` | `observed_at` older than the stored one. **Nothing overwritten**; the observation is still appended to `source_observations` if that collection is configured |
| `error` | validation or per-item storage error, with `code` and `message`. **Every exception is caught per item** |

The response is always `207` when any item is not `created`/`updated`/`unchanged`, else `200`.
The body is `{"results": [{"index", "source_key", "status", "id"?, "version"?, "code"?, "message"?}]}`.
A producer acknowledges an item on `created|updated|unchanged|stale` and retries only on `error`
with a retryable `code`.

Error codes: `validation_failed` (dead-letter, no retry), `schema_version_unsupported` (dead-letter),
`forbidden_collection` (dead-letter, and the producer pauses that collection), `rate_limited`
(retry, honour `Retry-After`), `unavailable` (retry).

## 4. Credentials

DC API keys already carry `allowed_collection_ids` and scopes (`auth_service.py`).

- **Ingest principal:** one key per producer system, `records:write` only, limited to the
  collections that system feeds. Not readable by agents.
- **Analytics principal (ChatGPT/Claude MCP):** `records:read` + `collection:read`, read-only.
- EvoMesh never gives an agent a DC credential. Agents call `dc_record_event`/`dc_query`, and the
  mesh's own client applies the agent's grants and then uses the principal.

## 5. Collections (v1)

`news_articles`, `source_observations`, `entities`, `contacts`, `web_pages`,
`agent_runs`, `artifacts`. Mail collections and `marketing_audits` are defined in PR 10 and
Milestone 4. Each collection schema lists its indexable fields explicitly. These are the only
fields aggregation (PR 08) may group or filter on.

## 6. Versioning

`contract_version` is an integer. The receiver accepts `N` and `N-1`. A producer that receives
`schema_version_unsupported` dead-letters the item and stops sending that version; it never
silently downgrades the payload. Fixtures for every `event_type` live in
`contracts/fixtures/v1/*.json` in each repo, and each repo's CI validates them.

## 7. Open questions for review

1. Should `source_observations` (immutable history) be written by the receiver from each upsert,
   or sent as its own event type? Proposal: the receiver writes it, so a producer cannot forget.
2. What retention applies to `source_observations` per collection? Default proposal: 180 days.
3. Tombstones: `event_type: "*.deleted"` with the same `source_key` soft-deletes the record and
   blocks re-creation until restored by a human. Is this acceptable for GDPR erasure, or is a hard
   delete required?
