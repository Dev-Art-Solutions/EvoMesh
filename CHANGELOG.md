# Changelog

All notable changes to EvoMesh are documented in this file.

## [0.2.0-alpha.6] - 2026-09-02

### Fixed

- **The claim this project has been making since the pipeline was written is now
  true.** "One stage per cycle means a tick never becomes a ten-minute
  validation run" -- except that the validate stage awaited the suite inline. An
  agent's mailbox loop and its cycle loop share one lock, so for the whole of a
  multi-minute run the Evolver answered no `/chat`, reported no `CURRENT WORK`,
  and looked exactly like an agent that had hung.
  - Validation now runs off the cycle in its own lane. The stage starts it,
    holds, and consumes the verdict on a later tick -- the same shape the propose
    stage already had since it moved onto the harness.
  - A separate lane from the harness worker on purpose: a tool loop is the GPU
    and a validation run is CPU and disk, so making one wait for the other would
    be a queue whose only effect is to slow the machine down.
  - `evolution.validate_seconds` (1800) bounds a run. Past it the suite is
    stopped and reported as **blocked, not failed** -- the candidate never got a
    verdict, and a suite this machine could not finish is not its fault.
  - Stopping the mesh cancels the run and resumes nothing: a candidate is a copy
    on disk, and re-running the suite costs time and nothing else.
  - The agent's phase stays `acting` and the enum does not grow again. Validating
    is the agent doing its own work, and `/evolution status` already names the
    stage; a third phase member would be a second place for the same fact to be
    wrong.

### Added

- **The linter's own fixer no longer spends the repair budget.** The budget
  exists to bound how often a *model* may rewrite a candidate; ruff's `--fix`
  costs nothing and cannot make the candidate worse, and a fix that changes
  nothing is already caught by the stall check rather than by a counter.
  Found the first time the whole loop ran: the model diagnosed the real failure
  and fixed it, ruff then objected to the import order it had produced, and the
  candidate went to a human over a finding the linter would have fixed for free.
  `max_repairs: 0` still means off, because a human who turned self-repair off
  asked for one shot and a verdict, not for a cheaper kind of repair.
- **Every candidate was failing validation on our own import.** A test helper
  was imported as `from tests.test_cycles import ...`, which resolves in the
  checkout and nowhere else: inside a candidate pytest's rootdir is the
  candidate and `tests` was not a package. Validation *is* this suite, so the
  loop was failing candidates for a line we wrote. Shared doubles now live in
  `tests/fakes.py`.
- A writing job that is past halfway and has changed nothing is told so, once.
  Found by this release's acceptance run: a 27B model spent all twenty steps and
  thirty-two tool calls reading -- re-reading the same two files at different
  offsets, which the repeat guard cannot see because every call is different --
  and the job ended having done nothing. A model cannot budget what it cannot
  see. Said once, because twice is noise.

## [0.2.0-alpha.5] - 2026-09-02

### Added

- The harness stopped being only the Evolver's. Any agent can be granted it,
  the way filesystem access is granted:
  `/harness grant "Notes Summarizer" D:/notes`, and `/harness revoke` to take it
  back. The grant records the root on the agent and issues the matching
  filesystem grant, so "what may this agent do" still has one answer in one
  place.
  - A granted agent takes a plan step **with tools instead of with a prompt**
    when the step starts with a looking verb -- investigate, find, search,
    check, inspect, review, diagnose. Decided by a prefix rather than by asking
    the model, because that would be one extra inference per cycle to answer
    what a list answers for free, and on a 4B model the answer would be noise.
  - The step is not consumed while the job runs, so the agent keeps one
    commitment instead of completing and re-adopting a plan every tick, and its
    phase reads `awaiting-harness`. The job number is remembered **on the step**:
    when it finishes, the step that asked for it consumes the answer, and a
    finished job is never mistaken for "no job yet".
  - The finding is written into the agent's memory as the step's own outcome. An
    agent that investigated something and did not remember it has investigated
    nothing.
  - An agent with no grant behaves exactly as before, prompt for prompt.

