from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class ProviderSettings(BaseModel):
    base_url: str
    model: str
    api_key: str | None = None


class ModelSettings(BaseModel):
    default_provider: str = "ollama"
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)


class AgentModelSettings(BaseModel):
    provider: str
    model: str


class Settings(BaseModel):
    environment_name: str = "local"
    data_path: Path = Path("data/evomesh.db")
    generation_path: Path = Path("generations")
    log_level: str = "INFO"
    models: ModelSettings = Field(default_factory=ModelSettings)
    system_agents: dict[str, AgentModelSettings] = Field(default_factory=dict)

    def resolve(self, root: Path) -> Settings:
        clone = self.model_copy(deep=True)
        if not clone.data_path.is_absolute():
            clone.data_path = root / clone.data_path
        if not clone.generation_path.is_absolute():
            clone.generation_path = root / clone.generation_path
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
