"""Schedule, list and remove this agent's own recurring crawl tasks.

A crawl task is an ordinary recurring goal (an interval or a cron schedule,
rule 20), marked "[crawl]" so it can be told apart from anything else the
agent is doing. This script only asks the mesh for it, through the same
command router a human types into (the control port, 127.0.0.1 only): no
second scheduler.

Which agent it schedules for comes from EVOMESH_AGENT_ID, set by the runtime
for the calling job -- never from the request -- so it can only ever add
tasks to itself. A floor on the interval and a cap on the number of tasks
keep a confused or manipulated model from flooding its own schedule.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import sys
from pathlib import Path

MARK = "[crawl]"
DEFAULTS = {"control_port": 8765, "min_interval_seconds": 300, "max_tasks": 20}
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


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


def load_config() -> dict:
    path = _config_path()
    loaded: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
    return {**DEFAULTS, **{k: v for k, v in loaded.items() if k in DEFAULTS}}


def command(port: int, text: str) -> str:
    """One console command over the mesh's control port; its text answer."""
    with socket.create_connection(("127.0.0.1", port), timeout=15) as connection:
        connection.sendall((json.dumps({"command": text}) + "\n").encode("utf-8"))
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                break
            buffer += chunk
    return str(json.loads(buffer.decode("utf-8")).get("output", ""))


def interval_seconds(every: str) -> int:
    """'3600', '30m', '2h', '1d' -> seconds."""
    match = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", every.lower())
    if match is None:
        raise ValueError(f"cannot read the interval {every!r}: use 30m, 2h, 1d or seconds")
    return int(match.group(1)) * _UNITS[match.group(2) or "s"]


def crawl_lines(port: int, agent: str) -> list[str]:
    return [line.strip() for line in command(port, f"/goals {agent}").splitlines() if MARK in line]


def run(request: dict, agent: str, config: dict) -> str:
    port = int(config["control_port"])
    action = str(request.get("action") or "list").lower()
    if action == "list":
        lines = crawl_lines(port, agent)
        return "\n".join(lines) if lines else "No crawl tasks are scheduled."
    if action == "remove":
        goal_id = str(request.get("goal_id") or "").strip()
        if not any(line.startswith(goal_id) for line in crawl_lines(port, agent)) or not goal_id:
            return f"There is no crawl task {goal_id!r} (see action=list)."
        return command(port, f"/goal drop {shlex.quote(agent)} {shlex.quote(goal_id)}")
    if action != "add":
        return "Unknown action: use add, list or remove."
    task = " ".join(str(request.get("task") or "").split())
    if len(task) < 12:
        return "Say what to crawl and what to look for: the task is one sentence or more."
    if len(crawl_lines(port, agent)) >= int(config["max_tasks"]):
        return f"Refused: {config['max_tasks']} crawl tasks already; remove one first."
    if request.get("cron"):
        schedule = str(request["cron"]).strip()
    elif request.get("every"):
        try:
            seconds = interval_seconds(str(request["every"]))
        except ValueError as exc:
            return str(exc)
        if seconds < int(config["min_interval_seconds"]):
            return (
                f"Refused: at most once every {config['min_interval_seconds']}s "
                "(config.json min_interval_seconds)."
            )
        schedule = str(seconds)
    else:
        return "Say when: every (30m, 2h, 1d) or cron (\"0 9 * * *\")."
    words = ["/goal", "add", agent, f"{MARK} {task}", "5", schedule]
    added = command(port, " ".join(shlex.quote(word) for word in words))
    found = re.search(r"Added goal (\S+) to", added)
    if found is None:
        return added
    if request.get("notify", True) is not False:
        command(port, f"/goal notify {shlex.quote(agent)} {shlex.quote(found.group(1))} on")
    return added


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    agent = os.environ.get("EVOMESH_AGENT_ID", "").strip()
    if not agent:
        print("Refused: no calling agent (EVOMESH_AGENT_ID is not set).")
        return 0
    raw = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    try:
        request = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        print(f"The request is not valid JSON: {exc}")
        return 0
    try:
        print(run(request, agent, load_config()))
    except OSError as exc:
        print(f"The mesh's control port did not answer: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
