from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from evomesh.contracts import McpServerConfig, TelegramSettings
from evomesh.git import (
    DEFAULT_AUTHOR_EMAIL,
    DEFAULT_AUTHOR_NAME,
    GitIdentity,
    PublishPolicy,
)
from evomesh.harness_tools import ToolLimits
from evomesh.memory import MemoryBudget

__all__ = ["McpServerConfig", "Settings", "TelegramSettings", "load_settings"]


class ProviderSettings(BaseModel):
    base_url: str
    model: str
    api_key: str | None = None
    # A name looked up in evomesh.secrets.yaml (gitignored, sibling to the
    # main config) instead of a literal key typed here. Exists so setting up
    # a new provider never means typing a real key into evomesh.yaml or,
    # worse, evomesh.yaml.example -- the latter IS committed (it is the
    # template every fresh checkout copies from), so a key pasted there by
    # habit during setup ships straight to the remote. When set, this wins
    # over `api_key` above -- load_settings() resolves it and overwrites
    # `api_key` with the real value, so every call site downstream
    # (Environment._build_providers, models.py) keeps reading the one field
    # it already knew. Two provider entries can point at two different refs
    # for the same underlying service (e.g. `openai_primary`/
    # `openai_backup`), which is how "more than one key for one provider"
    # is expressed -- an agent picks the key by picking the provider name
    # (AgentModelSettings.provider / AgentDefinition.provider), the same way
    # it already picks everything else about which endpoint it talks to.
    api_key_ref: str | None = None
    # Which wire dialect this endpoint speaks. "ollama" is Ollama's own
    # /api/generate + /api/chat; "anthropic" is Claude's Messages API;
    # "openai" is the OpenAI-compatible chat/completions shape that OpenAI
    # itself, OpenRouter, InferHub, vLLM and llama.cpp all speak. Unset falls
    # back to this provider's own key in `providers:` being literally
    # "ollama" (so an existing `providers: {ollama: ...}` block keeps working
    # unchanged) and to "openai" for every other key -- which is why
    # `inferhub`/`openai_compatible` in evomesh.yaml.example never had to
    # name a kind before this field existed.
    kind: Literal["ollama", "openai", "anthropic"] | None = None
    # A 30B model on a busy GPU answers a full prompt in minutes, not seconds.
    # Too low a ceiling here reads to a human as "the agent is broken".
    timeout_seconds: float = 600
    # Anthropic's Messages API requires this on every request (there is no
    # server-side default); every other dialect here ignores it.
    max_output_tokens: int = 8192
    # Ollama's own default is 2048 tokens regardless of what the Modelfile's
    # trained context is, and every prompt budget in this project (RuntimeSettings,
    # HarnessSettings.transcript_chars) is sized in *characters* on the assumption
    # that the server actually has room for them. Leaving this unset means the
    # server silently truncates from the oldest end -- exactly the failure the
    # character budgets exist to prevent -- so a local model gets no context
    # widening unless this is set to match. None keeps a provider's own default
    # (an OpenAI-compatible server has no equivalent knob and ignores this).
    # 64k is a starting point, not a measurement of any particular card: this
    # class has no way to know free VRAM, so it cannot size the number itself.
    num_ctx: int | None = 65536
    # Same knob, keyed by model tag, for a provider that serves more than one
    # model on the same endpoint -- a bigger reasoning model alongside a small,
    # fast one, say. Checked before the provider-wide default above whenever a
    # call names a model this dict has an entry for; an agent's own num_ctx
    # (AgentDefinition.num_ctx / AgentModelSettings.num_ctx) outranks both.
    model_num_ctx: dict[str, int] = Field(default_factory=dict)


class ModelSettings(BaseModel):
    default_provider: str = "ollama"
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)


class AgentModelSettings(BaseModel):
    provider: str
    model: str
    # Overrides the provider's num_ctx for this one agent -- the model it runs
    # may need a different window than its siblings on the same provider.
    # None defers to ProviderSettings.num_ctx instead.
    num_ctx: int | None = None


# The ratio this class's own defaults already assume, made explicit: 6000
# prompt_chars was picked so a 4096-token model is never silently truncated,
# once the system prompt, tool schemas and the model's own reply are left
# room for. RuntimeSettings.budget() applies that ratio uniformly to every
# agent, sized for the smallest model anyone in the mesh happens to be
# running -- safe, but only by accident for an agent on a genuinely small
# model whose own num_ctx nobody has told this setting about.
# budget_for_num_ctx() is what makes it deliberate.
SAFE_CHARS_PER_CONTEXT_TOKEN = 6000 / 4096


