"""Coverage for the small, load-bearing pieces of evomesh.contracts."""

from evomesh.contracts import AgentRuntimeState, belief_key


def test_belief_key_is_the_first_words_joined_by_dots() -> None:
    assert belief_key("the provider is ready") == "the.provider.is.ready"


def test_agent_runtime_state_describe_includes_the_goal_when_set() -> None:
    state = AgentRuntimeState(agent_id="agent-1", goal="find water")
    assert "goal=find water" in state.describe()
