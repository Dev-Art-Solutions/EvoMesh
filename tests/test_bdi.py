"""What makes these agents BDI rather than a loop with nice field names."""

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from evomesh.agents import WAKE_MIN_GAP
from evomesh.bdi import (
    BDIBehavior,
    BDIReasoner,
    Desire,
    DeterministicBehavior,
    PlanLibrary,
    PlanRecipe,
    ReflectiveBehavior,
    StepResult,
    parse_plan,
)
from evomesh.cognition import CycleContext, CycleOutcome
from evomesh.config import EvolutionSettings, RuntimeSettings, Settings
from evomesh.contracts import (
    AgentDefinition,
    AgentPhase,
    AgentStatus,
    Belief,
    BeliefChange,
    Goal,
    GoalStatus,
    Intention,
    IntentionStatus,
    Message,
    MindState,
    PlanStep,
    StepStatus,
    now_utc,
)
from evomesh.environment import Environment
from evomesh.harness_queue import HarnessQueue
from evomesh.memory import AgentMemory, MemoryBudget
from evomesh.models import MockProvider

PLAN_OF_THREE = "1. open the folder\n2. read every note\n3. write the summary\n"
STEP_DONE = "RESULT: did it.\nFACT: NONE\nSTATUS: done\n"
PLANNING_MARKER = "Break this goal into"


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        data_path=tmp_path / "state.db",
        generation_path=tmp_path / "generations",
        workspace_path=tmp_path / "workspace",
        runtime=RuntimeSettings(cycle_seconds=3600, stagger_seconds=0),
        evolution=EvolutionSettings(autonomous=False),
    )


class ScriptedProvider(MockProvider):
    """Answers a planning prompt with a plan and anything else with a step result."""

    def __init__(self, plan: str = PLAN_OF_THREE, step: str = STEP_DONE) -> None:
        super().__init__()
        self.plan = plan
        self.step = step

    async def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        num_ctx: int | None = None,
        format: dict[str, Any] | None = None,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "model": model,
                "num_ctx": num_ctx,
                "format": format,
            }
        )
        return self.plan if PLANNING_MARKER in prompt else self.step


def planning_calls(provider: MockProvider) -> int:
    return sum(1 for call in provider.calls if PLANNING_MARKER in str(call["prompt"]))


async def worker(
    tmp_path: Path, provider: MockProvider, goal: str = "Summarize the notes"
) -> tuple[Environment, AgentDefinition]:
    environment = Environment(settings_for(tmp_path), {"ollama": provider})
    await environment.start()
    agent = AgentDefinition(name="Worker", purpose="Work", status=AgentStatus.ACTIVE)
    agent.mind.add_goal(goal)
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)
    return environment, agent


# -- belief revision ----------------------------------------------------


def test_a_percept_revises_a_belief_instead_of_stacking_beside_it() -> None:
    mind = MindState()
    first = mind.revise([Belief(key="provider.ready", statement="no")])
    second = mind.revise([Belief(key="provider.ready", statement="no")])
    third = mind.revise([Belief(key="provider.ready", statement="yes")])

    assert first.added == ("provider.ready",)
    assert not second, "re-perceiving the same fact is not a change"
    assert third.updated == ("provider.ready",)
    assert len(mind.beliefs) == 1
    assert mind.believes("provider.ready", "yes")


def test_the_belief_base_drops_the_least_recently_confirmed() -> None:
    mind = MindState()
    mind.revise([Belief(key=f"k{index}", statement=str(index)) for index in range(6)], keep=6)
    mind.revise([Belief(key="k0", statement="0")], keep=6)  # re-confirm the oldest
    mind.revise([Belief(key="fresh", statement="new")], keep=6)

    keys = {item.key for item in mind.beliefs}
    assert "fresh" in keys
    assert "k0" in keys, "a fact that keeps being re-perceived is still current"
    assert "k1" not in keys


# -- commitment ---------------------------------------------------------


async def test_the_agent_plans_once_and_then_executes_the_plan(tmp_path: Path) -> None:
    """Commitment: one planning call per goal, not one per cycle."""
    provider = ScriptedProvider()
    environment, agent = await worker(tmp_path, provider)

    for _ in range(3):
        await environment.cycle_agent("Worker")

    assert planning_calls(provider) == 1
    intention = agent.mind.intentions[-1]
    assert intention.plan == "model"
    assert [step.description for step in intention.steps] == [
        "open the folder",
        "read every note",
        "write the summary",
    ]
    assert all(step.status is StepStatus.DONE for step in intention.steps)
    await environment.stop()


async def test_an_agent_with_tools_is_told_which_steps_get_them(tmp_path: Path) -> None:
    """Whether a step runs with tools is decided by its first word -- a rule
    the planning prompt never mentioned, so a plan could quietly leave a
    tooled agent answering from memory."""
    tooled_provider, plain_provider = ScriptedProvider(), ScriptedProvider()
    tooled_env, tooled = await worker(tmp_path / "a", tooled_provider)
    tooled.harness_root = str(tmp_path)
    plain_env, _ = await worker(tmp_path / "b", plain_provider)

    await tooled_env.cycle_agent("Worker")
    await plain_env.cycle_agent("Worker")

    def planning_prompt(provider: MockProvider) -> str:
        return next(str(c["prompt"]) for c in provider.calls if PLANNING_MARKER in str(c["prompt"]))

    assert "only carried out with them if it STARTS with" in planning_prompt(tooled_provider)
    assert "Fetch" in planning_prompt(tooled_provider)
    assert "STARTS with" not in planning_prompt(plain_provider)
    await tooled_env.stop()
    await plain_env.stop()


async def test_a_plan_that_paraphrases_away_a_tool_goal_is_replaced_by_the_goal(
    tmp_path: Path,
) -> None:
    goal = "Fetch the latest headlines with news_fetch and note what is new"
    provider = ScriptedProvider(plan="1. get the latest headlines\n2. note what is new\n")
    environment, agent = await worker(tmp_path, provider, goal=goal)
    agent.harness_root = str(tmp_path)

    await environment.cycle_agent("Worker")

    assert [step.description for step in agent.mind.intentions[-1].steps] == [goal]
    await environment.stop()


async def test_one_plan_step_is_executed_per_cycle(tmp_path: Path) -> None:
    provider = ScriptedProvider()
    environment, agent = await worker(tmp_path, provider)

    first = await environment.cycle_agent("Worker")
    assert first.step == "open the folder"
    assert agent.mind.current_intention().cursor == 1  # type: ignore[union-attr]

    second = await environment.cycle_agent("Worker")
    assert second.step == "read every note"
    assert not second.goal_done, "the plan is not finished yet"

    third = await environment.cycle_agent("Worker")
    assert third.step == "write the summary"
    assert third.goal_done, "the goal is met once the plan is exhausted"
    await environment.stop()