## [0.2.0-alpha.4] - 2026-09-02

### Added

- A `shell` tool, sixth of six and off unless a human says what may run. It
  answers the questions no file can: does this still import, does this parse,
  did that command work.
  - `harness.shell_allow` is an **allow-list of program names, empty by
    default** -- with nothing in it the tool is not even offered to the model,
    because an unusable tool in the schema is a tool a model will try. A
    deny-list was rejected: it is a promise that every dangerous command has
    been thought of, and it is wrong the first time a tool is installed.
  - **No shell interpreter.** The command is split with `shlex` and run
    directly, so `|`, `&&`, `>` and `$(...)` are arguments rather than
    operators. `curl x | python` is refused for `curl`, which is the point:
    every allow-list that has been defeated was defeated through a pipe.
  - The program name is matched **after** parsing, never against the raw string.
  - Everything runs in the job root -- there is no `cwd` argument -- and
    `harness.shell_seconds` (60) bounds one command, because a tool that can
    hang is a worker that never comes back and a queue that never drains. A
    timeout is a refusal the model can work around, not a crash.
  - Parsing uses POSIX rules on every platform. In Windows mode `shlex` keeps
    the quotes, so `python -c "print(1)"` reaches python as a string literal:
    it runs, prints nothing and exits 0 -- a command that looks like it worked
    and did nothing. The cost is that unquoted Windows paths lose their
    backslashes, and the tool's own description says to quote them.
  - This is not a sandbox and does not claim to be. The README has always said
    the filesystem policy is an application-level control; a shell tool does not
    weaken that sentence, it makes it matter more.

## [0.2.0-alpha.3] - 2026-09-02

### Added

- The transcript has a budget. The tools already capped their own output; the
  *pile* of it grew without asking, and a model server drops the oldest end of a
  prompt -- which is where the objective lives -- without saying so.
  - `harness.transcript_chars` (12000) bounds what the model is sent in one
    turn. Over it, the **oldest tool results** are replaced by a marker naming
    the tool and how much was dropped, so the model can tell "I have not read
    that" from "I read it and it said nothing".
  - The task and every assistant turn always survive. A turn is the model's own
    reasoning and it is small; a tool result is a file, and the model can read it
    again. Summarising dropped results with a second model call was considered
    and rejected: it spends inference compressing something the tools reproduce
    exactly.
- The same tool call three times in a row ends the job as `capped`. The second
  identical call is answered from the first result rather than run again -- it
  would produce the same bytes and cost a step -- and the third stops the job,
  because it is not going wrong, it has stopped making progress. `gemma:2b`
  issued one identical `grep` three times in the first release's acceptance run,
  and all three were executed.
- A job reports what it cost: `tool_chars` (everything the tools produced) and
  `prompt_chars` (the largest transcript the model was actually sent). The second
  is what says whether a job would survive on a smaller model.

## [0.2.0-alpha.2] - 2026-09-02

### Fixed

- **Roughly one candidate in three was failing validation for a reason it had
  not caused, and the Evolver was spending its repair budget on it.** Every
  subprocess in the project went through `asyncio.create_subprocess_exec`, whose
  transport the proactor loop finalises during a later garbage collection --
  after the loop that owned it has closed. The `ValueError: I/O operation on
  closed pipe` that follows is attributed by pytest to whichever test happens to
  be running, and candidate validation *is* the test suite.
  - Commands now run through `evomesh.processes.run_command`, a blocking
    `subprocess.run` on a worker thread. There is no transport to finalise, and
    these commands -- git plumbing, `uv run pytest` -- are blocking work anyway.
  - Measured: before, one full run in three failed on a different test each
    time; after, four consecutive runs green and the warning count down from
    five to one.
- **A missing toolchain is reported as blocked, not as a verdict.** `uv` not
  being installed said "the candidate failed", so the pipeline sent it to repair
  and asked a model to fix somebody's PATH. A command that knows it could not run
  now says so with a flag, which `environment_blocker()` reads before it falls
  back to matching strings in output.
