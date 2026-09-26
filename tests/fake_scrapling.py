"""A stand-in for the Scrapling CLI in tests: `extract get|fetch <url> <out>`.
Fetches the URL and writes the body where Scrapling would, so crawl_site's
real code path -- a subprocess, an output file -- runs without a browser or
the real program installed (CI has neither)."""

from __future__ import annotations

import sys
import urllib.error
import urllib.request


def main() -> int:
    _, _mode, url, output = sys.argv[1:5]
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
            body = response.read()
    except (urllib.error.URLError, OSError) as exc:
        print(f"fetch failed: {exc}")
        return 1
    with open(output, "wb") as handle:
        handle.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
