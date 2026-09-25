"""A WorkExecutor that is not the generation pipeline: proof the control
plane depends on the seam, not on generation internals (closure plan 18.2)."""

from __future__ import annotations

from collections.abc import Mapping

from evomesh.coordination import WorkItem
from evomesh.improvements import ExecutionScope, WorkHandle, WorkInspection, WorkState


class InlineExecutor:
    kind = "inline"

    def __init__(self) -> None:
        self.started: list[tuple[str, ExecutionScope]] = []
        self.states: dict[str, WorkState] = {}
        self.cancelled: list[str] = []

    async def submit(self, work: WorkItem, scope: ExecutionScope) -> WorkHandle:
        self.started.append((work.id, scope))
        ref = f"{scope.reference}:{work.id}"
        self.states[ref] = WorkState.PENDING
        return {"executor": self.kind, "ref": ref, "assignee": scope.assignee}

    def finish(self, ref: str, state: WorkState) -> None:
        self.states[ref] = state

    def inspect(self, handle: Mapping[str, str]) -> WorkInspection:
        return WorkInspection(self.states.get(handle["ref"], WorkState.UNKNOWN), {})

    async def request_cancel(self, handle: Mapping[str, str]) -> WorkInspection:
        self.cancelled.append(handle["ref"])
        self.states[handle["ref"]] = WorkState.CANCELLED
        return WorkInspection(WorkState.CANCELLED, {})
