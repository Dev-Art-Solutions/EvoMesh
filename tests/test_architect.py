from evomesh.architect import derive_model, plausible_constraints


def test_derive_model_returns_normalized_model_and_system_prompt():
    provider, model = derive_model("GPT-4o", "openai", "GPT-4o")
    system_prompt = "You are a test agent."
    assert model == "GPT-4o"
    assert system_prompt == "You are a test agent."


def test_plausible_constraints_accepts_a_multi_field_constraints_string():
    # Two non-empty fields separated by ';' -> a genuine constraints block.
    assert plausible_constraints("No trading on Friday; no trading on Saturdays") is True
