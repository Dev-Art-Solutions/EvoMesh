# Architecture closure report

**Status: `ARCHITECTURE_ACCEPTED_AND_LOCALLY_VALIDATED`**, re-established at
`add6da7` after four correction passes (below). The core architecture is frozen;
see "Freeze".

| | |
|---|---|
| Plan | `plans/EVOMESH_ARCHITECTURE_CLOSURE_PLAN_V2.md` |
| Baseline measured at | `815cd6c` (generation 1652): 907 passed, 8 skipped, ruff clean, pyright 0 |
| Implementation parent | `6bac351` (generation 1653, landed while the mesh still ran) |
| Tested commit | `cc74aef` (earlier: `2da3a5c`, withdrawn by the `9739188` review; `680ab91`, withdrawn by the `899778e` review; `0736eeb`, withdrawn by the `5b88f57` review; `cc74aef`, withdrawn by the `9e28d6a` review) |
| Manifest | `closure-evidence/acceptance.json` (generated at the tested commit by `python -m benchmarks.closure.acceptance`) |
| CI | green at `0736eeb`, and at `147f571` before it: ruff, pyright 0 errors including tests, pytest 1064 passed and 8 skipped on ubuntu-latest; desktop build and self-tests on windows-latest. Check the later commits' runs before relying on them |

## Correction pass after the `9739188` review

A review of `9739188` (`plans/EVOMESH_CLOSURE_AUDIT_9739188.md`) found that
the first acceptance claimed more than its tests showed. Each finding below
was confirmed in the source, got regressions that fail on the old code (24
new tests, all red at `9739188`), and was fixed inside the existing
boundaries. No schema changed. The additions are the WorkItem's `resources`,
the execution's `cancel_requested`/`cancel_reason`, and an output contract's
`required_evidence`; all have defaults.

| Finding | What was wrong | Now | Tests |
|---|---|---|---|
| R01 | A delegated child ran on its own procedure's budget with a fresh deadline. A zero-call allocation still reached the model. A replayed `DELEGATE` after completion ran the work again. Schema-valid output satisfied any success contract. | The child is admitted from the committed WorkItem and runs under its model calls, deadline (never later than the parent's) and attempts. Replacements inherit the spend and the deadline. The recipient keeps a durable ledger of accepted work. `required_evidence` makes a contract ask for the child's own validators. | `test_delegation_contract.py` R01-A to R01-E |
| R02 | Requester and recipient each checked a relative path under their own root, so the checks could name two different files. Only top-level, path-named fields counted. | A resource is resolved once, in the requester's scope, to one absolute file the recipient must reach under its own root and grant. Declared and nested fields count. The child's adapters may touch only the task's resources, and the requester's authority is rechecked before each effect. | R02-A to R02-D |
| R03 | Reconciling an operation in flight at cancellation set the execution back to `RUNNING`, and the next step ran. | Cancellation is a durable intent. The receipt is kept, an unapplied operation is not retried, an unknown one waits for an operator with the intent intact, and nothing after the cancelled step runs. | `test_procedure_cancel.py` R03-A to R03-D |
| R04 | The runtime log verified a fixed fault after three readings of unrelated activity. | The log speaks only for the faults it can still see. A fixed runtime fault stays `VERIFYING` until a target-specific probe or an operator confirms it. | `test_w3_improvement.py` R04-A to R04-C |
| E01 | W3 used a scripted harness and an inline executor for the executor seam, so the tests did not show the live identity or scope. | `test_w3_live.py` runs W3 through the real Environment: two code-capable agents bid, the pipeline owner is selected, and the real harness queue runs its jobs under its identity. The owner holds a grant on the candidate only, the review is read-only, and the post-change probe verifies. It also shows that code work goes only to the pipeline owner, never to another agent (known limitation 18). | `test_w3_live.py` |

Regenerating this evidence surfaced two more problems, both fixed:

- **Protected-surface check (`b141b0f`).** A candidate that was not its own
  repository was judged by the checkout around it. Git walks up (rule 11),
  so any dirty protected file there failed an unrelated validation.
- **Test timing (`680ab91`).** The weakened-oracle W3 test gave its
  background validation 40 cycles, and on a loaded machine that was not
  enough.

The local-model W2 evidence (`local-model-w2.json`) is carried over from
`2da3a5c` and was not re-run. The corrections do not touch W2's clean path.
They touch delegated budgets and deadlines, cancellation, and verification.