class RuntimeSettings(BaseModel):
    """How often agents think, and how much text they are allowed to think with.

    The character budgets exist for small local models. Raise them if the models
    configured above have a large context window; the defaults are sized so a
    4k-token model never has its memory silently truncated by the model server.
    """

    cycle_seconds: int = 60
    stagger_seconds: float = 1.5
    prompt_chars: int = 6000
    memory_chars: int = 3000
    context_chars: int = 1500
    inbox_chars: int = 1000
    beliefs_chars: int = 700
    preemption_enabled: bool = True
    preemption_minimum_score_delta: float = 50.0
    non_preemptible_goal_kinds: list[str] = Field(default_factory=list)
    preemption_deadline_override_seconds: float = 300.0

    def budget(self) -> MemoryBudget:
        return MemoryBudget(
            memory_chars=self.memory_chars,
            context_chars=self.context_chars,
            inbox_chars=self.inbox_chars,
            beliefs_chars=self.beliefs_chars,
            prompt_chars=self.prompt_chars,
        )

    def budget_for_num_ctx(self, num_ctx: int | None) -> MemoryBudget:
        """This agent's own budget -- shrunk below the configured defaults
        when its own resolved num_ctx is small enough to need it, never
        grown past them.

        Two agents in the same mesh can run genuinely different models (a
        35b generalist next to a 4b specialist kept small on purpose for
        cost or speed); a single global prompt_chars can only ever be safe
        for whichever one has the smallest window, wasting headroom for
        every other agent or -- worse, if a human raises it for the big
        model without noticing the small one -- silently truncating the
        small one's memory again, exactly the failure these budgets exist
        to prevent. Ungrown past the configured ceiling even for a large
        num_ctx: these are a deliberate cost/discipline choice (see
        evomesh.yaml's own comments), not "use all available context".
        """
        base = self.budget()
        if not num_ctx:
            return base
        safe_chars = num_ctx * SAFE_CHARS_PER_CONTEXT_TOKEN
        if safe_chars >= self.prompt_chars:
            return base
        scale = safe_chars / self.prompt_chars
        return MemoryBudget(
            memory_chars=max(200, round(base.memory_chars * scale)),
            context_chars=max(200, round(base.context_chars * scale)),
            inbox_chars=max(150, round(base.inbox_chars * scale)),
            beliefs_chars=max(150, round(base.beliefs_chars * scale)),
            prompt_chars=max(800, round(base.prompt_chars * scale)),
        )


class EvolutionSettings(BaseModel):
    autonomous: bool = True
    cycle_seconds: int = 300
    auto_validate: bool = True
    # How many times the Evolver may fix its own candidate before a failure
    # becomes the human's problem. Zero reports the first failure as final.
    # Raised from 2 after a whole session's logs showed several generations
    # validate real, useful leaves and then lose the entire candidate on
    # repair attempt 2 of 2 for the last one -- one more attempt is cheap
    # next to discarding work already proven.
    max_repairs: int = 3
    # Let the verdict decide: promote what validated, discard what did not, and
    # move on without asking. A run with no verdict still stops for a human.
    auto_promote: bool = False
    # A landed generation is code this process is not running. Restart into it
    # instead of leaving a human to notice the flag and do it by hand.
    auto_restart: bool = True
    # Breathing room between the decision and the shutdown, so the cycle that
    # promoted the generation finishes writing its summary to every channel.
    restart_delay_seconds: float = 5.0
    # A validation that outruns this is stopped and reported as blocked rather
    # than failed: the candidate never got a verdict.
    validate_seconds: float = 1800.0
    # Off by default. Draft a plan, have it reviewed, and recursively split it
    # into minimal work items before authoring anything, instead of asking the
    # harness for one mutation directly. See EvolverBehavior.auto_plan.
    auto_plan: bool = False
    # Off by default. Once a candidate validates, a read-only harness job reads
    # its diff against the objective; INCOMPLETE is repaired under max_repairs
    # or discarded, never landed. See EvolverBehavior._review. The budget is
    # its own: a review reads, it does not author, and should not need a
    # mutation's.
    review: bool = False
    review_max_steps: int = 40
    review_max_seconds: float = 900.0
    # On by default. Before any objective is picked, the whole test suite runs
    # on the live tree (once per landed change, in its own venv under
    # .runtime/); a red suite becomes the objective, and after
    # MAX_TARGET_ATTEMPTS failed fixes evolution waits for a human.
    baseline_tests: bool = True
    # Off by default. The "write ONE small test for an untested export"
    # fallback: with it on, ~14 of every 20 generations were a 5-line test
    # (2026-09-25). Off, an evolver with nothing substantive to do waits.
    test_backlog: bool = False
    objective: str | None = None


