from __future__ import annotations

import asyncio
from pathlib import Path

from evomesh.contracts import FilesystemGrant
from evomesh.storage import SQLiteRepository


class PermissionDeniedError(PermissionError):
    def __init__(self, agent_id: str, path: Path, operation: str) -> None:
        self.agent_id = agent_id
        self.path = path
        self.operation = operation
        super().__init__(f"{agent_id} has no {operation} grant for {path}")


class FilesystemPolicy:
    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository

    @staticmethod
    def normalize(path: Path | str) -> Path:
        return Path(path).expanduser().resolve(strict=False)

    async def grant(self, grant: FilesystemGrant) -> None:
        grant.path = str(self.normalize(grant.path))
        await self.repository.save_grant(grant)

    async def revoke(self, agent_id: str, path: Path | str) -> None:
        await self.repository.delete_grants(agent_id, str(self.normalize(path)))

    async def revoke_all(self, agent_id: str) -> None:
        """Every grant this agent holds, gone -- for deleting the agent itself
        rather than narrowing what it can still reach."""
        await self.repository.delete_all_grants(agent_id)

    async def prune_missing_paths(self) -> int:
        """Revoke every grant whose directory is already gone.

        A harness job's grant is scoped to its generation's candidate
        directory and is meant to "die with the directory" (see
        environment.py's `submit_harness_job`), but nothing ever enforced
        that -- found live: 10193 filesystem_grants rows in state.db, one
        for essentially every harness job this mesh has ever run, none of
        them ever revoked even as CandidateWorkspace.prune_stale() deleted
        the generation directories they pointed at. A grant for a path that
        no longer exists can never be used for anything (``require()``
        needs the target path to actually sit under the granted root), so
        this is safe for any agent's grant, not only the Evolver's.
        """
        grants = await self.repository.load_grants()
        stale = [
            grant
            for grant in grants
            if not await asyncio.to_thread(Path(grant.path).exists)
        ]
        if stale:
            await self.repository.delete_grants_by_id(grant.id for grant in stale)
        return len(stale)

    async def require(self, agent_id: str, path: Path | str, operation: str) -> Path:
        target = self.normalize(path)
        for grant in await self.repository.load_grants(agent_id):
            root = self.normalize(grant.path)
            if target == root or root in target.parents:
                if operation == "read" and grant.read:
                    return target
                if operation == "write" and grant.write:
                    return target
        raise PermissionDeniedError(agent_id, target, operation)

