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

Every line you actually report (not the silent misses, not the reasoning) also gets
appended, verbatim and timestamped, to `scripts/.news_reports.log` beside this SKILL.md
(`write`/`edit` it directly -- create it the first time it does not exist). Unlike the
reasoning scratch file below, this log **is** meant to be read back: it is the only
durable record of what you have ever actually told a human, since an announced report
line itself is not saved anywhere else once it has scrolled past in chat.

**A direct question ("what was your last analysis", "any recent findings", "what have
you found") is answered from that log, not from a fresh fetch.** Read the tail of
`scripts/.news_reports.log` and return the most recent entries as-is. Do not re-run
today's fetch-and-filter check for this -- "nothing in the last 24 hours" is true and
useless in the same breath when the human is asking about anything you have ever
reported, not only what is fresh since your last cycle. If the log does not exist yet
or is empty, say plainly that nothing has been analyzed yet, rather than silently
running the recurring-cycle logic and reporting an empty result as if it answered the
question.

**Keep the working-through-it part out of the reply.** Do the per-headline reasoning
(matching keywords, weighing direction, judging confidence) silently, and if you want a
record of it, `write`/append it to a scratch file such as `scripts/.news_reasoning.log`
beside this SKILL.md -- that file is your own scratchpad, never read by the human and
never pruned by this skill, so trim it yourself if it grows large. Calling `news_fetch`
(or reading the cache) is fine to narrate turn by turn -- that narration is never sent
anywhere. Only your **very last message, the one with no further tool call**, is what
reaches the human, and that message must contain **only** the formatted report line(s)
above, one per qualifying headline, and nothing else.

That means your last message never starts with, or contains anywhere in it, any of:
"Done", "Here's what I found", "I verified...", "I read the file directly...", "I
checked...", "Let me...", "Based on my analysis...", "Changes made", "no change
needed", or any other sentence describing what you just did, which files you
touched (`config.json`, the scratch log, the cache), or how sure you are that you
did it right -- all of that is process narration, not analysis, and belongs in the
scratch file if anywhere. In particular, never write a changelog-style summary of
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