- Two more host failures are recognised: `os error 32` and "being used by another
  process" -- Windows refusing to replace a file in the candidate's venv because
  something else held it open. Found by the test written for the case above.
- `/harness status` shows a job's own label. The repair job's objective has no
  `OBJECTIVE:` line, so the status line printed the first line of the project map
  instead of what the job was for.

## [0.2.0-alpha.1] - 2026-09-02

### Changed

- **The Evolver no longer writes whole files.** A generation is now a harness job
  in the candidate workspace: the model reads, greps and edits its way to the
  change instead of returning one complete file as a JSON string.
  - The single-file contract is **deleted**, not deprecated -- `FileMutation`,
    `parse_mutation`, `propose_mutation`, `propose_repair` and `apply_mutation`
    are gone. Two mutation paths would leave one that nobody exercises.
  - A generation can now change more than one file. The first real one did: a 27B
    local model wired the dead `humanize` module into `harness_tools`, in two
    edits, and explained which call site it chose and why.
  - **What the generation changed is read from the session, not from the model's
    word.** Each recorded change carries the unified diff, and
    `docs/evolution/<n>.md` prints it, so "why did it do that" is answerable from
    the commit rather than from a JSONL on one machine.
  - A job that finishes without changing a file is reported and stops there. The
    old path would have validated an untouched candidate, which passes -- and a
    generation that passes while changing nothing is the worst kind of progress.
  - Repair works the same way, with the failing command and its real output as
    the objective, so the model can read the file that failed before rewriting
    it. Ruff's own fixer still runs first, because it costs nothing.
  - The propose stage now spans several cycles (submit, observe, record) and is
    still one stage per cycle: the run happens in the worker.

### Fixed

- The environment now grants an agent the job root it hands it. A candidate is a
  directory the mesh creates for that agent, the harness runs under that agent's
  grants, and without the grant every tool was denied -- which is how the first
  real generation spent four steps discovering it could not read anything.
- `read` marks its line numbers with `|` and says in its own description that
  they are not part of the file. A 27B model copied the number and the apparent
  indentation into an edit anchor and lost two attempts to a target that was
  never there.
- `/harness status` shows a job's objective as one line. Since the Evolver asks
  through the harness, an objective carries the whole project map, and the status
  line printed all of it.

## [0.1.0-alpha.4] - 2026-09-02

### Added

- The harness became work an agent *asks for* rather than work it stops to do. A
  job queue and a worker task run the tool loop off the cycle, so an agent that
  wants minutes of file-reading keeps ticking, keeps answering `/chat`, and keeps
  appearing in `/agents` while its job runs.
  - This is what keeps "one cycle advances one stage" true. A harness run is
    minutes and a cycle has to stay a tick; the run happens in the worker and the
    agent's cycle only checks its handle.
  - **The result comes back as an ordinary inbound message**, not a callback, so
    it lands in the mailbox with the audit record every message gets and wakes the
    loop the agent already has. No behavior has to know a worker exists.
  - `harness.workers` defaults to **1**. Two tool loops on one card do not go
    twice as fast; they queue inside the GPU where nothing can see them, instead
    of in a queue where `/harness status` can.
  - A second submit from an agent that already has an open job returns the first
    handle. A behavior submitting once per cycle would otherwise fill the queue
    with copies of one objective, all editing the same files.
  - A new agent phase, `awaiting-harness`, derived from the queue and never
    stored. It is deliberately not `waiting-human`: those read the same on a
    roster and mean opposite things.
  - `harness.max_queue` (8) bounds the wait list, and `/harness status` shows the
    workers, the open jobs and what the last ones did.
  - **The queue is not durable.** Stopping the mesh cancels what is in it and
    tells the submitter so, because the only thing worse than a cancelled job is a
    plan step that no event will ever finish. A job that raises is cancelled with
    the reason and the worker carries on.

## [0.1.0-alpha.3] - 2026-09-02

### Added

