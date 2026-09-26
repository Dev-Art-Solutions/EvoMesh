from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import re
import threading
from pathlib import Path

from evomesh.config import load_settings
from evomesh.console import ConsoleChannel
from evomesh.control import CONTROL_HOST, CONTROL_PORT, ControlServer, wait_for_console_or_shutdown
from evomesh.environment import Environment, shutdown_step
from evomesh.processes import kill_running
from evomesh.singleton import AlreadyRunningError, SingletonLock
from evomesh.telegram import TelegramChannel

logger = logging.getLogger(__name__)

# The exit code that means "start me again, I have new code to run". Anything
# supervising the process -- the Control Center, start-evomesh-console.bat --
# treats it as a restart rather than a crash. It is deliberately not 0: a plain
# success must never be mistaken for a request to come back up.
RESTART_EXIT_CODE = 86

# A second instance refusing to start is not a crash -- there is nothing
# broken to back off from, the other process is already doing the job -- but
# it is not success either, so a launcher (or a human) can tell the two
# apart from the exit code alone.
ALREADY_RUNNING_EXIT_CODE = 2
# Once shutdown starts, the process is gone within this long whatever hangs:
# a restart that never exits is a mesh that never comes back. Durable state
# is SQLite, committed per transition; nothing in flight is resumed anyway.
SHUTDOWN_DEADLINE_SECONDS = 120.0

# --log-file grew unbounded before this: 24.8MB and 166542 lines on the
# day this was added, months into one continuous run, with nothing ever
# rotating it. A plain FileHandler has no ceiling on its own; this caps it
# at MESH_LOG_MAX_BYTES per segment and keeps MESH_LOG_BACKUP_COUNT old
# ones (mesh.log.1, .2, ...) rather than one file that grows forever. The
# Control Center's own tailer (EvoMeshRuntimeProcess.StartTailingMeshLog)
# already resets to the top when it sees the file shrink -- exactly what a
# rotation does -- so this needed no matching change on that side.
MESH_LOG_MAX_BYTES = 20 * 1024 * 1024
MESH_LOG_BACKUP_COUNT = 5


# A Telegram bot token is part of every Bot API URL (/bot<id>:<secret>/...),
# and httpx logs each request's URL at INFO -- found 2026-09-25: 13643 lines
# of mesh.log carried the live token in clear text.
SECRET_PATTERN = re.compile(r"bot\d+:[\w-]{20,}")
# Loggers whose INFO lines are per-request noise (and carry those URLs).
QUIET_LOGGERS = ("httpx", "httpcore")


class RedactSecrets(logging.Filter):
    """Mask bot tokens in any record, whichever logger or exception carries one."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if SECRET_PATTERN.search(message):
            record.msg = SECRET_PATTERN.sub("bot<redacted>", message)
            record.args = None
        return True


async def _restart_when_asked(environment: Environment, shutdown: asyncio.Event) -> None:
    """Turn a landed generation into a clean shutdown the supervisor can act on.

    The delay is not cosmetic. The cycle that promoted the generation is still
    writing its summary to the console, the control connection and Telegram, and
    a human who never sees why the process went away reads the restart as a
    crash.
    """
    await environment.restart_requested.wait()
    reason = environment.restart_reason or "a new generation landed"
    logger.info("Restarting: %s", reason)
    notice = environment.announce(f"EvoMesh is restarting: {reason}.")
    await shutdown_step("announcing the restart", notice)
    await asyncio.sleep(max(0.0, environment.settings.evolution.restart_delay_seconds))
    shutdown.set()


async def application(
    config: Path | None = None,
    *,
    headless: bool = False,
    control_host: str = CONTROL_HOST,
    control_port: int = CONTROL_PORT,
    log_file: Path | None = None,
) -> int:
    settings = load_settings(config)
    # Headless is how the Windows Control Center launches this process, with
    # its own console suppressed and stdout/stderr no longer piped to it (a
    # redirected pipe would make the mesh's logging -- and so its single
    # asyncio loop -- block whenever the Control Center's reader fell behind
    # or stopped). A stdout handler in that mode has nothing valid to write
    # to and no reader anyway; --log-file is the only sink that matters there.
    handlers: list[logging.Handler] = [] if headless else [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=MESH_LOG_MAX_BYTES,
                backupCount=MESH_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        )
    if not handlers:
        handlers.append(logging.NullHandler())
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format='{"time":"%(asctime)s","level":"%(levelname)s","message":"%(message)s"}',
        handlers=handlers,
        force=True,
    )
    for handler in handlers:
        handler.addFilter(RedactSecrets())
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    lock = SingletonLock(settings.lock_path) if settings.single_instance else None
    if lock is not None:
        try:
            lock.acquire(wait_seconds=5)
        except AlreadyRunningError as exc:
            logger.error(str(exc))
            return ALREADY_RUNNING_EXIT_CODE
    try:
        environment = Environment(settings)
        await environment.start(start_agent_loops=True)
        shutdown = asyncio.Event()
        control = ControlServer(environment, shutdown, control_host, control_port)
        telegram = TelegramChannel(environment, settings.telegram)
        environment.channels["telegram"] = telegram
        restart_watch = asyncio.create_task(_restart_when_asked(environment, shutdown))
        telegram_task = asyncio.create_task(telegram.run()) if telegram.configured else None
        try:
            await control.start()
            if headless:
                await shutdown.wait()
            else:
                await wait_for_console_or_shutdown(ConsoleChannel(environment), shutdown)
        finally:
            _start_shutdown_watchdog(environment)
            restart_watch.cancel()
            if telegram_task is not None:
                telegram.stop()
                telegram_task.cancel()
            for task in (restart_watch, telegram_task):
                if task is not None:
                    await shutdown_step(f"stopping {task.get_name()}", _settled(task))
            await shutdown_step("closing the control port", control.stop())
            await environment.stop()
        return RESTART_EXIT_CODE if environment.restart_requested.is_set() else 0
    finally:
        if lock is not None:
            lock.release()


async def _settled(task: asyncio.Task[None]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        pass


def _start_shutdown_watchdog(environment: Environment) -> None:
    """A thread, not a task: it has to fire even if the event loop itself is
    what hangs. Past the deadline it kills the children still running and
    exits with the code the launcher expects -- 86 for a restart."""
    code = RESTART_EXIT_CODE if environment.restart_requested.is_set() else 0

    def expire() -> None:
        logger.error(
            "shutdown did not finish in %.0fs; exiting with %s anyway",
            SHUTDOWN_DEADLINE_SECONDS,
            code,
        )
        kill_running()
        logging.shutdown()
        os._exit(code)

    timer = threading.Timer(SHUTDOWN_DEADLINE_SECONDS, expire)
    timer.daemon = True
    timer.start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the EvoMesh local environment")
    parser.add_argument("--config", type=Path, help="Path to evomesh.yaml")
    parser.add_argument("--headless", action="store_true", help="Run only the control server")
    parser.add_argument("--control-host", default=CONTROL_HOST)
    parser.add_argument("--control-port", type=int, default=CONTROL_PORT)
    parser.add_argument("--log-file", type=Path, help="Optional persistent runtime log file")
    args = parser.parse_args()
    code = asyncio.run(
        application(
            args.config,
            headless=args.headless,
            control_host=args.control_host,
            control_port=args.control_port,
            log_file=args.log_file,
        )
    )
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
