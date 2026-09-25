"""Explicit, observable LLM operations and compact task packets.

The runtime owns when and why a model is called. Providers remain transport
implementations; this module is the narrow service boundary that records the
cognitive reason without wrapping or changing provider behavior.
"""

from __future__ import annotations

import json
import time
from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from evomesh.contracts import now_utc
from evomesh.memory import clip
from evomesh.models import ChatMessage, ChatTurn, ModelProvider


class CognitiveServiceType(StrEnum):
    DECOMPOSE_GOAL = "decompose_goal"
    CREATE_NOVEL_PLAN = "create_novel_plan"
    INTERPRET_UNSTRUCTURED_INPUT = "interpret_unstructured_input"
    SYNTHESIZE_EVIDENCE = "synthesize_evidence"
    REFLECT_ON_FAILURE = "reflect_on_failure"
    GENERALIZE_PROCEDURE = "generalize_procedure"
    EXECUTE_STEP = "execute_step"
    CHAT_RESPONSE = "chat_response"
    FORMAT_REPORT = "format_report"
    SUMMARIZE_MEMORY = "summarize_memory"
    TOOL_LOOP = "tool_loop"
    DIRECT_INFERENCE = "direct_inference"
    OTHER = "other"


class ModelInvocationReason(StrEnum):
    NO_PLAN_MATCH = "no_plan_match"
    AMBIGUOUS_INPUT = "ambiguous_input"
    NOVEL_GOAL_DECOMPOSITION = "novel_goal_decomposition"
    SYNTHESIS_REQUIRED = "synthesis_required"
    NATURAL_LANGUAGE_INTERPRETATION = "natural_language_interpretation"
    FAILURE_REFLECTION = "failure_reflection"
    PROCEDURE_GENERALIZATION = "procedure_generalization"
    PLAN_STEP_REQUIRES_REASONING = "plan_step_requires_reasoning"
    HUMAN_CHAT_REQUIRES_RESPONSE = "human_chat_requires_response"
    DETERMINISTIC_FORMAT_REJECTED = "deterministic_format_rejected"
    MEMORY_BUDGET_EXCEEDED = "memory_budget_exceeded"
    TOOL_SELECTION_REQUIRES_MODEL = "tool_selection_requires_model"
    EXPLICIT_INFERENCE_REQUEST = "explicit_inference_request"
    OTHER = "other"


class ModelCallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ModelCallRecord(BaseModel):
    service: CognitiveServiceType
    reason: ModelInvocationReason
    provider: str
    model: str | None = None
    agent_id: str = ""
    goal_id: str = ""
    task_id: str = ""
    input_chars: int
    output_chars: int = 0
    duration_seconds: float
    status: ModelCallStatus
    error: str = ""
    created_at: datetime = Field(default_factory=now_utc)


class CognitiveMetrics:
    """Bounded structured call history with cheap aggregate snapshots."""

    def __init__(self, max_records: int = 2048) -> None:
        if max_records < 1:
            raise ValueError("max_records must be positive")
        self._records: deque[ModelCallRecord] = deque(maxlen=max_records)

    @property
    def records(self) -> tuple[ModelCallRecord, ...]:
        return tuple(self._records)

    def record(self, call: ModelCallRecord) -> None:
        self._records.append(call)

    def snapshot(self) -> dict[str, Any]:
        services = Counter(call.service.value for call in self._records)
        reasons = Counter(call.reason.value for call in self._records)
        failures = sum(call.status is ModelCallStatus.FAILED for call in self._records)
        return {
            "calls": len(self._records),
            "failures": failures,
            "input_chars": sum(call.input_chars for call in self._records),
            "output_chars": sum(call.output_chars for call in self._records),
            "duration_seconds": sum(call.duration_seconds for call in self._records),
            "by_service": dict(sorted(services.items())),
            "by_reason": dict(sorted(reasons.items())),
        }


@dataclass(frozen=True)
class TaskPacket:
    """Only the state relevant to one narrow cognitive operation."""

    role: str
    operation: CognitiveServiceType
    task: str
    goal: str = ""
    beliefs: str = ""
    intention: str = ""
    working: str = ""
    artifacts: tuple[str, ...] = ()
    recent_failure: str = ""
    world: str = ""
    memory: str = ""
    notes: str = ""
    inbox: str = ""
    output_contract: str = ""

    def sections(self) -> list[str]:
        sections: list[str] = []
        if self.goal:
            sections.append(f"GOAL: {self.goal}")
        if self.beliefs:
            sections.append("BELIEFS (what you currently hold true):\n" + self.beliefs)
        if self.intention:
            sections.append("YOUR COMMITTED PLAN:\n" + self.intention)
        if self.working:
            sections.append(
                "CURRENT WORK (live from the runtime, more current than MEMORY):\n"
                + self.working
            )
        if self.artifacts:
            sections.append("RELEVANT ARTIFACTS:\n" + "\n".join(self.artifacts))
        if self.recent_failure:
            sections.append("RECENT FAILURE:\n" + self.recent_failure)
        if self.world:
            sections.append("WORLD:\n" + self.world)
        if self.memory:
            sections.append("MEMORY (things you already know):\n" + self.memory)
        if self.notes:
            sections.append("YOUR WORKING NOTES:\n" + self.notes)
        if self.inbox:
            sections.append("INBOX:\n" + self.inbox)
        sections.append(self.task)
        if self.output_contract:
            sections.append("OUTPUT CONTRACT:\n" + self.output_contract)
        return sections


