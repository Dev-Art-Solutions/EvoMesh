from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
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
    # 3: the Data Collector outbox (datacollector.py). A row is written before
    # record_event reports success, and leaves only by an acknowledgement or a
    # human's cancel -- never by a full outbox or a failed send.
    """
    CREATE TABLE IF NOT EXISTS dc_outbox(
        event_id TEXT PRIMARY KEY, source_key TEXT NOT NULL, content_hash TEXT NOT NULL,
        entity_type TEXT NOT NULL, agent_id TEXT NOT NULL, payload TEXT NOT NULL,
        state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, acked_at TEXT NOT NULL DEFAULT '',
        receipt TEXT NOT NULL DEFAULT ''
    );
    CREATE UNIQUE INDEX IF NOT EXISTS dc_outbox_same_observation
        ON dc_outbox(source_key, content_hash);
    CREATE INDEX IF NOT EXISTS dc_outbox_due ON dc_outbox(state, next_attempt_at);
    INSERT OR IGNORE INTO schema_version(version) VALUES (3);
    """,
    # 4: mail read from IMAP/POP3 (mail_store.py) and drafts awaiting approval.
    # uid is TEXT because a POP3 UIDL is a string, not a number.
    """
    CREATE TABLE IF NOT EXISTS mail_messages(
        account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity TEXT NOT NULL,
        uid TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '',
        thread_key TEXT NOT NULL DEFAULT '', sender TEXT NOT NULL DEFAULT '',
        recipients TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',
        date TEXT NOT NULL DEFAULT '', flags TEXT NOT NULL DEFAULT '',
        snippet TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
        headers TEXT NOT NULL DEFAULT '{}', attachments TEXT NOT NULL DEFAULT '[]',
        fetched_at TEXT NOT NULL,
        PRIMARY KEY(account, folder, uidvalidity, uid)
    );
    CREATE INDEX IF NOT EXISTS mail_messages_by_date ON mail_messages(account, folder, date);
    CREATE INDEX IF NOT EXISTS mail_messages_by_thread ON mail_messages(account, thread_key);
    CREATE TABLE IF NOT EXISTS mail_sync(
        account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity TEXT NOT NULL,
        last_uid TEXT NOT NULL DEFAULT '', synced_at TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(account, folder)
    );
    CREATE TABLE IF NOT EXISTS mail_drafts(
        id TEXT PRIMARY KEY, account TEXT NOT NULL, state TEXT NOT NULL,
        sent_at TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS mail_drafts_sent ON mail_drafts(account, state, sent_at);
    INSERT OR IGNORE INTO schema_version(version) VALUES (4);
    """,
]
SCHEMA_VERSION = len(MIGRATIONS)