## Second correction pass after the `899778e` review

`plans/EVOMESH_CLOSURE_REVIEW_899778e.md` found two gaps in delegated work and
one overstated claim. As before, each fix was confirmed with regressions that
fail on the old code.

| Finding | What was wrong | Now | Tests |
|---|---|---|---|
| R01-F | Only a zero-call allocation was stopped from falling through. With a positive allocation, a child with no matching typed procedure (or with typed execution disabled) ran on legacy planning and model-backed steps. None of the WorkItem's limits applied there. | Bounded work (an allocation, a deadline or resources) runs only typed. Otherwise the goal fails `unsupported_execution`. Unbounded legacy delegation is unchanged. | `test_delegation_contract.py` R01-F1 to F5 |
| R01-G | The duplicate ledger entry was committed before the goal. After a crash in between, the replay was refused and the work was lost. | `accept_work` writes the entry and the agent holding the goal in one transaction. A failure leaves neither and answers `REJECT`. | R01-G1 (a real SQLite fault and a restart from the database), G2/G3, G5 |
| E01 | The live W3 trace proves the owner-only executor, not a separately selected coding agent. | The manifest records this as a scope decision (`SCOPE_DECISIONS`). Unaccepted, it is blocking. `operator:iliya` accepted it on 2026-09-26 as a deliberate reduction, and the requirement stays **partial**. | `test_acceptance_manifest.py` |

## Third correction pass after the `5b88f57` review

`plans/EVOMESH_TARGETED_REVIEW_5b88f57.md` found **R01-H**. The recipient put a
delegated goal into its live mind before the acceptance transaction decided.
A cycle scheduled in that window could start the goal even if it was a replayed
duplicate or its acceptance then failed, and any other save of the agent could
persist it outside the transaction. The fix (`cc74aef`):

- The goal is built on a scratch copy of the mind.
- Admission holds the agent's own lock.
- The agent row is rendered inside the transaction.
- The goal joins the live mind only once committed.
- A cancelled admission installs exactly what the transaction committed.
- An acceptance whose goal was lost while the parent still waits is recovered
  once.

Five regressions in `tests/test_delegation_contract.py` hold the transaction at
a controlled point and schedule a real cycle inside it: H1 to H4, plus the lost
acceptance. All five fail on `0736eeb`.

## Fourth correction pass after the `9e28d6a` review

`plans/EVOMESH_RECOVERY_REVIEW_9e28d6a.md` found **R01-I**. When a lost
acceptance was recovered, the recipient built a goal with a new id. Its
occurrence matched no execution, so the work would run again with a fresh
budget, and a write that had already landed would happen twice.

Recovery now reads the goal id the acceptance recorded, and that goal's
durable execution (`add6da7`):

- never started: the same goal is rebuilt;
- open: it resumes from its cursor, with its receipts and spent budget, and an
  unresolved effect keeps waiting for an operator;
- completed or failed: its ending is replayed to the parent.

A waiting parent alone never authorizes a new run. The five regressions (I1 to
I5) fail on `cc74aef`.

The same commit bounds shutdown. A live `/restart` hung for fourteen minutes.
Each step is now logged and bounded, running children are killed, and a
watchdog exits with the launcher's code.

The work went directly onto `main`, on the owner's instruction, with the live
mesh stopped so the Evolver could not commit meanwhile. No pre-existing user
changes were in the tree. Eleven commits, `49562af` to `2da3a5c`, changed 53
files: 33 added, 20 modified.

## Core gates

| Gate | Result | Evidence |
|---|---|---|
| CG1 Contracts and authority | passed | T01–T09 |
| CG2 Useful deterministic/mixed execution | passed | T10–T28; B1, B2, B4, B5; both shipped templates |
| CG3 Recovery and bounded effects | passed | T26–T28, T35–T43; B7, B8 |
| CG4 Real multi-agent cooperation | passed | T31, T44–T50; B6 |
| CG5 Trustworthy restricted learning | passed | T51–T55; B3, B9 |
| CG6 Useful self-improvement, not busywork | passed | T56–T60; B10, B11; the executor seam |
| CG7 Migration and operation | passed | T17–T20, T39–T43; B12; `/typed` controls; emergency disable |
| CG8 Reproducible evidence and quality | passed | final ruff, pyright and pytest logs at the tested commit, clean tree |

Every matrix row maps to named tests in `benchmarks/closure/acceptance.py`.
`tests/test_acceptance_manifest.py` checks that each mapped test exists and
that an accepted manifest cannot be built from missing, failing or foreign
evidence.