- The harness can change files. Two tools, both confined to the job root and both
  behind `harness.allow_write` (default **false**), which is deliberately separate
  from `harness.enabled`: turning the harness on to ask it questions should never
  quietly grant it the ability to edit your checkout.
  - **`edit` replaces an exact piece of text and refuses when that text is not
    unique.** The refusal is the reason the tool exists. A replacement that
    silently takes the first of three matches produces a change that passes ruff,
    pyright and pytest and does the wrong thing — worse than the whole-file
    rewrite it replaces, because that one fails loudly.
  - The refusal carries the match count **and the lines around each match**, so
    the model can widen its anchor without reading the file again. A stale anchor
    that matches nothing is refused too, and says the file may have changed.
  - `write` writes a whole file and refuses to replace an existing one unless
    `overwrite` is passed. Creating and replacing are different intentions, not
    the same call with different luck.
  - Every change is written into the session as a unified diff **before** the file
    is touched, so a process killed mid-write leaves a record of what it was about
    to do rather than a changed file and no explanation.
  - The result counts reads against changes. A job that changed three files having
    read none is the invented-module failure in a new hat, and the number is what a
    later phase will weigh before validating a generation.
  - `/harness do "<objective>" [path]` in the console, printing each diff as it
    lands. `/harness ask` is unchanged and is not even given the write tools.
- A tool call the model writes as prose is no longer mistaken for its final
  answer. On the native front end an answer with no tool calls normally ends the
  job; a model that has tools and still describes one in text is now reminded
  once, because a job that ends holding the answer to its own problem is the worst
  way to end. Observed on llama3.1:8B.

## [0.1.0-alpha.2] - 2026-09-02

### Added

- A model can now look at the project before it answers. `ModelProvider` gained a
  second way to be asked something — `chat(messages, tools)` alongside
  `generate(prompt)` — and `harness.py` runs the loop over it: send the transcript
  and the tool schemas, run what the model asked for, append the results, ask
  again, until it answers without calling a tool.
  - Three tools, all read-only: `read` (line numbers, offset and limit), `grep`
    and `ls`. Every one resolves its path against the job root, proves the result
    is inside it, and then asks the same `FilesystemPolicy` a skill asks. The
    check lives in the tool, so a fourth tool cannot arrive unguarded.
  - **Models with no tool calling drive the same tools through a one-line JSON
    protocol in plain text.** Most models that fit on a small card have none, so a
    harness that required it would not run on the hardware this project targets.
  - Tool output is truncated by the tool, which says how many lines it withheld
    and which offset asks for them. The alternative is the model server dropping
    the oldest end of the prompt — the objective — with nothing said.
  - A job that runs out of steps or wall clock ends as `capped`, not `failed`. It
    did not go wrong, it ran out of room, and a caller that treats those the same
    throws away work for the budget's fault.
  - Every job writes one JSONL file under `.runtime/harness/`, flushed as it runs,
    so a job that hangs or is killed still leaves the whole story up to that point.
  - `/harness ask "<question>"` in the console, off unless `harness.enabled`.
  - `grep` skips `.git`, `.venv` and their kind by comparing only the path parts
    **below the job root**. Matching the absolute path would have meant a checkout
    living under a directory called `bin` or `dist` had every file skipped and was
    told "no match" for code plainly there — which is how the test found it.
  - This is the seam the Evolver's single-file mutation contract is waiting for; it
    is deliberately not wired into the pipeline yet.

- The Evolver is shown the codebase before it is asked to change it. A new
  `evomesh.codebase` module surveys the package, resolves the import graph, and puts
  a map in the mutation prompt: which modules are load-bearing, and which are dead.
  - Until now the prompt carried the agent's memory and nothing about the project, so
    the model invented plausible new modules. Nothing imported them, so none of their
    code ever ran; ten accumulated, 431 lines of them.
  - The instruction now names a new file as the wrong answer and points at two real
    options: improve a module that runs, or edit one so it uses a dead module.
  - The repair prompt gets the map too, because "this module is unreachable" cannot be
    fixed by looking at the unreachable file.
