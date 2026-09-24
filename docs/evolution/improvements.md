# Improvement backlog

The Evolver's source of **substantive** objectives -- real changes to how EvoMesh behaves.
It is checked before either maintenance backlog (dead modules, untested exports) on every
generation opened under the standing goal; see `codebase.open_improvements` and
`EnvironmentEvolver.substantive_objective`.

Format: one `- [ ] <title>` line per item, followed by indented lines saying *where* and
*why* -- name the file and the function, and say what is wrong today -- and then its
**steps**, one per line, each one change to one existing function, method or constant:

    - [ ] <short imperative title>
        <what is wrong today and how you know>
        1. [ ] src/evomesh/<module>.py `<Name or Class.method>` -- <the change, one sentence>
        2. [ ] ...

Steps are what makes an item fit a small local model. A harness job's whole transcript is
`harness.transcript_chars` (12000); a job that has to find its own way through two large
files loses the first by the time it edits the second. A step's job is handed its anchor's
current source up front instead, and nothing else to navigate. Each step is one generation;
the pipeline ticks it (`1. [x]`) in the commit that landed it, and the item itself with its
last step. An item written without steps is split into steps by a *plan* generation first
(`codebase.plan_task`) -- only if that fails three times is it handed out whole. A step (or
a plan) three recent generations failed is set aside until those attempts age out of the
look-back window. Do not tick anything by hand unless you did the work yourself.

- [x] Back off exponentially when Telegram polling keeps failing, and log what actually failed
    `TelegramChannel`'s poll loop in src/evomesh/telegram.py catches `httpx.HTTPError` and
    logs `"Telegram poll failed, retrying: %s", exc`, then sleeps a fixed 5 seconds. Two real
    problems, both visible in mesh.log: an `httpx.ReadTimeout` stringifies to an empty string,
    so dozens of lines read just `retrying: ` with no cause; and a network outage retries every
    5s forever. Log `type(exc).__name__` alongside the message, and double the delay on each
    consecutive failure (5s, 10s, 20s ... capped at 120s), resetting to 5s after one success.
- [x] Let a restarted mesh wait briefly for the previous process to release its lock
    `SingletonLock.acquire` in src/evomesh/singleton.py fails immediately when the lock is held.
    After a promotion the process exits with code 86 and the launcher starts it again at once --
    mesh.log shows 25 `another EvoMesh process already holds the lock` refusals, the new process
    racing the old one that is still shutting down. Give `acquire` an optional
    `wait_seconds: float = 0.0` that retries the non-blocking lock every 0.25s until that much
    time has passed before raising `AlreadyRunningError`, and pass a few seconds from the
    caller in src/evomesh/__main__.py. The default of 0 keeps today's behavior for everyone else.
- [x] Stop uv's VIRTUAL_ENV warning from polluting every validation and repair output
    Validation and the `ruff --fix` autofix run `uv run ...` inside a candidate directory
    (src/evomesh/evolution.py, `CandidateValidator` / `CandidateRepairer.autofix`) while the
    mesh's own `VIRTUAL_ENV` is still set in the environment, so uv prints
    `warning: VIRTUAL_ENV=... does not match the project environment path` at the top of every
    output -- and that output is exactly what the repair prompt shows the model as "the
    failure". Add an optional `env: Mapping[str, str] | None = None` to `run_command` in
    src/evomesh/processes.py (passed through to `subprocess.run`), and have the candidate
    commands pass a copy of `os.environ` without `VIRTUAL_ENV`.
- [ ] Show the Evolver's recent success rate in `/evolution status`
    Nothing tells a human whether evolution is actually working: 363 of the last ~550
    generations were discarded "not validated" and that was only found by grepping mesh.log.
    Nothing keeps that count today: `GenerationSupervisor.promote` and
    `GenerationSupervisor.discard` (src/evomesh/evolution.py) both drop the candidate's entry
    from supervisor.json and remember nothing about how it ended. Have both also append the
    outcome to a short list in the metadata (the last 20), and add one line built from it to
    the `/evolution status` output (`ConsoleChannel._command_evolution` in
    src/evomesh/console.py), e.g. `recent: 4 promoted, 16 discarded of the last 20`.
    1. [ ] src/evomesh/evolution.py `GenerationSupervisor.promote` -- append the outcome `promoted` to a capped list (keep the last 20) stored in the supervisor metadata, creating the key on first use.
    2. [ ] src/evomesh/evolution.py `GenerationSupervisor.discard` -- append the outcome `discarded` to that same last-20 list in the supervisor metadata, alongside what `promote` writes.
    3. [ ] src/evomesh/console.py `ConsoleChannel._command_evolution` -- add one line summarising the last ~20 outcomes (e.g. `recent: 4 promoted, 16 discarded of the last 20`), built from the list `promote` and `discard` maintain.
- [x] Actually pass the VIRTUAL_ENV-free environment to the candidate's uv commands
    Generation 1370 added `env: Mapping[str, str] | None = None` to `run_command` in
    src/evomesh/processes.py, but nothing passes it yet, so uv still prints
    `warning: VIRTUAL_ENV=... does not match the project environment path` into every
    validation and autofix output. In src/evomesh/evolution.py, the two `uv` calls --
    `CandidateValidator` (`run_command(uv, *command[1:], cwd=generation.path)`) and
    `CandidateRepairer.autofix` (`run_command(uv, *self.AUTOFIX[1:], cwd=generation.path)`)
    -- should pass `env={k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}`.
    Put that dict behind one small helper so both call sites share it.
