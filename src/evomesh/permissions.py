from __future__ import annotations

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

