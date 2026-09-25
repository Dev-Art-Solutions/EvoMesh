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

`- [-]` marks an item a human rejected (a problem the code does not have, or a change not
worth making): it is never handed out and scouts will not propose it again. Say why on
the line below the title.

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
- [x] Show the Evolver's recent success rate in `/evolution status`
    Nothing tells a human whether evolution is actually working: 363 of the last ~550
    generations were discarded "not validated" and that was only found by grepping mesh.log.
    Nothing keeps that count today: `GenerationSupervisor.promote` and
    `GenerationSupervisor.discard` (src/evomesh/evolution.py) both drop the candidate's entry
    from supervisor.json and remember nothing about how it ended. Have both also append the
    outcome to a short list in the metadata (the last 20), and add one line built from it to
    the `/evolution status` output (`ConsoleChannel._command_evolution` in
    src/evomesh/console.py), e.g. `recent: 4 promoted, 16 discarded of the last 20`.
    1. [x] src/evomesh/evolution.py `GenerationSupervisor.promote` -- append the outcome `promoted` to a capped list (keep the last 20) stored in the supervisor metadata, creating the key on first use.
    2. [x] src/evomesh/evolution.py `GenerationSupervisor.discard` -- append the outcome `discarded` to that same last-20 list in the supervisor metadata, alongside what `promote` writes.
    3. [x] src/evomesh/console.py `ConsoleChannel._command_evolution` -- add one line summarising the last ~20 outcomes (e.g. `recent: 4 promoted, 16 discarded of the last 20`), built from the list `promote` and `discard` maintain.
- [x] Actually pass the VIRTUAL_ENV-free environment to the candidate's uv commands
    Generation 1370 added `env: Mapping[str, str] | None = None` to `run_command` in
    src/evomesh/processes.py, but nothing passes it yet, so uv still prints
    `warning: VIRTUAL_ENV=... does not match the project environment path` into every
    validation and autofix output. In src/evomesh/evolution.py, the two `uv` calls --
    `CandidateValidator` (`run_command(uv, *command[1:], cwd=generation.path)`) and
    `CandidateRepairer.autofix` (`run_command(uv, *self.AUTOFIX[1:], cwd=generation.path)`)
    -- should pass `env={k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}`.
    Put that dict behind one small helper so both call sites share it.
- [x] Kill the whole process group when run_command's timeout fires
    `run_command` is meant to run an external command "without leaving a transport behind," but on timeout it kills only the direct child; any grandchild the child spawned (a backgrounded process, a process group) becomes orphaned and keeps running. The `subprocess.run(...)` call has no `start_new_session`/`preexec_fn`, and the `except subprocess.TimeoutExpired` handler returns a fixed result without touching the group.
    >             completed = subprocess.run(  # noqa: S603 - the caller supplies the program
    >                 [program, *arguments],
    >                 cwd=str(cwd) if cwd else None,
    >                 stdout=subprocess.PIPE,
    >                 stderr=subprocess.STDOUT,
    >                 timeout=timeout_seconds,
    >                 check=False,
    >                 env=env,
    >             )
    >         except subprocess.TimeoutExpired as exc:
    >             return 124, exc.output or b"", True
    1. [x] src/evomesh/processes.py `run_command` -- add `start_new_session=True` to the `subprocess.run(...)` call so the child leads its own process group/session, separate from the worker thread.
    2. [x] src/evomesh/processes.py `run_command` -- in the `except subprocess.TimeoutExpired` handler, terminate that whole process group (via `os.killpg`) before returning the timeout result, so children/grandchildren don't outlive the parent.
- [x] Make the watcher's timeout actually kill the command, not just stop awaiting it
    `AgentWatcher._tick` calls `run_command` with no `timeout_seconds`, and `_loop` relies solely on `asyncio.wait_for` to enforce `self.timeout_seconds`. Per the `run_command` docstring (processes.py:58-67), cancelling the await on the worker thread does not stop the blocking `subprocess.run` running inside that thread, so a timed-out child keeps running for real — the watcher's own timeout never actually terminates the leaked process.
    > `await asyncio.wait_for(self._tick(), timeout=self.timeout_seconds)`
    > `result = await run_command(self._argv[0], *self._argv[1:], cwd=self.cwd)`
    1. [x] src/evomesh/watchers.py `AgentWatcher._tick` -- pass `timeout_seconds=self.timeout_seconds` into the `run_command` call so `subprocess.run` terminates the child itself on timeout (via its `timeout=`), turning the child into a real stop rather than a leaked waiter; the `wait_for` in `_loop` then serves only as a safety net.
