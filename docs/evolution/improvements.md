# Improvement backlog

The Evolver's source of **substantive** objectives -- real changes to how EvoMesh behaves.
It is checked before either maintenance backlog (dead modules, untested exports) on every
generation opened under the standing goal; see `codebase.open_improvements` and
`EnvironmentEvolver.substantive_objective`.

Format: one `- [ ] <title>` line per item, followed by indented lines saying *where* and
*why* -- name the file and the function, and say what is wrong today. A small local model
gets one harness job (~60 steps) per attempt, so an item has to be doable in one or two
files. An item three recent generations failed is set aside until those attempts age out
of the look-back window. The pipeline ticks an item (`- [x]`) inside the very commit that
implemented it; do not tick one by hand unless you did the work yourself.

- [ ] Back off exponentially when Telegram polling keeps failing, and log what actually failed
    `TelegramChannel`'s poll loop in src/evomesh/telegram.py catches `httpx.HTTPError` and
    logs `"Telegram poll failed, retrying: %s", exc`, then sleeps a fixed 5 seconds. Two real
    problems, both visible in mesh.log: an `httpx.ReadTimeout` stringifies to an empty string,
    so dozens of lines read just `retrying: ` with no cause; and a network outage retries every
    5s forever. Log `type(exc).__name__` alongside the message, and double the delay on each
    consecutive failure (5s, 10s, 20s ... capped at 120s), resetting to 5s after one success.
- [ ] Let a restarted mesh wait briefly for the previous process to release its lock
    `SingletonLock.acquire` in src/evomesh/singleton.py fails immediately when the lock is held.
    After a promotion the process exits with code 86 and the launcher starts it again at once --
    mesh.log shows 25 `another EvoMesh process already holds the lock` refusals, the new process
    racing the old one that is still shutting down. Give `acquire` an optional
    `wait_seconds: float = 0.0` that retries the non-blocking lock every 0.25s until that much
    time has passed before raising `AlreadyRunningError`, and pass a few seconds from the
    caller in src/evomesh/__main__.py. The default of 0 keeps today's behavior for everyone else.
- [ ] Stop uv's VIRTUAL_ENV warning from polluting every validation and repair output
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
    Count promoted vs discarded generations among the recent candidates the supervisor already
    tracks (src/evomesh/evolution.py, `GenerationSupervisor`) and add one line to the
    `/evolution status` output in src/evomesh/console.py, e.g.
    `recent: 4 promoted, 16 discarded of the last 20`.
