---
name: news-impact-analysis
description: How to turn a fetched headline into a price-impact judgment (instrument, direction, confidence) on the recurring analysis goal, and when to actually report one.
---

On the recurring analysis goal, call `news_fetch` for the configured feeds. If a cycle
was missed or you want to be sure nothing slipped past a feed's own "latest" window,
call it instead with `{"from_cache": true, "since_hours": 24}` (or whatever window makes
sense) to see everything actually gathered over that period, not just what a source is
showing right now.

For each headline you have not already assessed in a previous cycle (check your own
memory/context first -- never re-assess or re-report the same headline twice), decide:

1. **Instrument**: which symbol in `config.json`'s `instruments` map this headline
   plausibly relates to, by matching its keywords against the headline text. A headline
   that matches no configured instrument's keywords is not your concern -- skip it
   silently, the same way NewsWatcher skips a headline matching no watched keyword.
2. **Direction**: `bullish`, `bearish`, or `neutral` for that instrument, in plain
   terms a human can sanity-check (e.g. a rate-hike headline is bearish for gold).
3. **Confidence**: `low`, `medium`, or `high` -- how sure you actually are, not how
   sure you'd like to sound. A vague or ambiguous headline is `low`; only call `high`
   when the causal link is direct and well-established (a central bank rate decision,
   a confirmed war/sanctions event, an official inflation print).
4. **Why**: one sentence, plain language, naming the mechanism (e.g. "higher rates make
   non-yielding gold less attractive").

Only report headlines whose confidence is at or above `config.json`'s `min_confidence`
(`low` < `medium` < `high`). Silence is the correct, common outcome for most cycles --
most headlines are irrelevant to every configured instrument, and most relevant ones are
only `low` confidence. Format a reported item as one line:

```
<SYMBOL> <direction> (<confidence>): <headline> -- <why>
```

**Never** suggest a specific trade, position size, entry, or order -- that is a human's
(or Trader's own operator's) decision, not something this agent proposes. This skill is
about judging *whether news is relevant and which way it points*, not about acting on it.
