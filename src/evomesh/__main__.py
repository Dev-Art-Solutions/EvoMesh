from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from evomesh.config import load_settings
from evomesh.console import ConsoleChannel
from evomesh.control import CONTROL_HOST, CONTROL_PORT, ControlServer, wait_for_console_or_shutdown
from evomesh.environment import Environment
from evomesh.telegram import TelegramChannel

logger = logging.getLogger(__name__)

# The exit code that means "start me again, I have new code to run". Anything
# supervising the process -- the Control Center, start-evomesh-console.bat --
# treats it as a restart rather than a crash. It is deliberately not 0: a plain
# success must never be mistaken for a request to come back up.
RESTART_EXIT_CODE = 86


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
    await environment.announce(f"EvoMesh is restarting: {reason}.")
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
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format='{"time":"%(asctime)s","level":"%(levelname)s","message":"%(message)s"}',
        handlers=handlers,
        force=True,
    )
    environment = Environment(settings)
    await environment.start(start_agent_loops=True)
    shutdown = asyncio.Event()
    control = ControlServer(environment, shutdown, control_host, control_port)
    telegram = TelegramChannel(environment, settings.telegram)
    restart_watch = asyncio.create_task(_restart_when_asked(environment, shutdown))
    telegram_task = asyncio.create_task(telegram.run()) if telegram.configured else None
    try:
        await control.start()
        if headless:
            await shutdown.wait()
        else:
            await wait_for_console_or_shutdown(ConsoleChannel(environment), shutdown)
    finally:
        restart_watch.cancel()
        if telegram_task is not None:
            telegram.stop()
            telegram_task.cancel()
        for task in (restart_watch, telegram_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await control.stop()
        await environment.stop()
    return RESTART_EXIT_CODE if environment.restart_requested.is_set() else 0


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
