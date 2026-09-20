from __future__ import annotations

import asyncio
from collections import defaultdict

from evomesh.contracts import Message
from evomesh.storage import SQLiteRepository


class MessageBus:
    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository
        self._mailboxes: dict[str, asyncio.Queue[Message]] = defaultdict(asyncio.Queue)

    def register(self, agent_id: str) -> asyncio.Queue[Message]:
        return self._mailboxes[agent_id]

    def unregister(self, agent_id: str) -> None:
        """Drop a mailbox nobody will ever address again.

        For a real agent's own mailbox, never call this -- its message loop
        keeps listening on it for as long as the agent runs. It exists for
        the one-shot, private ``ask:<uuid>`` mailboxes ask_agent creates per
        call (see Environment._make_ask_agent): with nothing here to ever
        remove one, every such call -- success or timeout alike -- leaked one
        entry into this dict forever, the same unbounded-growth failure this
        project has already fixed for generation worktrees, filesystem
        grants, mesh.log, and the harness job queue.
        """
        self._mailboxes.pop(agent_id, None)

    async def send(self, message: Message) -> None:
        await self.repository.save_message(message)
        if message.recipient_id is None:
            for agent_id, mailbox in self._mailboxes.items():
                if agent_id != message.sender_id:
                    await mailbox.put(message)
            return
        await self._mailboxes[message.recipient_id].put(message)

    async def receive(self, agent_id: str, wait_seconds: float | None = None) -> Message:
        mailbox = self.register(agent_id)
        if wait_seconds is None:
            return await mailbox.get()
        return await asyncio.wait_for(mailbox.get(), wait_seconds)
