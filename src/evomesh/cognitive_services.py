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
        return [section.render() for section in self.section_specs()]

    def section_specs(self) -> list[ContextSection]:
        sections: list[ContextSection] = []
        if self.goal:
            sections.append(ContextSection("goal", "GOAL", self.goal, 1, True, 0.16))
        if self.beliefs:
            sections.append(ContextSection("beliefs", "BELIEFS", self.beliefs, 4, False, 0.12))
        if self.intention:
            sections.append(
                ContextSection(
                    "intention", "YOUR COMMITTED PLAN", self.intention, 2, False, 0.12
                )
            )
        if self.working:
            sections.append(ContextSection("working", "CURRENT WORK", self.working, 3, False, 0.10))
        if self.artifacts:
            sections.append(
                ContextSection(
                    "artifacts",
                    "RELEVANT ARTIFACTS",
                    "\n".join(self.artifacts),
                    5,
                    False,
                    0.08,
                )
            )
        if self.recent_failure:
            sections.append(
                ContextSection(
                    "recent_failure",
                    "RECENT FAILURE",
                    self.recent_failure,
                    6,
                    False,
                    0.08,
                )
            )
        if self.world:
            sections.append(ContextSection("world", "WORLD", self.world, 8, False, 0.06))
        if self.memory:
            sections.append(
                ContextSection("memory", "RELEVANT MEMORY", self.memory, 9, False, 0.10)
            )
        if self.notes:
            sections.append(
                ContextSection(
                    "notes", "RELEVANT WORKING NOTES", self.notes, 7, False, 0.08
                )
            )
        if self.inbox:
            sections.append(ContextSection("inbox", "RELEVANT INBOX", self.inbox, 10, False, 0.05))
        sections.append(ContextSection("task", "TASK", self.task, 0, True, 0.30))
        if self.output_contract:
            sections.append(
                ContextSection(
                    "output_contract",
                    "OUTPUT CONTRACT",
                    self.output_contract,
                    0,
                    True,
                    0.20,
                )
            )
        return sections


@dataclass(frozen=True)
class ContextSection:
    source: str
    label: str
    content: str
    priority: int
    required: bool
    budget_fraction: float

    def render(self, content: str | None = None) -> str:
        selected = self.content if content is None else content
        if self.source == "goal":
            return f"{self.label}: {selected}"
        return f"{self.label}:\n{selected}"


class ContextSelectionRecord(BaseModel):
    source: str
    reason: str
    available_chars: int
    included_chars: int
    truncated: bool
    required: bool


@dataclass(frozen=True)
class ContextAssembly:
    text: str
    provenance: tuple[ContextSelectionRecord, ...]


@dataclass(frozen=True)
class ContextAssembler:
    max_chars: int

    def assemble(self, packet: TaskPacket) -> str:
        return self.assemble_with_provenance(packet).text

    def assemble_with_provenance(self, packet: TaskPacket) -> ContextAssembly:
        if self.max_chars < 1:
            return ContextAssembly("", ())
        specs = packet.section_specs()
        allocations: dict[str, int] = {}
        remaining = max(0, self.max_chars - (2 * max(0, len(specs) - 1)))

        # Reserve labelled space for the task, goal and output contract before
        # optional history. Their contents may be clipped, but never disappear.
        required = sorted(
            (item for item in specs if item.required), key=lambda item: item.priority
        )
        required_full_cost = sum(
            len(section.label) + 2 + len(section.content) for section in required
        )
        if required_full_cost <= remaining:
            for section in required:
                allocation = len(section.label) + 2 + len(section.content)
                allocations[section.source] = allocation
                remaining -= allocation
        else:
            for section in required:
                label_cost = len(section.label) + 2
                desired = min(
                    len(section.content),
                    max(24, round(self.max_chars * section.budget_fraction)),
                )
                allocation = min(remaining, label_cost + desired)
                allocations[section.source] = allocation
                remaining -= allocation

        optional = sorted(
            (item for item in specs if not item.required), key=lambda item: item.priority
        )
        for section in optional:
            if remaining <= len(section.label) + 2:
                allocations[section.source] = 0
                continue
            desired = len(section.label) + 2 + min(
                len(section.content), max(16, round(self.max_chars * section.budget_fraction))
            )
            allocation = min(remaining, desired)
            allocations[section.source] = allocation
            remaining -= allocation

        required_order = {"goal": 0, "task": 1, "output_contract": 2}
        output_specs = sorted(
            specs,
            key=lambda item: (
                0 if item.required else 1,
                required_order.get(item.source, item.priority),
            ),
        )
        rendered: list[str] = []
        provenance: list[ContextSelectionRecord] = []
        for section in output_specs:
            allocation = allocations.get(section.source, 0)
            label_cost = len(section.label) + 2
            content_budget = max(0, allocation - label_cost)
            selected = clip(section.content, content_budget, keep="head")
            if allocation > 0:
                rendered.append(section.render(selected))
            provenance.append(
                ContextSelectionRecord(
                    source=section.source,
                    reason=(
                        "required cognitive contract"
                        if section.required
                        else f"priority {section.priority}"
                    ),
                    available_chars=len(section.content),
                    included_chars=len(selected),
                    truncated=len(selected) < len(section.content),
                    required=section.required,
                )
            )
        text = "\n\n".join(rendered)[: self.max_chars]
        return ContextAssembly(text, tuple(provenance))


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
