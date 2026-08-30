from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from evomesh.config import load_settings
from evomesh.console import ConsoleChannel
from evomesh.environment import Environment


async def application(config: Path | None = None) -> None:
    settings = load_settings(config)
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format='{"time":"%(asctime)s","level":"%(levelname)s","message":"%(message)s"}',
    )
    environment = Environment(settings)
    await environment.start()
    try:
        await ConsoleChannel(environment).run()
    finally:
        await environment.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the EvoMesh local environment")
    parser.add_argument("--config", type=Path, help="Path to evomesh.yaml")
    args = parser.parse_args()
    asyncio.run(application(args.config))


if __name__ == "__main__":
    main()

