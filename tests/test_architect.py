from evomesh.architect import derive_model


def test_derive_model_returns_normalized_model_and_system_prompt():
    provider, model = derive_model("GPT-4o", "openai", "GPT-4o")
    system_prompt = "You are a test agent."
    assert model == "GPT-4o"
    assert system_prompt == "You are a test agent."
