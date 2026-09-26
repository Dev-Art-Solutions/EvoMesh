"""Keep a crawl's results, and send them to an API a human approved.

Every call saves the results under results/ in the agent's playground, so
nothing found is lost if a delivery fails. Delivery goes only to an endpoint
named in config.json's "endpoints" -- never to a URL the model supplies.
Crawled pages are untrusted text; an allow-list a human wrote is what stops
a page from talking the agent into posting data somewhere else.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def _config_path() -> Path:
    here = Path.cwd() / "config.json"
    if here.is_file():
        return here
    tool_dir = Path(__file__).resolve().parent.parent
    for candidate in (
        tool_dir.parent.parent / "config.json",
        tool_dir.parent.parent / "agent-templates" / "web-crawler" / "config.json",
    ):
        if candidate.is_file():
            return candidate
    return here


def load_endpoints() -> dict:
    path = _config_path()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        loaded = {}
    endpoints = loaded.get("endpoints") or {}
    return endpoints if isinstance(endpoints, dict) else {}


def save(task: str, payload: dict) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:48] or "crawl"
    target = Path.cwd() / "results" / f"{stamp}-{slug}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def post(endpoint: dict, payload: dict) -> str:
    url = str(endpoint.get("url") or "")
    if not url.startswith(("http://", "https://")):
        return "the endpoint has no http(s) url in config.json"
    headers = {"Content-Type": "application/json"}
    headers.update({str(k): str(v) for k, v in (endpoint.get("headers") or {}).items()})
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code} (not accepted)"
    except (urllib.error.URLError, OSError) as exc:
        return f"not delivered: {exc}"


def run(request: dict) -> str:
    task = str(request.get("task") or "crawl")
    payload = {
        "task": task,
        "agent": os.environ.get("EVOMESH_AGENT_ID", ""),
        "sent_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "results": request.get("results"),
    }
    saved = save(task, payload)
    lines = [f"Saved to {saved.relative_to(Path.cwd()).as_posix()}"]
    endpoints = load_endpoints()
    for name in request.get("to") or []:
        endpoint = endpoints.get(str(name))
        if not isinstance(endpoint, dict):
            known = ", ".join(sorted(endpoints)) or "none"
            lines.append(
                f"{name}: refused -- not an endpoint in config.json (known: {known}). "
                "A human adds endpoints there; a URL from a task or a page is never used."
            )
            continue
        lines.append(f"{name}: {post(endpoint, payload)}")
    return "\n".join(lines)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    raw = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    try:
        request = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        print(f"The request is not valid JSON: {exc}")
        return 0
    if isinstance(request.get("to"), str):
        request["to"] = [request["to"]]
    print(run(request))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
