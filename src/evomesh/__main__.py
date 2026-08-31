from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from evomesh.config import load_settings
from evomesh.console import ConsoleChannel
from evomesh.control import CONTROL_HOST, CONTROL_PORT, ControlServer, wait_for_console_or_shutdown
from evomesh.environment import Environment


async def application(
    config: Path | None = None,
    *,
    headless: bool = False,
    control_host: str = CONTROL_HOST,
    control_port: int = CONTROL_PORT,
    log_file: Path | None = None,
) -> None:
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
    try:
        await control.start()
        if headless:
            await shutdown.wait()
        else:
            await wait_for_console_or_shutdown(ConsoleChannel(environment), shutdown)
    finally:
        await control.stop()
        await environment.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the EvoMesh local environment")
    parser.add_argument("--config", type=Path, help="Path to evomesh.yaml")
    parser.add_argument("--headless", action="store_true", help="Run only the control server")
    parser.add_argument("--control-host", default=CONTROL_HOST)
    parser.add_argument("--control-port", type=int, default=CONTROL_PORT)
    parser.add_argument("--log-file", type=Path, help="Optional persistent runtime log file")
    args = parser.parse_args()
    asyncio.run(
        application(
            args.config,
            headless=args.headless,
            control_host=args.control_host,
            control_port=args.control_port,
            log_file=args.log_file,
        )
    )


if __name__ == "__main__":
    main()