class HarnessSettings(BaseModel):
    """A model that can look at the project before it answers.

    Off by default. Its tools can read, search and list; with ``allow_write`` on
    they can also edit and create files, inside the job's root and no further.
    The caps are not tuning knobs -- they are what turns a model that keeps
    asking for one more file into a job that ends and says why.
    """

    enabled: bool = False
    # Whether any harness job may change a file. Off by default and separate
    # from `enabled`, so turning the harness on to ask it questions never
    # quietly grants it the ability to edit the checkout.
    allow_write: bool = False
    # Steps, not tool calls: one step is one model turn, which may ask for
    # several tools at once. Raised from 24/300 after a session's logs showed
    # 204 of 276 discarded generations (74%) were a job hitting the step cap
    # having written nothing at all -- not a bad plan, just cut off
    # mid-thought, on a model slow enough that its worst capped run already
    # spent 290 of the 300 available seconds getting there. max_seconds is
    # raised in proportion so it does not just become the next cap.
    max_steps: int = 40
    max_seconds: float = 600.0
    # The plan pipeline's own stages -- draft, evaluate, decompose, and a
    # leaf's propose -- each write exactly one small file (PLAN_DRAFT_RULES
    # etc. in evolution.py all say so) and never need the exploration a
    # from-scratch mutation or a repair does. Giving them the full budget
    # above just gives a weak model more room to wander before that one
    # write, or to cap out at 40 steps having written nothing -- the same
    # failure the smaller 24-step budget already produced, just slower to
    # discover. A leaf task that truly cannot be done in ~12 steps should
    # fail fast and free the pipeline for the next one, not burn the same
    # ten minutes a real mutation gets.
    plan_max_steps: int = 12
    plan_max_seconds: float = 120.0
    # What the model may be sent in one turn. The tools cap their own output;
    # this caps the pile of it, which is the part that grows without asking and
    # is dropped by the model server from the oldest end -- where the objective
    # lives -- when nobody caps it here.
    transcript_chars: int = 12000
    # Programs the shell tool may run, by bare name. Empty -- the default --
    # means the tool is not offered at all. This is an allow-list rather than a
    # deny-list because a deny-list is a promise that every dangerous command
    # has been thought of, and it is wrong the first time a tool is installed.
    shell_allow: list[str] = Field(default_factory=list)
    shell_seconds: float = 60.0
    # Run against a writing job's root, right before it is allowed to end,
    # whenever it actually changed a file -- e.g. "ruff check . && pyright"
    # is not one command this can run directly (see harness.py's own `shell`
    # tool: everything here is shlex.split, never a real shell), so point
    # this at a small wrapper script for more than one check. A non-zero
    # exit is fed back as one more turn so the job can fix it itself,
    # bounded by self_check_max_attempts (further bounded by the job's own
    # step budget either way) -- past that, the last failure is folded into
    # the job's own answer rather than silently disappearing. Empty (the
    # default) turns this off entirely; unlike shell_allow, this is a
    # command the human who owns this file chose, never one the model
    # picked, so it is trusted the same way harness.enabled/allow_write
    # already are.
    self_check_command: str = ""
    self_check_max_attempts: int = 2
    # When true, learn_skill/patch_skill (harness_tools.py) never write
    # straight to skills/ -- they stage the proposed content in
    # Environment.pending_skill_writes and a human commits it with
    # `/learn approve <n>` (or discards with `/learn reject <n>`). Off by
    # default: AgentDefinition.can_learn_skills is already the deliberate
    # per-agent grant; this is a second, mesh-wide dial for a human who
    # wants every write reviewed regardless of which agent made it, not a
    # requirement for the capability to work at all.
    skill_write_approval: bool = False
    # When true, a harness job that falls back to the text protocol (a model
    # with no native tool calling) has its generate() call constrained to
    # Ollama's own `format` field -- see harness.py's TEXT_PROTOCOL_FORMAT
    # and HarnessRunner.structured_fallback. Off by default: only
    # OllamaProvider honors it (see models.py, other dialects just accept
    # and drop the parameter), and it only ever touches the fallback branch
    # of _ask -- the native-tools path (chat() with tools=) is never
    # affected. Found live: a small model on the fallback path repeatedly
    # fabricated tool-call envelopes with invented field names or wrong
    # file paths, caught only after the fact by validation. Grammar-
    # constrained decoding makes an unparseable envelope structurally
    # impossible rather than merely unlikely.
    structured_fallback: bool = False

    def shell_programs(self) -> frozenset[str]:
        return frozenset(name.strip().lower() for name in self.shell_allow if name.strip())

    def transcript_chars_for_num_ctx(self, num_ctx: int | None) -> int:
        """This job's own transcript budget -- shrunk below the configured
        default when the agent it runs for has a smaller resolved num_ctx,
        never grown past it.

        Mirrors RuntimeSettings.budget_for_num_ctx: transcript_chars was
        sized in characters "on the assumption that the server actually has
        room for them" (see the field's own comment) -- true only for
        whichever num_ctx that sizing had in mind. A harness job for an
        agent on a genuinely smaller model (a per-agent num_ctx override, a
        provider default nobody raised, or an Ollama endpoint that falls
        back to its own built-in 2048) got the same flat 12000-char pile
        regardless, silently truncated from the oldest end by the model
        server -- the objective's own instructions, which live at the start
        of the transcript. Exactly the failure this project's character
        budgets exist to prevent, just not reached from this field before.
        """
        if not num_ctx:
            return self.transcript_chars
        safe_chars = round(num_ctx * SAFE_CHARS_PER_CONTEXT_TOKEN)
        return min(self.transcript_chars, max(1500, safe_chars))

    # How much of a file may enter the transcript. Rule of the house: the trim
    # is ours, and the tool says what it withheld so the model can ask again.
    tool_result_chars: int = 4000
    tool_result_lines: int = 200
    grep_matches: int = 40
    # Empty means .runtime/harness next to the checkout.
    session_path: Path = Path(".runtime/harness")
    # One tool loop at a time. Two on one card do not go twice as fast; they
    # queue inside the GPU, where nothing can see them, instead of in a queue
    # where /harness status can. The number is a setting because a second
    # card or a remote provider is a different bet, and a hard-coded 1 is an
    # argument nobody can test.
    workers: int = 1
    # A separate, dedicated worker that reads only priority jobs -- a human's
    # reactive question (bdi.py's respond(), submitted with priority=True),
    # never the Evolver's pipeline or an agent's own plan step. Without one,
    # a question asked while the background lane is deep into a
    # harness.max_seconds job waits out the rest of it even though it always
    # sorted first in the old shared queue: priority there only changed
    # order, never preempted a job already running. Raising this past 1 buys
    # nothing extra on the single-GPU setup `workers` above already warns
    # about; it exists as a lane, not a throughput knob.
    priority_workers: int = 1
    max_queue: int = 8

    def limits(self) -> ToolLimits:
        return ToolLimits(
            result_chars=self.tool_result_chars,
            result_lines=self.tool_result_lines,
            grep_matches=self.grep_matches,
        )


