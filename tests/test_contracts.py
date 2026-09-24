"""Coverage for the small, load-bearing pieces of evomesh.contracts."""

from pathlib import Path

from evomesh.contracts import AgentRuntimeState, SkillDefinition, belief_key


def test_belief_key_is_the_first_words_joined_by_dots() -> None:
    assert belief_key("the provider is ready") == "the.provider.is.ready"


def test_agent_runtime_state_describe_includes_the_goal_when_set() -> None:
    state = AgentRuntimeState(agent_id="agent-1", goal="find water")
    assert "goal=find water" in state.describe()


def test_skill_definition_is_built_from_its_fields() -> None:
    skill = SkillDefinition(
        name="read_file",
        description="Read a file",
        path=Path("skills/read_file/SKILL.md"),
    )
    assert skill.name == "read_file"
    assert skill.path == Path("skills/read_file/SKILL.md")
