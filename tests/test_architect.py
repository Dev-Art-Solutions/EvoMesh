from evomesh.architect import (
    derive_access,
    derive_model,
    derive_name,
    derive_skills,
    plausible_constraints,
    plausible_name,
    plausible_purpose,
)


def test_derive_model_returns_the_default_provider_and_model_when_no_need_pattern_matches():
    # With no `provider:model` token in `need`, derive_model falls back to the
    # defaults passed in -- this is the obvious, real behavior of the function.
    assert derive_model("Just talk to me about gold prices", "openai", "GPT-4o") == (
        "openai",
        "GPT-4o",
    )


def test_derive_access_returns_the_path_token_in_the_need():
    need = "Give the agent read/write to /home/trade/config.json"
    assert derive_access(need) == "/home/trade/config.json"


def test_derive_access_returns_none_when_no_path_is_present():
    assert derive_access("Just talk to me about gold prices") == "none"


def test_plausible_constraints_accepts_a_multi_field_constraints_string():
    # Two non-empty fields separated by ';' -> a genuine constraints block.
    assert plausible_constraints("No trading on Friday; no trading on Saturdays") is True


def test_plausible_name_accepts_a_single_real_word_name():
    # A short, alphabetic, single-word name has no path/sentence/empty issues,
    # so plausible_name treats it as a genuine name.
    assert plausible_name("Trade") is True


def test_plausible_purpose_rejects_a_purpose_that_is_shorter_than_the_need():
    # It is not a pure word-count gate: the returned value must be at least as
    # long (in characters) as the human's need, so a one-word purpose can't
    # survive when the need was a full sentence.
    need = "Explain the risk and expected return of this position"
    assert plausible_purpose("trading", need) is False


def test_derive_name_returns_the_explicitly_named_name_when_given_a_sentence():
    # An explicit `named "..."` token wins: the name that was stated is what comes
    # back, title-cased.
    assert derive_name("Please create an agent named Mercury") == "Mercury"


def test_derive_skills_returns_the_installed_skill_the_need_mentions():
    # derive_skills matches the human's sentence against whatever skills the
    # registry currently holds (name -> description), so a word in the need that
    # appears in a skill's name or description is the reason that skill is kept.
    assert derive_skills(
        "I need an agent that reads markdown files",
        {"Markdown.Read": "Reads markdown documents"},
    ) == ["Markdown.Read"]