class ScrapingSettings(BaseModel):
    """The Web.Fetch skill: an agent asks for a URL, gets back readable text.

    Off by default, same as the harness. Scrapling itself is not a runtime
    dependency -- rule 16 in CLAUDE.md keeps that list at five, and Scrapling's
    fetchers extra alone pulls in a dozen more, several of them a browser
    automation stack. It runs from its own isolated environment instead,
    provisioned once by scripts/install-scrapling.ps1 / .sh, and this only
    points at the executable that produces -- an empty path leaves the skill
    unregistered even when `enabled` is true, rather than silently trying
    whatever `scrapling` happens to resolve to on PATH.
    """

    enabled: bool = False
    executable: str = ""
    timeout_seconds: float = 30
    # A fetched page can be any size, and it is about to sit in a model's
    # prompt -- the same character-budget discipline as everything else this
    # project hands a model (rule 3). The trim is ours, and the skill says
    # what it withheld, same as the harness's own tools.
    max_content_chars: int = 20000


class GitSettings(BaseModel):
    """Who signs a generation, and where it is published once it lands."""

    author_name: str = DEFAULT_AUTHOR_NAME
    author_email: str = DEFAULT_AUTHOR_EMAIL
    # Push a landed generation to the remote. A failed push never undoes the
    # commit: the generation is in the tree either way, only unpublished.
    auto_push: bool = True
    remote: str = "origin"
    # Empty means the branch the checkout is already on.
    branch: str = ""

    def identity(self) -> GitIdentity:
        return GitIdentity(name=self.author_name, email=self.author_email)

    def publish_policy(self) -> PublishPolicy:
        return PublishPolicy(enabled=self.auto_push, remote=self.remote, branch=self.branch)