- [x] Set the watcher's command timeout from settings instead of the hardcoded 20s default
    The `AgentWatcher.__init__` accepts an optional `timeout_seconds` but defaults it to `DEFAULT_TIMEOUT_SECONDS = 20.0`, and no caller in `mesh.py` ever passes it — so a watcher with no settings-backed timeout override is stuck at 20s. The running mesh logged two `timed_out` results for the exact command `['python', '...\\agent-templates\\news-watcher\\scripts\\watch_news.py']`; that script cycles on `while True:` with `time.sleep(60)`, so it can never finish within 20s and will always time out.
    > timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    1. [x] src/evomesh/watchers.py `AgentWatcher.__init__` -- read `timeout_seconds` out of the watcher settings (defaulting to `DEFAULT_TIMEOUT_SECONDS` only when unset) instead of hard-coding the module constant, and pass it through from `Mesh.ensure_watcher` so the per-watcher command timeout comes from config, not a fixed number.
- [x] Make cron treat weekday `7` as Sunday
    `_FIELD_RANGES` in `src/evomesh/cron.py` declares the weekday field range as `(0, 7)`, so `7` is a valid Sunday value that `parse()` accepts. But `_day_matches` computes `dow_ok` with `(day.weekday() + 1) % 7`, which maps Sunday (Python weekday 6) to `0` and never to `7` — so a weekday set containing only `7` never matches, and `next_after` then gives up with "never matches within four years" instead of firing on Sundays.
    > `dow_ok = ((day.weekday() + 1) % 7) in weekday_set`
    1. [x] src/evomesh/cron.py `parse` -- after building `weekday_set` (line 57), fold the Sunday value `7` into `0` (e.g. `if 7 in weekday_set: weekday_set.discard(7); weekday_set.add(0)`) so `0`/`7` are treated identically, keeping the set canonical for `_day_matches`/`next_after`.
- [x] Render week-plus durations in `humanize_duration` using the true second-counts per unit
    `humanize_duration` formats any duration of a week or more in a multi-part string, but its four `divmod` divisors descend one unit level too far: the weeks bucket divides by `days*hours*minutes` (86,400 s = one day), the days bucket by `days*hours` (1,440 s), the hours bucket by `hours` (60 s = one minute), and the minutes bucket then divides by `minutes` (60) a value that is already `< 60`, so minutes is always `0`. A timestamp two weeks ago (`delta` = 1,209,600 s) therefore renders as `"14w ago"` instead of `"2w ago"`, and the branch is only reached for the `humanize_timestamp` "ago" text and for the `harness_session` elapsed / mean-elapsed values once they cross a week. No test covers it (`tests/test_humanize.py` only exercises sub-second durations and sub-1024 byte sizes).
    >         weeks, remaining = divmod(remaining, _DAYS * _HOURS * _MINUTES)
    1. [x] src/evomesh/humanize.py `humanize_duration` -- replace the four `divmod` divisors with the correct number of seconds per unit (weeks → `_DAYS * _HOURS * _MINUTES * _WEEKS`, days → `_DAYS * _HOURS * _MINUTES`, hours → `_HOURS * _MINUTES`, minutes → `_MINUTES`) so the bucket counts are no longer shifted by one level.
- [-] Log the exception detail when a watcher command times out
    Rejected 2026-09-25: the TimeoutError from wait_for carries no message, the log line already names the command, and `exc=` is not a logging argument.
    The watchdog log line is empty of cause: the mesh logged "Watcher command timed out" but the handler that emits it captures the `TimeoutError` yet logs nothing about it, so operators can't tell what actually failed. This is a distinct gap from the already-done timeout-kill fix — nothing about logging the cause has been addressed.
    >             except TimeoutError:
    >                 logger.warning("Watcher command timed out: %s", self._argv)
    1. [ ] src/evomesh/watchers.py `AgentWatcher._loop` -- add `exc=e` to the timeout log so the `TimeoutError` (with its traceback/cause) is recorded.
