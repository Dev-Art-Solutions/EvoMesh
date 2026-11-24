"""Backup and restore of one EvoMesh installation.

    python -m evomesh.backup create [--root DIR] [--out FILE] [--include-secrets]
                                    [--include-browser-profile]
    python -m evomesh.backup verify FILE
    python -m evomesh.backup restore FILE --target DIR [--overwrite]

What a backup holds: the SQLite database (a consistent copy through SQLite's
own backup API, so a running mesh can be backed up), everything else under
``data/`` (the email audit, browser state unless left out), ``workspace/``
(agent memory, playgrounds, ideas), ``skills/``, ``tools/`` and
``agent-templates/`` (self-authored ones are untracked and exist nowhere
else), ``evomesh.yaml``, and a ``manifest.json`` with the SHA-256 of every
file and the database's schema version.

Secrets are left out unless asked for. With ``--include-secrets`` on Windows
they are encrypted with DPAPI for the current user, so the archive is useless
to anyone else; elsewhere they are refused rather than written in the clear.
The browser profile holds signed-in sessions and is left out by default.

**Restore never writes over a live installation**: it goes into a directory
of its own, refuses one that already has a database unless ``--overwrite``,
and refuses while a mesh holds that directory's lock. Run the restored copy
side by side first; that is the rehearsal.

Executed as its own process, not imported by the mesh -- listed in
``codebase.ENTRY_POINTS``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evomesh.storage import SCHEMA_VERSION

FORMAT = 1
DIRECTORIES = ("workspace", "skills", "tools", "agent-templates")
SKIP_PARTS = {"__pycache__", ".pytest-tmp", ".git", "node_modules"}
BROWSER_PROFILE = Path("data") / "browser" / "profile"
SECRETS = "evomesh.secrets.yaml"


class BackupError(Exception):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _protect(data: bytes, *, unprotect: bool = False) -> bytes:
    """DPAPI for the current Windows user; nothing else can open it."""
    if sys.platform != "win32":
        raise BackupError("secrets are only backed up on Windows, where DPAPI can protect them")
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    call = crypt32.CryptUnprotectData if unprotect else crypt32.CryptProtectData
    ok = call(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target))
    if not ok:
        raise BackupError(f"DPAPI failed (error {ctypes.get_last_error()})")  # type: ignore[attr-defined]
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _database_copy(database: Path) -> bytes:
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "evomesh.db"
        source = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        target = sqlite3.connect(copy)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return copy.read_bytes()


def _schema_version(data: bytes) -> int:
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "check.db"
        path.write_bytes(data)
        connection = sqlite3.connect(path)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("the database copy fails SQLite's integrity check")
            row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
            return int(row[0] or 0)
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"the database copy is not readable: {exc}") from exc
        finally:
            connection.close()


def create(
    root: Path,
    out: Path,
    *,
    database: Path | None = None,
    include_secrets: bool = False,
    include_browser_profile: bool = False,
) -> dict[str, Any]:
    root = root.resolve()
    database = database or root / "data" / "evomesh.db"
    files: dict[str, str] = {}
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:

        def add(name: str, data: bytes) -> None:
            archive.writestr(name, data)
            files[name] = _sha256(data)

        schema = 0
        if database.exists():
            data = _database_copy(database)
            schema = _schema_version(data)
            add("data/evomesh.db", data)
        data_dir = root / "data"
        for path in sorted(data_dir.rglob("*")) if data_dir.exists() else []:
            relative = path.relative_to(root)
            if not path.is_file() or path.resolve() == database.resolve():
                continue
            if path.name.startswith("evomesh.db-") or "backups" in relative.parts:
                continue  # WAL/SHM are inside the backup copy; old backups are not re-backed-up
            if not include_browser_profile and relative.parts[:3] == BROWSER_PROFILE.parts:
                continue
            add(relative.as_posix(), path.read_bytes())
        for directory in DIRECTORIES:
            base = root / directory
            for path in sorted(base.rglob("*")) if base.exists() else []:
                if path.is_file() and not SKIP_PARTS & set(path.relative_to(root).parts):
                    add(path.relative_to(root).as_posix(), path.read_bytes())
        config = root / "evomesh.yaml"
        if config.exists():
            add("evomesh.yaml", config.read_bytes())
        secrets = root / SECRETS
        if include_secrets and secrets.exists():
            add(f"{SECRETS}.dpapi", _protect(secrets.read_bytes()))
        manifest = {
            "format": FORMAT,
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_root": str(root),
            "schema_version": schema,
            "secrets": "dpapi" if include_secrets and secrets.exists() else "not included",
            "browser_profile": include_browser_profile,
            "files": files,
        }
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
    return manifest


def verify(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != FORMAT:
                raise BackupError(f"unknown backup format {manifest.get('format')}")
            names = set(archive.namelist()) - {"manifest.json"}
            missing = set(manifest["files"]) - names
            extra = names - set(manifest["files"])
            if missing or extra:
                raise BackupError(
                    "archive does not match its manifest "
                    f"(missing {sorted(missing)[:5]}, extra {sorted(extra)[:5]})"
                )
            for name, digest in manifest["files"].items():
                if _sha256(archive.read(name)) != digest:
                    raise BackupError(f"{name} does not match its checksum")
            if "data/evomesh.db" in manifest["files"]:
                found = _schema_version(archive.read("data/evomesh.db"))
                if found != manifest["schema_version"]:
                    raise BackupError("database schema differs from the manifest")
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise BackupError(f"{path} is not an EvoMesh backup: {exc}") from exc
    if manifest["schema_version"] > SCHEMA_VERSION:
        raise BackupError(
            f"the backup is at schema {manifest['schema_version']}; this EvoMesh knows "
            f"{SCHEMA_VERSION}. Restore it with the newer version that wrote it."
        )
    return manifest


def restore(path: Path, target: Path, *, overwrite: bool = False) -> dict[str, Any]:
    manifest = verify(path)
    target = target.resolve()
    lock = target / ".runtime" / "evomesh.lock"
    if lock.exists() and _lock_held(lock):
        raise BackupError(f"a mesh is running from {target}; stop it before restoring there")
    if (target / "data" / "evomesh.db").exists() and not overwrite:
        raise BackupError(
            f"{target} already has a database. Restore into an empty directory to rehearse, "
            "or pass --overwrite once you mean to replace it."
        )
    with zipfile.ZipFile(path) as archive:
        for name in manifest["files"]:
            destination = (target / name).resolve()
            if target not in destination.parents:
                raise BackupError(f"refusing a path outside the target: {name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            data = archive.read(name)
            if name == f"{SECRETS}.dpapi":
                destination = target / SECRETS
                data = _protect(data, unprotect=True)
            destination.write_bytes(data)
    for stale in ("evomesh.db-wal", "evomesh.db-shm"):
        (target / "data" / stale).unlink(missing_ok=True)
    return manifest


def _lock_held(lock: Path) -> bool:
    """The singleton lock is held while a mesh runs; try to take it."""
    from evomesh.singleton import AlreadyRunningError, SingletonLock

    probe = SingletonLock(lock)
    try:
        probe.acquire(wait_seconds=0)
    except AlreadyRunningError:
        return True
    probe.release()
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evomesh.backup")
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("create")
    make.add_argument("--root", type=Path, default=Path.cwd())
    make.add_argument("--out", type=Path, default=None)
    make.add_argument("--include-secrets", action="store_true")
    make.add_argument("--include-browser-profile", action="store_true")
    check = commands.add_parser("verify")
    check.add_argument("file", type=Path)
    back = commands.add_parser("restore")
    back.add_argument("file", type=Path)
    back.add_argument("--target", type=Path, required=True)
    back.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            out = args.out or args.root / "backups" / f"evomesh-{stamp}.zip"
            if args.include_browser_profile:
                print("warning: the browser profile holds signed-in sessions", file=sys.stderr)
            manifest = create(
                args.root,
                out,
                include_secrets=args.include_secrets,
                include_browser_profile=args.include_browser_profile,
            )
            print(
                f"{out}: {len(manifest['files'])} files, schema {manifest['schema_version']}, "
                f"secrets {manifest['secrets']}"
            )
        elif args.command == "verify":
            manifest = verify(args.file)
            print(f"OK: {len(manifest['files'])} files match, schema {manifest['schema_version']}")
        else:
            manifest = restore(args.file, args.target, overwrite=args.overwrite)
            print(f"Restored {len(manifest['files'])} files into {args.target.resolve()}")
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
