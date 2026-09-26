# Typed procedures

A typed procedure is a small, validated program an agent runs for a goal of a
known kind, instead of asking a model to plan and act. It is the closure
architecture's answer to "the same work, every time, without a model
deciding each step" (`plans/EVOMESH_ARCHITECTURE_CLOSURE_PLAN_V2.md`).

| Where | What |
|---|---|
| `src/evomesh/procedures.py` | the format: definitions, bindings, predicates, schema subset, static validation |
| `src/evomesh/procedure_runtime.py` | admission registry, executor, operation journal, adapters, checks |
| `src/evomesh/procedure_host.py` | the live mesh as host; `/typed` operator controls |
| `src/evomesh/procedure_traces.py` | traces from model-directed work, candidate extraction, replay |
| `procedures/*.json`, `procedures/admissions.json` | shipped definitions and their reviewed approvals |

## Format

A `ProcedureDefinition` (schema version 1) names a `goal_kind`, a strict
`parameter_schema` and `output_schema`, declared `required_capabilities`, and a
graph of steps with explicit edges. Seven step kinds:

| Kind | Does | Model call |
|---|---|---|
| `tool` | one registered adapter under the agent's own authority | never |
| `cognitive` | one schema-bound request to the model service, no tools | yes, counted |
| `branch` | a three-valued predicate; `unknown` fails, never picks a side | never |
| `validate` | a trusted check (`artifact_matches_source`, `artifact_matches_output`) | never |
| `delegate` | one durable child work item, routed by capability | never |
| `await` | a durable wait on work, evidence or time | never |
| `complete` | the terminal output, checked against `output_schema` | never |

Values are bound, never templated: `{"ref": {"scope": ..., "path": [...]}}`
over `goal`, `result`, `context`, `work` or `constants`, or
`{"literal": ...}`. Text that looks like code or a template is data. A missing
reference is `MISSING_BINDING`; `null` and missing are different. Predicates
(`eq ne gt ge lt le exists all any not`) are strictly typed: comparing
incompatible types is an error, not `false`.

Validation happens before anything can run: unknown fields, kinds, operators or
schema versions; duplicate ids, dangling edges, cycles, unreachable steps; a
result used after a merge that only one branch produced; unknown adapters or
contract versions; a model-backed tool; an undeclared external effect; secret
constants; capabilities the definition uses but does not declare.

## Admission

Content and admission are separate. A revision is immutable: the same
`procedure_id@revision` with a different body is `REVISION_CONFLICT`, and an
execution stays pinned to the digest it started with.

`INVALID` (quarantined, never deleted) · `CANDIDATE` (learned, never selected)
· `VALIDATED` · `PROMOTED` · `DEGRADED` · `RETIRED`.

Only `PROMOTED` is selected, only for its approved goal kinds, and only when
its adapter contracts still match. Approval is bound to the exact digest and
needs a trusted actor: `model…` and `agent:…` are refused, and a definition's
own fields cannot promote it. Shipped definitions are approved by
`procedures/admissions.json`, checked into the repository with their digests;
an operator's later degrade or retire survives a restart.

## Selection in the BDI reasoner

Each cycle, before any recipe, scheduled shortcut, learned textual plan or
planning call: an open execution for the goal's current occurrence is resumed;
otherwise the registry selects for the goal's kind. Only `NO_MATCH` and
`INCOMPATIBLE_PROCEDURE` fall back to the legacy path. Permission denied,
invalid, approval required, budget exhausted, or a reconciliation owed each
fail the goal. None of them hands the goal to a model to route around.

A goal is complete when its own success conditions hold against the
execution's evidence for this occurrence: validator results and applied
receipts. The graph reaching `complete` is not enough
(`POSTCONDITION_UNSATISFIED`). A goal with no conditions completes and is
labelled `legacy_completion`.

## Execution and recovery

One step advances at a time. `advance()` holds the execution process-wide,
and every state change is a compare-and-set on the execution's version,
committed together with the operation records it touches.

Every effect has a logical key, `occurrence:execution:step`. Its record is
`dispatching → applied | rejected | unknown | waiting`. After a crash a
dispatching record is reconciled, never blindly repeated:

- a `SAFE` read is repeated;
- `core.json_write` checks its receipt sidecar and the file digest:
  `APPLIED` settles without writing again; `NOT_APPLIED` gives back the
  per-step attempt (root counters keep it, so a crash loop still runs out
  of budget); a foreign or changed file is `CONFLICT`;
- an adapter that cannot reconcile leaves the execution in
  `NEEDS_RECONCILIATION` until an operator decides `recheck`, `not_applied`
  or `fail`.

Cancellation is an intent on the execution (`cancel_requested`), not only a
status. An operation already dispatched is settled first: a known effect keeps
its receipt and the execution becomes `CANCELLED`; one proven not applied is
not retried; one nobody can prove stays `NEEDS_RECONCILIATION` with the intent
intact, and the operator's decision then ends it as `CANCELLED`. Reconciliation
never turns a cancelled execution back to `RUNNING`, so no later step runs. A
settlement that loses its compare-and-set to the operator's cancel reloads and
records the receipt on the newer version.