- Unreachable modules fail validation. `tests/test_reachability.py` walks the import
  graph and fails on anything nothing imports and nothing runs, so such a candidate
  enters the repair stage instead of landing.
  - A ratchet, not a cleanup order: the modules that were already dead are listed in
    `docs/evolution/known-dead-modules.txt` and tolerated, anything new is not. Wiring
    one up simply makes its line there stale, which is what lets a one-file mutation
    fix it.
- Every generation writes `docs/evolution/<number>.md` into its own commit, with an
  index beside it: the objective, each file it touched and the reason the model gave,
  each self-repair, and the validation commands with their exit codes and output.
  Reasoning that lives only in a local database is reasoning nobody can review.

- A landed generation is now published. It is committed under the mesh's own identity
  (`Mesh Evo Agent` by default, configurable as `git.author_name`/`author_email`) and
  pushed to the remote, so the agent's work is distinguishable from a human's in the
  history and does not stop at the local disk.
  - The identity is passed to git per invocation: the mesh never edits your checkout's
    configuration, and it signs correctly on a machine with no `user.name` at all.
  - The push is the last step, never a gate. A missing remote, a detached HEAD, or
    credentials git cannot supply leave the generation landed and report the reason
    under `published:` in `/evolution status`.
  - `git.auto_push: false` keeps generations local.
- `evolution.auto_restart` (on by default) restarts the mesh into the generation it just
  landed. Until this, promotion put new code in the tree and the process went on running
  the old code until a human noticed the flag.
  - The process exits with code **86**, which means *start me again*; both the Control
    Center and `start-evomesh-console.bat` bring it back up, and the console launcher
    re-syncs dependencies first.
  - The durable `restart_required` flag is still written before the exit, so the rollback
    path survives a process that does not come back.
  - `evolution.restart_delay_seconds` (default 5) lets the cycle that promoted the
    generation finish writing its summary to every channel first.
  - `/restart` asks for the same thing by hand.
- A Telegram bot as a second console. Messages route through the same command router the
  Control Center uses, so `/status`, `/agents`, `/evolution status`, `/chat <agent>` and
  plain conversation behave identically from a phone.
  - Configured from the Control Center's Settings tab: paste the BotFather token, enable
    it, save. Saving while the mesh runs offers the restart that picks it up.
  - An empty `allowed_chat_ids` with `adopt_first_chat` on lets the first `/start` claim
    the bot -- the only way to learn a chat id without sending a human hunting for it.
    Every later stranger is turned away by id.
  - The update offset is persisted, so the command that triggered an automatic restart is
    not replayed by the process that comes back up.
  - `announcements: true` reports promotions and restarts unprompted.
  - `/exit` is refused from Telegram: the control port is localhost-only, so a shutdown
    from a phone would leave nothing running that could be asked to start again.

- Telegram is managed from the Control Center rather than only configured there.
  - **Test token** asks Telegram itself with the token in the box, needing neither a
    save nor a running mesh, so a bad token is caught before a restart is spent on it.
  - **Live status**, **Allow chat id** and **Revoke chat id** drive the new
    `/telegram status|test|allow|revoke` console commands against the running mesh.
  - A chat that claimed the bot at runtime is stored in the database, not the config
    file, so the settings tab could never show it. `/telegram status` lists every
    allowed chat, adopted ones included, and says whether the poller is connected.

### Fixed

- The Control Center checks whether the mesh is alive continuously instead of once at
  startup. It probed a single time on open, and any mesh that appeared later -- one
  started from the launcher script, one that restarted itself -- was reported as STOPPED
  indefinitely while plainly running. The status now carries the time it was last
  verified.
- Saving settings from the Control Center no longer resets the settings it does not show.
  It built a fresh configuration object from the visible fields, so one save silently
  reverted the evolution objective, `auto_promote`, `max_repairs`, the prompt budgets,
  the runtime cadence and every provider timeout to their defaults. The editor now
  writes over the file that is there.

