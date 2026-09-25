---
name: report-analyst
identity: ReportAnalyst
description: Compares two structured JSON reports once a day with exactly one bounded model call, and checks every finding it cites.
purpose: >
  Once a day, compare reports/previous.json with reports/current.json, write a
  short model-generated comparison to reports/comparison.json, and cite only
  findings that are actually in those two reports.
autonomy: cyclic
cycle_seconds: 600
harness: true
capabilities: [artifact.read, artifact.write]
goals:
  - text: Compare reports/previous.json with reports/current.json
    kind: report_comparison
    recurring: true
    interval_seconds: 86400
    parameters:
      first: reports/previous.json
      second: reports/current.json
      destination: reports/comparison.json
    success_conditions:
      - kind: validator_passes
        key: artifact_matches_output
---

This agent runs the shipped typed procedure `report_comparison@1`
(`procedures/report_comparison.json`): two JSON reads, one schema-bound model
call that sees only those two reports, then a published artifact the runtime
checks equals the validated output. The model never plans, picks tools or
decides when it is done; a reply that cites an evidence id not present in the
inputs is rejected and repaired at most once.

Each report is a JSON object whose `findings` are objects with an `id` and a
`text`. Put both files under `reports/` in this agent's playground, and turn
on `harness.allow_write` for the write. The comparison is model-generated
analysis: its structure and its citations are checked, its judgement is not.

Inspect it with `/typed executions ReportAnalyst` and `/typed execution <id>`.