async def test_a_finished_plan_is_marked_achieved_and_a_new_one_is_adopted(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider()
    environment, agent = await worker(tmp_path, provider)
    agent.mind.goals[0].recurring = True  # a standing job re-plans after each pass

    for _ in range(4):
        await environment.cycle_agent("Worker")

    statuses = [item.status for item in agent.mind.intentions]
    assert IntentionStatus.ACHIEVED in statuses
    assert agent.mind.current_intention() is not None, "it committed again"
    assert planning_calls(provider) == 2, "one planning call per pass, not per cycle"
    await environment.stop()


# -- reconsideration ----------------------------------------------------


def test_reconsideration_is_triggered_by_the_things_that_matter() -> None:
    reasoner = BDIReasoner()
    mind = MindState()
    goal = mind.add_goal("do the work")
    intention = mind.commit(goal.id, ["a", "b"], context_keys=["world.state"])
    quiet = BeliefChange()

    assert reasoner.reconsider(intention, mind, quiet) is None, "commitment holds"

    changed = BeliefChange(updated=("world.state",))
    assert reasoner.reconsider(intention, mind, changed) is not None

    unrelated = BeliefChange(updated=("something.else",))
    assert reasoner.reconsider(intention, mind, unrelated) is None

    urgent = mind.add_goal("drop everything", priority=1)
    assert "higher-priority" in (reasoner.reconsider(intention, mind, quiet) or "")

    urgent.status = GoalStatus.DONE
    goal.status = GoalStatus.DONE
    assert "no longer open" in (reasoner.reconsider(intention, mind, quiet) or "")

    assert reasoner.reconsider(None, mind, quiet) is not None


async def test_a_higher_priority_goal_takes_the_commitment(tmp_path: Path) -> None:
    provider = ScriptedProvider()
    environment, agent = await worker(tmp_path, provider)
    await environment.cycle_agent("Worker")
    assert agent.mind.current_intention().steps[0].description == "open the folder"  # type: ignore[union-attr]

    urgent = agent.mind.add_goal("Handle the incident", priority=1)
    await environment.cycle_agent("Worker")

    intention = agent.mind.current_intention()
    assert intention is not None
    assert intention.goal_id == urgent.id
    dropped = [item for item in agent.mind.intentions if item.status is IntentionStatus.DROPPED]
    assert dropped, "the previous commitment was abandoned, not silently kept"
    await environment.stop()


# -- a goal's own cadence, independent of the agent's cycle_seconds -------


def test_a_goal_in_cooldown_is_not_open() -> None:
    """interval_seconds is not a cycle counter -- it is a real clock, checked
    against next_attempt_at rather than counted in ticks, so it means the
    same thing regardless of how fast the agent's own cycle_seconds runs."""
    goal = Goal(description="Check example.com", recurring=True, interval_seconds=3600)
    assert goal.is_open, "no cooldown set yet -- due immediately"

    goal.next_attempt_at = now_utc() + timedelta(seconds=3600)
    assert not goal.is_open, "still inside its own interval"

    goal.next_attempt_at = now_utc() - timedelta(seconds=1)
    assert goal.is_open, "the interval has passed"


def test_next_goal_skips_a_cooldown_and_surfaces_other_work() -> None:
    """The reason this exists at all: an agent with one goal on an hourly
    cadence must not go quiet for everything else in between. A lower-
    priority goal that is actually due gets the commitment instead of the
    higher-priority one still in cooldown."""
    mind = MindState()
    hourly = mind.add_goal("Check example.com", priority=1, interval_seconds=3600)
    hourly.next_attempt_at = now_utc() + timedelta(seconds=3600)
    chat_reply = mind.add_goal("Answer what was asked", priority=5)

    assert mind.next_goal() is chat_reply


async def test_a_finished_goal_with_an_interval_waits_before_reopening(
    tmp_path: Path,
) -> None:
    """Exercises the actual wiring in AgentRuntime._apply, not just the Goal
    model in isolation: a real goal_done outcome sets next_attempt_at, and a
    recurring goal stays open (not DONE) while it waits."""
    environment, agent = await worker(tmp_path, ScriptedProvider())
    goal = agent.mind.add_goal(
        "Check example.com", priority=1, recurring=True, interval_seconds=3600
    )
    runtime = environment.runtimes[agent.id]

    await runtime._apply(  # noqa: SLF001 - exercising the wiring directly, not through a full cycle
        CycleOutcome(summary="fetched it", goal_done=True, phase=AgentPhase.IDLE, worked=True),
        goal,
    )

    assert goal.status is not GoalStatus.DONE
    assert goal.next_attempt_at is not None
    assert goal.next_attempt_at > now_utc()
    assert not goal.is_open
    await environment.stop()


def test_a_cron_goal_waits_for_its_first_scheduled_time() -> None:
    """Unlike interval_seconds, a cron schedule is an appointment: adding the
    goal must not make it due right away just because it is new."""
    mind = MindState()
    goal = mind.add_goal("Check example.com", cron_expression="0 * * * *")

    assert goal.next_attempt_at is not None
    assert not goal.is_open
    assert goal.recurring is False, "add_goal itself does not force recurring -- the console does"


async def test_a_finished_cron_goal_reschedules_to_its_next_occurrence(
    tmp_path: Path,
) -> None:
    """Exercises AgentRuntime._apply's cron branch: finishing a cycle moves
    next_attempt_at to the next matching time, not a fixed offset."""
    environment, agent = await worker(tmp_path, ScriptedProvider())
    goal = agent.mind.add_goal(
        "Check example.com", priority=1, recurring=True, cron_expression="0 * * * *"
    )
    runtime = environment.runtimes[agent.id]

    await runtime._apply(  # noqa: SLF001 - exercising the wiring directly, not through a full cycle
        CycleOutcome(summary="fetched it", goal_done=True, phase=AgentPhase.IDLE, worked=True),
        goal,
    )

    assert goal.next_attempt_at is not None
    assert goal.next_attempt_at.minute == 0
    assert goal.next_attempt_at > now_utc()
    assert not goal.is_open
    await environment.stop()


# -- announcing a goal's summary, opt in -----------------------------------


async def test_a_recurring_goal_with_notify_on_announces_every_completion(
    tmp_path: Path,
) -> None:
    """A recurring goal has no first-cycle rubber stamp to wait out -- every
    goal_done is real, so it should announce every time. It should not repeat
    the goal description each cycle -- the human already knows their standing
    goal, so only the fresh summary is worth another message."""
    environment, agent = await worker(tmp_path, ScriptedProvider())
    goal = agent.mind.add_goal("Check example.com", recurring=True, notify=True)
    runtime = environment.runtimes[agent.id]
    announced: list[str] = []

    async def record(text: str) -> None:
        announced.append(text)

    runtime.announce = record

    await runtime._apply(  # noqa: SLF001 - exercising the wiring directly, not through a full cycle
        CycleOutcome(summary="all quiet", goal_done=True, phase=AgentPhase.IDLE, worked=True),
        goal,
    )

    assert len(announced) == 1
    assert agent.name in announced[0]
    assert "all quiet" in announced[0]
    await environment.stop()


async def test_a_recurring_goal_with_a_silent_outcome_does_not_announce(
    tmp_path: Path,
) -> None:
    """A recurring goal whose skill says silence is the correct outcome (e.g.
    NewsAnalyzer's "nothing cleared the bar") should not spam a "found
    nothing to report" filler every single cycle forever."""
    environment, agent = await worker(tmp_path, ScriptedProvider())
    goal = agent.mind.add_goal("Check example.com", recurring=True, notify=True)
    runtime = environment.runtimes[agent.id]
    announced: list[str] = []

    async def record(text: str) -> None:
        announced.append(text)

    runtime.announce = record

    await runtime._apply(  # noqa: SLF001 - exercising the wiring directly, not through a full cycle
        CycleOutcome(summary="", goal_done=True, phase=AgentPhase.IDLE, worked=True),
        goal,
    )
    await runtime._apply(  # noqa: SLF001
        CycleOutcome(
            summary="harness job 42 found nothing to report",
            goal_done=True,
            phase=AgentPhase.IDLE,
            worked=True,
        ),
        goal,
    )

    assert announced == []
    await environment.stop()


async def test_report_pattern_keeps_only_matching_lines(tmp_path: Path) -> None:
    """The deterministic backstop for a skill's strict-format rule: only the
    line(s) shaped like the goal's own report format reach chat."""
    environment, agent = await worker(tmp_path, ScriptedProvider())
    goal = agent.mind.add_goal(
        "Assess headlines",
        recurring=True,
        notify=True,
        report_pattern=r"^[A-Z]+ (bullish|bearish): .+$",
    )
    runtime = environment.runtimes[agent.id]
    announced: list[str] = []

    async def record(text: str) -> None:
        announced.append(text)

    runtime.announce = record

    await runtime._apply(  # noqa: SLF001
        CycleOutcome(
            summary="Checked the feed, nothing else to add.\nXAU bullish: Fed pause.",
            goal_done=True,
            phase=AgentPhase.IDLE,
            worked=True,
        ),
        goal,
    )

    assert len(announced) == 1
    assert "XAU bullish: Fed pause." in announced[0]
    assert "Checked the feed" not in announced[0]


async def test_report_pattern_ignores_case(tmp_path: Path) -> None:
    """Found live: 106 of 106 NewsAnalyzer cycles that had something to say
    were filtered to nothing, because the model wrote "Bullish"/"Medium"
    where the pattern's own literal was lowercase. Case is not part of the
    format contract the pattern is meant to enforce -- only the shape is."""
    environment, agent = await worker(tmp_path, ScriptedProvider())
    goal = agent.mind.add_goal(
        "Assess headlines",
        recurring=True,
        notify=True,
        report_pattern=r"^[A-Z]+ (bullish|bearish): .+$",
    )
    runtime = environment.runtimes[agent.id]
    announced: list[str] = []

    async def record(text: str) -> None:
        announced.append(text)

    runtime.announce = record

    await runtime._apply(  # noqa: SLF001
        CycleOutcome(
            summary="XAU Bullish: Fed pause.",
            goal_done=True,
            phase=AgentPhase.IDLE,
            worked=True,
        ),
        goal,
    )

    assert len(announced) == 1
    assert "XAU Bullish: Fed pause." in announced[0]


async def test_report_pattern_is_never_applied_to_an_unfinished_cycle(
    tmp_path: Path,
) -> None:
    """A cron goal that takes several cycles to finish (NewsAnalyzer: 3-4
    ticks per 30-minute window) was running the filter -- and, when nothing
    matched, an extra model call to re-extract a report -- on every single
    mid-plan cycle, not just the one that actually finished. Every one of
    those calls was wasted: an intermediate summary is never the report."""
    provider = ScriptedProvider()
    environment, agent = await worker(tmp_path, provider)
    goal = agent.mind.add_goal(
        "Assess headlines",
        recurring=True,
        notify=True,
        report_pattern=r"^[A-Z]+ (bullish|bearish): .+$",
    )
    runtime = environment.runtimes[agent.id]
    calls_before = len(provider.calls)

    await runtime._apply(  # noqa: SLF001
        CycleOutcome(
            summary="called news_fetch, reading headline 3 of 8",
            goal_done=False,
            phase=AgentPhase.ACTING,
            worked=True,
            step="reading headline 3 of 8",
        ),
        goal,
    )

    assert len(provider.calls) == calls_before, "an unfinished cycle must never call the model"


async def test_report_pattern_falls_back_to_the_sanitizer_only_on_the_finishing_cycle(
    tmp_path: Path,
) -> None:
    """When the finishing cycle's raw answer has something real but not
    already pattern-shaped, the sanitizer call is the one place that is
    worth spending a model call on -- and it should get one more chance to
    match after that call, not be trusted unfiltered."""
    provider = ScriptedProvider(step="XAU bullish: extracted from narration.")
    environment, agent = await worker(tmp_path, provider)
    goal = agent.mind.add_goal(
        "Assess headlines",
        recurring=True,
        notify=True,
        report_pattern=r"^[A-Z]+ (bullish|bearish): .+$",
    )
    runtime = environment.runtimes[agent.id]
    announced: list[str] = []

    async def record(text: str) -> None:
        announced.append(text)

    runtime.announce = record

    await runtime._apply(  # noqa: SLF001
        CycleOutcome(
            summary="I noticed gold looks bullish on the Fed pause headline.",
            goal_done=True,
            phase=AgentPhase.IDLE,
            worked=True,
        ),
        goal,
    )

    assert len(announced) == 1
    assert "XAU bullish: extracted from narration." in announced[0]


async def test_a_finished_cycle_that_matches_nothing_logs_the_raw_answer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A human who never sees this agent announce anything has no way to
    tell "it never found anything" from "the pattern eats everything it
    finds" -- the raw answer has to survive somewhere even when chat stays
    silent, or a broken pattern looks identical to a quiet news day forever."""
    provider = ScriptedProvider(step="Still nothing shaped like the format.")
    environment, agent = await worker(tmp_path, provider)
    goal = agent.mind.add_goal(
        "Assess headlines",
        recurring=True,
        notify=True,
        report_pattern=r"^[A-Z]+ (bullish|bearish): .+$",
    )
    runtime = environment.runtimes[agent.id]
    announced: list[str] = []

    async def record(text: str) -> None:
        announced.append(text)

    runtime.announce = record

    with caplog.at_level(logging.INFO):
        await runtime._apply(  # noqa: SLF001
            CycleOutcome(
                summary="Read three headlines, none of them moved anything.",
                goal_done=True,
                phase=AgentPhase.IDLE,
                worked=True,
            ),
            goal,
        )

    assert announced == []
    assert any(
        "matched nothing" in record.message
        and "Read three headlines" in record.message
        for record in caplog.records
    )


async def test_a_one_shot_goals_first_completion_is_progress_not_finished(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The first goal_done on a fresh, non-recurring goal is a small model's
    rubber stamp (see _apply), not a real finish -- logged as progress, not
    announced, since calling it "finished" would tell a human the work is
    over right before the agent quietly re-checks it once more. A step that
    is only progress is mechanics for the log, not something worth
    interrupting a human over; only the real finish is announced."""
    environment, agent = await worker(tmp_path, ScriptedProvider())
    goal = agent.mind.add_goal("Summarize the notes", notify=True)
    runtime = environment.runtimes[agent.id]
    announced: list[str] = []

    async def record(text: str) -> None:
        announced.append(text)

    runtime.announce = record
    outcome = CycleOutcome(
        summary="done", step="read the notes", goal_done=True, phase=AgentPhase.IDLE, worked=True
    )

    with caplog.at_level(logging.INFO, logger="evomesh.agents"):
        await runtime._apply(outcome, goal)  # noqa: SLF001 - first cycle: the rubber stamp
    assert not announced, "a rubber-stamp completion is progress, not a real finish"
    assert "progress" in caplog.text

    caplog.clear()
    await runtime._apply(outcome, goal)  # noqa: SLF001 - second cycle: the real finish
    assert len(announced) == 1
    assert "finished" in announced[0]
    await environment.stop()


async def test_a_notified_goal_logs_progress_on_an_ordinary_step_instead_of_announcing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Mid-plan progress is mechanics, not the answer a recurring goal was
    asked to produce -- worth having in the logs for whoever wants to look,
    not worth interrupting a human over. Only a genuine finish (or an error)
    reaches announce()."""
    environment, agent = await worker(tmp_path, ScriptedProvider())
    goal = agent.mind.add_goal("Check example.com", recurring=True, notify=True)
    runtime = environment.runtimes[agent.id]
    announced: list[str] = []

    async def record(text: str) -> None:
        announced.append(text)

    runtime.announce = record

    with caplog.at_level(logging.INFO, logger="evomesh.agents"):
        await runtime._apply(  # noqa: SLF001 - exercising the wiring directly, not a full cycle
            CycleOutcome(step="opened the page", phase=AgentPhase.ACTING, worked=True),
            goal,
        )

    assert not announced
    assert "progress" in caplog.text
    assert "opened the page" in caplog.text
    await environment.stop()


async def test_a_notified_goal_announces_an_error(tmp_path: Path) -> None:
    environment, agent = await worker(tmp_path, ScriptedProvider())
    goal = agent.mind.add_goal("Check example.com", recurring=True, notify=True)
    runtime = environment.runtimes[agent.id]
    announced: list[str] = []

    async def record(text: str) -> None:
        announced.append(text)

    runtime.announce = record

    await runtime._apply(  # noqa: SLF001 - exercising the wiring directly, not through a full cycle
        CycleOutcome(error="the site timed out", phase=AgentPhase.ERROR, worked=True),
        goal,
    )

    assert len(announced) == 1
    assert "error" in announced[0]
    assert "the site timed out" in announced[0]
    await environment.stop()


async def test_notify_defaults_off(tmp_path: Path) -> None:
    environment, agent = await worker(tmp_path, ScriptedProvider())
    goal = agent.mind.add_goal("Check example.com", recurring=True)
    runtime = environment.runtimes[agent.id]
    announced: list[str] = []

    async def record(text: str) -> None:
        announced.append(text)

    runtime.announce = record

    await runtime._apply(  # noqa: SLF001 - exercising the wiring directly, not through a full cycle
        CycleOutcome(summary="all quiet", goal_done=True, phase=AgentPhase.IDLE, worked=True),
        goal,
    )

    assert announced == []
    await environment.stop()


async def test_environment_announce_is_also_kept_for_pull_based_polling(tmp_path: Path) -> None:
    """Telegram gets announcements pushed; a request-response channel like the
    control port cannot be pushed to, so the same text has to be readable by
    cursor too."""
    environment, _agent = await worker(tmp_path, ScriptedProvider())

    await environment.announce("first")
    await environment.announce("second")

    ids = [item[0] for item in environment.announcement_log]
    texts = [item[2] for item in environment.announcement_log]
    assert texts == ["first", "second"]
    assert ids == sorted(ids)
    await environment.stop()


async def test_ask_agent_reaches_a_live_agents_reactive_answer(tmp_path: Path) -> None:
    """The whole point of ask_agent (harness_tools.tool_ask_agent, wired
    through Environment._make_ask_agent): a real synchronous round trip
    through the same reactive path a human's /chat command uses, not a
    message left for some later cycle to notice."""
    provider = MockProvider(["Flat, no open positions."])
    environment = Environment(settings_for(tmp_path), {"ollama": provider})
    await environment.start()
    trader = AgentDefinition(name="Trader", purpose="Trade")
    await environment.register_agent(trader)
    await environment.start_agent(trader.id, start_delay=3600)

    ask = environment._make_ask_agent("news-watcher")  # noqa: SLF001
    answer = await ask("Trader", "what is your current position?")

    assert answer == "Flat, no open positions."
    await environment.stop()


async def test_ask_agent_reply_cannot_be_stolen_by_the_targets_own_message_loop(
    tmp_path: Path,
) -> None:
    """The reason for the private reply_to mailbox: the target agent's own
    _message_loop never stops listening on its own agent_id mailbox, so a
    naive reply-to-sender would be a race between this call's own wait and
    that loop's next iteration -- run several askers at once and every one
    of them still has to get back its own answer, not someone else's."""
    provider = MockProvider(["the only answer this mock ever gives"])
    environment = Environment(settings_for(tmp_path), {"ollama": provider})
    await environment.start()
    trader = AgentDefinition(name="Trader", purpose="Trade")
    await environment.register_agent(trader)
    await environment.start_agent(trader.id, start_delay=3600)
    ask = environment._make_ask_agent("asker")  # noqa: SLF001

    answers = await asyncio.gather(*(ask("Trader", f"question {i}") for i in range(5)))

    assert answers == ["the only answer this mock ever gives"] * 5
    await environment.stop()


async def test_ask_agents_private_mailbox_is_cleaned_up_after_it_answers(
    tmp_path: Path,
) -> None:
    """Nothing ever removed the one-shot `ask:<uuid>` mailbox each call
    creates -- every ask_agent call, answered or not, leaked one entry into
    MessageBus._mailboxes forever, the same unbounded-growth failure already
    fixed elsewhere in this project (generation worktrees, filesystem
    grants, mesh.log, the harness job queue)."""
    provider = MockProvider(["Flat, no open positions."])
    environment = Environment(settings_for(tmp_path), {"ollama": provider})
    await environment.start()
    trader = AgentDefinition(name="Trader", purpose="Trade")
    await environment.register_agent(trader)
    await environment.start_agent(trader.id, start_delay=3600)
    ask = environment._make_ask_agent("news-watcher")  # noqa: SLF001
    before = set(environment.bus._mailboxes)  # noqa: SLF001

    await ask("Trader", "what is your current position?")

    after = set(environment.bus._mailboxes)  # noqa: SLF001
    assert after == before, f"a reply mailbox was left behind: {after - before}"
    await environment.stop()


async def test_ask_agents_private_mailbox_is_cleaned_up_even_when_the_call_fails(
    tmp_path: Path,
) -> None:
    """The cleanup has to run on the unhappy path too -- a timed-out or
    errored ask() must not be the one case that still leaks the mailbox."""
    environment, agent = await worker(tmp_path, ScriptedProvider())
    ask = environment._make_ask_agent("asker")  # noqa: SLF001
    before = set(environment.bus._mailboxes)  # noqa: SLF001

    original_receive = environment.bus.receive

    async def flaky_for_ask_mailboxes(
        agent_id: str, wait_seconds: float | None = None
    ) -> object:
        if agent_id.startswith("ask:"):
            raise TimeoutError("no reply in time")
        return await original_receive(agent_id, wait_seconds)

    environment.bus.receive = flaky_for_ask_mailboxes  # type: ignore[method-assign]

    with pytest.raises(TimeoutError):
        await ask("Worker", "anything")

    after = set(environment.bus._mailboxes)  # noqa: SLF001
    assert after == before, f"a reply mailbox was left behind: {after - before}"
    environment.bus.receive = original_receive  # type: ignore[method-assign]
    await environment.stop()


async def test_ask_agent_refuses_to_ask_itself(tmp_path: Path) -> None:
    environment, agent = await worker(tmp_path, ScriptedProvider())
    ask = environment._make_ask_agent(agent.id)  # noqa: SLF001

    with pytest.raises(ValueError, match="cannot ask itself"):
        await ask(agent.id, "anything")

    await environment.stop()


# -- means-ends reasoning -----------------------------------------------


async def test_a_library_plan_is_used_without_calling_the_model(tmp_path: Path) -> None:
    provider = ScriptedProvider()
    environment = Environment(settings_for(tmp_path), {"ollama": provider})
    await environment.start(start_agent_loops=True)

    await environment.cycle_agent("guardian")

    guardian = environment.registry.get("guardian")
    intention = guardian.mind.intentions[-1]
    assert intention.plan in {"health-sweep", "investigate-degradation"}
    assert provider.calls == [], "a deterministic agent must not touch the model"
    await environment.stop()


async def test_a_model_that_is_down_still_yields_a_usable_plan(tmp_path: Path) -> None:
    class Broken(MockProvider):
        async def generate(
            self,
            prompt: str,
            *,
            system: str = "",
            model: str | None = None,
            num_ctx: int | None = None,
            format: dict[str, Any] | None = None,
        ) -> str:
            raise RuntimeError("model is down")

    environment, agent = await worker(tmp_path, Broken(), goal="Keep the notes tidy")

    outcome = await environment.cycle_agent("Worker")

    intention = agent.mind.intentions[-1]
    assert intention.plan == "ad-hoc"
    assert intention.steps[0].description == "Keep the notes tidy"
    assert outcome.error is not None, "planning degraded, execution still reported the failure"
    await environment.stop()


def test_plan_parsing_rejects_a_reply_that_is_not_a_plan() -> None:
    assert parse_plan("1. one\n2. two") == ["one", "two"]
    assert parse_plan("- alpha\n- beta") == ["alpha", "beta"]
    # A model that ignores the format and answers in fields has given no plan.
    assert parse_plan("STEP: x\nRESULT: y\nDONE: no") == []
    assert len(parse_plan("\n".join(f"{i}. step" for i in range(9)))) == 4


# -- dropping an impossible intention ------------------------------------


async def test_a_blocked_step_drops_the_intention_and_blocks_the_goal(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(step="RESULT: no access to the folder\nSTATUS: blocked\n")
    environment, agent = await worker(tmp_path, provider)

    outcome = await environment.cycle_agent("Worker")

    assert "impossible" in outcome.summary
    assert agent.mind.intentions[-1].status is IntentionStatus.IMPOSSIBLE
    assert agent.mind.goals[0].status is GoalStatus.BLOCKED
    assert agent.mind.current_intention() is None
    await environment.stop()


# -- option generation ----------------------------------------------------


async def test_a_behavior_can_generate_a_desire_that_becomes_a_goal(
    tmp_path: Path,
) -> None:
    class Ambitious(BDIBehavior):
        name = "ambitious"

        async def perceive(self, context: CycleContext) -> list[Belief]:
            return [Belief(key="disk.full", statement="the disk is full")]

        async def options(
            self, context: CycleContext, change: BeliefChange
        ) -> list[Desire]:
            if "disk.full" in change.keys:
                return [Desire("Free up disk space", priority=1)]
            return []

        def library(self) -> PlanLibrary:
            return PlanLibrary(
                (PlanRecipe(name="tidy", steps=("delete old candidates",)),)
            )

        async def execute(
            self, context: CycleContext, intention: Intention, step: PlanStep
        ) -> StepResult:
            return StepResult(summary=f"ran: {step.description}")

    definition = AgentDefinition(name="Ambitious", purpose="Keep the disk clean")
    definition.mind.add_goal("Routine upkeep", priority=5)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
    )

    outcome = await Ambitious().cycle(context)

    assert any(goal.description == "Free up disk space" for goal in definition.mind.goals)
    # The new desire outranks the routine goal, so that is what it committed to.
    intention = definition.mind.intentions[-1]
    assert definition.mind.goal(intention.goal_id).description == "Free up disk space"
    assert intention.plan == "tidy"
    assert "delete old candidates" in outcome.summary


# -- the prompt carries the mental state ---------------------------------


async def test_beliefs_and_the_committed_plan_reach_the_prompt(tmp_path: Path) -> None:
    definition = AgentDefinition(name="Grounded", purpose="Stay grounded")
    goal = definition.mind.add_goal("Tidy the notes")
    definition.mind.revise([Belief(key="notes.count", statement="there are 12 notes")])
    definition.mind.commit(goal.id, ["open the folder", "sort the notes"], plan="model")
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
    )

    prompt = await context.build_prompt("do the next step")

    assert "there are 12 notes" in prompt
    assert "open the folder" in prompt
    assert "You are on: open the folder" in prompt