- SQLite runs in WAL with a 30s busy timeout. Every agent loop writes through its own
  connection, so the default rollback journal had writers taking exclusive locks on each
  other and a contended write surfaced as `database is locked` after five seconds --
  which reached a human as a failed test rather than a busy disk.
- Validation gives pytest its own `--basetemp` inside the candidate. On a host where
  the shared temp root is not writable by the running user, every `tmp_path` test
  errored at fixture setup, so no candidate could pass validation and each one was
  reported as failed for something it did not do.
- A validation failure that names a host problem -- a permission error, a full disk,
  an unreachable network -- no longer enters the repair stage or condemns the
  candidate. It is reported as not validated, and the candidate keeps its status.

### Added

- Promotion now lands the generation. The candidate's commit is cherry-picked onto the
  checkout the mesh runs from, so a promoted generation is code in the tree rather than
  a number in a metadata file.
  - Never applied over uncommitted changes in the checkout, and a pick that conflicts is
    aborted rather than left half-applied. Under a promotion policy either refusal parks
    the candidate for a human instead of discarding it.
  - `/evolution rollback` resets the tree to the commit the last promotion replaced.
  - `/evolution status` reports `RESTART REQUIRED` while the tree is ahead of the running
    process; starting the mesh clears it. The restart is deliberately manual -- a
    rollback path cannot live inside the process it may have to rescue.
  - Candidate scratch (`MUTATION_OBJECTIVE.md`, validation records) is ignored, so it
    never rides along into a promoted commit.
- `evolution.auto_promote` closes the loop. A candidate that validated is promoted, one
  that failed is discarded, and the next pass starts without asking anyone.
  - The policy acts only on a verdict validation produced. With validation off, or when
    the host blocked the run, the candidate still waits for a human.
  - Promotion moves supervisor metadata; it does not yet swap the code the running mesh
    executes.
- The evolution pipeline repairs its own candidates. A failed validation now enters a
  `repair` stage instead of going straight to a verdict, and re-validates afterwards.
  - Ruff's own `--fix` runs first and costs no model call, so a generation is no longer
    lost to an autofixable lint rule; only what survives it is sent to the model.
  - A model repair is prompted with the failing command, its real output, and the file
    the last change wrote, and must return one whole corrected file.
  - Repairing stops on evidence, not just on a budget: an identical failure means the
    repair changed nothing, and the candidate goes to the human as it stands.
  - `evolution.max_repairs` (default 2) bounds the attempts; `0` restores the previous
    single-shot behaviour.
  - A model that cannot author a usable repair reports the failure rather than resetting
    the pipeline, which previously stranded the candidate and opened another.
  - `/evolution status` reports the repair count, and the verdict reads
    `validation passed after 1 repair attempt`.
- Real BDI cognition. Agents run the Rao and Georgeff practical-reasoning loop:
  perceive, revise beliefs, generate options, commit to an intention with a plan, and
  execute one step of it per cycle.
  - Beliefs are keyed and revisable, so a percept replaces the belief it contradicts
    instead of stacking a near-duplicate beside it.
  - Behaviors can generate desires from what they now believe. Guardian wants to
    investigate when it perceives an agent stopped, and discharges that goal when the
    mesh recovers.
  - Intentions are commitments: a plan is adopted once and executed across cycles.
    Reconsideration is triggered only by the plan running out, the goal closing, a
    higher-priority goal appearing, or a revision of a belief the plan declared it
    depends on -- and never costs a model call.
  - An intention can be held rather than consumed while it waits on an external
    decision, so a parked agent keeps one commitment instead of one per cycle.
  - Means-ends reasoning is a plan library first, the model only as a fallback.
    Guardian, Evaluator and the Evolver now reason with no model reachable at all, and
    a generic agent makes one planning call per goal rather than one per cycle.
  - The Evolver's mutation pipeline is now a plan, visible as a checklist in
    `/intentions`, and `/evolution promote|discard` makes it reconsider on its own.
- Console commands `/beliefs` and `/intentions`.
- Beliefs and the committed plan are carried in every prompt, within their own budget
  (`runtime.beliefs_chars`), and written to `context.md`.
