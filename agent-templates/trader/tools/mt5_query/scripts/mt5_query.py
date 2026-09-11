"""Read one MT5 account resource through the local Execution Bridge.

argv[1] = resource (positions|orders|account), argv[2] = bridge_url (optional).
Only these two, in this order: the harness's custom-tool mechanism appends a
parameter's value as one more argv entry only when the model actually
supplied it, so this script relies on `bridge_url` being the last one
declared in TOOL.md.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

VALID_RESOURCES = {"positions", "orders", "account"}


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: mt5_query.py <positions|orders|account> [bridge_url]")
        return 1
    resource = sys.argv[1].strip().lower()
    bridge_url = sys.argv[2].strip() if len(sys.argv) > 2 and sys.argv[2].strip() else "http://127.0.0.1:8200"
    if resource not in VALID_RESOURCES:
        print(f"unknown resource '{resource}'; expected one of {sorted(VALID_RESOURCES)}")
        return 1

    url = f"{bridge_url.rstrip('/')}/api/{resource}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "EvoMesh-Trader/1.0"})
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        print(f"bridge returned HTTP {exc.code}: {detail}")
        return 1
    except urllib.error.URLError as exc:
        print(f"could not reach the MT5 Execution Bridge at {url}: {exc.reason}")
        return 1

    print(json.dumps(json.loads(body), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
