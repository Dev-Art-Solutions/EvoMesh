---
name: data-collector
description: When and how to save observed records to the Data Collector with dc_record_event, and how to query them with dc_query.
---

The Data Collector is the operator's central store for what agents observe.
You never hold its key: `dc_record_event` queues an event in EvoMesh's local
outbox, which delivers it when the collector is reachable. A record you
queued is safe even if the network is down.

## When to record

- Something you **observed** at a source and would be useful later: an
  article, a business listing, a website check, a review, a finished report.
- Not chat, not your own reasoning, not guesses, and never credentials,
  cookies, tokens or private mail you were not asked to export.

## How to record

```json
{
  "entity_type": "business",
  "source_key": "business:google_maps:ChIJ...",
  "payload": {
    "name": "Example Bakery",
    "city": "Sofia",
    "website": "https://example.bg",
    "rating": 4.6,
    "rating_count": 212,
    "ads_tag": {"state": "not_detected", "method": "page html scan"}
  },
  "source_url": "https://maps.google.com/?cid=...",
  "observed_at": "2026-11-24T08:00:00Z",
  "privacy_class": "business_contact"
}
```

- `source_key` is the record's **stable identity**: the canonical URL, or the
  provider plus its own id. Never a time, never a random id. The same
  `source_key` with the same content is a duplicate and is not stored twice.
- Say how you know: `{"state": "verified_present" | "verified_absent" |
  "not_detected" | "not_checked" | "unavailable", ...}`. "I did not see an
  ad tag" is `not_detected`, never `verified_absent`.
- `privacy_class`: `public` (published content), `business_contact` (public
  business details), `personal`, `restricted`.

## Querying

`dc_query` searches one entity type you may read, with a small filter:

```json
{"entity_type": "news_article", "filter": {"field": "payload.feed", "op": "eq", "value": "https://n.example/rss"}, "limit": 20}
```

For counts and groupings over many records, ask the human to use the Data
Collector's own analytics; do not page through thousands of rows here.

## When something is refused

- `DENIED: you may record ...` -- your grant does not cover that type. Tell
  the human (`/dc grant <you> write <type>`).
- `outbox holds N unsent events` -- the collector has been unreachable for a
  while. Stop recording and tell the human (`/dc status`).
