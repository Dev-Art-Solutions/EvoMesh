from evomesh.architect import derive_access, derive_model, plausible_constraints


def test_derive_model_returns_normalized_model_and_system_prompt():
    provider, model = derive_model("GPT-4o", "openai", "GPT-4o")
    system_prompt = "You are a test agent."
    assert model == "GPT-4o"
    assert system_prompt == "You are a test agent."


def test_derive_access_returns_the_path_token_in_the_need():
    need = "Give the agent read/write to /home/trade/config.json"
    assert derive_access(need) == "/home/trade/config.json"


def test_derive_access_returns_none_when_no_path_is_present():
    assert derive_access("Just talk to me about gold prices") == "none"


def test_plausible_constraints_accepts_a_multi_field_constraints_string():
    # Two non-empty fields separated by ';' -> a genuine constraints block.
    assert plausible_constraints("No trading on Friday; no trading on Saturdays") is True