async def test_the_console_shows_beliefs_and_intentions(tmp_path: Path) -> None:
    from evomesh.console import ConsoleChannel

    environment = Environment(settings_for(tmp_path), {"ollama": ScriptedProvider()})
    await environment.start(start_agent_loops=True)
    await environment.cycle_agent("guardian")
    console = ConsoleChannel(environment)

    beliefs = await console.route("/beliefs guardian")
    intentions = await console.route("/intentions guardian")

    assert "provider.ready" in beliefs
    assert "mesh.degraded" in beliefs
    assert "plan '" in intentions
    assert "[x]" in intentions or "[ ]" in intentions
    await environment.stop()


async def test_the_evolver_keeps_one_commitment_across_the_whole_pipeline(
    tmp_path: Path,
) -> None:
    """A plan that advances its own state must not treat that as a reason to re-plan."""
    from evomesh.behaviors import EvolverBehavior
    from evomesh.evolution import CandidateWorkspace, EnvironmentEvolver, ValidationResult
    from evomesh.storage import SQLiteRepository

    from .fakes import FakeHarness

    class StubValidator:
        async def validate(self, generation: object) -> ValidationResult:
            return ValidationResult(passed=True, commands=[{"command": "stub", "exit_code": 0}])

    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("ACTIVE = True\n", encoding="utf-8")
    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    mutation = '{"relative_path": "src/app.py", "content": "X = 1\\n", "rationale": "flip"}'
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"),
        repository,
        MockProvider([mutation]),
        StubValidator(),  # type: ignore[arg-type]
    )
    definition = AgentDefinition(name="Environment Evolver", purpose="Evolve")
    definition.mind.add_goal("Improve health reporting", recurring=True)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
        services={"evolver": evolver, "harness": FakeHarness([[("src/app.py", "X = 1\n")]])},
    )
    behavior = EvolverBehavior(auto_validate=True)

    for _ in range(4):
        await behavior.cycle(context)

    committed = definition.mind.intentions
    assert len(committed) == 1, "one plan carried the whole pipeline, not one per stage"
    intention = committed[0]
    assert intention.plan == "evolve-generation"
    # Repair is the fourth step and nothing broke, so it is the one box the
    # checklist honestly leaves unticked.
    assert [step.status for step in intention.steps] == [
        StepStatus.DONE,
        StepStatus.DONE,
        StepStatus.DONE,
        StepStatus.PENDING,
        StepStatus.DONE,
    ]
    assert (await evolver.pipeline_state())["stage"] == "await-human"


