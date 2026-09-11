"""An agent template is a bundle on disk: AGENT.md plus the skills/tools it
names, installed as one unit and instantiated into a live, running agent --
possibly more than once."""

from __future__ import annotations

from pathlib import Path

import pytest

from evomesh.agent_templates import (
    AgentTemplateRegistry,
    InvalidAgentTemplateError,
    MissingAgentTemplateError,
    parse_agent_template,
)
from evomesh.config import Settings
from evomesh.environment import Environment
from evomesh.models import MockProvider

VALID = (
    "---\n"
    "name: trader\n"
    "identity: Trader\n"
    "purpose: Execute and monitor MT5 trades under human-set thresholds.\n"
    "autonomy: cyclic\n"
    "cycle_seconds: 90\n"
    "goals:\n"
    "  - text: Watch open positions and account equity\n"
    "    priority: 3\n"
    "    recurring: true\n"
    "    interval_seconds: 5\n"
    "skills: [trading-strategy]\n"
    "tools: [mt5_bridge]\n"
    "---\n\n"
    "Notes for a human reading this template.\n"
)


def write_template(root: Path, name: str, text: str) -> Path:
    directory = root / "agent-templates" / name
    directory.mkdir(parents=True)
    target = directory / "AGENT.md"
    target.write_text(text, encoding="utf-8")
    return target


def test_parse_agent_template_reads_frontmatter() -> None:
    definition = parse_agent_template(Path("agent-templates/trader/AGENT.md"), VALID)

    assert definition.name == "trader"
    assert definition.identity == "Trader"
    assert definition.cycle_seconds == 90
    assert definition.skills == ["trading-strategy"]
    assert definition.tools == ["mt5_bridge"]
    assert len(definition.goals) == 1
    assert definition.goals[0].interval_seconds == 5


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter here at all\n",
        "---\nname: trader\npurpose: unfinished",
        "---\nname: missing a purpose\n---\nbody",
        "---\npurpose: missing a name\n---\nbody",
    ],
)
def test_parse_agent_template_rejects_malformed_frontmatter(text: str) -> None:
    with pytest.raises(InvalidAgentTemplateError):
        parse_agent_template(Path("agent-templates/broken/AGENT.md"), text)


async def test_registry_discovers_installed_templates(tmp_path: Path) -> None:
    write_template(tmp_path, "trader", VALID)
    registry = AgentTemplateRegistry(tmp_path)
    await registry.load()

    found = registry.discover("trader")
    assert [item.name for item in found] == ["trader"]
    with pytest.raises(MissingAgentTemplateError):
        registry.get("no-such-template")


async def test_install_directory_copies_the_bundle(tmp_path: Path) -> None:
    source = tmp_path / "source" / "trader"
    source.mkdir(parents=True)
    (source / "AGENT.md").write_text(VALID, encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir()
    registry = AgentTemplateRegistry(project)
    installed = await registry.install_directory(source)

    assert installed.name == "trader"
    assert (project / "agent-templates" / "trader" / "AGENT.md").is_file()


async def test_instantiate_creates_and_starts_a_running_agent(tmp_path: Path) -> None:
    write_template(tmp_path, "trader", VALID)
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()

    definition = await environment.agent_templates.instantiate(environment, "trader")

    assert definition.identity == "Trader"
    assert definition.id in environment.runtimes
    assert definition.mind.goals[0].description == "Watch open positions and account equity"
    await environment.stop()


async def test_instantiate_can_give_the_new_agent_its_own_telegram_bot(tmp_path: Path) -> None:
    write_template(tmp_path, "trader", VALID)
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()

    definition = await environment.agent_templates.instantiate(
        environment, "trader", agent_name="Trader Two", telegram_token="123:abc"
    )

    assert definition.name == "Trader Two"
    assert definition.telegram is not None
    assert definition.telegram.token == "123:abc"
    await environment.stop()
