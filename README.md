<p align="center"><strong>EvoMesh</strong></p>
<p align="center">A local-first environment where agents live, collaborate, learn skills, and evolve safely.</p>

## What it is

EvoMesh is an experimental, open-source multi-agent runtime for local AI models. The environment owns agent lifecycle, messaging, persistent state, skills, permissions, model access, health, and evolutionary history. Agents communicate through the environment rather than private network endpoints.

## Why it exists

Most agent systems treat agents as prompts around API calls. EvoMesh treats them as persistent participants in a shared world, with structured minds, reusable capabilities, explicit access grants, and a generational path for improving the runtime itself.

## Current status

Version 0.1.0-alpha.1 is an early runnable foundation. Implemented: the console, SQLite persistence, asynchronous messaging, system-agent bootstrap, a deterministic Agent Architect interview, BDI-shaped state, local model adapters, filesystem grants, built-in skills, isolated candidate workspaces, supervisor metadata, and automated tests. Experimental: model-driven runtime behavior and manual generation promotion. Planned: richer mutation authoring, stronger OS sandboxing, web/Telegram channels, and autonomous promotion policies.

## Core ideas and architecture

```text
Human -> Console Channel -> EvoMesh Environment
                              |-- Agent Registry / Lifecycle
                              |-- Async Message Bus -> Mailboxes
                              |-- SQLite State
                              |-- Skill Registry -> Permission Policy
                              |-- Local Model Providers
                              `-- Candidate Workspace -> Validator

Supervisor metadata (outside candidate) -> ACTIVE / LAST-KNOWN-GOOD / CANDIDATE
```

The environment is explicit application state, not a global singleton. SQLite access sits behind a repository. Model and channel contracts are replaceable. Generation supervisor metadata lives outside candidate workspaces.

## Quick start

For Windows, the easiest installation is the self-contained desktop archive from [GitHub Releases](https://github.com/Dev-Art-Solutions/EvoMesh/releases). Extract it to a writable directory, install `uv`, and run `start-evomesh.bat`. The archive includes the .NET desktop runtime; Ollama remains optional.

To run from source, use the following steps.

Prerequisites: Python 3.13+, [uv](https://docs.astral.sh/uv/), Git, and optionally Ollama.

```bash
git clone https://github.com/Dev-Art-Solutions/EvoMesh.git
cd EvoMesh
uv sync
cp evomesh.yaml.example evomesh.yaml
uv run evomesh
```

On PowerShell use `Copy-Item evomesh.yaml.example evomesh.yaml`. EvoMesh still boots when a configured local model is absent and reports an actionable provider warning; agent inference requires the provider to become available.

### Windows Control Center

On Windows, double-click `start-evomesh.bat`. It synchronizes the Python environment and opens the WinForms Control Center. From there you can:

- start and stop the mesh;
- chat with the selected agent and send slash commands;
- ask Agent Architect to create a new agent;
- start or stop individual agents;
- list Ollama models and assign a different provider/model to each agent;
- edit `evomesh.yaml` while the mesh is stopped.

Settings that require a restart are visible but disabled while EvoMesh is running. Per-agent model changes remain available at runtime and safely restart only the affected agent. For direct terminal use, run `start-evomesh-console.bat`.

## Local model configuration

`evomesh.yaml` configures Ollama (default), InferHub, or another local OpenAI-compatible endpoint. No cloud AI account is required. For Ollama, install the configured model, for example `ollama pull qwen3`. InferHub is optional and uses its OpenAI-compatible local endpoint.

Each agent persists its own `provider` and `model_name`. The runtime passes that model on every inference request, so agents can use different Ollama models concurrently. Use the Agents tab in the Control Center or:

```text
/models ollama
/model "Research Agent" qwen3:14b ollama
/model "Fast Router" qwen3:4b ollama
```

## Agent Architect and agents

Run `/chat architect` and describe the agent you need. Architect gathers a name, purpose, constraints, filesystem needs, skills, and the provider/model it should use, then builds a candidate. `/confirm` persists, starts, and selects it; `/cancel` discards it. Agent definitions include identity, provider/model, generation, parentage, skills, permissions, memory behavior, status, and timestamps.

## BDI-inspired cognition

Beliefs, goals, and intentions are structured fields persisted with the agent, not hidden in one giant prompt. This is the initial cognition model and is intentionally replaceable.

## Skills

The shared registry supports discovery, attachment, and invocation. Built-ins are `Filesystem.Read`, `Filesystem.Write`, `Markdown.Read`, `Markdown.Write`, `Git.Status`, and `Git.Diff`. A skill never grants path access by itself.

## Filesystem access grants

Use `/grant <agent> <path> read|write` and `/revoke <agent> <path>`. Paths are resolved before comparison, descendants inherit only the granted operation, and denials are structured exceptions. These are application-level controls, not an OS security sandbox.

## Messaging

Direct and broadcast messages use per-agent asynchronous mailboxes, correlation/conversation IDs, and durable audit records. Agent business behavior does not call other agent objects directly.

## Evolution and generations

Candidates are copied into isolated generation directories and never overwrite the active tree. The supervisor tracks active, candidate, and last-known-good generations in atomic metadata. Candidate validation runs `uv sync`, Ruff, Pyright, and pytest and writes a result record. Promotion is deliberately human-controlled in v0.1; rollback switches metadata to the last-known-good generation.

## Git history

Git is the intended evolutionary lineage. The initial substrate records mutation objectives/results and isolates code. Rich model-authored patches, structured mutation commits, generation tags, and promotion UX remain experimental follow-up work.

## Project structure

```text
src/evomesh/   runtime, contracts, storage, agents, skills, permissions, evolution
desktop/       Windows Forms Control Center
tests/         unit, integration, and restart scenario coverage
data/          local SQLite state (ignored)
generations/   supervisor metadata and candidate workspaces (ignored)
scripts/       development launchers
*.bat          one-click Windows launchers
```

## Development and candidate validation

```bash
uv sync
uv run ruff check .
uv run pyright
uv run pytest
```

CI runs the same checks on Python 3.13 without Ollama or a GPU by using a mock provider.

## Roadmap

Near-term milestones include a Skill Architect, richer mutation application and Git lineage, more capable long-term memory, a local web channel, Telegram, PDF skills, benchmark-based promotion, and stronger process isolation.

## Security / experimental status

EvoMesh executes local model output and is designed to modify candidate code. Treat it as experimental. Filesystem policies are enforced inside EvoMesh but are not a hardened sandbox. Review candidates before promotion, use least-privilege grants, keep backups, and never expose local model endpoints or EvoMesh state to untrusted networks without additional controls.

## Contributing

Open an issue before large architectural changes. Keep features local-first, preserve environment-mediated messaging and permissions, add deterministic tests, and update documentation to match what actually works.

## License

MIT. See [LICENSE](LICENSE).