async def test_entering_await_human_announces_once_to_chat(tmp_path: Path) -> None:
    """A human away from the console must hear that evolution is parked, and
    why -- not just find out by checking /evolution status days later."""
    from evomesh.behaviors import EvolverBehavior
    from evomesh.evolution import CandidateWorkspace, EnvironmentEvolver, ValidationResult
    from evomesh.storage import SQLiteRepository

    from .fakes import FakeHarness

    class StubValidator:
        async def validate(self, generation: object) -> ValidationResult:
            return ValidationResult(passed=True, commands=[{"command": "stub", "exit_code": 0}])

    class FakeEnvironment:
        def __init__(self) -> None:
            self.announced: list[str] = []

        async def announce(self, text: str) -> None:
            self.announced.append(text)

    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("ACTIVE = True\n", encoding="utf-8")
    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    mutation = '{"relative_path": "src/app.py", "content": "X = 1\\n", "rationale": "flip"}'
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"),
        repository,
        MockProvider([mutation]),
        StubValidator(),  # type: ignore[arg-type]
    )
    definition = AgentDefinition(name="Environment Evolver", purpose="Evolve")
    definition.mind.add_goal("Improve health reporting", recurring=True)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    fake_environment = FakeEnvironment()
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
        services={
            "evolver": evolver,
            "harness": FakeHarness([[("src/app.py", "X = 1\n")]]),
            "environment": fake_environment,
        },
    )
    behavior = EvolverBehavior(auto_validate=True)

    for _ in range(4):
        await behavior.cycle(context)
    assert (await evolver.pipeline_state())["stage"] == "await-human"
    assert len(fake_environment.announced) == 1, "announced exactly once, on the transition"
    assert fake_environment.announced[0].startswith("Evolution needs you: ")
    assert "generation" in fake_environment.announced[0]

    # Parked cycles that follow must not repeat the announcement.
    for _ in range(3):
        await behavior.cycle(context)
    assert len(fake_environment.announced) == 1, "still just the one announcement"