## Quality commands at the tested commit

| Command | Result |
|---|---|
| `ruff check .` | All checks passed |
| `pyright` | 0 errors other than this workspace venv's unresolved `pytest` import; CI runs pyright without that gap and is green |
| `python -m pytest -q` | 1116 passed (Windows 11, this workspace; the Linux CI count differs by the platform-skipped tests) |

Logs: `closure-evidence/quality-gate-logs/final-*.txt`, with JUnit XML beside
them.

## Workflows and benchmarks

| Case | Mode | Measured |
|---|---|---|
| W1 / B1 local JSON snapshot | runtime integration, real adapters | typed_authored, goal done, **0 provider calls**, 2 tool attempts, `artifact_matches_source` passed |
| W2 / B2 report comparison | runtime integration, scripted provider | **1 provider call**, 0 planning or routing calls, 733-char prompt holding only the declared inputs |
| B3 learned procedure | runtime integration, scripted legacy model | legacy 3/3/3 calls per occurrence; after candidate → held-out replay → operator approval, the typed run used **0 calls** (ratio 1.0 for these runs only) |
| W2 on a local model | local-model experiment | ornith-1.5:35b Q4_K_M, Ollama 0.34.2, 4k and 32k context (server-reported): **18/18 completed on the first call**, all citations in the inputs |
| W3 / B10 fixture defect | real adapter, real acceptance check | red on the live tree → routed candidate → green on the candidate → review bound to the same revision → promoted → one post-change suite observation → VERIFIED; next cycle idle with no model call |
| W3 live (E01) | runtime integration, scripted provider | the real Environment's routing picks the pipeline owner over a second capable agent; the real harness worker runs the implementation job (writes) and the review job (read-only) under the owner's identity, with a grant on the candidate only; acceptance and review bound to one revision; VERIFIED by the post-change probe |
| B4–B9, B11, B12 | unit / runtime / real adapter | all passed; results in `closure-evidence/benchmark-results.json` |

## What already existed and what changed

The entry audit is `phase3-gap-analysis.md`.

**Reused unchanged:** GoalManager's lifecycle, the harness path resolver and
permit, FilesystemPolicy, the CognitiveModelService accounting, ContractNet,
the blackboard, and the generation pipeline.

**Added:**

- the typed procedure format and validator;
- admission;
- the durable executor with its operation journal and reconciliation;
- the Environment host and the BDI selection and completion path;
- learning from contract-backed traces;
- `/typed`;
- two templates.

**Changed:**

- **ImprovementControl:** an executor seam; verification by real
  observations; verdicts bound to a revision; a cancelled task is not success.
- **The Evolver:** idle on an empty backlog; a protected surface in candidate
  validation.
- **Conditions-completed goals** now publish `GOAL_COMPLETED`.
- **`core.json_write`** writes canonical bytes (CRLF on Windows had broken
  reconciliation).

## Remaining legacy paths, kept on purpose

- Behavior recipes, the scheduled shortcut, `ProcedureLearner`'s textual
  learned plans and model planning still serve every goal that has no admitted
  typed procedure (`NO_MATCH`). They are the compatibility path. None of them
  can override a typed refusal.
- The generation pipeline remains the executor for code improvements, behind
  the `WorkExecutor` seam.

## Deferred, not scheduled

`closure-evidence/known-limitations.md` lists 18 items. The ones a human should
act on or know about:

- human backlog items now wait for `/improvements verify`;
- a fixed runtime fault now also waits for `/improvements verify` (or a
  target-specific probe; none ships yet);
- code work goes only to the agent running the candidate pipeline;
- the Evolver idles unless `evolution.scout_when_idle` is on;
- the live mesh has not been restarted onto this code;
- procedure tables are never pruned.

## Freeze

The core contracts are frozen: the procedure format, admission, the executor
and journal, BDI selection and completion, delegation, the verification rules
and the protected surface. The next stage is using the system on bounded real
tasks through the stable extension points: new adapters, checks, output
contracts, shipped definitions and templates. No Phase 4 is created.

Reopen architecture work only for one of these:

- a reproducible safety or correctness defect;
- a measured bottleneck blocking a real workload;
- an unavoidable compatibility issue;
- an explicitly authorized product requirement that cannot fit these points.

Self-improvement may continue within its policy. A change to the protected
surface needs a human's review.
