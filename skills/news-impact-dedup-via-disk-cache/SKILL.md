---
name: news-impact-dedup-via-disk-cache
description: When news_fetch truncates its live view (and from_cache does not help), reading the on-disk _cache/news_cache.json gives the full current headline inventory so you can dedup new headlines against prior assessments before assessing anything.
---

## The problem
`news_fetch` truncates its output at ~5000 chars, so I can't see the full headline list to dedup against things I've already assessed. The `from_cache: true` mode does NOT fix this — it returns an identical truncated view, and a live fetch may swap the headline set.

## The full-inventory workaround
Read the cache JSON directly from disk. It has the *complete* current inventory even when the tool view is truncated:

```python
import json
data = json.load(open('_cache/news_cache.json'))
items = data['items']
for it in items:
    print(it['index'], it['id'], it['text'], it['published_utc'])
```

`news_fetch(from_cache=True)` also dumps the file path in the tool's printed header — it's `~/.agents/agents/news-impact-analysis/workspace/_cache/news_cache.json`.

## Dedup correctly — by text+timestamp, NOT by bare id
The `index` field is a *per-page ordering counter*, NOT a stable ID. It resets for each page/batch, so id `24` on one page is a different headline from id `24` on another. Prior-day items (e.g. the India/Trump "on track to close a deal" story) keep appearing verbatim. **Always dedup headlines by a combination of text + published_utc**, never by the bare id or text alone.

## Workflow
1. `news_fetch(from_cache=True)` — get the full current inventory from the printed file path.
2. Read the on-disk cache JSON directly to get every headline (bypasses the tool truncation).
3. Read `scratch/news_assessments.json` for the prior assessed list (`ids`, `items[]` with `text`/`instrument`/`direction`/`confidence`).
4. Load config.json to know `min_confidence` (0.6) and `instruments`.
5. Dedup current inventory against prior assessments by **text + timestamp** — treat a prior headline as assessed only if the text matches closely enough. Ignore exact duplicates within the current batch.
6. Assess each genuinely-new headline (instrument, direction, confidence) against the 5-question framework; report only items clearing min_confidence.

## Gotchas
- `readcfg.py` prints config only if `config.json` exists. If it doesn't, read `config.json` directly from the working dir.
- `news_cache.json` and `news_assessments.json` live in `_cache/` and `scratch/` respectively — use forward slashes; the shell tool may misreport `tools/` as present when it isn't.
