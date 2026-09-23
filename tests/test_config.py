"""RuntimeSettings.budget_for_num_ctx -- one mesh, models of different sizes.

A single global prompt_chars can only ever be safe for whichever agent has
the smallest context window; every other agent either wastes headroom, or
-- if a human raises it for a big model without checking every sibling --
silently truncates the small one's memory again, exactly the failure these
character budgets exist to prevent in the first place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evomesh.config import HarnessSettings, RuntimeSettings, load_settings


def test_a_large_num_ctx_keeps_the_configured_defaults() -> None:
    settings = RuntimeSettings(prompt_chars=6000, memory_chars=3000)

    budget = settings.budget_for_num_ctx(65536)

    assert budget.prompt_chars == 6000
    assert budget.memory_chars == 3000


def test_no_num_ctx_keeps_the_configured_defaults() -> None:
    """Nothing resolved (no provider default, no per-agent override) is not
    the same as "small" -- there is nothing to size against, so this must
    not guess."""
    settings = RuntimeSettings(prompt_chars=6000)

    budget = settings.budget_for_num_ctx(None)

    assert budget.prompt_chars == 6000


def test_a_small_num_ctx_shrinks_every_sub_budget_proportionally() -> None:
    settings = RuntimeSettings(
        prompt_chars=6000, memory_chars=3000, context_chars=1500,
        inbox_chars=1000, beliefs_chars=700,
    )

    budget = settings.budget_for_num_ctx(1024)

    assert budget.prompt_chars < 6000
    assert budget.memory_chars < 3000
    assert budget.context_chars < 1500
    assert budget.inbox_chars < 1000
    assert budget.beliefs_chars < 700
    # Proportional, not independently clamped: every field shrinks by
    # roughly the same fraction relative to its own configured default.
    ratio = budget.memory_chars / 3000
    assert budget.context_chars / 1500 == ratio


def test_a_small_num_ctx_never_shrinks_a_sub_budget_below_its_floor() -> None:
    """A model small enough to make the proportional math collapse toward
    zero still needs a workable minimum -- an empty belief section is not
    "safe", it is silently useless."""
    settings = RuntimeSettings(prompt_chars=6000, beliefs_chars=700)

    budget = settings.budget_for_num_ctx(64)

    assert budget.beliefs_chars >= 150
    assert budget.prompt_chars >= 800


def test_the_configured_default_never_grows_past_itself() -> None:
    """These are a deliberate cost/discipline ceiling (see evomesh.yaml's
    own comments), not "use all available context" -- an enormous num_ctx
    must not inflate the budget past what a human actually configured."""
    settings = RuntimeSettings(prompt_chars=6000)

    budget = settings.budget_for_num_ctx(10_000_000)

    assert budget.prompt_chars == 6000


# -- HarnessSettings.transcript_chars_for_num_ctx -- the same failure, reached
# from a harness job's own transcript pile instead of a cycle's prompt.


def test_a_harness_jobs_transcript_budget_keeps_the_default_for_a_roomy_model() -> None:
    settings = HarnessSettings(transcript_chars=12000)

    assert settings.transcript_chars_for_num_ctx(65536) == 12000


def test_a_harness_jobs_transcript_budget_is_unchanged_with_no_num_ctx_resolved() -> None:
    """Nothing resolved is not the same as "small" -- there is nothing to
    size against (see the RuntimeSettings equivalent above)."""
    settings = HarnessSettings(transcript_chars=12000)

    assert settings.transcript_chars_for_num_ctx(None) == 12000


def test_a_harness_jobs_transcript_budget_shrinks_for_a_small_model() -> None:
    """A job for an agent on a genuinely small model (a per-agent override, or
    an Ollama endpoint that fell back to its own built-in 2048) must not get
    the same flat pile sized for the mesh's biggest model -- the objective's
    own instructions live at the start of the transcript, right where the
    model server truncates from first."""
    settings = HarnessSettings(transcript_chars=12000)

    assert settings.transcript_chars_for_num_ctx(1024) < 12000


def test_a_harness_jobs_transcript_budget_never_collapses_below_a_floor() -> None:
    settings = HarnessSettings(transcript_chars=12000)

    assert settings.transcript_chars_for_num_ctx(8) >= 1500


# -- load_settings: api_key_ref resolved against evomesh.secrets.yaml -------
#
# The whole point: a real key never has to be typed into evomesh.yaml (or,
# worse, the git-tracked evomesh.yaml.example) during setup. It lives only
# in evomesh.secrets.yaml, which .gitignore keeps out of every commit.


def test_an_api_key_ref_is_resolved_from_the_sibling_secrets_file(tmp_path: Path) -> None:
    (tmp_path / "evomesh.yaml").write_text(
        "models:\n"
        "  providers:\n"
        "    openai:\n"
        "      base_url: https://api.openai.com/v1\n"
        "      model: gpt-5\n"
        "      api_key_ref: openai_primary\n",
        encoding="utf-8",
    )
    (tmp_path / "evomesh.secrets.yaml").write_text(
        "openai_primary: sk-real-key\n", encoding="utf-8"
    )

    settings = load_settings(tmp_path / "evomesh.yaml")

    assert settings.models.providers["openai"].api_key == "sk-real-key"


def test_two_provider_entries_can_use_two_different_refs(tmp_path: Path) -> None:
    """"More than one key for one provider" is expressed as two named
    provider blocks, each with its own ref -- an agent picks the key by
    picking which provider name it uses."""
    (tmp_path / "evomesh.yaml").write_text(
        "models:\n"
        "  providers:\n"
        "    openai_primary:\n"
        "      base_url: https://api.openai.com/v1\n"
        "      model: gpt-5\n"
        "      api_key_ref: primary\n"
        "    openai_backup:\n"
        "      base_url: https://api.openai.com/v1\n"
        "      model: gpt-5\n"
        "      api_key_ref: backup\n",
        encoding="utf-8",
    )
    (tmp_path / "evomesh.secrets.yaml").write_text(
        "primary: sk-one\nbackup: sk-two\n", encoding="utf-8"
    )

    settings = load_settings(tmp_path / "evomesh.yaml")

    assert settings.models.providers["openai_primary"].api_key == "sk-one"
    assert settings.models.providers["openai_backup"].api_key == "sk-two"


def test_a_ref_with_no_secrets_file_fails_fast(tmp_path: Path) -> None:
    """Silently running unauthenticated would be a worse failure than
    refusing to start -- this must be loud, and must name the missing ref
    and the file it expected to find it in."""
    (tmp_path / "evomesh.yaml").write_text(
        "models:\n"
        "  providers:\n"
        "    openai:\n"
        "      base_url: https://api.openai.com/v1\n"
        "      model: gpt-5\n"
        "      api_key_ref: openai_primary\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="openai_primary"):
        load_settings(tmp_path / "evomesh.yaml")


def test_a_ref_missing_from_an_existing_secrets_file_fails_fast(tmp_path: Path) -> None:
    (tmp_path / "evomesh.yaml").write_text(
        "models:\n"
        "  providers:\n"
        "    openai:\n"
        "      base_url: https://api.openai.com/v1\n"
        "      model: gpt-5\n"
        "      api_key_ref: openai_primary\n",
        encoding="utf-8",
    )
    (tmp_path / "evomesh.secrets.yaml").write_text("unrelated_ref: sk-x\n", encoding="utf-8")

    with pytest.raises(ValueError, match="openai_primary"):
        load_settings(tmp_path / "evomesh.yaml")


def test_a_literal_api_key_still_works_with_no_ref(tmp_path: Path) -> None:
    """Backward compatible: a provider that never opts into api_key_ref
    keeps working exactly as before this feature existed."""
    (tmp_path / "evomesh.yaml").write_text(
        "models:\n"
        "  providers:\n"
        "    openai:\n"
        "      base_url: https://api.openai.com/v1\n"
        "      model: gpt-5\n"
        "      api_key: sk-literal\n",
        encoding="utf-8",
    )

    settings = load_settings(tmp_path / "evomesh.yaml")

    assert settings.models.providers["openai"].api_key == "sk-literal"
