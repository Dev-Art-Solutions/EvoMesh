#!/usr/bin/env python3
"""Check whether a URL responds, and how fast. Standard library only."""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check.py <url>", file=sys.stderr)
        return 2
    url = sys.argv[1]
    request = urllib.request.Request(url, headers={"User-Agent": "evomesh-check-site/1.0"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            elapsed = time.monotonic() - started
            print(f"{response.status} {url} responded in {elapsed:.2f}s")
            return 0
    except urllib.error.HTTPError as exc:
        elapsed = time.monotonic() - started
        print(f"{exc.code} {url} responded in {elapsed:.2f}s (HTTP error)")
        return 0
    except urllib.error.URLError as exc:
        print(f"{url} did not respond: {exc.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
