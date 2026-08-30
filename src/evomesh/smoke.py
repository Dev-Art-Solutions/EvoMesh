from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from evomesh.config import Settings
from evomesh.contracts import Message
from evomesh.environment import Environment, HealthState
from evomesh.models import MockProvider


async def smoke() -> None:
    with tempfile.TemporaryDirectory(prefix="evomesh-smoke-") as directory:
        root = Path(directory)
        settings = Settings(data_path=root / "state.db", generation_path=root / "generations")
        environment = Environment(settings, {"ollama": MockProvider()})
        await environment.start()
        assert environment.health_state == HealthState.READY
        assert len(environment.registry.all()) == 4
        environment.bus.register("smoke-receiver")
        await environment.send_message(
            Message(sender_id="smoke", recipient_id="smoke-receiver", content="ping")
        )
        received = await environment.bus.receive("smoke-receiver", wait_seconds=1)
        assert received.content == "ping"
        await environment.stop()
        assert environment.health_state == HealthState.STOPPED


def main() -> None:
    asyncio.run(smoke())


if __name__ == "__main__":
    main()
