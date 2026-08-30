# Changelog

All notable changes to EvoMesh are documented in this file.

## [0.1.0-alpha.1] - 2026-08-30

### Added

- Local-first multi-agent runtime with persistent SQLite state and asynchronous messaging.
- Agent Architect interview flow for creating and starting agents.
- Per-agent Ollama and OpenAI-compatible provider/model selection.
- Built-in skills with explicit filesystem access grants.
- Candidate generation workspaces, validation, and supervisor metadata.
- Windows Forms Control Center for lifecycle, chat, agents, models, and settings.
- One-click Windows launchers for the Control Center and console.
- Self-contained Windows x64 release packaging and Windows CI validation.
- Repository-scoped NuGet configuration for deterministic public builds.

### Changed

- Agent model assignments can be changed at runtime by restarting only the affected agent.
- Restart-required settings are disabled in the Control Center while the mesh is running.

### Known limitations

- EvoMesh is experimental and its permissions are application-level controls, not an OS sandbox.
- Evolution promotion remains human-controlled.
- The packaged desktop application targets Windows x64; the Python runtime still requires `uv`.
- Local model weights and Ollama are not bundled.

[0.1.0-alpha.1]: https://github.com/Dev-Art-Solutions/EvoMesh/releases/tag/v0.1.0-alpha.1
