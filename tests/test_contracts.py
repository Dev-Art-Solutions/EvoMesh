"""Coverage for the small, load-bearing pieces of evomesh.contracts."""

from evomesh.contracts import belief_key


def test_belief_key_is_the_first_words_joined_by_dots() -> None:
    assert belief_key("the provider is ready") == "the.provider.is.ready"
