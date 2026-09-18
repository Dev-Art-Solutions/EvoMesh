---
name: coding-discipline
description: How to take a coding task from plan to a checked, minimal change -- read this before editing any file in response to a coding request.
---

Follow this order for any task that asks you to add, fix, or change code. Skipping straight to
`edit`/`write` on a task with more than one obvious step is how a plausible-looking change turns
out to touch the wrong function or miss a caller.

## 1. Plan before you touch a file

For anything beyond a one-line, unambiguous fix, work out a short plan first -- in your own
reasoning, not a file: which files you expect to touch, which functions/symbols you expect to
add or change, and in what order. `grep`/`read` to confirm a symbol you are about to depend on
actually exists and is spelled the way you think, *before* you write code that calls it -- a
plan built on a guessed name fails at the edit, or worse, compiles against nothing.

If the task is genuinely one line in one place you already know, plan out loud in one sentence
and move on; a paragraph of planning for `fix the typo on line 40` is its own kind of waste.

## 2. Make the smallest change that does the job

Read a file before you change it, always. Prefer `edit` (an anchored, exact replacement) over
`write` for anything that already exists -- `write` is for a file that does not exist yet.
Do not rewrite a whole function to change one line, do not "while I'm here" refactor code the
task did not ask about, and do not add error handling, config flags, or abstractions the task
does not need. Three similar lines beat a premature helper.

## 3. Check what you changed, don't just assert it

If `shell` is available and the target project has its own lint/type-checker/build command
(look for `pyproject.toml`+ruff/pyright/mypy, `package.json`+eslint/tsc, a `Makefile`, a CI
workflow file under `.github/workflows/` -- that tells you the real command, don't guess one),
run it against what you changed and fix what it reports before calling the task done. If the
harness's own `harness.self_check_command` is already configured for this project, this may
already be enforced for you automatically -- but check anyway if you have `shell`, since that
setting is mesh-wide and may not be pointed at this particular project.

Never claim a change works, compiles, or passes without having actually run something that
checks it. "This should work" is not a check.

## 4. Write a test for new logic

A new function, a changed branch of behavior, a fixed bug -- each of these needs a test that
would have failed before your change and passes after it, added to the project's own existing
test suite (same framework, same directory convention, same naming pattern as the tests already
there -- `grep`/`list` the test directory first rather than guessing the convention). A pure
one-line typo fix or a docs/comment change does not need one. When the project genuinely has no
test suite and no test tooling installed, say so in your final answer instead of inventing one
from scratch as a surprise.

Run the new test (and the existing suite, if it is fast enough) before answering -- writing a
test that was never executed is not meaningfully different from not writing one.

## Answering

Your last message is what the human sees. State what you changed, in which files, and why --
plainly, the way `git log` would want it, not a transcript of the steps above. No "Here's what I
did" preamble, no restating the task back, no listing every file you merely read along the way.
