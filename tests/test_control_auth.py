"""The control port answers only clients holding the token the mesh wrote.

Before this, any line that parsed as JSON ran as a console command, and a line
that did not was answered and skipped. A web page's fetch() to 127.0.0.1:8765
therefore worked: the HTTP request line and headers were skipped one by one,
and the body ran as a command. These tests drive that exact byte stream.
"""

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from evomesh.config import Settings
from evomesh.control import ControlServer, read_token
from evomesh.environment import Environment
from evomesh.models import MockProvider


@pytest.fixture
async def control(tmp_path: Path) -> AsyncIterator[tuple[ControlServer, asyncio.Event, int]]:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    shutdown = asyncio.Event()
    server = ControlServer(
        environment, shutdown, port=0, token_path=tmp_path / ".runtime" / "control.token"
    )
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    try:
        yield server, shutdown, port
    finally:
        await server.stop()
        await environment.stop()


async def _exchange(port: int, raw: bytes) -> list[dict[str, object]]:
    """Send ``raw``, then read answers until the server closes the connection."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    answers: list[dict[str, object]] = []
    try:
        while line := await asyncio.wait_for(reader.readline(), 5):
            answers.append(json.loads(line))
    finally:
        writer.close()
        await writer.wait_closed()
    return answers


async def test_a_browser_shaped_request_never_runs_its_body(
    control: tuple[ControlServer, asyncio.Event, int],
) -> None:
    _, shutdown, port = control
    body = json.dumps({"command": "/exit"}) + "\n"
    request = (
        "POST / HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nContent-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\n\r\n{body}"
    ).encode()

    answers = await _exchange(port, request)

    assert len(answers) == 1, "the request line must be the last line read"
    assert answers[0]["error"] is True
    assert not shutdown.is_set()


async def test_a_command_without_the_token_is_refused_and_the_connection_closed(
    control: tuple[ControlServer, asyncio.Event, int],
) -> None:
    _, shutdown, port = control
    stolen_guess = {"command": "/exit", "token": "not-the-token"}
    lines = (json.dumps(stolen_guess) + "\n" + json.dumps({"command": "/exit"}) + "\n").encode()

    answers = await _exchange(port, lines)

    assert len(answers) == 1
    assert answers[0]["unauthorized"] is True
    assert not shutdown.is_set()


async def test_ping_needs_no_token_and_a_tokened_command_runs(
    control: tuple[ControlServer, asyncio.Event, int],
) -> None:
    server, _, port = control
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    async def request(payload: dict[str, str]) -> dict[str, object]:
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        return json.loads(await reader.readline())

    assert (await request({"command": "/ping"}))["output"] == "EvoMesh control ready"
    status = await request({"command": "/status", "token": server.token})
    assert "status: READY" in str(status["output"])
    writer.close()
    await writer.wait_closed()


async def test_the_token_file_is_fresh_private_named_and_removed(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()
    path = tmp_path / ".runtime" / "control.token"
    path.parent.mkdir(parents=True)
    path.write_text("left-over-from-a-crashed-run", encoding="ascii")
    server = ControlServer(environment, asyncio.Event(), port=0, token_path=path)

    await server.start()
    try:
        assert read_token(path) == server.token
        assert os.environ["EVOMESH_CONTROL_TOKEN_FILE"] == str(path)
        if sys.platform != "win32":
            assert path.stat().st_mode & 0o077 == 0
    finally:
        await server.stop()
        await environment.stop()

    assert not path.exists()
    assert "EVOMESH_CONTROL_TOKEN_FILE" not in os.environ
