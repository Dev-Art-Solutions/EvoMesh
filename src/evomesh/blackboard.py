"""Structured shared world state with provenance and human-readable projection."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from evomesh.contracts import now_utc
from evomesh.coordination import WorkItem, WorkStatus
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


TERMINAL_WORK = frozenset({WorkStatus.COMPLETED, WorkStatus.FAILED, WorkStatus.CANCELLED})


class Blackboard:
    """Shared structured world state: facts, artifacts and work with provenance.

    Bounded (oldest entries go first; finished work before open work) and
    persisted by the environment through ``dump``/``load``. Events are kept
    for diagnosis only and are not persisted.
    """

    def __init__(
        self,
        max_events: int = 200,
        max_facts: int = 200,
        max_artifacts: int = 200,
        max_work: int = 200,
    ) -> None:
        self.facts: dict[str, WorldFact] = {}
        self.artifacts: dict[str, ArtifactRecord] = {}
        self.work_items: dict[str, WorkItem] = {}
        self.events: list[Event] = []
        self.max_events = max_events
        self.max_facts = max_facts
        self.max_artifacts = max_artifacts
        self.max_work = max_work

    def publish_fact(self, fact: WorldFact) -> None:
        self.facts.pop(fact.key, None)
        self.facts[fact.key] = fact
        while len(self.facts) > self.max_facts:
            self.facts.pop(next(iter(self.facts)))

    def publish_artifact(self, artifact: ArtifactRecord) -> None:
        self.artifacts.pop(artifact.key, None)
        self.artifacts[artifact.key] = artifact
        while len(self.artifacts) > self.max_artifacts:
            self.artifacts.pop(next(iter(self.artifacts)))

    def publish_work(self, item: WorkItem) -> None:
        self.work_items[item.id] = item
        while len(self.work_items) > self.max_work:
            finished = next(
                (key for key, work in self.work_items.items() if work.status in TERMINAL_WORK),
                next(iter(self.work_items)),
            )
            self.work_items.pop(finished)

    def publish_event(self, event: Event) -> None:
        self.events = [*self.events, event][-self.max_events :]

    def fact(self, key: str, *, at: datetime | None = None) -> WorldFact | None:
        fact = self.facts.get(key)
        moment = at or now_utc()
        return None if fact and fact.expires_at and fact.expires_at <= moment else fact

    def open_work(self) -> list[WorkItem]:
        return [item for item in self.work_items.values() if item.status not in TERMINAL_WORK]

    def projection(self, limit: int = 12) -> dict[str, str]:
        """A human- and prompt-readable view, newest ``limit`` of each kind."""
        facts = [
            f"- {key} = {fact.value} (source: {fact.source})"
            for key, fact in list(self.facts.items())[-limit:]
            if self.fact(key) is not None
        ]
        artifacts = [
            f"- {key}: {artifact.path} (source: {artifact.source})"
            for key, artifact in list(self.artifacts.items())[-limit:]
        ]
        work = [
            f"- {item.id}: {item.objective} [{item.status}]"
            + (f" -> {item.assigned_agent_id}" if item.assigned_agent_id else "")
            for item in self.open_work()[-limit:]
        ]
        return {
            "Facts": "\n".join(facts) or "none",
            "Artifacts": "\n".join(artifacts) or "none",
            "Work items": "\n".join(work) or "none",
        }

    def dump(self) -> dict[str, Any]:
        return {
            "facts": [item.model_dump(mode="json") for item in self.facts.values()],
            "artifacts": [item.model_dump(mode="json") for item in self.artifacts.values()],
            "work_items": [item.model_dump(mode="json") for item in self.work_items.values()],
        }

    def load(self, payload: object) -> None:
        """Replace facts, artifacts and work with a ``dump``; anything
        malformed is skipped rather than failing the boot."""
        if not isinstance(payload, dict):
            return
        self.facts, self.artifacts, self.work_items = {}, {}, {}
        for raw in payload.get("facts", []):
            with suppress(ValueError):
                self.publish_fact(WorldFact.model_validate(raw))
        for raw in payload.get("artifacts", []):
            with suppress(ValueError):
                self.publish_artifact(ArtifactRecord.model_validate(raw))
        for raw in payload.get("work_items", []):
            with suppress(ValueError):
                self.publish_work(WorkItem.model_validate(raw))
