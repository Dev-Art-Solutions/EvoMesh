from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from evomesh.contracts import AgentDefinition, FilesystemGrant, Message, SkillDefinition

MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY, definition TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS skills(name TEXT PRIMARY KEY, definition TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS agent_skills(
        agent_id TEXT NOT NULL, skill_name TEXT NOT NULL,
        PRIMARY KEY(agent_id, skill_name)
    );
    CREATE TABLE IF NOT EXISTS filesystem_grants(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS mutation_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL
    );
    INSERT OR IGNORE INTO schema_version(version) VALUES (1);
    """
]


class SQLiteRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            for migration in MIGRATIONS:
                await db.executescript(migration)
            await db.commit()

    async def save_agent(self, agent: AgentDefinition) -> None:
        await self._upsert("agents", "id", agent.id, "definition", agent.model_dump_json())

    async def load_agents(self) -> list[AgentDefinition]:
        rows = await self._all("SELECT definition FROM agents")
        return [AgentDefinition.model_validate_json(row[0]) for row in rows]

    async def save_message(self, message: Message) -> None:
        await self._upsert("messages", "id", message.id, "payload", message.model_dump_json())

    async def load_messages(self) -> list[Message]:
        rows = await self._all("SELECT payload FROM messages ORDER BY rowid")
        return [Message.model_validate_json(row[0]) for row in rows]

    async def save_grant(self, grant: FilesystemGrant) -> None:
        await self._upsert(
            "filesystem_grants", "id", grant.id, "payload", grant.model_dump_json()
        )

    async def delete_grants(self, agent_id: str, path: str) -> None:
        grants = await self.load_grants(agent_id)
        async with aiosqlite.connect(self.path) as db:
            for grant in grants:
                if grant.path == path:
                    await db.execute("DELETE FROM filesystem_grants WHERE id = ?", (grant.id,))
            await db.commit()

    async def load_grants(self, agent_id: str | None = None) -> list[FilesystemGrant]:
        rows = await self._all("SELECT payload FROM filesystem_grants")
        grants = [FilesystemGrant.model_validate_json(row[0]) for row in rows]
        return [grant for grant in grants if agent_id is None or grant.agent_id == agent_id]

    async def save_skill(self, skill: SkillDefinition) -> None:
        await self._upsert("skills", "name", skill.name, "definition", skill.model_dump_json())

    async def load_skills(self) -> list[SkillDefinition]:
        rows = await self._all("SELECT definition FROM skills")
        return [SkillDefinition.model_validate_json(row[0]) for row in rows]

    async def attach_skill(self, agent_id: str, skill_name: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO agent_skills(agent_id, skill_name) VALUES (?, ?)",
                (agent_id, skill_name),
            )
            await db.commit()

    async def save_state(self, key: str, value: object) -> None:
        await self._upsert("state", "key", key, "value", json.dumps(value))

    async def load_state(self, key: str) -> object | None:
        rows = await self._all("SELECT value FROM state WHERE key = ?", (key,))
        return json.loads(rows[0][0]) if rows else None

    async def record_mutation(self, payload: dict[str, object]) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO mutation_history(payload) VALUES (?)", (json.dumps(payload),)
            )
            await db.commit()

    async def _upsert(
        self, table: str, key_column: str, key: str, value_column: str, value: str
    ) -> None:
        allowed = {
            ("agents", "id", "definition"),
            ("messages", "id", "payload"),
            ("filesystem_grants", "id", "payload"),
            ("skills", "name", "definition"),
            ("state", "key", "value"),
        }
        if (table, key_column, value_column) not in allowed:
            raise ValueError("Unsupported storage target")
        sql = (
            f"INSERT INTO {table}({key_column}, {value_column}) VALUES (?, ?) "
            f"ON CONFLICT({key_column}) DO UPDATE SET {value_column} = excluded.{value_column}"
        )
        async with aiosqlite.connect(self.path) as db:
            await db.execute(sql, (key, value))
            await db.commit()

    async def _all(
        self, sql: str, parameters: tuple[object, ...] = ()
    ) -> list[aiosqlite.Row]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(sql, parameters)
            return list(await cursor.fetchall())
