from pathlib import Path

from evomesh.architect import ArchitectInterview
from evomesh.config import Settings
from evomesh.contracts import FilesystemGrant, Message
from evomesh.environment import Environment
from evomesh.models import MockProvider


async def test_create_grant_message_and_restart(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "state.db", generation_path=tmp_path / "generations")
    interview = ArchitectInterview()
    interview.begin("Create an agent called Researcher that summarizes markdown papers")
    interview.refine("ollama:qwen3:8b")
    agent = interview.confirm()
    assert agent.name == "Researcher"

    environment = Environment(settings, {"ollama": MockProvider(["summary complete"])})
    await environment.start()
    await environment.register_agent(agent)
    papers = tmp_path / "papers"
    await environment.grant_access(FilesystemGrant(agent_id=agent.id, path=str(papers)))
    await environment.start_agent(agent.id)
    await environment.send_message(
        Message(sender_id="human", recipient_id=agent.id, content="Summarize the papers")
    )
    response = await environment.bus.receive("human", wait_seconds=1)
    assert response.content == "summary complete"
    assert environment.providers["ollama"].calls[-1]["model"] == "qwen3:8b"  # type: ignore[attr-defined]
    await environment.stop()

    restarted = Environment(settings, {"ollama": MockProvider()})
    await restarted.start()
    assert restarted.registry.get("Researcher").id == agent.id
    assert any(grant.agent_id == agent.id for grant in await restarted.repository.load_grants())
    assert len(await restarted.repository.load_messages()) == 2
    await restarted.stop()
