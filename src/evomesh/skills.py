from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from evomesh.contracts import SkillDefinition
from evomesh.permissions import FilesystemPolicy
from evomesh.processes import run_command
from evomesh.storage import SQLiteRepository

SkillHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]


class MissingSkillError(LookupError):
    pass


class SkillRegistry:
    def __init__(self, repository: SQLiteRepository, policy: FilesystemPolicy) -> None:
        self.repository = repository
        self.policy = policy
        self._skills: dict[str, SkillDefinition] = {}
        self._handlers: dict[str, SkillHandler] = {}

    async def load(self) -> None:
        self._skills = {skill.name: skill for skill in await self.repository.load_skills()}

    async def register(self, skill: SkillDefinition, handler: SkillHandler) -> None:
        self._skills[skill.name] = skill
        self._handlers[skill.name] = handler
        await self.repository.save_skill(skill)

    def discover(self, query: str = "") -> list[SkillDefinition]:
        needle = query.lower()
        return [
            skill
            for skill in self._skills.values()
            if needle in skill.name.lower() or needle in skill.description.lower()
        ]

    async def attach(self, agent_id: str, skill_name: str) -> None:
        if skill_name not in self._skills:
            raise MissingSkillError(skill_name)
        await self.repository.attach_skill(agent_id, skill_name)

    async def invoke(self, agent_id: str, skill_name: str, inputs: dict[str, Any]) -> Any:
        handler = self._handlers.get(skill_name)
        if handler is None:
            raise MissingSkillError(skill_name)
        return await handler(agent_id, inputs)

    async def register_builtins(self) -> None:
        async def read_text(agent_id: str, inputs: dict[str, Any]) -> str:
            path = await self.policy.require(agent_id, str(inputs["path"]), "read")
            return await asyncio.to_thread(path.read_text, encoding="utf-8")

        async def write_text(agent_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
            path = await self.policy.require(agent_id, str(inputs["path"]), "write")
            path.parent.mkdir(parents=True, exist_ok=True)
            content = str(inputs["content"])
            await asyncio.to_thread(path.write_text, content, encoding="utf-8")
            return {"path": str(path), "bytes": len(content.encode())}

        async def git_command(agent_id: str, inputs: dict[str, Any]) -> str:
            root = await self.policy.require(agent_id, str(inputs["path"]), "read")
            command = str(inputs.get("command", "status"))
            arguments = ["git", "-C", str(root), command]
            if command == "diff":
                arguments.append("--")
            return (await run_command(*arguments)).output

        definitions = [
            ("Filesystem.Read", "Read a UTF-8 file", read_text, ["filesystem:read"]),
            ("Filesystem.Write", "Write a UTF-8 file", write_text, ["filesystem:write"]),
            ("Markdown.Read", "Read Markdown text", read_text, ["filesystem:read"]),
            ("Markdown.Write", "Write Markdown text", write_text, ["filesystem:write"]),
            ("Git.Status", "Read Git status", git_command, ["filesystem:read"]),
            ("Git.Diff", "Read Git diff", git_command, ["filesystem:read"]),
        ]
        for name, description, handler, permissions in definitions:
            await self.register(
                SkillDefinition(
                    name=name,
                    description=description,
                    entrypoint=f"builtin:{name}",
                    required_permissions=permissions,
                ),
                handler,
            )
