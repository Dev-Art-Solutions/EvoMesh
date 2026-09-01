from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

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
    objective: str | None = None


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

    def resolve(self, root: Path) -> Settings:
        clone = self.model_copy(deep=True)
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