async def test_a_waiting_evolver_keeps_its_commitment_instead_of_re_adopting(
    tmp_path: Path,
) -> None:
    """Parked on a human decision, it must not burn one plan per cycle."""
    from evomesh.behaviors import EvolverBehavior
    from evomesh.evolution import CandidateWorkspace, EnvironmentEvolver
    from evomesh.storage import SQLiteRepository

    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("ACTIVE = True\n", encoding="utf-8")
    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"), repository, MockProvider()
    )
    await evolver.set_pipeline_state({"stage": "await-human", "generation": 2})
    definition = AgentDefinition(name="Environment Evolver", purpose="Evolve")
    definition.mind.add_goal("Improve EvoMesh", recurring=True)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
        services={"evolver": evolver},
    )
    behavior = EvolverBehavior(auto_validate=False)

    outcome = await behavior.cycle(context)
    for _ in range(3):
        outcome = await behavior.cycle(context)

    assert "waiting for a human" in outcome.summary
    assert len(definition.mind.intentions) == 1, "one held commitment, not four"
    intention = definition.mind.intentions[0]
    assert intention.status is IntentionStatus.ACTIVE
    assert intention.cursor == 0, "a held step is not consumed"
    # With validation off, the plan never advertises a step that will not run.
    assert "validate the candidate" not in [step.description for step in intention.steps]