class Settings(BaseModel):
    environment_name: str = "local"
    data_path: Path = Path("data/evomesh.db")
    generation_path: Path = Path("generations")
    workspace_path: Path = Path("workspace")
    log_level: str = "INFO"
    # A second EvoMesh against the same data races the first for the control
    # port, the git repo, and the generation counter -- see singleton.py. On
    # by default; a config that genuinely wants two (e.g. pointed at two
    # different data_paths from the same checkout) can turn it off.
    single_instance: bool = True
    lock_path: Path = Path(".runtime/evomesh.lock")
    models: ModelSettings = Field(default_factory=ModelSettings)
    system_agents: dict[str, AgentModelSettings] = Field(default_factory=dict)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    evolution: EvolutionSettings = Field(default_factory=EvolutionSettings)
    harness: HarnessSettings = Field(default_factory=HarnessSettings)
    scraping: ScrapingSettings = Field(default_factory=ScrapingSettings)
    git: GitSettings = Field(default_factory=GitSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    # Mesh-wide default MCP servers, merged with each agent's own
    # AgentDefinition.mcp_servers (agent wins on a name collision) -- see
    # McpServerConfig's own docstring and Environment.active_mcp_tools.
    # Unlike telegram above, there is no separate `enabled` flag: a list of
    # independently-named servers has no single on/off switch, so an empty
    # list (the default) is itself "off", the same shape HarnessSettings.
    # shell_allow already uses for its own allow-list.
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)

    def resolve(self, root: Path) -> Settings:
        clone = self.model_copy(deep=True)
        if not clone.harness.session_path.is_absolute():
            clone.harness.session_path = root / clone.harness.session_path
        for name in ("data_path", "generation_path", "workspace_path", "lock_path"):
            value = getattr(clone, name)
            if not value.is_absolute():
                setattr(clone, name, root / value)
        return clone


SECRETS_FILENAME = "evomesh.secrets.yaml"


def _load_secrets(root: Path) -> dict[str, str]:
    """A flat ``{ref: api key}`` map from evomesh.secrets.yaml next to the
    main config, or ``{}`` if that file does not exist -- gitignored on
    purpose (see .gitignore), so it is the one place a real key can live
    without ever being staged by an ordinary `git add`."""
    secrets_path = root / SECRETS_FILENAME
    if not secrets_path.exists():
        return {}
    raw = yaml.safe_load(secrets_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{secrets_path} must be a mapping of ref: key, got {type(raw).__name__}")
    return {str(ref): str(value) for ref, value in raw.items()}


def _resolve_api_key_refs(settings: Settings, root: Path) -> Settings:
    """Every ProviderSettings.api_key_ref resolved against evomesh.secrets.yaml
    into the real api_key, failing fast (not silently running unauthenticated)
    when a provider names a ref that file does not have."""
    refs_used = [
        (name, provider.api_key_ref)
        for name, provider in settings.models.providers.items()
        if provider.api_key_ref
    ]
    if not refs_used:
        return settings
    secrets = _load_secrets(root)
    for name, ref in refs_used:
        if ref not in secrets:
            secrets_path = root / SECRETS_FILENAME
            raise ValueError(
                f"models.providers.{name}.api_key_ref '{ref}' is not in {secrets_path} "
                f"({'the file does not exist' if not secrets_path.exists() else 'no such key'}). "
                f"See {SECRETS_FILENAME}.example for the format."
            )
        settings.models.providers[name].api_key = secrets[ref]
    return settings


def load_settings(path: Path | None = None) -> Settings:
    config_path = path or Path("evomesh.yaml")
    if not config_path.exists():
        example = Path("evomesh.yaml.example")
        config_path = example if example.exists() else config_path
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    root = config_path.resolve().parent
    settings = Settings.model_validate(raw).resolve(root)
    return _resolve_api_key_refs(settings, root)
