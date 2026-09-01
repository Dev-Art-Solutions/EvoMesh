from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from evomesh.config import EvolutionSettings, RuntimeSettings, Settings
from evomesh.contracts import AgentPhase, Message
from evomesh.environment import Environment, HealthState
from evomesh.models import MockProvider

CYCLE_REPLY = (
    "STEP: check the roster\n"
    "RESULT: The mesh looks healthy.\n"
    "FACT: Smoke test ran the cycle loop.\n"
    "DONE: no\n"
)


async def smoke() -> None:
    with tempfile.TemporaryDirectory(prefix="evomesh-smoke-") as directory:
        root = Path(directory)
        settings = Settings(
            data_path=root / "state.db",
            generation_path=root / "generations",
            workspace_path=root / "workspace",
            runtime=RuntimeSettings(cycle_seconds=3600, stagger_seconds=0),
            evolution=EvolutionSettings(autonomous=False),
        )
        environment = Environment(settings, {"ollama": MockProvider([CYCLE_REPLY])})
        await environment.start(start_agent_loops=True)
        assert environment.health_state == HealthState.READY
        assert len(environment.registry.all()) == 4

        environment.bus.register("smoke-receiver")
        await environment.send_message(
            Message(sender_id="smoke", recipient_id="smoke-receiver", content="ping")
        )
        received = await environment.bus.receive("smoke-receiver", wait_seconds=1)
        assert received.content == "ping"

        # Every agent must have a goal and a live phase, and a cycle must leave
        # something behind on disk. A mesh that boots but never thinks is the
        # exact failure this smoke test exists to catch.
        for definition in environment.registry.all():
            assert definition.mind.next_goal() is not None, definition.name
        outcome = await environment.cycle_agent("guardian")
        assert outcome.worked, outcome.summary
        states = environment.runtime_states()
        assert states["guardian"].phase is not AgentPhase.OFFLINE
        assert states["guardian"].cycles >= 1

        memory = environment.memory_for(environment.registry.get("guardian"))
        assert memory.memory_path.exists()
        assert memory.context_path.exists()
        assert "Current goal" in await memory.read_context()
        assert environment.world.path.exists()

        await environment.stop()
        assert environment.health_state == HealthState.STOPPED


def main() -> None:
    asyncio.run(smoke())


if __name__ == "__main__":
    main()
