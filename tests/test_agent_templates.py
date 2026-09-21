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
from evomesh.contracts import Autonomy
from evomesh.environment import Environment
from evomesh.models import MockProvider

REPO_TEMPLATES = Path(__file__).resolve().parent.parent / "agent-templates"

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


def test_parse_agent_template_reads_an_optional_project_path() -> None:
    text = VALID.replace("identity: Trader\n", "identity: Trader\nproject: D:\\Code\\SomeRepo\n")

    definition = parse_agent_template(Path("agent-templates/trader/AGENT.md"), text)

    assert definition.project == "D:\\Code\\SomeRepo"


def test_parse_agent_template_project_defaults_to_empty() -> None:
    definition = parse_agent_template(Path("agent-templates/trader/AGENT.md"), VALID)

    assert definition.project == ""


async def test_instantiate_uses_the_templates_own_project_path(tmp_path: Path) -> None:
    """An agent with a configured project works there -- see
    Environment.default_harness_root -- instead of its own mesh-managed
    playground, once the harness actually grants it (tools: makes that
    grant happen at spawn time)."""
    project_dir = tmp_path / "real-project"
    project_dir.mkdir()
    templated = VALID.replace(
        "tools: [mt5_bridge]\n", f"tools: [mt5_bridge]\nproject: {project_dir}\n"
    )
    write_template(tmp_path, "trader", templated)
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()

    definition = await environment.agent_templates.instantiate(environment, "trader")

    assert definition.project_path == str(project_dir)
    assert Path(definition.harness_root) == project_dir
    await environment.stop()


async def test_instantiate_uses_the_templates_own_self_check_default(tmp_path: Path) -> None:
    text = VALID.replace("identity: Trader\n", 'identity: Trader\nself_check: "ruff check ."\n')
    write_template(tmp_path, "trader", text)
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()

    definition = await environment.agent_templates.instantiate(environment, "trader")

    assert definition.self_check_command == "ruff check ."
    await environment.stop()


async def test_instantiate_self_check_defaults_to_none_when_unset(tmp_path: Path) -> None:
    write_template(tmp_path, "trader", VALID)
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()

    definition = await environment.agent_templates.instantiate(environment, "trader")

    assert definition.self_check_command is None
    await environment.stop()


async def test_harness_true_grants_access_with_no_bundled_tools(tmp_path: Path) -> None:
    """A template with no custom tools -- a general coding agent, using only
    the harness's own built-in read/edit/write/shell -- got no automatic
    grant before `harness: true` existed, unlike one naming `tools:`."""
    text = VALID.replace("name: trader\n", "name: coder\n").replace(
        "tools: [mt5_bridge]\n", "tools: []\nharness: true\n"
    )
    write_template(tmp_path, "coder", text)
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()

    definition = await environment.agent_templates.instantiate(environment, "coder")

    assert definition.harness_root != ""
    playground = environment.memory_for(definition).playground_path
    assert Path(definition.harness_root) == playground
    await environment.stop()


async def test_learn_skills_true_grants_both_capabilities_with_no_bundled_tools(
    tmp_path: Path,
) -> None:
    """learn_skill is useless without somewhere to run it -- learn_skills:
    true has to grant harness access too, the same as harness: true does on
    its own, not just flip AgentDefinition.can_learn_skills and leave
    harness_root empty."""
    text = VALID.replace("name: trader\n", "name: researcher\n").replace(
        "tools: [mt5_bridge]\n", "tools: []\nlearn_skills: true\n"
    )
    write_template(tmp_path, "researcher", text)
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()

    definition = await environment.agent_templates.instantiate(environment, "researcher")

    assert definition.can_learn_skills is True
    assert definition.harness_root != ""
    await environment.stop()


async def test_learn_skills_defaults_false(tmp_path: Path) -> None:
    write_template(tmp_path, "trader", VALID)  # no learn_skills: line at all
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()

    definition = await environment.agent_templates.instantiate(environment, "trader")

    assert definition.can_learn_skills is False
    await environment.stop()


async def test_instantiate_accepts_an_explicit_project_override(tmp_path: Path) -> None:
    write_template(tmp_path, "trader", VALID)
    override_dir = tmp_path / "override-project"
    override_dir.mkdir()
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()

    definition = await environment.agent_templates.instantiate(
        environment, "trader", project_path=str(override_dir)
    )

    assert definition.project_path == str(override_dir)
    await environment.stop()


# -- the shipped coder / mt5-coder templates, from the real repo files ----


async def test_the_coder_template_spawns_a_working_reactive_agent(tmp_path: Path) -> None:
    """Spawned from the actual files this repo ships, not a synthetic
    fixture -- catches a real frontmatter/skill mistake a hand-built VALID
    string never would."""
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    await environment.agent_templates.install_directory(REPO_TEMPLATES / "coder")

    definition = await environment.agent_templates.instantiate(environment, "coder")

    assert definition.identity == "Coder"
    assert definition.autonomy is Autonomy.REACTIVE
    # harness: true with no bundled tools -- must still get a grant.
    assert definition.harness_root != ""
    assert Path(definition.harness_root) == environment.memory_for(definition).playground_path
    assert [skill.name for skill in environment.skills.discover("coding-discipline")] == [
        "coding-discipline"
    ]
    await environment.stop()


async def test_the_mt5_coder_template_spawns_with_both_bundled_skills(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    await environment.agent_templates.install_directory(REPO_TEMPLATES / "mt5-coder")

    definition = await environment.agent_templates.instantiate(environment, "mt5-coder")

    assert definition.identity == "MT5 Coder"
    assert definition.autonomy is Autonomy.REACTIVE
    assert definition.harness_root != ""
    installed = {skill.name for skill in environment.skills.discover()}
    assert {"coding-discipline", "mql5-conventions"} <= installed
    await environment.stop()


async def test_the_news_watcher_template_spawns_with_learn_skills_granted(
    tmp_path: Path,
) -> None:
    """Spawned from the actual AGENT.md this repo ships: learn_skills: true
    there must reach both AgentDefinition.can_learn_skills and a real
    harness_root, and news-report-export must be installed alongside
    news-triage."""
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    await environment.agent_templates.install_directory(REPO_TEMPLATES / "news-watcher")

    definition = await environment.agent_templates.instantiate(environment, "news-watcher")

    assert definition.identity == "NewsWatcher"
    assert definition.can_learn_skills is True
    assert definition.harness_root != ""
    installed = {skill.name for skill in environment.skills.discover()}
    assert {"news-triage", "news-report-export"} <= installed
    await environment.stop()
