"""Typed, bounded runtime events for deterministic reactions and wakeups."""

from __future__ import annotations

import inspect
import json
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from evomesh.contracts import now_utc


class EventType(StrEnum):
    BELIEF_CHANGED = "belief_changed"
    GOAL_CREATED = "goal_created"
    GOAL_UNBLOCKED = "goal_unblocked"
    GOAL_COMPLETED = "goal_completed"
    TASK_FAILED = "task_failed"
    TASK_COMPLETED = "task_completed"
    AGENT_STALLED = "agent_stalled"
    MESSAGE_RECEIVED = "message_received"
    HUMAN_FEEDBACK_RECEIVED = "human_feedback_received"
    # A rule's EMIT_EVENT effect; payload carries the rule event's own type.
    RULE_EVENT = "rule_event"


@dataclass(frozen=True)
class Event:
    type: EventType
    source: str
    agent_id: str = ""
    goal_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: object = field(default_factory=now_utc)


EventHandler = Callable[[Event], Awaitable[None] | None]
EventFilter = Callable[[Event], bool]


@dataclass(frozen=True)
class EventSubscription:
    handler: EventHandler
    predicate: EventFilter | None = None


class EventBus:
    """In-process dispatcher; handlers run in registration order.

    History is diagnostic only and bounded.  State remains in MindState and the
    repository, so replaying this buffer is never required for correctness.
    """

    def __init__(self, max_history: int = 512, dedup_window_seconds: float = 0.1) -> None:
        if max_history < 1:
            raise ValueError("max_history must be positive")
        self._handlers: dict[EventType, list[EventSubscription]] = defaultdict(list)
        self._history: deque[Event] = deque(maxlen=max_history)
        self._dedup_window_seconds = max(0.0, dedup_window_seconds)
        self._recent: dict[str, float] = {}

    @property
    def history(self) -> tuple[Event, ...]:
        return tuple(self._history)

    def subscribe(
        self,
        event_type: EventType,
        handler: EventHandler,
        *,
        predicate: EventFilter | None = None,
    ) -> None:
        subscription = EventSubscription(handler, predicate)
        if subscription not in self._handlers[event_type]:
            self._handlers[event_type].append(subscription)

    def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None:
        self._handlers[event_type] = [
            item for item in self._handlers[event_type] if item.handler != handler
        ]

    async def publish(self, event: Event) -> None:
        now = time.monotonic()
        fingerprint = self._fingerprint(event)
        previous = self._recent.get(fingerprint)
        if previous is not None and now - previous <= self._dedup_window_seconds:
            return
        self._recent[fingerprint] = now
        self._recent = {
            key: seen
            for key, seen in self._recent.items()
            if now - seen <= self._dedup_window_seconds
        }
        self._history.append(event)
        for subscription in tuple(self._handlers[event.type]):
            if subscription.predicate is not None and not subscription.predicate(event):
                continue
            result = subscription.handler(event)
            if inspect.isawaitable(result):
                await result

    @staticmethod
    def _fingerprint(event: Event) -> str:
        payload = json.dumps(event.payload, sort_keys=True, default=str)
        return "|".join(
            (event.type.value, event.source, event.agent_id, event.goal_id, payload)
        )

