from __future__ import annotations

from typing import Protocol


class Channel(Protocol):
    async def run(self) -> None: ...


class Output:
    def write(self, text: str) -> None:
        print(text)