class SchemaTooNewError(RuntimeError):
    """The database was written by a newer EvoMesh. Running older code over it
    could drop what it does not know, so the mesh refuses to start instead."""


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
            found = await self._found_version(db)
            if found > SCHEMA_VERSION:
                raise SchemaTooNewError(
                    f"{self.path} is at schema version {found}; this EvoMesh knows up to "
                    f"{SCHEMA_VERSION}. Start the newer EvoMesh that wrote it, or restore a "
                    "backup taken before the upgrade (python -m evomesh.backup restore)."
                )
            if 0 < found < SCHEMA_VERSION:
                await self._copy_before_migration(db, found)
            for migration in MIGRATIONS:
                await db.executescript(migration)
            await db.commit()

    @staticmethod
    async def _found_version(db: aiosqlite.Connection) -> int:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        )
        if await cursor.fetchone() is None:
            return 0
        cursor = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    async def _copy_before_migration(self, db: aiosqlite.Connection, found: int) -> None:
        """A consistent copy of the database as it was, before any new migration
        touches it -- the rollback for an upgrade gone wrong."""
        target = self.path.parent / "backups" / f"{self.path.stem}-pre-v{SCHEMA_VERSION}.db"
        if target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(target) as copy:
            await db.backup(copy)
        logger.warning(
            "database at schema %s, migrating to %s; the old one is kept at %s",
            found,
            SCHEMA_VERSION,
            target,
        )

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

    async def accept_work(
        self, key: str, value: object, agent_id: str, render: Callable[[], str]
    ) -> bool:
        """Record that an agent accepted a piece of work and store the agent
        holding its goal, in one transaction. False, with nothing written,
        when the work was accepted before. A crash can leave neither or both
        -- never an acceptance whose goal was lost, which a replay would then
        refuse as a duplicate.

        ``render`` produces the agent's row once the write lock is held, so
        it is the agent as it is at commit time -- not a snapshot taken before
        waiting for the lock, which would undo a save that landed meanwhile."""
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "INSERT INTO state(key, value) VALUES (?, ?)", (key, json.dumps(value))
                )
            except sqlite3.IntegrityError:
                await db.rollback()
                return False
            try:
                await db.execute(
                    "INSERT INTO agents(id, definition) VALUES (?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET definition = excluded.definition",
                    (agent_id, render()),
                )
            except BaseException:
                await db.rollback()
                raise
            await db.commit()
            return True

    # -- Data Collector outbox (datacollector.py) ----------------------------

    async def dc_enqueue(self, row: dict[str, object]) -> bool:
        """Store one event. False when the same observation (source_key and
        content_hash) is already in the outbox, acknowledged or not."""
        columns = (
            "event_id, source_key, content_hash, entity_type, agent_id, payload, state, created_at"
        )
        values = tuple(row[name.strip()] for name in columns.split(","))
        async with self._connect() as db:
            cursor = await db.execute(
                f"INSERT OR IGNORE INTO dc_outbox({columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            await db.commit()
            return cursor.rowcount == 1

    async def dc_counts(self) -> dict[str, int]:
        rows = await self._all("SELECT state, COUNT(*) FROM dc_outbox GROUP BY state")
        return {str(state): int(count) for state, count in rows}

    async def dc_rows(
        self, states: Sequence[str], *, due_before: float | None = None, limit: int = 50
    ) -> list[dict[str, object]]:
        marks = ", ".join("?" for _ in states)
        sql = f"SELECT * FROM dc_outbox WHERE state IN ({marks})"
        parameters: list[object] = list(states)
        if due_before is not None:
            sql += " AND next_attempt_at <= ?"
            parameters.append(due_before)
        sql += " ORDER BY created_at LIMIT ?"
        parameters.append(limit)
        return await self._dicts(sql, tuple(parameters))

    async def dc_update(self, event_id: str, **fields: object) -> None:
        allowed = {"state", "attempts", "next_attempt_at", "last_error", "acked_at", "receipt"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"dc_update: unsupported fields {sorted(set(fields) - allowed)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        await self._write(
            [
                (
                    f"UPDATE dc_outbox SET {assignments} WHERE event_id = ?",
                    (*fields.values(), event_id),
                )
            ]
        )

    # -- mail (mail_store.py) ------------------------------------------------

    MAIL_COLUMNS = (
        "account",
        "folder",
        "uidvalidity",
        "uid",
        "message_id",
        "thread_key",
        "sender",
        "recipients",
        "subject",
        "date",
        "flags",
        "snippet",
        "body",
        "headers",
        "attachments",
        "fetched_at",
    )

    async def mail_put(self, rows: Sequence[dict[str, object]]) -> None:
        names = ", ".join(self.MAIL_COLUMNS)
        marks = ", ".join("?" for _ in self.MAIL_COLUMNS)
        await self._write(
            [
                (
                    f"INSERT OR REPLACE INTO mail_messages({names}) VALUES ({marks})",
                    tuple(row.get(name, "") for name in self.MAIL_COLUMNS),
                )
                for row in rows
            ]
        )

    async def mail_select(
        self, where: str, parameters: tuple[object, ...], *, limit: int, offset: int = 0
    ) -> list[dict[str, object]]:
        """Rows matching a WHERE clause mail_store.py built from fixed
        fragments -- never from text a model wrote."""
        return await self._dicts(
            f"SELECT * FROM mail_messages WHERE {where} ORDER BY date DESC, uid DESC "
            "LIMIT ? OFFSET ?",
            (*parameters, limit, offset),
        )

    async def mail_set_flags(
        self, account: str, folder: str, uidvalidity: str, uid: str, flags: str
    ) -> None:
        await self._write(
            [
                (
                    "UPDATE mail_messages SET flags = ? WHERE account = ? AND folder = ? "
                    "AND uidvalidity = ? AND uid = ?",
                    (flags, account, folder, uidvalidity, uid),
                )
            ]
        )

    async def mail_drop(self, account: str, folder: str | None = None) -> None:
        """Forget cached mail: a folder whose UIDVALIDITY changed, a message
        moved away, or a whole account that was removed."""
        if folder is None:
            await self._write(
                [
                    ("DELETE FROM mail_messages WHERE account = ?", (account,)),
                    ("DELETE FROM mail_sync WHERE account = ?", (account,)),
                ]
            )
            return
        await self._write(
            [
                ("DELETE FROM mail_messages WHERE account = ? AND folder = ?", (account, folder)),
                ("DELETE FROM mail_sync WHERE account = ? AND folder = ?", (account, folder)),
            ]
        )

    async def mail_drop_message(
        self, account: str, folder: str, uidvalidity: str, uid: str
    ) -> None:
        await self._write(
            [
                (
                    "DELETE FROM mail_messages WHERE account = ? AND folder = ? "
                    "AND uidvalidity = ? AND uid = ?",
                    (account, folder, uidvalidity, uid),
                )
            ]
        )

    async def mail_sync_state(self, account: str, folder: str) -> dict[str, object] | None:
        rows = await self._dicts(
            "SELECT * FROM mail_sync WHERE account = ? AND folder = ?", (account, folder)
        )
        return rows[0] if rows else None

    async def mail_save_sync(self, row: dict[str, object]) -> None:
        await self._write(
            [
                (
                    "INSERT OR REPLACE INTO mail_sync(account, folder, uidvalidity, last_uid, "
                    "synced_at, last_error) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row["account"],
                        row["folder"],
                        row["uidvalidity"],
                        row.get("last_uid", ""),
                        row.get("synced_at", ""),
                        row.get("last_error", ""),
                    ),
                )
            ]
        )

    async def mail_sync_rows(self) -> list[dict[str, object]]:
        return await self._dicts("SELECT * FROM mail_sync ORDER BY account, folder")

    async def draft_save(
        self, draft_id: str, account: str, state: str, sent_at: str, payload: str
    ) -> None:
        await self._write(
            [
                (
                    "INSERT OR REPLACE INTO mail_drafts(id, account, state, sent_at, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (draft_id, account, state, sent_at, payload),
                )
            ]
        )

    async def draft_claim(self, draft_id: str, expected: str, new: str) -> bool:
        """Compare-and-swap a draft's state: the one place two approvals of the
        same draft are told apart, so a send can happen only once."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE mail_drafts SET state = ? WHERE id = ? AND state = ?",
                (new, draft_id, expected),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def draft_rows(
        self, states: Sequence[str] | None = None, limit: int = 50
    ) -> list[dict[str, object]]:
        if states is None:
            return await self._dicts(
                "SELECT * FROM mail_drafts ORDER BY rowid DESC LIMIT ?", (limit,)
            )
        marks = ", ".join("?" for _ in states)
        return await self._dicts(
            f"SELECT * FROM mail_drafts WHERE state IN ({marks}) ORDER BY rowid DESC LIMIT ?",
            (*states, limit),
        )

    async def draft_get(self, draft_id: str) -> dict[str, object] | None:
        rows = await self._dicts("SELECT * FROM mail_drafts WHERE id = ?", (draft_id,))
        return rows[0] if rows else None

    async def sends_since(self, account: str, since: str) -> int:
        rows = await self._all(
            "SELECT COUNT(*) FROM mail_drafts WHERE account = ? AND state IN ('sent', "
            "'uncertain') AND sent_at >= ?",
            (account, since),
        )
        return int(rows[0][0]) if rows else 0

    async def _dicts(
        self, sql: str, parameters: tuple[object, ...] = ()
    ) -> list[dict[str, object]]:
        try:
            return await self._dict_select(sql, parameters)
        except sqlite3.OperationalError as exc:
            if not _schema_is_missing(exc):
                raise
        await self._rebuild_schema()
        return await self._dict_select(sql, parameters)

    async def _dict_select(
        self, sql: str, parameters: tuple[object, ...]
    ) -> list[dict[str, object]]:
        async with self._connect() as db:
            cursor = await db.execute(sql, parameters)
            names = [column[0] for column in cursor.description or ()]
            return [dict(zip(names, row, strict=True)) for row in await cursor.fetchall()]

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
