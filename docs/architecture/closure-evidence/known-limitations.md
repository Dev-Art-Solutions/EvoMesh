# Known limitations and deferred items

Recorded at architecture closure (plan v2 §25.2, §26). None of these is an
in-scope correctness, permission, data-loss or duplicate-effect blocker. Each
is a boundary of what the closure demonstrates, or a follow-up that may be
scheduled only for a real workload (§26.3). None of them starts new
architecture work automatically.

## What the evidence does not show

1. **Local-model quality.** W2 ran 18 times on one model: `ornith-1.5:35b`,
   Q4_K_M, digest `9f3b89b2…`, on Ollama 0.34.2. The runs used 4k and 32k
   contexts, which the server itself reported as loaded. All 18 completed on
   the first call, and every citation named an id present in its inputs. The
   checks cover structure and citations, not whether the analysis is right.
   Nothing here speaks for a smaller model or another family.
2. **B3 savings.** The legacy path in B3 used a scripted model, three turns
   per occurrence. The measured drop from about 3 calls to 0 compares those
   runs only; it is not a real-model saving.
3. **No comparable legacy baseline for W1/W2.** No legacy workflow performed
   the same file work, so W1 and W2 report absolute counts (0 and 1 calls).
   No before-state was estimated.

## Behaviour a human should know about

4. **Human backlog items now wait for a human to verify them.** An item from
   `docs/evolution/improvements.md` that lands stays `VERIFYING`, with the
   reason shown in `/improvements`, until `/improvements verify <id> <reason>`.
   Before, it was marked verified once its title left the file.
5. **An empty backlog now idles the Evolver.** `evolution.scout_when_idle` is
   off by default; turn it on to get the old exploring behaviour back.
6. **The live mesh was stopped for this work and has not been restarted.**
   The typed path is proven through the real `Environment` in tests and in
   the spawned-template tests, not yet on the running mesh.

## Known gaps, bounded

7. **The runtime log cannot verify a fixed fault.** It shows that the mesh
   ran, not that the repaired path did, so its readings no longer count
   toward verifying a runtime fault or a runtime event (corrected after the
   9739188 audit; before, three readings of unrelated activity verified one).
   It still counts a fault that comes back. A fixed runtime fault stays
   `VERIFYING`, naming the reason, until a target-specific probe reports on
   it or an operator runs `/improvements verify <id> <reason>`. No such probe
   ships yet.
8. **Trace model-call counts use the diagnostic call log.** That log holds
   2048 records. On a very busy mesh the count can be low. The per-execution
   budget counters are durable and exact (T42).
9. **Procedure tables are never pruned.** Definitions, executions, operations
   and traces grow without bound. That is safe but unbounded.
10. **Operator identity is local trust.** Anyone at the console or the
    127.0.0.1 control port acts as `operator:console`.
11. **One mesh process per database.** Concurrent advances inside one process
    are serialized, and the version compare-and-set guards the rest. Two
    processes sharing a database are prevented by the singleton lock, and
    are not tested beyond T35.
12. **Emergency disable does not persist.** `/typed disable` lasts until the
    process restarts. For a durable stop, set `procedures.enabled: false`.
13. **Cancelling a parent does not cancel its child.** The child runs to
    completion; its late result is recorded but revives nothing.
14. **Only straight-line learning.** Candidates come only from sequences of
    the two json adapters. A cognitive step in a harness trace is not
    reconstructed; that case is `NOT_COMPILABLE` by design.
15. **The protected surface is path-based.** A change to an unprotected
    module that a protected one imports is not caught, so separate review
    stays necessary for core changes.
16. **Platforms.** The full suite runs on Windows 11 in this workspace. CI
    runs ruff, pyright and pytest on ubuntu-latest and the desktop build and
    self-tests on windows-latest. On this workspace's venv, pyright cannot
    resolve `pytest`, so it reports only `reportMissingImports` noise; CI is
    the authority for that gate.
17. **Local effects only.** The effect adapters are `core.json_read` and
    `core.json_write` (local, with receipts) plus the idempotent blackboard
    fact publish. External effects are refused at validation.
18. **Only the pipeline owner carries out code work.** Capability routing
    awards an improvement's work item to the agent running the candidate
    pipeline, and to nobody when that agent is not among the capable
    bidders. A second code-capable agent is never selected for it. The
    live W3 trace (`tests/test_w3_live.py`) proves this path: the owner's
    identity and a grant scoped to the candidate reach the real harness
    jobs, and the review runs read-only. It does not show a different
    agent with its own model and tools doing the work; that would be new
    routing, not a correction. This is narrower than the plan's
    separate-executor requirement. The acceptance manifest records it as a
    scope decision (`SCOPE_DECISIONS` in `benchmarks/closure/acceptance.py`),
    and until an operator accepts it by name it is a blocking finding.