- Goal-driven execution cycle for every agent. Each agent now runs a mailbox loop and a
  cycle loop that advances its highest-priority open goal through perceive, deliberate,
  act, and reflect, on a configurable interval with a staggered first tick.
- File-backed agent memory: `workspace/agents/<agent>/memory.md` for durable facts and
  `context.md` for working context, plus a shared `workspace/context.md` world snapshot.
  Memory is compacted into a summary once it outgrows its budget.
- Character budgets for every prompt (`runtime.prompt_chars`, `memory_chars`,
  `context_chars`, `inbox_chars`), sized for small local models.
- Replaceable per-agent behaviors, with dedicated ones for Guardian, Evaluator, Evolver,
  and Architect.
- First-class goals with priority, status, attempts, progress notes, and a recurring flag.
- Observed agent phase (`offline`, `starting`, `idle`, `thinking`, `acting`,
  `waiting-human`, `error`) reported separately from the persisted desired status.
- Console commands `/cycle`, `/goals`, `/goal add|done|drop`, `/memory`, `/context`, and
  `/evolution start|promote|discard|rollback`.
- Settings sections `runtime:` and `evolution:`, and a `workspace_path`.
- Localhost control channel so the Windows Control Center can attach to an already running mesh.
- Persistent Control Center and mesh logs under `.runtime/logs`.
- Dynamic Ollama model dropdowns in both Agents and Settings, populated directly from the configured Ollama instance.
- Per-system-agent provider/model settings for Architect, Guardian, Evaluator, and Evolver.

### Changed

- Agent Architect no longer runs a six-question interview. It derives a complete draft from
  one sentence, uses at most one model call to improve the wording, and is refined by
  instruction rather than by answering questions.
- The Environment Evolver is a staged pipeline that opens a candidate generation on its
  first cycle and advances one stage per tick, parking at `await-human` until the candidate
  is promoted or discarded.
- System agents boot with a standing goal instead of an empty mind.

### Fixed

- Discarding a candidate generation left its directory behind while removing its
  metadata, so the next candidate collided with the leftovers and failed to open.
- The Environment Evolver never did anything: `EnvironmentEvolver` was never constructed by
  the runtime, so the agent was a chat echo with no pipeline behind it.
- Agent status was meaningless. Agents were persisted as `active` whether or not a loop was
  running, Architect was reported `active` while explicitly excluded from all loops, and a
  stale status from a previous run survived a restart. Status and phase are now separate,
  and an agent that cannot start reports `offline` with the reason.
- Agents lost their memory partway through a goal, because prompts carried no memory and
  were truncated by the model server from the oldest end.
- A goal could be closed by a small model rubber-stamping `DONE: yes` on its first look at
  it, leaving the agent idle forever.
- Guardian reported running agents as offline: its view of the mesh was snapshotted when it
  started rather than resolved per cycle, and it printed raw agent ids.
- A candidate generation that was never validated was recorded as failed.
- Candidate generations contained the live SQLite database and every agent's memory,
  because the workspace copy only skipped directories named `data` and `workspace`. It
  now excludes the configured state paths explicitly, which also removes a race between
  the copy and the running mesh.
- Every agent came back `stopped` after a restart: shutting the mesh down persisted the
  same status a human `/agent stop` does. A shutdown now leaves desired status untouched,
  and changing an agent's model no longer disables it.
- Agent Architect accepted whatever a small model returned, so a draft could be degraded
  to `name: D:/notes`, `name: agent`, or a purpose of `read`. Model output must now be a
  plausible improvement or the derived value stands.
- Reasoning-model `<think>` blocks leaked into stored memory and subsequent prompts.
- Saving settings from the Control Center silently dropped `workspace_path`, `runtime:`,
  and `evolution:` from `evomesh.yaml`.
- Robust Windows `uv.exe` discovery and actionable startup diagnostics.
- Explicit button foreground/background colors for readable labels across Windows themes.
- Settings model dropdown no longer closes while Ollama models are refreshed.

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
