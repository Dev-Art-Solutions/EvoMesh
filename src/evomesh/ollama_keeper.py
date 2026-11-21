"""Keep the local Ollama server up while the mesh runs.

Every agent on an ``ollama`` provider fails its cycles while Ollama is down,
and the mesh itself stays up and answers /ping, so nothing else notices.
scripts/run-supervised.ps1 starts Ollama before it starts the mesh, but a
mesh started any other way -- the Control Center, a console -- never had
that. Found 2026-11-21: Ollama's own auto-update closed the server and its
relaunch (``cmd.exe /C ... "ollama app.exe"``) died with 0xc0000142, and
the 0.40.17 tray app then failed to bring its server up at all. Every job
ended in ``ConnectError`` until a human noticed.

So the mesh process checks the provider's ``/api/version`` before the
agents start and then once a minute. When nothing answers and no
``ollama serve`` is already running (one may still be loading), it starts
``ollama serve`` itself -- the server binary, not the tray app, which is
the part that failed. The server is detached from the mesh so a /restart
does not take every loaded model down with it. Only a local base_url is
ever started; a process EvoMesh did not start is never stopped.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from evomesh.browser import ProcessInfo, list_processes

logger = logging.getLogger(__name__)

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
CHECK_SECONDS = 60.0
START_WAIT_SECONDS = 60.0


def is_local(base_url: str) -> bool:
    return (urlsplit(base_url).hostname or "").lower() in LOCAL_HOSTS


def find_ollama() -> Path | None:
    """ollama on PATH, else where the Windows installer puts it."""
    found = shutil.which("ollama")
    if found:
        return Path(found)
    if sys.platform == "win32":
        for root in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
            if not root:
                continue
            for candidate in (
                Path(root) / "Programs" / "Ollama" / "ollama.exe",
                Path(root) / "Ollama" / "ollama.exe",
            ):
                if candidate.is_file():
                    return candidate
    return None


def is_serving(process: ProcessInfo) -> bool:
    """An ``ollama serve`` process, whoever started it."""
    line = process.command_line.replace("\\", "/").lower()
    name = line.split(" serve", 1)[0]
    return " serve" in line and "ollama" in name.rsplit("/", 1)[-1]


async def answers(base_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{base_url.rstrip('/')}/api/version")
            return response.status_code == 200
    except httpx.HTTPError:
        return False


def _spawn(exe: Path, base_url: str, log_path: Path) -> int:
    environment = dict(os.environ)
    parts = urlsplit(base_url)
    if parts.port and parts.port != 11434:
        environment["OLLAMA_HOST"] = f"{parts.hostname}:{parts.port}"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        log.write(f"\n--- {datetime.now(UTC).isoformat()} evomesh started {exe} serve\n".encode())
        log.flush()
        kwargs: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "env": environment,
            "close_fds": True,
        }
        if sys.platform == "win32":
            detached = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
            try:
                # Out of any job the mesh runs in, so the server outlives it.
                process = subprocess.Popen(  # noqa: S603
                    [str(exe), "serve"],
                    creationflags=detached | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                    **kwargs,  # type: ignore[arg-type]
                )
            except OSError:
                process = subprocess.Popen(  # noqa: S603
                    [str(exe), "serve"],
                    creationflags=detached,
                    **kwargs,  # type: ignore[arg-type]
                )
        else:
            process = subprocess.Popen(  # noqa: S603
                [str(exe), "serve"],
                start_new_session=True,
                **kwargs,  # type: ignore[arg-type]
            )
    return process.pid


class OllamaKeeper:
    """One local Ollama endpoint, kept answering."""

    def __init__(
        self,
        base_url: str,
        log_path: Path,
        *,
        exe: Path | None = None,
        check_seconds: float = CHECK_SECONDS,
        start_wait_seconds: float = START_WAIT_SECONDS,
        probe: Callable[[str], Awaitable[bool]] = answers,
        processes: Callable[[], Iterable[ProcessInfo]] = list_processes,
        spawn: Callable[[Path, str, Path], int] = _spawn,
    ) -> None:
        self.base_url = base_url
        self.log_path = log_path
        self.exe = exe
        self.check_seconds = check_seconds
        self.start_wait_seconds = start_wait_seconds
        self._probe = probe
        self._processes = processes
        self._spawn = spawn
        self.started: list[int] = []
        self._was_up: bool | None = None

    async def ensure(self) -> str:
        """One check. Returns what it found or did: up, started, waiting,
        not_found, failed."""
        if await self._probe(self.base_url):
            if self._was_up is False:
                logger.info("Ollama at %s is answering again", self.base_url)
            self._was_up = True
            return "up"
        self._was_up = False
        serving = [p for p in await asyncio.to_thread(self._processes) if is_serving(p)]
        if serving:
            # Loading, or the app's own server: give it time, never stop it.
            logger.warning(
                "Ollama at %s is not answering; an ollama serve process is running "
                "(pid %s), waiting for it",
                self.base_url,
                ", ".join(str(p.pid) for p in serving),
            )
            return "up" if await self._wait() else "waiting"
        exe = self.exe or find_ollama()
        if exe is None:
            logger.error(
                "Ollama at %s is not answering and ollama was not found to start it",
                self.base_url,
            )
            return "not_found"
        try:
            pid = await asyncio.to_thread(self._spawn, exe, self.base_url, self.log_path)
        except OSError as exc:
            logger.error("could not start %s serve: %s", exe, exc)
            return "failed"
        self.started.append(pid)
        logger.warning(
            "Ollama at %s was not answering; started %s serve (pid %s, log %s)",
            self.base_url,
            exe,
            pid,
            self.log_path,
        )
        if await self._wait():
            logger.info("Ollama at %s is up", self.base_url)
            self._was_up = True
            return "started"
        logger.error(
            "Ollama at %s still not answering %.0fs after starting it; see %s",
            self.base_url,
            self.start_wait_seconds,
            self.log_path,
        )
        return "failed"

    async def _wait(self) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.start_wait_seconds
        while loop.time() < deadline:
            if await self._probe(self.base_url):
                return True
            await asyncio.sleep(min(2.0, self.start_wait_seconds))
        return False

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.check_seconds)
            try:
                await self.ensure()
            except Exception:  # noqa: BLE001 - the keeper must outlive one bad check
                logger.exception("Ollama check failed")


def keepers_for(providers: dict[str, object], log_dir: Path) -> list[OllamaKeeper]:
    """A keeper per distinct local Ollama endpoint whose provider allows it."""
    seen: set[str] = set()
    keepers: list[OllamaKeeper] = []
    for name, config in providers.items():
        kind = getattr(config, "kind", None) or ("ollama" if name == "ollama" else "openai")
        url = str(getattr(config, "base_url", "")).rstrip("/")
        if kind != "ollama" or not getattr(config, "autostart", True) or not is_local(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        keepers.append(OllamaKeeper(url, log_dir / "ollama-serve.log"))
    return keepers