async def test_a_reactive_question_calls_a_tool_through_the_harness_when_granted(
    tmp_path: Path,
) -> None:
    """A direct chat question must not just answer from memory when the agent
    has real tool access -- the same gap through_harness() closes for a plan
    step, closed here for a human's own question."""
    from tests.fakes import FakeHarness

    definition = AgentDefinition(
        name="Trader", purpose="Trade", harness_root=str(tmp_path)
    )
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    harness = FakeHarness([[]], answer="Balance is 10247.53, equity 10251.88.")
    context = CycleContext(
        definition=definition,
        provider=MockProvider(["should never be called"]),
        memory=memory,
        budget=MemoryBudget(),
        services={"harness": harness},
    )

    answer = await BDIBehavior().respond(
        context,
        Message(sender_id="human", recipient_id=definition.id, content="What is my balance?"),
    )

    assert answer == "Balance is 10247.53, equity 10251.88."
    assert harness.objectives and "What is my balance?" in harness.objectives[0]


async def test_a_reactive_question_submits_as_priority(tmp_path: Path) -> None:
    """A human waiting on a chat reply must cut ahead of whatever background
    work (the Evolver's pipeline, another agent's own plan step) is already
    queued -- see HarnessQueue's own priority ordering."""
    from tests.fakes import FakeHarness

    definition = AgentDefinition(name="Trader", purpose="Trade", harness_root=str(tmp_path))
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    harness = FakeHarness([[]], answer="Balance is 10247.53, equity 10251.88.")
    context = CycleContext(
        definition=definition,
        provider=MockProvider(["should never be called"]),
        memory=memory,
        budget=MemoryBudget(),
        services={"harness": harness},
    )

    await BDIBehavior().respond(
        context,
        Message(sender_id="human", recipient_id=definition.id, content="What is my balance?"),
    )

    assert harness.priorities == [True]


async def test_a_reactive_question_carries_the_recent_conversation(tmp_path: Path) -> None:
    """A harness job otherwise sees only the one bare message -- "send it as
    a PDF" names no content of its own, and without the preceding "give me
    the last 10 news" two messages back a job has nothing to build one from.
    Found live: NewsWatcher asked what a bare "send it as PDF" should
    contain, then -- still with no memory of "as a PDF" -- just repeated the
    headlines as chat text again instead of ever reaching document_write."""
    from tests.fakes import FakeHarness

    definition = AgentDefinition(
        name="NewsWatcher", purpose="Watch news", harness_root=str(tmp_path)
    )
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    harness = FakeHarness([[]], answer="done")
    earlier = Message(sender_id="human", recipient_id=definition.id, content="give me last 10 news")
    current = Message(sender_id="human", recipient_id=definition.id, content="send it as a PDF")
    context = CycleContext(
        definition=definition,
        provider=MockProvider(["should never be called"]),
        memory=memory,
        budget=MemoryBudget(),
        services={"harness": harness},
        inbox=[earlier, current],
    )

    await BDIBehavior().respond(context, current)

    assert harness.objectives
    objective = harness.objectives[0]
    assert "give me last 10 news" in objective
    assert "send it as a PDF" in objective
    # Not duplicated: the current message is already named by "Answer this
    # question directly: ...", the recent-messages section only repeats
    # what came *before* it.
    assert objective.count("send it as a PDF") == 1


async def test_a_reactive_question_with_no_prior_history_carries_no_hint(
    tmp_path: Path,
) -> None:
    """A first message in a conversation has nothing before it -- the recent-
    messages section must not appear at all, not an empty one."""
    from tests.fakes import FakeHarness

    definition = AgentDefinition(name="Trader", purpose="Trade", harness_root=str(tmp_path))
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    harness = FakeHarness([[]], answer="done")
    message = Message(sender_id="human", recipient_id=definition.id, content="hello")
    context = CycleContext(
        definition=definition,
        provider=MockProvider(["should never be called"]),
        memory=memory,
        budget=MemoryBudget(),
        services={"harness": harness},
        inbox=[message],
    )

    await BDIBehavior().respond(context, message)

    assert harness.objectives
    assert "Recent messages" not in harness.objectives[0]


