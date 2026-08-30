from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def now_utc() -> datetime:
    return datetime.now(UTC)


class AgentStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    STOPPED = "stopped"


class MindState(BaseModel):
    beliefs: list[dict[str, Any]] = Field(default_factory=list)
    goals: list[dict[str, Any]] = Field(default_factory=list)
    intentions: list[dict[str, Any]] = Field(default_factory=list)


class AgentDefinition(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    type: str = "agent"
    generation: int = 1
    created_by: str = "human"
    parent_agent_id: str | None = None
    identity: str = ""
    purpose: str
    provider: str = "ollama"
    model_name: str = "qwen3"
    mind: MindState = Field(default_factory=MindState)
    skills: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    memory_enabled: bool = True
    memory_strategy: str = "persistent"
    status: AgentStatus = AgentStatus.CANDIDATE
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    sender_id: str
    recipient_id: str | None
    conversation_id: str = Field(default_factory=lambda: str(uuid4()))
    correlation_id: str | None = None
    type: str = "text"
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)


class FilesystemGrant(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    agent_id: str
    path: str
    read: bool = True
    write: bool = False


class SkillDefinition(BaseModel):
    name: str
    version: str = "1.0.0"
    generation: int = 1
    description: str
    entrypoint: str
    inputs: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    required_permissions: list[str] = Field(default_factory=list)
    required_dependencies: list[str] = Field(default_factory=list)
    created_by: str = "system"
    parent_generation: int = 1
    status: str = "active"

