---
name: json-archivist
identity: Archivist
description: Keeps a validated snapshot of a JSON record file every hour -- a typed procedure, no model call per run.
purpose: >
  Every hour, copy inbox/records.json to archive/records.snapshot.json exactly,
  and prove the copy matches what this run read before calling it done.
autonomy: cyclic
cycle_seconds: 300
harness: true
capabilities: [artifact.read, artifact.write]
goals:
  - text: Snapshot inbox/records.json into archive/records.snapshot.json
    kind: local_json_snapshot
    recurring: true
    interval_seconds: 3600
    parameters:
      source: inbox/records.json
      destination: archive/records.snapshot.json
    success_conditions:
      - kind: validator_passes
        key: artifact_matches_source
---

This agent runs the shipped typed procedure `local_json_snapshot@1`
(`procedures/local_json_snapshot.json`): read the source, write it as canonical
JSON, then check that the written file equals what this run read. No model is
asked to plan, route or judge any of it -- the goal is done only when that
check passes, and a failed or denied run fails the goal instead of being
handed to a model to improvise around.

Put the file to archive at `inbox/records.json` inside this agent's own
playground (its harness root). `harness.allow_write: true` in `evomesh.yaml`
is needed for the write; without it the goal fails with PERMISSION_DENIED,
which is the point. Each hour is a new occurrence: it re-reads the source and
replaces its own previous snapshot. A file at the destination this agent did
not write is a DESTINATION_CONFLICT, never silently overwritten.

Inspect it with `/typed executions Archivist`, `/typed explain Archivist <goal-id>`,
or stop all new typed runs with `/typed disable`.
