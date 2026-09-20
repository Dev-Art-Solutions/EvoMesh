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
  a local TCP port on the other.
- tools/chrome-browser/ (a custom EvoMesh tool, TOOL.md + a thin script):
  what a harness job actually calls. It is a short-lived subprocess and
  cannot itself be what Chrome launches -- it just connects to this
  process's TCP port, sends one request, and prints the one response it
  gets back.

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
import json
import logging
import struct
import sys
import threading
from typing import Any, BinaryIO
from uuid import uuid4

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 8799

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
    return json.loads(payload.decode("utf-8"))


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
    """Correlates one TCP request at a time with the extension's own reply.

    Every outgoing request carries a fresh ``id``; the extension is expected
    to echo it back on the matching reply (browser-extension/background.js
    does exactly that). A reply with an id nothing is waiting on -- the
    asker already timed out, or Chrome replayed something stale -- is
    logged and dropped rather than raising, since a background thread has no
    good way to surface that to anyone.
    """

    def __init__(self, outbound: BinaryIO, loop: asyncio.AbstractEventLoop) -> None:
        self._outbound = outbound
        self._loop = loop
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Set once the reader thread stops -- a real native-messaging host is
        # expected to exit when Chrome disconnects, not linger with nothing
        # left to talk to (see run()).
        self.disconnected = asyncio.Event()

    def on_message(self, message: dict[str, Any]) -> None:
        """Called from the reader thread via call_soon_threadsafe."""
        request_id = message.get("id")
        future = self._pending.pop(request_id, None) if request_id else None
        if future is None:
            logger.warning("native message with no matching request: %r", message)
            return
        if not future.done():
            future.set_result(message)

    def on_reader_stopped(self, exc: BaseException | None) -> None:
        """Chrome closed the pipe -- nothing still waiting will ever hear back."""
        pending, self._pending = self._pending, {}
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
        request_id = str(uuid4())
        future: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        self._pending[request_id] = future
        write_message(self._outbound, {**request, "id": request_id})
        try:
            async with asyncio.timeout(timeout_seconds):
                return await future
        finally:
            self._pending.pop(request_id, None)


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


async def handle_tool_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, bridge: ExtensionBridge
) -> None:
    """One line in, one line out -- see tools/chrome-browser's own script."""
    try:
        line = await reader.readline()
        if not line:
            return
        try:
            request = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            response: dict[str, Any] = {"error": f"invalid request: {exc}"}
        else:
            try:
                response = await bridge.ask(request)
            except TimeoutError:
                response = {"error": "the browser extension did not answer in time"}
            except NativeMessagingEOF:
                response = {
                    "error": "the browser extension is not connected "
                    "(is Chrome running with the extension loaded?)"
                }
        writer.write((json.dumps(response) + "\n").encode("utf-8"))
        await writer.drain()
    finally:
        writer.close()


async def serve(
    bridge: ExtensionBridge, *, host: str = HOST, port: int = PORT
) -> asyncio.Server:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_tool_request(reader, writer, bridge)

    return await asyncio.start_server(handler, host, port)


async def run() -> None:
    loop = asyncio.get_running_loop()
    bridge = ExtensionBridge(sys.stdout.buffer, loop)
    start_reader_thread(sys.stdin.buffer, bridge, loop)
    server = await serve(bridge)
    logger.info("browser bridge listening on %s:%s", HOST, PORT)
    async with server:
        # Chrome expects a native-messaging host to exit once it disconnects,
        # not linger with a TCP port open and nothing on the other end of it.
        await bridge.disconnected.wait()
    logger.info("extension disconnected; shutting down")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
