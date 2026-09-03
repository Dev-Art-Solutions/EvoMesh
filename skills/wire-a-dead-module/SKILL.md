---
name: wire-a-dead-module
description: Wire one unreachable module into code that actually runs, instead of writing a new file.
---

`docs/evolution/known-dead-modules.txt` lists modules nothing imports. Wiring
one of them into a module that runs is real, validated work; a brand new file
is not -- nothing imports it either, so the reachability check fails it the
same way, and the project's own harness rules already say not to write one.

1. Read `docs/evolution/known-dead-modules.txt` and pick exactly one module
   from the list. Do not try to wire in more than one in a single generation.
2. Read that module. Note its public functions or classes -- what it actually
   offers a caller.
3. Find a load-bearing module (one with real callers already) where one of
   those functions would do real work, not just satisfy an import. `grep`
   for a similar existing call to see the pattern the codebase already uses
   nearby.
4. Add one import and one real call at a genuine call site -- not a call
   inside a docstring, a comment, or a branch that never executes. Prefer
   `edit` over `write`; you are changing an existing file, not creating one.
5. Read the file back after editing it to confirm the call is where you
   think it is and the import is not duplicated.
6. Do not remove the module's line from known-dead-modules.txt yourself --
   say what you wired in the summary, and the line goes stale on its own the
   next time someone reads the list. Pruning it is a separate, human step.
7. If nothing in the list has an obvious real caller, say so and stop rather
   than forcing a call that does not do anything real -- a wired-in function
   nobody would ever call for a reason is the same failure with extra steps.
