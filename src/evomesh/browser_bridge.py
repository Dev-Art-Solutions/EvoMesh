"""The native-messaging bridge between a real Chrome tab and a harness tool.

Every other way this mesh reaches the web goes through Scrapling: a separate,
headless, logged-out browser. That is wrong for a page behind a login a human
already has open in their own Chrome -- a bank portal, a dashboard, anything
session-gated. This is the other path: an agent asks Chrome, through the
human's own already-authenticated browser, to read or navigate the page it
already has open.

Three pieces, only one of which lives in this package:

- browser-extension/ (a Chrome MV3 extension, loaded unpacked by a human):
  connects to this process via chrome.runtime.connectNative and executes
  what it is asked -- read the active tab, navigate it, list open tabs.
- This module: Chrome spawns it (via the native-messaging host manifest a
  human registers with scripts/install-chrome-bridge.ps1) the moment the
  extension connects. It speaks Chrome's native-messaging protocol on
  stdin/stdout -- a 4-byte little-endian length prefix before each UTF-8
  JSON message -- on one side, and a one-request-per-line JSON protocol on
  a loopback TCP port on the other. The port is free-chosen and, with a
  random token every request must carry, registered in
  data/browser/bridges/<pid>.json together with the Chrome process that
  started this bridge -- which is how the mesh tells EvoMesh's automation
  Chrome from the human's own.
- browser.py, in the mesh process: the ``browser`` harness tool, which
  checks the agent's grant and asks a human about sensitive actions before
  it sends anything here. (The ``chrome-browser`` custom tool that used to
  connect here directly, with no grant at all, is retired.)

stdin here is a blocking, synchronous stream, and Windows' asyncio
ProactorEventLoop cannot drive it as a pipe the way Unix can -- so unlike
everywhere else in this codebase, the read side runs on a plain background
thread, handed back to the event loop with call_soon_threadsafe rather than
awaited directly. That thread, and Chrome's own one-tab-at-a-time reality,
are why this never tries to multiplex more than one request in flight per
correlation id -- a page navigation racing a read of the page it is
navigating away from was never a case worth supporting.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import struct
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
# The old fixed port. A bridge now listens on a free port and registers it
# (with its token) in the state directory, because every Chrome profile that
# has the extension starts a bridge of its own and two cannot share a port.
PORT = 8799
# One request line from a client. Requests are small; anything longer is
# refused before it is parsed.
MAX_REQUEST_BYTES = 64 * 1024
REQUEST_READ_SECONDS = 10.0
# A reply that arrives in pieces (a screenshot) may not grow past this.
MAX_REPLY_CHARS = 48 * 1024 * 1024

# Chrome's own documented ceiling for a message a native host sends *to* it.
# A message the other way (extension -> host) is allowed up to 4GB, but nothing
# this bridge asks for is ever that large, so the same cap is used for both
# directions -- a page's text is truncated by the extension long before this,
# and hitting it on an inbound frame is itself the sign of something to
# refuse rather than a limit to raise.
MAX_MESSAGE_BYTES = 1024 * 1024


class NativeMessagingEOF(Exception):
    """Chrome closed the pipe -- the extension disconnected, or was closed."""


def read_message(stream: BinaryIO) -> dict[str, Any]:
    """One framed message from a native-messaging stdin, or raise on EOF.

    Chrome's own framing: a 4-byte little-endian length, then that many
    bytes of UTF-8 JSON. ``stream.read(n)`` on a real pipe can legitimately
    return fewer than ``n`` bytes on one call without being at EOF, so both
    the length prefix and the payload are read in a loop, not a single call.
    """
    length_bytes = _read_exactly(stream, 4)
    (length,) = struct.unpack("<I", length_bytes)
    if length > MAX_MESSAGE_BYTES:
        raise ValueError(f"native message too large: {length} bytes")
    payload = _read_exactly(stream, length)
    message = json.loads(payload.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("native message is not a JSON object")
    return message


def _read_exactly(stream: BinaryIO, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise NativeMessagingEOF
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def write_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    encoded = json.dumps(message).encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError(f"native message too large to send: {len(encoded)} bytes")
    stream.write(struct.pack("<I", len(encoded)))
    stream.write(encoded)
    stream.flush()


class ExtensionBridge:
    """Correlates TCP requests with the extension's own replies.

    Every outgoing request carries a fresh ``id``; the extension echoes it
    back on the matching reply (browser-extension/background.js does exactly
    that). A reply too large for one native message arrives as numbered
    ``part``s and is reassembled here. A reply with an id nothing is waiting
    on -- the asker already timed out, or Chrome replayed something stale --
    is logged and dropped rather than raising, since a background thread has
    no good way to surface that to anyone.
    """

    def __init__(self, outbound: BinaryIO, loop: asyncio.AbstractEventLoop) -> None:
        self._outbound = outbound
        self._loop = loop
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._parts: dict[str, list[str | None]] = {}
        self._write_lock = threading.Lock()
        # Set once the reader thread stops -- a real native-messaging host is
        # expected to exit when Chrome disconnects, not linger with nothing
        # left to talk to (see run()).
        self.disconnected = asyncio.Event()

    def on_message(self, message: dict[str, Any]) -> None:
        """Called from the reader thread via call_soon_threadsafe."""
        request_id = message.get("id")
        if request_id and "chunk_index" in message:
            assembled = self._assemble(str(request_id), message)
            if assembled is None:
                return
            message = assembled
        future = self._pending.pop(request_id, None) if request_id else None
        if future is None:
            logger.warning("native message with no matching request: %r", str(message)[:300])
            return
        if not future.done():
            future.set_result(message)

    def _assemble(self, request_id: str, message: dict[str, Any]) -> dict[str, Any] | None:
        future = self._pending.get(request_id)
        try:
            index = int(message["chunk_index"])
            count = int(message["chunk_count"])
            part = str(message["part"])
        except (KeyError, TypeError, ValueError):
            self._fail(request_id, ValueError("malformed reply chunk"))
            return None
        if future is None or not 0 < count <= MAX_REPLY_CHARS // 1024 or not 0 <= index < count:
            self._parts.pop(request_id, None)
            if future is not None:
                self._fail(request_id, ValueError("malformed reply chunk"))
            return None
        parts = self._parts.setdefault(request_id, [None] * count)
        if len(parts) != count:
            self._fail(request_id, ValueError("reply chunks disagree on their count"))
            return None
        parts[index] = part
        if sum(len(item) for item in parts if item) > MAX_REPLY_CHARS:
            self._fail(request_id, ValueError("reply too large"))
            return None
        if any(item is None for item in parts):
            return None
        del self._parts[request_id]
        assembled = json.loads("".join(item or "" for item in parts))
        if not isinstance(assembled, dict):
            self._fail(request_id, ValueError("reply is not a JSON object"))
            return None
        return assembled

    def _fail(self, request_id: str, exc: BaseException) -> None:
        self._parts.pop(request_id, None)
        future = self._pending.pop(request_id, None)
        if future is not None and not future.done():
            future.set_exception(exc)

    def on_reader_stopped(self, exc: BaseException | None) -> None:
        """Chrome closed the pipe -- nothing still waiting will ever hear back."""
        pending, self._pending = self._pending, {}
        self._parts.clear()
        for future in pending.values():
            if not future.done():
                if exc is not None:
                    future.set_exception(exc)
                else:
                    future.set_exception(NativeMessagingEOF())
        self.disconnected.set()

    async def ask(
        self, request: dict[str, Any], *, timeout_seconds: float = 20.0
    ) -> dict[str, Any]:
        if self.disconnected.is_set():
            raise NativeMessagingEOF
        request_id = str(uuid4())
        future: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        self._pending[request_id] = future
        try:
            with self._write_lock:
                write_message(self._outbound, {**request, "id": request_id})
            async with asyncio.timeout(timeout_seconds):
                return await future
        finally:
            # Cancelled, timed out or answered: nothing is left waiting.
            self._pending.pop(request_id, None)
            self._parts.pop(request_id, None)


def start_reader_thread(
    inbound: BinaryIO, bridge: ExtensionBridge, loop: asyncio.AbstractEventLoop
) -> threading.Thread:
    def run() -> None:
        exc: BaseException | None = None
        try:
            while True:
                message = read_message(inbound)
                loop.call_soon_threadsafe(bridge.on_message, message)
        except NativeMessagingEOF:
            pass
        except Exception as caught:  # noqa: BLE001 - reported to the bridge, not raised here
            exc = caught
            logger.exception("native messaging reader failed")
        finally:
            loop.call_soon_threadsafe(bridge.on_reader_stopped, exc)

    thread = threading.Thread(target=run, name="evomesh-browser-bridge-reader", daemon=True)
    thread.start()
    return thread


def _ask_seconds(request: dict[str, Any]) -> float:
    """How long the extension gets: the action's own wait plus headroom,
    never unbounded."""
    try:
        wait = float(request.get("timeout_seconds") or 0)
    except (TypeError, ValueError):
        wait = 0.0
    return min(max(wait, 0.0), 30.0) + 30.0


async def handle_tool_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    bridge: ExtensionBridge,
    *,
    token: str | None = None,
) -> None:
    """One line in, one line out.

    With a ``token`` (always, when Chrome runs this -- see run()), the line
    must be ``{"token": ..., "request": {...}}`` with the token this bridge
    registered; anything else is refused before the extension is asked.
    Without one (the unit tests' own fake bridges) the line is the request.
    """
    try:
        try:
            line = await asyncio.wait_for(reader.readline(), REQUEST_READ_SECONDS)
        except TimeoutError:
            line = b""
            response: dict[str, Any] | None = {"error": "no request arrived in time"}
        except (ValueError, asyncio.LimitOverrunError):
            line = b""
            response = {"error": f"request larger than {MAX_REQUEST_BYTES} bytes, refused"}
        else:
            response = None
        if response is None:
            if not line:
                return
            response = await _answer(line, bridge, token)
        writer.write((json.dumps(response) + "\n").encode("utf-8"))
        await writer.drain()
    finally:
        writer.close()


async def _answer(line: bytes, bridge: ExtensionBridge, token: str | None) -> dict[str, Any]:
    try:
        request = json.loads(line.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"error": f"invalid request: {exc}"}
    if not isinstance(request, dict):
        return {"error": "invalid request: not a JSON object"}
    if token is not None:
        offered = request.get("token")
        if not isinstance(offered, str) or not hmac.compare_digest(offered, token):
            logger.warning("browser bridge: refused a request without the right token")
            return {"error": "unauthorized", "success": False}
        inner = request.get("request")
        if not isinstance(inner, dict):
            return {"error": "invalid request: no request object"}
        request = inner
    try:
        return await bridge.ask(request, timeout_seconds=_ask_seconds(request))
    except TimeoutError:
        return {"error": "the browser extension did not answer in time"}
    except NativeMessagingEOF:
        return {
            "error": "the browser extension is not connected "
            "(is Chrome running with the extension loaded?)"
        }
    except ValueError as exc:
        return {"error": f"the browser extension sent a bad reply: {exc}"}


async def serve(
    bridge: ExtensionBridge,
    *,
    host: str = HOST,
    port: int = PORT,
    token: str | None = None,
) -> asyncio.Server:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_tool_request(reader, writer, bridge, token=token)

    return await asyncio.start_server(handler, host, port, limit=MAX_REQUEST_BYTES)


# -- registration ------------------------------------------------------------


def state_dir() -> Path:
    """Where bridges register. chrome-native-host.bat starts this in the
    project root, so the default is the mesh's own data/browser."""
    configured = os.environ.get("EVOMESH_BROWSER_STATE")
    return Path(configured) if configured else Path.cwd() / "data" / "browser"


def process_parents() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, executable name), for walking up to the Chrome that
    started this bridge."""
    table: dict[int, tuple[int, str]] = {}
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class ProcessEntry(ctypes.Structure):
            _fields_ = [  # noqa: RUF012 - ctypes layout
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if snapshot in (None, wintypes.HANDLE(-1).value):
            return table
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(ProcessEntry)
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                table[int(entry.th32ProcessID)] = (
                    int(entry.th32ParentProcessID),
                    entry.szExeFile.lower(),
                )
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return table
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            stat = (entry / "stat").read_text()
            name = stat[stat.index("(") + 1 : stat.rindex(")")].lower()
            parent = int(stat[stat.rindex(")") + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        table[int(entry.name)] = (parent, name)
    return table


def chrome_ancestor(pid: int, table: dict[int, tuple[int, str]]) -> int | None:
    """The nearest ancestor of ``pid`` that is Chrome -- the browser process
    of the profile whose extension started this bridge."""
    seen: set[int] = set()
    current = table.get(pid, (0, ""))[0]
    while current and current not in seen and len(seen) < 16:
        seen.add(current)
        parent, name = table.get(current, (0, ""))
        if name in ("chrome.exe", "chrome", "google-chrome", "chromium", "chromium-browser"):
            return current
        current = parent
    return None


def register(directory: Path, port: int, token: str, extension: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{os.getpid()}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "port": port,
                "token": token,
                "chrome_pid": chrome_ancestor(os.getpid(), process_parents()),
                "started_at": datetime.now(UTC).isoformat(),
                "extension": extension,
            }
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


async def run() -> None:
    loop = asyncio.get_running_loop()
    bridge = ExtensionBridge(sys.stdout.buffer, loop)
    start_reader_thread(sys.stdin.buffer, bridge, loop)
    token = secrets.token_urlsafe(32)
    server = await serve(bridge, port=0, token=token)
    port = int(server.sockets[0].getsockname()[1])
    try:
        extension = await bridge.ask({"action": "ping"}, timeout_seconds=5)
    except (TimeoutError, NativeMessagingEOF):
        extension = {}
    extension.pop("id", None)
    registration = register(state_dir() / "bridges", port, token, extension)
    logger.info("browser bridge listening on %s:%s (%s)", HOST, port, registration)
    try:
        async with server:
            # Chrome expects a native-messaging host to exit once it
            # disconnects, not linger with a port open and nothing behind it.
            await bridge.disconnected.wait()
    finally:
        registration.unlink(missing_ok=True)
    logger.info("extension disconnected; shutting down")


def main() -> None:
    # stdout is Chrome's pipe; anything logged must go to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
