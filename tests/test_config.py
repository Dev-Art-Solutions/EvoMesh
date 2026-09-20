"""RuntimeSettings.budget_for_num_ctx -- one mesh, models of different sizes.

A single global prompt_chars can only ever be safe for whichever agent has
the smallest context window; every other agent either wastes headroom, or
-- if a human raises it for a big model without checking every sibling --
silently truncates the small one's memory again, exactly the failure these
character budgets exist to prevent in the first place.
"""

from __future__ import annotations

from evomesh.config import RuntimeSettings


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