async def test_a_reactive_question_names_the_agents_own_skills_to_the_harness(
    tmp_path: Path,
) -> None:
    """Found live: NewsAnalyzer answered a chat question with a full
    narrated report despite its own news-impact-analysis skill spelling out
    a one-line answer format with BAD/GOOD examples -- the skill was simply
    never read for a reactive question, only during its recurring goal. The
    mesh-wide skill catalog every harness job already gets is easy to skim
    past; naming this agent's own skills imperatively is what closes that."""
    from tests.fakes import FakeHarness

    definition = AgentDefinition(
        name="NewsAnalyzer",
        purpose="Analyze news",
        harness_root=str(tmp_path),
        skills=["news-impact-analysis"],
    )
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    harness = FakeHarness([[]], answer="XAUUSD bearish (medium): Fed hawkish -- rates up.")
    context = CycleContext(
        definition=definition,
        provider=MockProvider(["should never be called"]),
        memory=memory,
        budget=MemoryBudget(),
        services={"harness": harness},
    )

    await BDIBehavior().respond(
        context,
        Message(sender_id="human", recipient_id=definition.id, content="any news on gold?"),
    )

    assert harness.objectives
    assert "news-impact-analysis" in harness.objectives[0]
    assert "read it first" in harness.objectives[0]


async def test_a_pdf_export_request_points_the_news_watcher_at_its_own_skill(
    tmp_path: Path,
) -> None:
    """A news-watcher-shaped agent (its real skills: from AGENT.md) asking
    for "the last 10 news as a PDF" gets news-report-export named to it the
    same way news-impact-analysis gets named above -- the skill is what
    actually spells out news_fetch then document_write then FILE: <path>;
    without being pointed at it, a small model has no reason to know that
    order, or that document_write exists at all for this."""
    from tests.fakes import FakeHarness

    definition = AgentDefinition(
        name="NewsWatcher",
        purpose="Watch news",
        harness_root=str(tmp_path),
        skills=["news-triage", "news-report-export"],
    )
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    harness = FakeHarness([[]], answer="FILE: news.pdf")
    context = CycleContext(
        definition=definition,
        provider=MockProvider(["should never be called"]),
        memory=memory,
        budget=MemoryBudget(),
        services={"harness": harness},
    )

    await BDIBehavior().respond(
        context,
        Message(
            sender_id="human",
            recipient_id=definition.id,
            content="give me the last 10 news as a PDF",
        ),
    )

    assert harness.objectives
    assert "news-report-export" in harness.objectives[0]
    assert "FILE: <path>" in harness.objectives[0]


async def test_a_plan_step_nudges_a_granted_agent_to_learn_from_it(tmp_path: Path) -> None:
    """learn_skill reaches a plan step's own harness job the same way it
    reaches a reactive chat reply (both go through environment.py's single
    _run_harness_job) -- but through_harness()'s own task text needed the
    same nudge _respond_through_harness got, or a cyclic goal (where a
    reusable procedure is actually most likely to emerge) would never
    mention the tool at all."""
    from tests.fakes import FakeHarness

    definition = AgentDefinition(
        name="NewsWatcher",
        purpose="Watch news",
        harness_root=str(tmp_path),
        can_learn_skills=True,
    )
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    harness = FakeHarness([[]], answer="done")
    context = CycleContext(
        definition=definition,
        provider=MockProvider(["should never be called"]),
        memory=memory,
        budget=MemoryBudget(),
        services={"harness": harness},
    )
    step = PlanStep(description="investigate the export options and report back")

    await BDIBehavior().through_harness(context, step)

    assert harness.objectives
    assert "learn_skill" in harness.objectives[0]


async def test_a_plan_step_says_nothing_about_learn_skill_without_the_grant(
    tmp_path: Path,
) -> None:
    from tests.fakes import FakeHarness

    definition = AgentDefinition(
        name="NewsWatcher", purpose="Watch news", harness_root=str(tmp_path)
    )  # can_learn_skills: False
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    harness = FakeHarness([[]], answer="done")
    context = CycleContext(
        definition=definition,
        provider=MockProvider(["should never be called"]),
        memory=memory,
        budget=MemoryBudget(),
        services={"harness": harness},
    )
    step = PlanStep(description="investigate the export options and report back")

    await BDIBehavior().through_harness(context, step)

    assert harness.objectives
    assert "learn_skill" not in harness.objectives[0]


async def test_a_reactive_question_tells_the_harness_job_to_hand_a_document_back(
    tmp_path: Path,
) -> None:
    """document_write creates a real .pdf/.docx/.xlsx -- but nothing hands it
    back to the human unless the model's own reply says FILE: <path>
    (cognition.extract_file_references, uploaded by telegram.py). The plain
    non-harness respond() instruction has always said this; the harness task
    _respond_through_harness submits did not, so a chat answer that actually
    called document_write during a harness job could create the file and
    still never mention it."""
    from tests.fakes import FakeHarness

    definition = AgentDefinition(
        name="NewsWatcher", purpose="Watch news", harness_root=str(tmp_path)
    )
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    harness = FakeHarness([[]], answer="FILE: news.pdf")
    context = CycleContext(
        definition=definition,
        provider=MockProvider(["should never be called"]),
        memory=memory,
        budget=MemoryBudget(),
        services={"harness": harness},
    )

    answer = await BDIBehavior().respond(
        context,
        Message(
            sender_id="human",
            recipient_id=definition.id,
            content="give me the last 10 news as a PDF",
        ),
    )

    assert answer == "FILE: news.pdf"
    assert harness.objectives and "FILE: <path>" in harness.objectives[0]


async def test_a_reactive_question_answers_from_memory_without_harness_access(
    tmp_path: Path,
) -> None:
    """No harness_root granted -- unchanged behavior, answer from the model."""
    definition = AgentDefinition(name="Architect", purpose="Draft agents")
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    context = CycleContext(
        definition=definition,
        provider=MockProvider(["From memory: no tool access here."]),
        memory=memory,
        budget=MemoryBudget(),
        services={},
    )

    answer = await BDIBehavior().respond(
        context,
        Message(sender_id="human", recipient_id=definition.id, content="What is my balance?"),
    )

    assert answer == "From memory: no tool access here."


async def test_a_reactive_question_answers_from_memory_when_a_job_is_already_open(
    tmp_path: Path,
) -> None:
    """An agent already mid-job on something else must not have that job
    hijacked (or a second one queued) by an unrelated question."""
    from evomesh.harness_queue import HarnessGateway

    definition = AgentDefinition(
        name="Trader", purpose="Trade", harness_root=str(tmp_path)
    )
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    harness = HarnessGateway(HarnessQueue(), {})
    harness.submit("investigate something else entirely", agent_id=definition.id, root=tmp_path)
    context = CycleContext(
        definition=definition,
        provider=MockProvider(["Answering from memory instead."]),
        memory=memory,
        budget=MemoryBudget(),
        services={"harness": harness},
    )

    answer = await BDIBehavior().respond(
        context,
        Message(sender_id="human", recipient_id=definition.id, content="What is my balance?"),
    )

    assert answer == "Answering from memory instead."


def test_parse_plan_extracts_numbered_steps() -> None:
    plan = parse_plan(
        "1. Check the account\n2. Place a market order\n3. Set a stop loss\n"
    )

    assert plan == ["Check the account", "Place a market order", "Set a stop loss"]


# -- waking a cycle early -------------------------------------------------------


