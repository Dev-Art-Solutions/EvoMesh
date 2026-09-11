"""Submit one trading signal to the local MT5 Execution Bridge.

argv[1] = payload (a JSON object), argv[2] = bridge_url (optional). The
Bridge itself validates and risk-checks the signal; this script only builds
a well-formed request and reports back whatever the Bridge decided.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from uuid import uuid4

REQUIRED_FIELDS = ("symbol", "action", "risk_percent")
OPTIONAL_FIELDS = ("stop_loss", "take_profit", "strategy", "comment")
POLL_ATTEMPTS = 5
POLL_DELAY_SECONDS = 1.0


def _request(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("usage: mt5_signal.py '<json payload>' [bridge_url]")
        return 1
    try:
        payload = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        print(f"payload is not valid JSON: {exc}")
        return 1
    if not isinstance(payload, dict):
        print("payload must be a JSON object")
        return 1
    missing = [field for field in REQUIRED_FIELDS if not payload.get(field)]
    if missing:
        print(f"payload is missing required fields: {', '.join(missing)}")
        return 1

    bridge_url = sys.argv[2].strip() if len(sys.argv) > 2 and sys.argv[2].strip() else "http://127.0.0.1:8200"
    base = bridge_url.rstrip("/")

    signal = {
        "signal_id": f"evomesh-{uuid4().hex[:16]}",
        "timestamp": datetime.now(UTC).isoformat(),
        "symbol": str(payload["symbol"]),
        "action": str(payload["action"]).upper(),
        "risk_percent": float(payload["risk_percent"]),
    }
    for field in OPTIONAL_FIELDS:
        if payload.get(field) is not None:
            signal[field] = payload[field]

    try:
        accepted = _request("POST", f"{base}/api/signals", signal)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        print(f"bridge rejected the signal (HTTP {exc.code}): {detail}")
        return 1
    except urllib.error.URLError as exc:
        print(f"could not reach the MT5 Execution Bridge at {base}: {exc.reason}")
        return 1

    if not accepted.get("accepted"):
        print(json.dumps(accepted, indent=2))
        return 0

    status = accepted
    for _ in range(POLL_ATTEMPTS):
        time.sleep(POLL_DELAY_SECONDS)
        try:
            status = _request("GET", f"{base}/api/signals/{signal['signal_id']}")
        except (urllib.error.HTTPError, urllib.error.URLError):
            break
        if status.get("status") not in ("RECEIVED", "PROCESSING"):
            break

    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