Tested windows: a crash before the claim commits, after the effect but before
its receipt (in-process and a killed subprocess), an older ledger restored
over newer receipts, and concurrent advances. `json_write` writes canonical
bytes; text mode put CRLF on disk on Windows and no digest matched.

A recurring occurrence re-reads its source and may replace its own earlier
artifact. The per-path ownership index proves that artifact is unchanged since
this runtime wrote it; anything else at the destination is
`DESTINATION_CONFLICT`.

## Budgets

One envelope per goal occurrence covers model calls, tool attempts, step
attempts and reserved child model calls, plus delegation depth. Retries and
replacement executions inherit it, and inherit the deadline too: a restart or
a replan is not new time, and `DEADLINE_EXCEEDED` ends the goal. A cognitive step spends at most
`max_model_calls + repair_calls`, and a tool-call attempt ends it at once.
Mandatory input over the context guard fails before any call rather than being
truncated. Unreported tokens stay `None`.

## Delegation and waits

A `delegate` step creates one child `WorkItem` with a deterministic id,
commits it together with the parent's cursor, then delivers it as a `DELEGATE`
message. The child is admitted from that committed WorkItem, not from the
message's copy. The WorkItem's allocation is what the child runs under: its
model calls cap the child and everything it delegates further, its deadline is
never later than the parent's, and its `max_attempts` bounds how many
executions the occurrence may start. Bounded work -- any WorkItem with an
allocation, a deadline or resources -- runs only typed. If no admitted
procedure matches it, whether through `NO_MATCH`, `INCOMPATIBLE_PROCEDURE` or
the emergency switch, the goal fails `unsupported_execution`. It is never
handed to legacy planning or model-backed steps, which enforce none of those
limits. Unbounded legacy delegation (the harness's `delegate_work`, a stall's
assistance) still takes the legacy path.

The recipient keeps a durable ledger of accepted WorkItems, and writes the
ledger entry and the agent holding the new goal in one transaction. A replayed
`DELEGATE` finds the entry and creates nothing, even after the child finished
and its goal was pruned. A crash or a failed write leaves neither, and the
requester gets a `REJECT`, so the replay recovers the work instead of being
refused as a duplicate of something lost.

Files are resolved once, in the requester's scope. The router finds every
resource the work names: the parameters an admitted procedure binds into an
adapter's file argument, and any path-named field at any depth. It resolves
each to an absolute path under the requester's root, requires the requester's
read (or write, for a destination), and rewrites the input to that absolute
path. A peer is eligible only if the same file is inside its own root and its
own grant covers it. A same-named file in another root is not the same
resource, and a cross-root task with no shared file is refused. The WorkItem
carries the resolved set as `resources`. At the child's adapter boundary a
file outside that set is refused even when the child's own grant would allow
it, and the requester's authority is checked again, so a revocation after
assignment stops the effect.

The child runs its own typed procedure. Its validated output settles the
parent's operation once, and only from the assigned executor. The parent then
checks the WorkItem's contract: the output schemas, their semantic checks, and
every validator the success contract lists in `required_evidence`, passed by
the child itself (`CHILD_CONTRACT_UNSATISFIED` otherwise). Schema-valid output
alone does not satisfy a contract that asks for evidence. A failed or
cancelled child is not success. Waits re-read durable state, so evidence
published before the wait began still counts and a lost notification changes
nothing. Waiting never calls a model and never spends step attempts.

## Security

- Adapters reuse `harness_tools._resolve`/`_permit` with the agent's own
  `FilesystemPolicy`; nothing runs with human authority (`policy=None`).
- Capabilities are rechecked at every dispatch, so a grant revoked mid-run
  stops the next effect.
- Output that names a handler, an approval or a status is data.
- Learning traces replace secret-named fields and are then ineligible.
- The protected surface (`codebase.PROTECTED_PATHS`) covers these modules,
  the closure tests, the approvals and the evidence. A candidate generation
  that touches it fails validation, so no candidate can rewrite its own
  oracle.

## Learning, conservatively

Model-directed harness jobs get `json_read`/`json_write`. These run the same
adapters and are journaled against the goal occurrence they serve. A goal that
completes on its own success conditions, re-checked at that moment, leaves an
`OperationTrace`. Failed operations, oversized values, redactions or
self-reported completion make a trace ineligible, and each occurrence counts
once.

`extract_candidate` needs three or more distinct eligible occurrences with one
operation sequence. It also needs an operator's `BindingMap`, which is checked
against every trace. Equal values are never taken as lineage. An unbound
argument becomes an exact-scope literal only if every trace agrees. A candidate
registers as `CANDIDATE`. A sandboxed replay on a held-out input can mark it
`VALIDATED`, and an operator promotes it. A procedure's own validation or
output-schema failure degrades it.

## Operating it

`/typed` lists, shows and statically validates revisions. It approves a
revision only when you type at least 12 characters of its digest, and it
degrades or retires one. It explains why a goal did or did not select a
procedure, lists and inspects executions with the reason each is waiting,
pauses, resumes, cancels and reconciles them, and flips the emergency switch.

`procedures.enabled: false` in `evomesh.yaml` stops new typed executions;
running ones still settle. `procedures.collect_traces` gates the harness
json tools and trace capture.