- [-] Delete the lock file in SingletonLock.release()
    Rejected 2026-09-25: the lock is flock/msvcrt on a file whose existence means nothing. Unlinking it lets a restarting process that already holds it open lock an orphaned inode while a third creates a fresh file (two meshes on POSIX), and on Windows unlink of an open file raises PermissionError.
    The singleton lock writes the file at self._path (`.runtime/evomesh.lock` from config, created via `self._path.mkdir(parents=True, exist_ok=True)` then `self._path.write_bytes(b"\0")` in acquire()), but release() only unlocks and closes the file handle — it never unlinks the file, so the lock file lingers forever after each process exits.
    > `self._path.write_bytes(b"\0")`
    > `handle.close()`
    1. [ ] src/evomesh/singleton.py `SingletonLock.release` -- after unlocking and before closing, delete the lock file if it exists (e.g. `try: self._path.unlink() except FileNotFoundError: pass`), so the stale lock file does not accumulate in `.runtime/`.
- [-] Give `ToolDefinition.parameters_schema` the real JSON schema type of each parameter instead of always "string"
    Rejected 2026-09-25: ToolParameter has only name/description/required -- there is no `annotation` to map -- and every value is appended to argv as a string, so "string" is the true type.
    <every ToolDefinition.parameter becomes {"type": "string"} regardless of the model field — an `int`, `float`, `bool` or enum parameter is declared as a string. The model fields are pydantic types, so `param.annotation` gives the true type.>
    >                     param.name: {"type": "string", "description": param.description}
    >                     for param in self.parameters
    1. [ ] src/evomesh/tools.py `ToolDefinition.parameters_schema` -- map each parameter's declared field annotation (`param.annotation`) to the matching JSON-schema `"type"` (e.g. `int` -> "integer", `float` -> "number", `bool` -> "boolean", everything else -> "string") instead of hardcoding "string".
