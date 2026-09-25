from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from evomesh.contracts import AgentDefinition, FilesystemGrant, Message

logger = logging.getLogger(__name__)

# The one table every operation needs. Its absence is how a wiped database
# announces itself, because SQLite reports a missing file as an empty one.
SENTINEL_TABLE = "agents"

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
    """,
    # 2: typed procedures (architecture closure v2). Additive: nothing above
    # is altered, so an older database migrates by gaining these tables.
    """
    CREATE TABLE IF NOT EXISTS procedure_definitions(
        procedure_id TEXT NOT NULL, revision INTEGER NOT NULL,
        digest TEXT NOT NULL, content TEXT NOT NULL,
        PRIMARY KEY(procedure_id, revision)
    );
    CREATE TABLE IF NOT EXISTS procedure_admissions(
        procedure_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(procedure_id, revision)
    );
    CREATE TABLE IF NOT EXISTS procedure_executions(
        execution_id TEXT PRIMARY KEY, occurrence_id TEXT NOT NULL,
        agent_id TEXT NOT NULL, status TEXT NOT NULL,
        version INTEGER NOT NULL, payload TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS procedure_one_open_execution
        ON procedure_executions(occurrence_id)
        WHERE status NOT IN ('completed', 'failed', 'cancelled');
    CREATE TABLE IF NOT EXISTS procedure_operations(
        operation_key TEXT PRIMARY KEY, execution_id TEXT NOT NULL,
        state TEXT NOT NULL, payload TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS procedure_operations_by_execution
        ON procedure_operations(execution_id);
    CREATE TABLE IF NOT EXISTS procedure_traces(
        trace_id TEXT PRIMARY KEY, goal_kind TEXT NOT NULL,
        occurrence_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL
    );
    INSERT OR IGNORE INTO schema_version(version) VALUES (2);
    """,
]


class SQLiteRepository:
    # Long enough to outlast a slow disk or a virus scanner holding the file,
    # short enough that a genuine deadlock still surfaces as an error.
    BUSY_TIMEOUT_SECONDS = 30.0

    def __init__(self, path: Path) -> None:
        self.path = path
        # Serialises rebuilds so a wipe does not have every agent loop
        # recreating the schema at once.
        self._repair_lock = asyncio.Lock()

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        """One connection per operation, but never one that gives up in five seconds.

        Every agent loop writes through its own connection, so the default
        rollback journal has writers taking exclusive locks on each other. WAL
        lets readers through and keeps writes to the log, and the busy timeout
        makes a contended write wait rather than raising "database is locked" --
        which used to reach a human as a failed test rather than a busy disk.

        The busy timeout is per connection and set here. The journal mode is
        not: it is a persistent property of the file, set once in ``initialize``.
        """
        async with aiosqlite.connect(self.path, timeout=self.BUSY_TIMEOUT_SECONDS) as db:
            await db.execute(f"PRAGMA busy_timeout={int(self.BUSY_TIMEOUT_SECONDS * 1000)}")
            yield db

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            # Switching journal mode takes an exclusive lock that SQLite refuses
            # outright rather than waiting for busy_timeout, so doing it on every
            # connection was a "database is locked" race whenever several loops
            # opened a freshly created file at once -- which is exactly what a
            # rebuild after a wipe does. Once here is also all WAL needs.
            await db.execute("PRAGMA journal_mode=WAL")
            for migration in MIGRATIONS:
                await db.executescript(migration)
            await db.commit()

    async def save_agent(self, agent: AgentDefinition) -> None:
        await self._upsert("agents", "id", agent.id, "definition", agent.model_dump_json())

    async def delete_agent(self, agent_id: str) -> None:
        await self._write([("DELETE FROM agents WHERE id = ?", (agent_id,))])

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
        await self._write(
            [
                ("DELETE FROM filesystem_grants WHERE id = ?", (grant.id,))
                for grant in grants
                if grant.path == path
            ]
        )

    async def delete_all_grants(self, agent_id: str) -> None:
        grants = await self.load_grants(agent_id)
        await self._write(
            [("DELETE FROM filesystem_grants WHERE id = ?", (grant.id,)) for grant in grants]
        )

    async def delete_grants_by_id(self, grant_ids: Iterable[str]) -> None:
        await self._write(
            [("DELETE FROM filesystem_grants WHERE id = ?", (grant_id,)) for grant_id in grant_ids]
        )

    async def load_grants(self, agent_id: str | None = None) -> list[FilesystemGrant]:
        rows = await self._all("SELECT payload FROM filesystem_grants")
        grants = [FilesystemGrant.model_validate_json(row[0]) for row in rows]
        return [grant for grant in grants if agent_id is None or grant.agent_id == agent_id]

    async def save_state(self, key: str, value: object) -> None:
        await self._upsert("state", "key", key, "value", json.dumps(value))

    async def load_state(self, key: str) -> object | None:
        rows = await self._all("SELECT value FROM state WHERE key = ?", (key,))
        return json.loads(rows[0][0]) if rows else None

    async def record_mutation(self, payload: dict[str, object]) -> None:
        await self._write(
            [("INSERT INTO mutation_history(payload) VALUES (?)", (json.dumps(payload),))]
        )

    # -- typed procedures ---------------------------------------------------
    #
    # Narrow operations, each one transaction. Definition content is written
    # once per (procedure_id, revision); an execution moves only by
    # compare-and-swap on its version, together with the operation records
    # that transition settles (plan 6.5, 11.4).

    async def insert_procedure_definition(
        self, procedure_id: str, revision: int, digest: str, content: str
    ) -> bool:
        """Store immutable content. True if stored or already identical;
        False if that revision exists with a different digest."""
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT digest FROM procedure_definitions WHERE procedure_id = ? AND revision = ?",
                (procedure_id, revision),
            )
            row = await cursor.fetchone()
            if row is not None:
                await db.rollback()
                return str(row[0]) == digest
            await db.execute(
                "INSERT INTO procedure_definitions(procedure_id, revision, digest, content) "
                "VALUES (?, ?, ?, ?)",
                (procedure_id, revision, digest, content),
            )
            await db.commit()
            return True

    async def load_procedure_definitions(self) -> list[tuple[str, int, str, str]]:
        rows = await self._all(
            "SELECT procedure_id, revision, digest, content FROM procedure_definitions"
        )
        return [(str(row[0]), int(row[1]), str(row[2]), str(row[3])) for row in rows]

    async def save_procedure_admission(
        self, procedure_id: str, revision: int, payload: str
    ) -> None:
        await self._write(
            [
                (
                    "INSERT INTO procedure_admissions(procedure_id, revision, payload) "
                    "VALUES (?, ?, ?) ON CONFLICT(procedure_id, revision) "
                    "DO UPDATE SET payload = excluded.payload",
                    (procedure_id, revision, payload),
                )
            ]
        )

    async def load_procedure_admissions(self) -> list[str]:
        rows = await self._all("SELECT payload FROM procedure_admissions")
        return [str(row[0]) for row in rows]

    async def create_procedure_execution(
        self, execution_id: str, occurrence_id: str, agent_id: str, status: str, payload: str
    ) -> bool:
        """False when the occurrence already has an open execution."""
        try:
            await self._write(
                [
                    (
                        "INSERT INTO procedure_executions"
                        "(execution_id, occurrence_id, agent_id, status, version, payload) "
                        "VALUES (?, ?, ?, ?, 1, ?)",
                        (execution_id, occurrence_id, agent_id, status, payload),
                    )
                ]
            )
        except sqlite3.IntegrityError:
            return False
        return True

    async def load_procedure_execution(self, execution_id: str) -> tuple[int, str] | None:
        rows = await self._all(
            "SELECT version, payload FROM procedure_executions WHERE execution_id = ?",
            (execution_id,),
        )
        return (int(rows[0][0]), str(rows[0][1])) if rows else None

    async def list_procedure_executions(
        self, *, occurrence_id: str | None = None, open_only: bool = False
    ) -> list[tuple[int, str]]:
        sql = "SELECT version, payload FROM procedure_executions WHERE 1 = 1"
        parameters: list[object] = []
        if occurrence_id is not None:
            sql += " AND occurrence_id = ?"
            parameters.append(occurrence_id)
        if open_only:
            sql += " AND status NOT IN ('completed', 'failed', 'cancelled')"
        rows = await self._all(sql + " ORDER BY rowid", tuple(parameters))
        return [(int(row[0]), str(row[1])) for row in rows]

    async def commit_procedure_transition(
        self,
        execution_id: str,
        expected_version: int,
        status: str,
        payload: str,
        operations: Sequence[tuple[str, str, str, str | None]] = (),
    ) -> bool:
        """Atomically move an execution from ``expected_version`` and write
        its operation records ``(key, state, payload, expected_state)``.

        ``expected_state`` None means the record must not exist yet; "*" means
        any. Any mismatch rolls the whole transition back and returns False,
        which is how two competing advances end with exactly one dispatch.
        """
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE procedure_executions SET status = ?, payload = ?, version = version + 1 "
                "WHERE execution_id = ? AND version = ?",
                (status, payload, execution_id, expected_version),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            for key, state, record, expected_state in operations:
                current = await db.execute(
                    "SELECT state FROM procedure_operations WHERE operation_key = ?", (key,)
                )
                row = await current.fetchone()
                if expected_state is None and row is not None:
                    await db.rollback()
                    return False
                if expected_state not in (None, "*") and (
                    row is None or str(row[0]) != expected_state
                ):
                    await db.rollback()
                    return False
                await db.execute(
                    "INSERT INTO procedure_operations(operation_key, execution_id, state, payload) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(operation_key) DO UPDATE SET "
                    "state = excluded.state, payload = excluded.payload",
                    (key, execution_id, state, record),
                )
            await db.commit()
            return True

    async def load_procedure_operation(self, operation_key: str) -> tuple[str, str] | None:
        rows = await self._all(
            "SELECT state, payload FROM procedure_operations WHERE operation_key = ?",
            (operation_key,),
        )
        return (str(rows[0][0]), str(rows[0][1])) if rows else None

    async def list_procedure_operations(self, execution_id: str) -> list[tuple[str, str]]:
        rows = await self._all(
            "SELECT state, payload FROM procedure_operations WHERE execution_id = ? ORDER BY rowid",
            (execution_id,),
        )
        return [(str(row[0]), str(row[1])) for row in rows]

    async def update_procedure_operation(
        self, operation_key: str, expected_state: str, state: str, payload: str
    ) -> bool:
        """Settle one operation record outside an execution transition (a
        child work result arriving), only from ``expected_state``."""
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE procedure_operations SET state = ?, payload = ? "
                "WHERE operation_key = ? AND state = ?",
                (state, payload, operation_key, expected_state),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.commit()
            return True

    async def insert_procedure_trace(
        self, trace_id: str, goal_kind: str, occurrence_id: str, payload: str
    ) -> bool:
        """False for a trace or occurrence already recorded (no double count)."""
        try:
            await self._write(
                [
                    (
                        "INSERT INTO procedure_traces(trace_id, goal_kind, occurrence_id, payload) "
                        "VALUES (?, ?, ?, ?)",
                        (trace_id, goal_kind, occurrence_id, payload),
                    )
                ]
            )
        except sqlite3.IntegrityError:
            return False
        return True

    async def list_procedure_traces(self, goal_kind: str | None = None) -> list[str]:
        if goal_kind is None:
            rows = await self._all("SELECT payload FROM procedure_traces ORDER BY rowid")
        else:
            rows = await self._all(
                "SELECT payload FROM procedure_traces WHERE goal_kind = ? ORDER BY rowid",
                (goal_kind,),
            )
        return [str(row[0]) for row in rows]

    async def _upsert(
        self, table: str, key_column: str, key: str, value_column: str, value: str
    ) -> None:
        allowed = {
            ("agents", "id", "definition"),
            ("messages", "id", "payload"),
            ("filesystem_grants", "id", "payload"),
            ("state", "key", "value"),
        }
        if (table, key_column, value_column) not in allowed:
            raise ValueError("Unsupported storage target")
        sql = (
            f"INSERT INTO {table}({key_column}, {value_column}) VALUES (?, ?) "
            f"ON CONFLICT({key_column}) DO UPDATE SET {value_column} = excluded.{value_column}"
        )
        await self._write([(sql, (key, value))])

    async def _write(self, statements: Sequence[tuple[str, tuple[object, ...]]]) -> None:
        """Apply statements in one transaction, rebuilding the schema if it vanished."""
        if not statements:
            return
        try:
            await self._apply(statements)
            return
        except sqlite3.OperationalError as exc:
            if not _schema_is_missing(exc):
                raise
        await self._rebuild_schema()
        await self._apply(statements)

    async def _all(
        self, sql: str, parameters: tuple[object, ...] = ()
    ) -> list[aiosqlite.Row]:
        """Read rows, rebuilding the schema if it vanished."""
        try:
            return await self._select(sql, parameters)
        except sqlite3.OperationalError as exc:
            if not _schema_is_missing(exc):
                raise
        await self._rebuild_schema()
        return await self._select(sql, parameters)

    async def _apply(self, statements: Sequence[tuple[str, tuple[object, ...]]]) -> None:
        async with self._connect() as db:
            for sql, parameters in statements:
                await db.execute(sql, parameters)
            await db.commit()

    async def _select(
        self, sql: str, parameters: tuple[object, ...] = ()
    ) -> list[aiosqlite.Row]:
        async with self._connect() as db:
            cursor = await db.execute(sql, parameters)
            return list(await cursor.fetchall())

    async def _rebuild_schema(self) -> None:
        """Put the tables back after the file was removed underneath a running mesh.

        ``data/`` is gitignored, so a ``git clean -xfd`` deletes the live
        database while the mesh is running. ``initialize`` only runs at startup,
        and SQLite reports a missing file as an empty one, so every connection
        after the wipe made a fresh empty file and every query failed with
        "no such table" until a human noticed -- 693 dead cycles, in the run
        that prompted this.

        What comes back is the schema and never the rows, so this is a way to
        keep running rather than a substitute for a backup. It says so at
        WARNING once per wipe: an empty mesh that looks healthy is its own bug.
        """
        async with self._repair_lock:
            # Another operation may have rebuilt it while this one queued.
            if await self._schema_exists():
                return
            logger.warning(
                "The database at %s has no schema; it was removed or replaced while the "
                "mesh was running. Recreating the tables -- the rows it held are gone.",
                self.path,
            )
            await self.initialize()

    async def _schema_exists(self) -> bool:
        rows = await self._select(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (SENTINEL_TABLE,),
        )
        return bool(rows)


def _schema_is_missing(exc: sqlite3.OperationalError) -> bool:
    """True for the wiped-database error, false for every other SQLite failure.

    A locked or corrupt database must still surface as itself: rebuilding the
    schema would not fix either, and retrying would hide the real fault.
    """
    return str(exc).startswith("no such table")