@dataclass(frozen=True)
class ContextAssembler:
    max_chars: int

    def assemble(self, packet: TaskPacket) -> str:
        sections = packet.sections()
        rendered = "\n\n".join(sections)
        if len(rendered) <= self.max_chars:
            return rendered
        # A task packet is not a log: clipping its tail can remove the goal and
        # leave the model with evidence but no objective.  Preserve the goal at
        # the front, then spend the remaining budget on the most recent fields.
        prefix = sections[0]
        if len(prefix) >= self.max_chars:
            return prefix[: self.max_chars]
        remaining = self.max_chars - len(prefix) - 2
        tail = clip("\n\n".join(sections[1:]), remaining)
        return f"{prefix}\n\n{tail}"[: self.max_chars]


@dataclass
class CognitiveModelService:
    metrics: CognitiveMetrics = field(default_factory=CognitiveMetrics)

    async def generate(
        self,
        provider: ModelProvider,
        prompt: str,
        *,
        service: CognitiveServiceType,
        reason: ModelInvocationReason,
        provider_name: str = "",
        agent_id: str = "",
        goal_id: str = "",
        task_id: str = "",
        system: str = "",
        model: str | None = None,
        num_ctx: int | None = None,
        format: dict[str, Any] | None = None,
    ) -> str:
        started = time.monotonic()
        input_chars = len(prompt) + len(system) + len(json.dumps(format or {}))
        try:
            output = await provider.generate(
                prompt, system=system, model=model, num_ctx=num_ctx, format=format
            )
        except Exception as exc:
            self._record(
                service=service,
                reason=reason,
                provider=provider_name or type(provider).__name__,
                model=model,
                agent_id=agent_id,
                goal_id=goal_id,
                task_id=task_id,
                input_chars=input_chars,
                output_chars=0,
                started=started,
                status=ModelCallStatus.FAILED,
                error=str(exc),
            )
            raise
        self._record(
            service=service,
            reason=reason,
            provider=provider_name or type(provider).__name__,
            model=model,
            agent_id=agent_id,
            goal_id=goal_id,
            task_id=task_id,
            input_chars=input_chars,
            output_chars=len(output),
            started=started,
            status=ModelCallStatus.SUCCEEDED,
        )
        return output

    async def chat(
        self,
        provider: ModelProvider,
        messages: Sequence[ChatMessage],
        *,
        service: CognitiveServiceType,
        reason: ModelInvocationReason,
        provider_name: str = "",
        agent_id: str = "",
        goal_id: str = "",
        task_id: str = "",
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
        model: str | None = None,
        num_ctx: int | None = None,
    ) -> ChatTurn:
        started = time.monotonic()
        input_chars = (
            sum(len(message.content) for message in messages)
            + len(system)
            + len(json.dumps(tools or []))
        )
        try:
            output = await provider.chat(
                list(messages),
                tools=tools,
                system=system,
                model=model,
                num_ctx=num_ctx,
            )
        except Exception as exc:
            self._record(
                service=service,
                reason=reason,
                provider=provider_name or type(provider).__name__,
                model=model,
                agent_id=agent_id,
                goal_id=goal_id,
                task_id=task_id,
                input_chars=input_chars,
                output_chars=0,
                started=started,
                status=ModelCallStatus.FAILED,
                error=str(exc),
            )
            raise
        output_chars = len(output.text) + sum(
            len(call.name) + len(json.dumps(call.arguments, default=str))
            for call in output.tool_calls
        )
        self._record(
            service=service,
            reason=reason,
            provider=provider_name or type(provider).__name__,
            model=model,
            agent_id=agent_id,
            goal_id=goal_id,
            task_id=task_id,
            input_chars=input_chars,
            output_chars=output_chars,
            started=started,
            status=ModelCallStatus.SUCCEEDED,
        )
        return output

    def _record(
        self,
        *,
        service: CognitiveServiceType,
        reason: ModelInvocationReason,
        provider: str,
        model: str | None,
        agent_id: str,
        goal_id: str,
        task_id: str,
        input_chars: int,
        output_chars: int,
        started: float,
        status: ModelCallStatus,
        error: str = "",
    ) -> None:
        self.metrics.record(
            ModelCallRecord(
                service=service,
                reason=reason,
                provider=provider,
                model=model,
                agent_id=agent_id,
                goal_id=goal_id,
                task_id=task_id,
                input_chars=input_chars,
                output_chars=output_chars,
                duration_seconds=max(0.0, time.monotonic() - started),
                status=status,
                error=error,
            )
        )
