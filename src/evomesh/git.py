from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass
class GitRepository:
    root: Path

    async def run(self, *arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.root),
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        text = output.decode(errors="replace")
        if process.returncode != 0:
            raise GitError(text.strip())
        return text

    async def status(self) -> str:
        return await self.run("status", "--short")

    async def diff(self) -> str:
        return await self.run("diff", "--")

    async def current_commit(self) -> str:
        return (await self.run("rev-parse", "HEAD")).strip()

    async def create_branch(self, name: str) -> None:
        await self.run("switch", "-c", name)

    async def commit_mutation(self, generation: int, objective: str) -> str:
        await self.run("add", "-A")
        message = (
            f"evolve(environment): candidate generation {generation}\n\n"
            f"Objective:\n{objective}\n\nCreated by:\nEnvironmentEvolver"
        )
        await self.run("commit", "-m", message)
        return await self.current_commit()

    async def history(self, limit: int = 20) -> str:
        return await self.run("log", f"-{limit}", "--oneline", "--decorate")

    async def tag_generation(self, generation: int) -> None:
        await self.run("tag", f"evomesh-generation-{generation:06d}")

