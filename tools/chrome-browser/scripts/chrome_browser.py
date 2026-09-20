#!/usr/bin/env python3
"""Ask the browser bridge (evomesh.browser_bridge, run by Chrome itself via
scripts/chrome-native-host.bat) to do one thing in the human's own Chrome.

Standard library only, on purpose: a custom tool's own script runs as a plain
subprocess of a harness job, which only ever allow-lists bare program names
(harness.shell_allow) -- "python" needs nothing installed beyond the
interpreter itself to actually work.
"""

from __future__ import annotations

import json
import socket
import sys

HOST = "127.0.0.1"
PORT = 8799
TIMEOUT_SECONDS = 25.0


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: chrome_browser.py '<json request>'", file=sys.stderr)
        return 2
    try:
        request = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        print(f"not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(request, dict) or not request.get("action"):
        print('the request needs an "action" field', file=sys.stderr)
        return 2

    try:
        with socket.create_connection((HOST, PORT), timeout=TIMEOUT_SECONDS) as sock:
            sock.settimeout(TIMEOUT_SECONDS)
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            response = _read_line(sock)
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        print(
            "the browser bridge is not reachable on 127.0.0.1:8799 "
            f"({exc}). Is Chrome running with the EvoMesh Browser Bridge "
            "extension loaded, and scripts/install-chrome-bridge.ps1 run?",
            file=sys.stderr,
        )
        return 1

    try:
        payload = json.loads(response)
    except json.JSONDecodeError:
        print(f"the bridge sent something that was not JSON: {response!r}", file=sys.stderr)
        return 1
    if "error" in payload:
        print(payload["error"], file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _read_line(sock: socket.socket) -> str:
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks).decode("utf-8").strip()


if __name__ == "__main__":
    raise SystemExit(main())
