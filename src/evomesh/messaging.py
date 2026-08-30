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
