"""Structured shared world state with provenance and human-readable projection."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from evomesh.contracts import now_utc
from evomesh.coordination import WorkItem
from evomesh.events import Event


class WorldFact(BaseModel):
    key: str
    value: Any
    source: str
    confidence: float = 1.0
    created_at: datetime = Field(default_factory=now_utc)
    expires_at: datetime | None = None


class ArtifactRecord(BaseModel):
    key: str
    path: str
    source: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)


class Blackboard:
    def __init__(self, max_events: int = 200) -> None:
        self.facts: dict[str, WorldFact] = {}
        self.artifacts: dict[str, ArtifactRecord] = {}
        self.work_items: dict[str, WorkItem] = {}
        self.events: list[Event] = []
        self.max_events = max_events

    def publish_fact(self, fact: WorldFact) -> None:
        self.facts[fact.key] = fact

    def publish_artifact(self, artifact: ArtifactRecord) -> None:
        self.artifacts[artifact.key] = artifact

    def publish_work(self, item: WorkItem) -> None:
        self.work_items[item.id] = item

    def publish_event(self, event: Event) -> None:
        self.events = [*self.events, event][-self.max_events :]

    def fact(self, key: str, *, at: datetime | None = None) -> WorldFact | None:
        fact = self.facts.get(key)
        moment = at or now_utc()
        return None if fact and fact.expires_at and fact.expires_at <= moment else fact

    def projection(self) -> dict[str, str]:
        return {
            "Facts": "\n".join(
                f"- {key} = {fact.value} (source: {fact.source})"
                for key, fact in sorted(self.facts.items())
                if self.fact(key) is not None
            ) or "none",
            "Artifacts": "\n".join(
                f"- {key}: {artifact.path} (source: {artifact.source})"
                for key, artifact in sorted(self.artifacts.items())
            ) or "none",
            "Work items": "\n".join(
                f"- {item.id}: {item.objective} [{item.status}]"
                for item in self.work_items.values()
            ) or "none",
        }
