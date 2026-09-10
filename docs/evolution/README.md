# Evolution backlog

One entry per generation the Environment Evolver produced: what it
changed, the reason it gave, and how the change was checked. Written
by the mesh itself, into the same commit as the code.

- [Generation 370](000370.md) — # Plan: Wire `cycles.py` into the harness to detect cyclic agent dependencies ## Goal The module `src/evomesh/cycles.py` contains two functions that no code calls: - `cycle_agents(dependencies: Mapping[str, Iterable[str]]) -> set[str]` — runs Tarjan's strongly-connected-components algorithm over a dependency graph and returns the set of nodes that participate in a dependency cycle (a node in an SC...
- [Generation 368](000368.md) — # Plan: Wire the dead `cycles` module (cycle detection) into the agent-execution order so the dependency graph actually runs ## Goal Give EvoMesh's agent dependency graph a real cycle detector instead of a silent, incorrect fallback. Today, when the inter-agent dependency graph in `contracts.py` contains a cycle, `AgentExecutionOrder.topological_sort()` falls back to returning the unordered list o...
- [Generation 356](000356.md) — -
- [Generation 355](000355.md) — # Plan ## Title Wire the dead `cycles` module (cycle detection) into the runtime so the agent dependency graph actually runs. ## Why this and not the others The audit named four dead modules. `cycles.py` is the cheapest real win: it is two tiny pure functions (`cycle_agents`, `explain_not_running`) that take a `Mapping[str, Iterable[str]]` and return a `set[str]` or a `str`. Wiring them in needs n...
- [Generation 271](000271.md) — # Plan: Introduce a stable AgentId service ## Goal Give EvoMesh a single, shared, stable identity service for agents instead of the current ad-hoc, per-site id assignment. Today an agent's `id` is a `str(uuid4())` generated at different times in different places (`environment.py` at roster build, `harness_session.py` at session start), and the one library that actually implements agent-id concepts...
- [Generation 261](000261.md) — The model changed `test_syntax.py` but gave no rationale for it -- see the diff below for what actually moved.
- [Generation 256](000256.md) — The model changed `_run_check.py, src/evomesh/harness_session.py` but gave no rationale for it -- see the diff below for what actually moved.
- [Generation 251](000251.md) — The model changed `check_reach.py, src/evomesh/contracts.py` but gave no rationale for it -- see the diff below for what actually moved.
- [Generation 245](000245.md) — The model changed `src/evomesh/contracts.py` but gave no rationale for it -- see the diff below for what actually moved.
- [Generation 240](000240.md) — The model changed `run_checks.py, src/evomesh/harness_tools.py` but gave no rationale for it -- see the diff below for what actually moved.
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
