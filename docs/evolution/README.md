# Evolution backlog

One entry per generation the Environment Evolver produced: what it
changed, the reason it gave, and how the change was checked. Written
by the mesh itself, into the same commit as the code.

- [Generation 236](000236.md) — The model changed `src/evomesh/harness_tools.py` but gave no rationale for it -- see the diff below for what actually moved.
- [Generation 216](000216.md) — The model changed `src/evomesh/contracts.py` but gave no rationale for it -- see the diff below for what actually moved.
- [Generation 212](000212.md) — The model changed `src/evomesh/phase_label.py` but gave no rationale for it -- see the diff below for what actually moved.
- [Generation 211](000211.md) — The model changed `src/evomesh/contracts.py` but gave no rationale for it -- see the diff below for what actually moved.
- [Generation 192](000192.md) — The model changed `src/evomesh/_agent_ids.py, src/evomesh/contracts.py` but gave no rationale for it -- see the diff below for what actually moved.
- [Generation 184](000184.md) — The model changed `src/evomesh/contracts.py` but gave no rationale for it -- see the diff below for what actually moved.
- [Generation 165](000165.md) — --- title: "Plan: wire a dead module into the live harness path" status: candidate --- # Goal Improve EvoMesh by making one currently-dead module reachable from code that actually runs, then pruning it from the known-dead backlog. The improvement is measured by the reachability ratchet (one fewer dead module), not by any feature added. # Approach The package has a set of modules that nothing impor...
- [Generation 107](000107.md) — --- # Plan frontmatter. # # Goals are HIGH LEVEL ("what", agnostic to implementation). Each is a # verifiable outcome. # # The approach section is where you commit. Describe the concrete strategy # you intend to take and the reasoning behind it. This is the part that # gets split into work items. # # - "Improve" is defined by the reach analysis at # docs/evolution/plans/README.md#reach-analysis. #...
- [Generation 104](000104.md) — --- title: "Plan: agent-type labels in the console agent view" status: candidate --- # Agent-type labels in the console `agents` view ## Goal The console `agents` command (`Console._command_agents`) prints each agent's type as a raw enum member, e.g. Evolver [agent_architect] ... Evolver [trader] ... `AgentRole` is a `StrEnum` whose members (`agent_architect`, `guardian`, `evaluator`, `evolver`, `...
- [Generation 94](000094.md) — The model changed `check_reach.py, inspect_reach.py, run_check.py` but gave no rationale for it -- see the diff below for what actually moved.
- [Generation 88](000088.md) — The model changed `src/evomesh/harness_session.py` but gave no rationale for it -- see the diff below for what actually moved.
- [Generation 86](000086.md) — The model changed `search_tmp_check.py, src/evomesh/console.py, src/evomesh/humanize.py, verify_smoke.py` but gave no rationale for it -- see the diff below for what actually moved.
- [Generation 50](000050.md) — The model changed `src/evomesh/harness_session.py` but gave no rationale for it -- see the diff below for what actually moved.
- [Generation 29](000029.md) — The model changed `src/evomesh/harness_session.py` but gave no rationale for it -- see the diff below for what actually moved.
- [Generation 25](000025.md) — -
