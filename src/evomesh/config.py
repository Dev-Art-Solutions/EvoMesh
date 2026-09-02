from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from evomesh.git import (
    DEFAULT_AUTHOR_EMAIL,
    DEFAULT_AUTHOR_NAME,
    GitIdentity,
    PublishPolicy,
)
from evomesh.harness_tools import ToolLimits
from evomesh.memory import MemoryBudget


class ProviderSettings(BaseModel):
    base_url: str
    model: str
    api_key: str | None = None
    # A 30B model on a busy GPU answers a full prompt in minutes, not seconds.
    # Too low a ceiling here reads to a human as "the agent is broken".
    timeout_seconds: float = 600


class ModelSettings(BaseModel):
    default_provider: str = "ollama"
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)


class AgentModelSettings(BaseModel):
    provider: str
    model: str


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

    def budget(self) -> MemoryBudget:
        return MemoryBudget(
            memory_chars=self.memory_chars,
            context_chars=self.context_chars,
            inbox_chars=self.inbox_chars,
            beliefs_chars=self.beliefs_chars,
            prompt_chars=self.prompt_chars,
        )


class EvolutionSettings(BaseModel):
    autonomous: bool = True
    cycle_seconds: int = 300
    auto_validate: bool = True
    # How many times the Evolver may fix its own candidate before a failure
    # becomes the human's problem. Zero reports the first failure as final.
    max_repairs: int = 2
    # Let the verdict decide: promote what validated, discard what did not, and
    # move on without asking. A run with no verdict still stops for a human.
    auto_promote: bool = False
    # A landed generation is code this process is not running. Restart into it
    # instead of leaving a human to notice the flag and do it by hand.
    auto_restart: bool = True
    # Breathing room between the decision and the shutdown, so the cycle that
    # promoted the generation finishes writing its summary to every channel.
    restart_delay_seconds: float = 5.0
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
    # several tools at once.
    max_steps: int = 24
    max_seconds: float = 300.0
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

    def shell_programs(self) -> frozenset[str]:
        return frozenset(name.strip().lower() for name in self.shell_allow if name.strip())
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
    max_queue: int = 8

    def limits(self) -> ToolLimits:
        return ToolLimits(
            result_chars=self.tool_result_chars,
            result_lines=self.tool_result_lines,
            grep_matches=self.grep_matches,
        )


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


class TelegramSettings(BaseModel):
    """A Telegram bot as a second console onto the same mesh.

    ``token`` is the string BotFather hands back. ``allowed_chat_ids`` is the
    allow-list; leaving it empty and keeping ``adopt_first_chat`` on lets the
    first person who says /start claim the bot, which is the only way to learn
    a chat id without asking a human to go find it.
    """

    enabled: bool = False
    token: str = ""
    allowed_chat_ids: list[int] = Field(default_factory=list)
    adopt_first_chat: bool = True
    # Long-poll window. Telegram holds the request open this long when idle.
    poll_timeout_seconds: int = 30
    # Announce what the mesh does on its own -- promotions, restarts -- rather
    # than only answering when spoken to.
    announcements: bool = True


class Settings(BaseModel):
    environment_name: str = "local"
    data_path: Path = Path("data/evomesh.db")
    generation_path: Path = Path("generations")
    workspace_path: Path = Path("workspace")
    log_level: str = "INFO"
    models: ModelSettings = Field(default_factory=ModelSettings)
    system_agents: dict[str, AgentModelSettings] = Field(default_factory=dict)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    evolution: EvolutionSettings = Field(default_factory=EvolutionSettings)
    harness: HarnessSettings = Field(default_factory=HarnessSettings)
    git: GitSettings = Field(default_factory=GitSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)

    def resolve(self, root: Path) -> Settings:
        clone = self.model_copy(deep=True)
        if not clone.harness.session_path.is_absolute():
            clone.harness.session_path = root / clone.harness.session_path
        for name in ("data_path", "generation_path", "workspace_path"):
            value = getattr(clone, name)
            if not value.is_absolute():
                setattr(clone, name, root / value)
        return clone


def load_settings(path: Path | None = None) -> Settings:
    config_path = path or Path("evomesh.yaml")
    if not config_path.exists():
        example = Path("evomesh.yaml.example")
        config_path = example if example.exists() else config_path
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(raw).resolve(config_path.resolve().parent)
