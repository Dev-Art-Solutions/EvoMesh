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


TERMINAL_WORK = frozenset(
    {
        WorkStatus.COMPLETED,
        WorkStatus.FAILED,
        WorkStatus.NEEDS_HUMAN,
        WorkStatus.CANCELLED,
    }
)


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
        max_fact_versions: int = 8,
    ) -> None:
        self.facts: dict[str, WorldFact] = {}
        self.fact_history: dict[str, list[WorldFact]] = {}
        self.artifacts: dict[str, ArtifactRecord] = {}
        self.work_items: dict[str, WorkItem] = {}
        self.events: list[Event] = []
        self.max_events = max_events
        self.max_facts = max_facts
        self.max_artifacts = max_artifacts
        self.max_work = max_work
        self.max_fact_versions = max(2, max_fact_versions)

    def publish_fact(self, fact: WorldFact) -> None:
        versions = self.fact_history.setdefault(fact.key, [])
        if not versions or versions[-1].model_dump() != fact.model_dump():
            versions.append(fact)
            self.fact_history[fact.key] = versions[-self.max_fact_versions :]
        self.facts.pop(fact.key, None)
        self.facts[fact.key] = fact
        while len(self.facts) > self.max_facts:
            expired_key = next(iter(self.facts))
            self.facts.pop(expired_key)
            self.fact_history.pop(expired_key, None)

    def fact_versions(self, key: str) -> tuple[WorldFact, ...]:
        """All retained claims for a key, including conflicting sources."""
        return tuple(self.fact_history.get(key, ()))

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
        moment = at or now_utc()
        fact = self.facts.get(key)
        if fact is None:
            return None
        if fact.created_at > moment:
            return None
        if fact.expires_at and fact.expires_at <= moment:
            return None
        return fact

    def work_history(self) -> dict[object, tuple[int, int]]:
        """Successes and failures per ``(agent, work type, capabilities)``
        from finished work, the evidence Contract Net ranks bidders by."""
        history: dict[object, tuple[int, int]] = {}
        for item in self.work_items.values():
            if item.assigned_agent_id is None or item.status not in TERMINAL_WORK:
                continue
            if item.status is WorkStatus.CANCELLED:
                continue
            key = (
                item.assigned_agent_id,
                item.type,
                ",".join(sorted(item.required_capabilities)),
            )
            ok, failed = history.get(key, (0, 0))
            history[key] = (
                (ok + 1, failed) if item.status is WorkStatus.COMPLETED else (ok, failed + 1)
            )
        return history

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
            "fact_history": [
                item.model_dump(mode="json")
                for versions in self.fact_history.values()
                for item in versions
            ],
            "artifacts": [item.model_dump(mode="json") for item in self.artifacts.values()],
            "work_items": [item.model_dump(mode="json") for item in self.work_items.values()],
        }

    def load(self, payload: object) -> None:
        """Replace facts, artifacts and work with a ``dump``; anything
        malformed is skipped rather than failing the boot."""
        if not isinstance(payload, dict):
            return
        self.facts, self.fact_history, self.artifacts, self.work_items = {}, {}, {}, {}
        stored_facts = payload.get("fact_history") or payload.get("facts", [])
        for raw in stored_facts:
            with suppress(ValueError):
                self.publish_fact(WorldFact.model_validate(raw))
        for raw in payload.get("artifacts", []):
            with suppress(ValueError):
                self.publish_artifact(ArtifactRecord.model_validate(raw))
        for raw in payload.get("work_items", []):
            with suppress(ValueError):
                self.publish_work(WorkItem.model_validate(raw))
