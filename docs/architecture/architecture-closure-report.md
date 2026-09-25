# Architecture closure report

**Status: `ARCHITECTURE_ACCEPTED_AND_LOCALLY_VALIDATED`**. The core
architecture is frozen; see "Freeze" below.

| | |
|---|---|
| Plan | `plans/EVOMESH_ARCHITECTURE_CLOSURE_PLAN_V2.md` |
| Baseline measured at | `815cd6c` (generation 1652): 907 passed, 8 skipped, ruff clean, pyright 0 |
| Implementation parent | `6bac351` (generation 1653, landed while the mesh still ran) |
| Tested commit | `2da3a5c` |
| Manifest | `closure-evidence/acceptance.json` (generated at the tested commit by `python -m benchmarks.closure.acceptance`) |
| CI at tested commit | green: ruff, pyright including tests, pytest on ubuntu-latest; desktop build and self-tests on windows-latest |

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
| `python -m pytest -q` | 1046 passed (Windows 11, this workspace) |

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

`closure-evidence/known-limitations.md` lists 17 items. The ones a human should
act on or know about:

- human backlog items now wait for `/improvements verify`;
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
