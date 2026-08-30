from pathlib import Path

from evomesh.config import Settings
from evomesh.console import ConsoleChannel
from evomesh.environment import Environment
from evomesh.models import MockProvider


async def test_console_routes_commands(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    console = ConsoleChannel(environment)
    assert "status: READY" in (await console.route("/status"))
    assert "Agent Architect" in (await console.route("/agents"))
    assert "Filesystem.Read" in (await console.route("/skills"))
    assert await console.route("/chat architect") == "Talking to Agent Architect."
    await environment.stop()

