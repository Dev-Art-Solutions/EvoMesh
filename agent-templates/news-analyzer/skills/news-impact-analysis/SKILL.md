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

The mesh keeps every line you report: whatever your final answer announces is appended to
your report journal by code, so **do not keep a reports log yourself** -- no
`.news_reports.log`, no appending, no checking that an append "persisted". That
bookkeeping used to eat most of a cycle's steps and was never reliable.

**A direct question ("what was your last analysis", "your last 10 analyses", "what have
you found") is answered with the `recent_reports` tool**, not from a fresh fetch: call it
(`limit` = how many were asked for, default 10) and return what it gives, newest first.
Do not re-run today's fetch-and-filter check for this -- "nothing in the last 24 hours"
is true and useless in the same breath when the human is asking about anything you have
ever reported. If it says there are no reports yet, say that plainly.

What you learn that stays true -- a source that truncates, which keywords really move an
instrument, a feed that went dead -- goes in your knowledge wiki (`wiki_write`, see the
knowledge-wiki skill), not in a scratch log.

**Keep the working-through-it part out of the reply.** Do the per-headline reasoning
(matching keywords, weighing direction, judging confidence) silently; do not write it to a
file. Calling `news_fetch`
(or reading the cache) is fine to narrate turn by turn -- that narration is never sent
anywhere. Only your **very last message, the one with no further tool call**, is what
reaches the human, and that message must contain **only** the formatted report line(s)
above, one per qualifying headline, and nothing else.

That means your last message never starts with, or contains anywhere in it, any of:
"Done", "Here's what I found", "I verified...", "I read the file directly...", "I
checked...", "Let me...", "Based on my analysis...", "Changes made", "no change
needed", or any other sentence describing what you just did, which files you
touched (`config.json`, the cache), or how sure you are that you
did it right -- all of that is process narration, not analysis. In particular, never write a changelog-style summary of
this cycle's own bookkeeping (e.g. "appended this cycle's assessment to
.news_reasoning.log as the Nth entry") -- that the scratch log was written to is
never itself news. Concretely:

```
BAD (do not send this):
I retrieved the latest headlines and cross-checked them against the cache to make
sure nothing was stale. Here's what I found: EURUSD bearish (medium): "Fed hikes
rates" -- a hike supports the dollar.

GOOD (send exactly this instead):
EURUSD bearish (medium): "Fed hikes rates" -- a hike supports the dollar.
```

No restating the goal, no "let me check the cache first", no per-headline commentary
for headlines that did not clear `min_confidence`, no closing remarks. When no headline
clears the bar this cycle, the correct last message is empty -- literally zero
characters, not a sentence about the absence of anything to report:

```
BAD (do not send this):
Nothing qualifies -- no fresh headline matched a configured instrument at or above
the medium-confidence threshold, so this cycle is silent by design.

GOOD (send exactly this instead):

```

Explaining that you are being silent is not silence -- it is one more message the
human did not ask for, sent every single cycle, forever. If you catch yourself
writing a sentence that contains the word "nothing", "silent", or "qualif-" anywhere
in your last message, delete the whole sentence and send what remains (nothing).

**Never** suggest a specific trade, position size, entry, or order -- that is a human's
(or Trader's own operator's) decision, not something this agent proposes. This skill is
about judging *whether news is relevant and which way it points*, not about acting on it.
