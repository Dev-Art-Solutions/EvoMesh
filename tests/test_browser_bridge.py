"""The native-messaging bridge, tested without Chrome or a real pipe.

read_message/write_message and ExtensionBridge are pure logic, testable
against io.BytesIO and manual calls exactly as the reader thread and the TCP
handler would drive them. handle_tool_request/serve are tested end to end
over a real loopback TCP connection, which is the one piece that genuinely
needs asyncio rather than a mock.
"""

from __future__ import annotations

import asyncio
import io
import json
import struct

import pytest

from evomesh.browser_bridge import (
    MAX_MESSAGE_BYTES,
    ExtensionBridge,
    NativeMessagingEOF,
    handle_tool_request,
    read_message,
    serve,
    write_message,
)


def test_write_then_read_round_trips_a_message() -> None:
    stream = io.BytesIO()
    write_message(stream, {"id": "abc", "action": "list_tabs"})
    stream.seek(0)

    assert read_message(stream) == {"id": "abc", "action": "list_tabs"}


def test_read_message_raises_eof_on_an_empty_stream() -> None:
    with pytest.raises(NativeMessagingEOF):
        read_message(io.BytesIO(b""))


def test_read_message_raises_eof_on_a_truncated_length_prefix() -> None:
    with pytest.raises(NativeMessagingEOF):
        read_message(io.BytesIO(b"\x01\x02"))


def test_read_message_raises_eof_on_a_truncated_payload() -> None:
    stream = io.BytesIO(struct.pack("<I", 100) + b"{}")
    with pytest.raises(NativeMessagingEOF):
        read_message(stream)


def test_read_message_refuses_a_length_over_the_cap() -> None:
    stream = io.BytesIO(struct.pack("<I", MAX_MESSAGE_BYTES + 1))
    with pytest.raises(ValueError, match="too large"):
        read_message(stream)


def test_write_message_refuses_to_send_over_the_cap() -> None:
    huge = {"text": "x" * (MAX_MESSAGE_BYTES + 1)}
    with pytest.raises(ValueError, match="too large"):
        write_message(io.BytesIO(), huge)


async def test_bridge_correlates_a_reply_to_the_asker_that_sent_it() -> None:
    outbound = io.BytesIO()
    bridge = ExtensionBridge(outbound, asyncio.get_running_loop())

    ask_task = asyncio.create_task(bridge.ask({"action": "list_tabs"}))
    await asyncio.sleep(0)  # let ask() write its request and register the future
    outbound.seek(0)
    sent = read_message(outbound)
    bridge.on_message({"id": sent["id"], "tabs": []})

    assert await ask_task == {"id": sent["id"], "tabs": []}


async def test_bridge_ignores_a_reply_with_no_matching_id() -> None:
    bridge = ExtensionBridge(io.BytesIO(), asyncio.get_running_loop())

    bridge.on_message({"id": "nobody-is-waiting-on-this", "tabs": []})  # must not raise


async def test_bridge_times_out_when_the_extension_never_answers() -> None:
    bridge = ExtensionBridge(io.BytesIO(), asyncio.get_running_loop())

    with pytest.raises(TimeoutError):
        await bridge.ask({"action": "list_tabs"}, timeout_seconds=0.05)


async def test_bridge_fails_every_pending_ask_when_the_reader_stops() -> None:
    bridge = ExtensionBridge(io.BytesIO(), asyncio.get_running_loop())
    first = asyncio.create_task(bridge.ask({"action": "list_tabs"}, timeout_seconds=5))
    second = asyncio.create_task(bridge.ask({"action": "read_page"}, timeout_seconds=5))
    await asyncio.sleep(0)

    bridge.on_reader_stopped(None)

    with pytest.raises(NativeMessagingEOF):
        await first
    with pytest.raises(NativeMessagingEOF):
        await second
    assert bridge.disconnected.is_set()


class _FakeBridge:
    """Answers every ask() with a fixed script, in order -- stands in for a
    real ExtensionBridge so the TCP layer can be tested on its own."""

    def __init__(self, answers: list[dict | Exception]) -> None:
        self._answers = answers

    async def ask(self, request: dict, *, timeout_seconds: float = 20.0) -> dict:
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


async def test_handle_tool_request_relays_a_real_answer_over_tcp() -> None:
    bridge = _FakeBridge([{"tabs": [{"id": 1, "url": "https://example.com"}]}])
    server = await serve(bridge, port=0)  # type: ignore[arg-type]
    try:
        host, port = server.sockets[0].getsockname()[:2]
        reader, writer = await asyncio.open_connection(host, port)
        writer.write((json.dumps({"action": "list_tabs"}) + "\n").encode("utf-8"))
        await writer.drain()
        response = json.loads((await reader.readline()).decode("utf-8"))
        writer.close()
    finally:
        server.close()

    assert response == {"tabs": [{"id": 1, "url": "https://example.com"}]}


async def test_handle_tool_request_reports_a_timeout_as_a_clean_error() -> None:
    bridge = _FakeBridge([TimeoutError()])
    server = await serve(bridge, port=0)  # type: ignore[arg-type]
    try:
        host, port = server.sockets[0].getsockname()[:2]
        reader, writer = await asyncio.open_connection(host, port)
        writer.write((json.dumps({"action": "read_page"}) + "\n").encode("utf-8"))
        await writer.drain()
        response = json.loads((await reader.readline()).decode("utf-8"))
        writer.close()
    finally:
        server.close()

    assert "error" in response
    assert "did not answer in time" in response["error"]


async def test_handle_tool_request_reports_a_disconnected_extension() -> None:
    bridge = _FakeBridge([NativeMessagingEOF()])
    server = await serve(bridge, port=0)  # type: ignore[arg-type]
    try:
        host, port = server.sockets[0].getsockname()[:2]
        reader, writer = await asyncio.open_connection(host, port)
        writer.write((json.dumps({"action": "read_page"}) + "\n").encode("utf-8"))
        await writer.drain()
        response = json.loads((await reader.readline()).decode("utf-8"))
        writer.close()
    finally:
        server.close()

    assert "error" in response
    assert "not connected" in response["error"]


async def test_handle_tool_request_reports_invalid_json_without_asking_the_bridge() -> None:
    class _UnreachableBridge:
        async def ask(self, request: dict, *, timeout_seconds: float = 20.0) -> dict:
            raise AssertionError("must not ask the extension over malformed input")

    reader = asyncio.StreamReader()
    reader.feed_data(b"not json\n")
    reader.feed_eof()

    class _FakeWriter:
        def __init__(self) -> None:
            self.written = b""

        def write(self, data: bytes) -> None:
            self.written += data

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    writer = _FakeWriter()
    await handle_tool_request(reader, writer, _UnreachableBridge())  # type: ignore[arg-type]

    payload = json.loads(writer.written.decode("utf-8"))
    assert "error" in payload