- [x] Make `agent_label` return the role itself for roles absent from `_AGENT_LABELS` instead of the hard-coded "agent"
    `agent_label(role)` maps known roles to labels but returns the string `"agent"` for anything not in `_AGENT_LABELS`; its callers (`agents.py`, `console.py`) pass `role=agent.type`, so any custom, new, or unknown agent type (e.g. `type="researcher") is rendered as the generic "agent" rather than as `researcher`.
    > `    return _AGENT_LABELS.get(role, "agent")`
    1. [x] src/evomesh/agent_label.py `agent_label` -- change the fallback of the `.get` on the final line from the literal `"agent"` to the `role` argument itself (e.g. `return _AGENT_LABELS.get(role, role)`).
- [x] Capitalize only the first letter when rendering an unknown phase, not the whole string
    `phase_label` mangles any phase string that contains uppercase letters or digits: `"E2E"` becomes `"E2e"`, `"2D"` becomes `"2d"`, because the fallback calls `.capitalize()`, which lowercases everything after the first character. That fallback exists precisely to render phases not in `_LABELS`, and the function is typed and called with arbitrary phase strings.
    >         return text.replace("_", " ").strip().capitalize() or "Unknown"
    1. [x] src/evomesh/phase_label.py `phase_label` -- in the `except KeyError` fallback, replace `.capitalize()` with an upper-case-only-first-char transform (e.g. `text[:1].upper() + text[1:]`) so trailing digits/uppercase are preserved.
- [x] Add backoff so a watcher that keeps timing out slows down instead of hammering the failing command every interval
    `AgentWatcher._loop` catches `TimeoutError`, logs one line, and then falls straight to `asyncio.sleep(self.interval_seconds)` and loops — so three consecutive timeouts (the logged failure) means three commands launched `interval_seconds` apart with no delay growing with the failure count, even though `run_command` has to tear down the whole process group after each one.
    >             except TimeoutError:
    >                 logger.warning("Watcher command timed out: %s", self._argv)
    >             except Exception:  # noqa: BLE001 - one bad tick must not end the watcher
    >                 logger.exception("Watcher command failed: %s", self._argv)
    >             await asyncio.sleep(self.interval_seconds)
    1. [x] src/evomesh/watchers.py `AgentWatcher._loop` -- keep a timeout streak (start at 0 in `__init__`): after a tick that finishes, set it to 0; in the `except TimeoutError:` branch add 1, capped at 5; then sleep `self.interval_seconds * 2 ** streak` instead of `self.interval_seconds`, so each consecutive timeout doubles the wait (up to 32x) and one good tick restores the normal interval.
- [x] Make `_sum` use a correctly-rounded float sum instead of a running total
    `_sum` accumulates values with `total += value`, so values of very different magnitudes lose precision to rounding error, which then propagates into `mean` (called on every elapsed record in `harness_session.py`). It is the sole summation primitive in the module and has no more accurate implementation; the existing test only checks `1.0+2.0+3.0 == 6.0`, which a correct sum also satisfies, so the inaccuracy is currently uncaught.
    >
    >     total = 0.0
    >     for value in values:
    >         total += value
    >     return total
    1. [x] src/evomesh/metrics.py `_sum` -- replace the manual `total += value` loop with `math.fsum(values)` (adding `import math` at the top) so the total — and therefore `mean` — is correctly rounded rather than accumulating rounding error.
- [x] Fix `phase_label` to return `None` for an empty/whitespace/`None` phase, matching its documented contract ("empty or `None` -> `None`")
    `phase_label` violates its own documented contract (stated in the module docstring at the top and quoted in `tests/test_phase_label.py`): for an empty or whitespace input it returns `"Unknown"` instead of `None`. The unknown-phase fallback `return text[:1].upper() + text[1:] or "Unknown"` evaluates to `"Unknown"` for `""` and `"   "` (an all-spaces input capitalizes to `""`, whose `or` falls through), so `phase_label("")` and `phase_label("   ")` both yield `"Unknown"`. This is verified: `phase_label("")` returns `'Unknown'` and `phase_label("   ")` returns `'Unknown'`, whereas the contract says both should return `None`. It is also the only human-facing phase renderer, with `src/evomesh/contracts.py` line 574 f-stringing `phase_label(self.phase)` into `CycleOutcome.__repr__`, so an empty phase should collapse to `None` (no phase) rather than the synthetic `"Unknown"` label. Note this is NOT the already-done "capitalize only the first character of the unknown phase" item (that concern — leaving the rest of the string untouched — is handled at lines 47-48 of `phase_label.py`); this is the empty/`None`-fallback path.
    >         return text or None
    1. [x] src/evomesh/phase_label.py `phase_label` -- in the unknown-phase fallback, replace `return text[:1].upper() + text[1:] or "Unknown"` with `return text or None` (so empty/whitespace/`None` yields `None`), keeping the unknown-but-non-empty case returning `"Unknown"`.
- [x] Validate a tool parameter's declared `type` when parsing the tool, rejecting unrecognized ones
    `parse_tool` reads each parameter's `type` from the tool's YAML and stores it on the `ToolParameter` model, but `ToolParameter` has no validation on it, so a malformed value (a typo like `striiing`, or `None`) is accepted silently and only surfaces later as an opaque pydantic error at runtime in `harness_tools.validator.validate_python`.
    > `parameters = [ToolParameter.model_validate(item) for item in raw_parameters]`
    1. [x] src/evomesh/tools.py `ToolParameter` -- after the existing fields, add a Pydantic validator that raises `TypeError("... 'type' is not a recognized Python type: ...")` when `type` isn't the name of a class that Pydantic can validate against (e.g. a value of `type='striiing'` or `type=None` is rejected at parse time).
- [x] Make `_download` catch `httpx` errors and raise a `TelegramError` instead of letting them propagate raw
    `_download` awaits the attachment with `self._client.get(url)` but has no `try/except`, so a `ReadTimeout`/`ReadError` escapes the poll loop (matching the `ReadError`/`ReadTimeout` poll failures logged since the file last changed). Every other HTTP call in the file is wrapped and re-raised as a `TelegramError` with a backoff (`except (httpx.HTTPError, ...) -> ... TelegramError(...)`), and the sibling `_send_document` does the same for uploads — but downloads are unhandled.
    >         response = await self._client.get(url)
    1. [x] src/evomesh/telegram.py `TelegramChannel._download` -- wrap the `await self._client.get(url)` in a `try/except (httpx.HTTPError) as exc:` block that logs the failure and re-raises a `TelegramError` with a retry delay, matching `_send_document`'s handling right next to it.