async def _cycles_reach(runtime: Any, count: int, within: float) -> bool:
    for _ in range(int(within / 0.05)):
        if runtime.state.cycles >= count:
            return True
        await asyncio.sleep(0.05)
    return runtime.state.cycles >= count


async def test_a_new_agent_is_due_at_once_however_long_its_interval(tmp_path: Path) -> None:
    """The first cycle used to be due only once time.monotonic() -- seconds
    since boot on Linux -- passed the agent's own interval: CI's fresh VM never
    cycled a 600-second agent, and a just-booted host would hold every agent
    back the same way. An interval longer than any uptime pins it down."""
    environment = Environment(settings_for(tmp_path), {"ollama": ScriptedProvider()})
    await environment.start()
    agent = AgentDefinition(
        name="Worker", purpose="Work", status=AgentStatus.ACTIVE, cycle_seconds=10**9
    )
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)
    runtime = environment.runtimes[agent.id]

    assert runtime._due_in() <= 0  # noqa: SLF001 - the loop's own check, before any cycle
    assert runtime.stuck_for() is None
    # The status every reply carries says so, instead of round(-inf) raising
    # inside the reply path (found when the fix above first went in: every
    # chat with an agent that had not cycled yet hung).
    assert "next cycle in about 0s" in runtime._work_summary()  # noqa: SLF001
    await environment.stop()


async def _slow_worker(tmp_path: Path, provider: MockProvider) -> tuple[Environment, Any]:
    """An agent whose own interval is ten minutes, cycling once at start."""
    environment = Environment(settings_for(tmp_path), {"ollama": provider})
    await environment.start()
    agent = AgentDefinition(
        name="Worker", purpose="Work", status=AgentStatus.ACTIVE, cycle_seconds=600
    )
    agent.mind.add_goal("Summarize the notes")
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=0)
    runtime = environment.runtimes[agent.id]
    assert await _cycles_reach(runtime, 1, 5.0)
    return environment, runtime


async def test_a_woken_agent_cycles_now_not_at_the_end_of_its_interval(
    tmp_path: Path,
) -> None:
    """Found 2026-09-24: generation 1382's edit took 37 seconds and the
    generation fourteen minutes -- five stages each waiting out a 120-second
    interval for work that had already finished."""
    environment, runtime = await _slow_worker(tmp_path, ScriptedProvider())

    runtime.wake()

    assert await _cycles_reach(runtime, 2, WAKE_MIN_GAP + 3.0)
    await environment.stop()


async def test_a_finished_background_job_wakes_the_agent_that_polls_for_it(
    tmp_path: Path,
) -> None:
    """notify=False jobs (a pipeline stage, a plan step) are not delivered as
    messages -- but the agent still has to consume them, so it is woken."""
    from evomesh.harness_queue import HarnessJob

    environment, runtime = await _slow_worker(tmp_path, ScriptedProvider())
    job = HarnessJob(
        number=1, objective="x", root=tmp_path, agent_id=runtime.definition.id, notify=False
    )

    await environment._deliver_harness(job)  # noqa: SLF001 - the worker's own completion hook

    assert await _cycles_reach(runtime, 2, WAKE_MIN_GAP + 3.0)
    await environment.stop()


async def test_a_cycle_that_asks_again_is_followed_at_once_but_never_in_a_spin(
    tmp_path: Path,
) -> None:
    class Eager(ReflectiveBehavior):
        async def cycle(self, context: CycleContext) -> CycleOutcome:
            return CycleOutcome(summary="more to do", again=True)

    environment = Environment(settings_for(tmp_path), {"ollama": ScriptedProvider()})
    await environment.start()
    agent = AgentDefinition(
        name="Eager", purpose="Work", status=AgentStatus.ACTIVE, cycle_seconds=600
    )
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=0)
    runtime = environment.runtimes[agent.id]
    runtime.behavior = Eager()

    runtime.wake()
    assert await _cycles_reach(runtime, 3, 2 * WAKE_MIN_GAP + 3.0)
    # Asking every time still leaves WAKE_MIN_GAP between two cycles.
    assert runtime.state.cycles <= 2 + int(8.0 / WAKE_MIN_GAP)
    await environment.stop()


def test_deterministic_behavior_builds_a_plan_library_from_its_plans() -> None:
    """DeterministicBehavior::library wraps the plans it was given, not prompts."""
    recipe = PlanRecipe(name="noop", steps=())
    behavior = DeterministicBehavior(plans=(recipe,))

    # Its name is a convenience base for "code, not prompts": it exposes the
    # plans it was constructed with through a PlanLibrary, exactly as given.
    library = behavior.library()
    assert library.recipes == (recipe,)
    assert library.select(Goal(description="noop"), MindState()) is recipe


def test_report_pattern_forgives_dashes_bullets_and_bold() -> None:
    """Found live: well-formed NewsAnalyzer signals dropped for an em dash
    instead of ``--`` or a Markdown bullet/bold around the line."""
    from evomesh.agents import _apply_report_pattern

    pattern = r"^[A-Za-z0-9_.]+ (bullish|bearish|neutral) \((low|medium|high)\): .+ -- .+$"
    answer = "\n".join(
        (
            "## Summary",
            'EURUSD bearish (medium): "Fed braces for hikes" \u2014 a hike supports the dollar.',
            "- **XAUUSD bullish (high): Gold hits a record -- safe-haven demand**",
            "**Bottom line:** one actionable assessment \u2014 EURUSD bearish, medium.",
        )
    )

    assert _apply_report_pattern(answer, pattern).splitlines() == [
        'EURUSD bearish (medium): "Fed braces for hikes" -- a hike supports the dollar.',
        "XAUUSD bullish (high): Gold hits a record -- safe-haven demand",
    ]


async def test_a_report_written_as_markdown_blocks_is_rewritten_not_lost(
    tmp_path: Path,
) -> None:
    """Found live: NewsAnalyzer wrote each signal as a Markdown block
    (Instrument / Direction / Confidence), the sanitizer was only allowed to
    *extract* lines that already matched, and every real signal was dropped."""

    class Reformatter(ScriptedProvider):
        async def generate(self, prompt: str, **kwargs: Any) -> str:
            if "Rewrite each item" in prompt:
                return "XAUUSD bearish (medium): Gold tilts lower -- rate-hike bets"
            return await super().generate(prompt, **kwargs)

    environment, agent = await worker(tmp_path, Reformatter())
    goal = agent.mind.add_goal(
        "Assess headlines",
        recurring=True,
        notify=True,
        report_pattern=(
            r"^[A-Za-z0-9_.]+ (bullish|bearish|neutral) \((low|medium|high)\): .+ -- .+$"
        ),
    )
    runtime = environment.runtimes[agent.id]
    announced: list[str] = []

    async def record(text: str) -> None:
        announced.append(text)

    runtime.announce = record

    await runtime._apply(  # noqa: SLF001
        CycleOutcome(
            summary=(
                "## Reported\n\n**Gold tilts lower as rate-hike bets grow**\n"
                "- Instrument: **XAUUSD**\n- Direction: **Bearish**\n- Confidence: **Medium**"
            ),
            goal_done=True,
            phase=AgentPhase.IDLE,
            worked=True,
        ),
        goal,
    )

    assert len(announced) == 1
    assert "XAUUSD bearish (medium): Gold tilts lower -- rate-hike bets" in announced[0]
