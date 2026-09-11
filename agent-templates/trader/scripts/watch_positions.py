"""The Trader's deterministic order/equity watcher.

Run on its own short interval by evomesh.watchers.AgentWatcher -- never on the
agent's own cognition cycle. Polls the MT5 Execution Bridge's read-only
endpoints and prints one line only when a human-configured threshold is
crossed. Empty stdout means "nothing to report"; the watcher loop treats
silence as silence, not as a failure.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = TEMPLATE_DIR / "config.json"
STATE_PATH = TEMPLATE_DIR / "scripts" / ".watch_state.json"

DEFAULT_CONFIG = {
    "bridge_url": "http://127.0.0.1:8200",
    "equity_floor": None,
    "equity_drop_percent": 5.0,
    "max_position_loss": None,
}


def _load_json(path: Path, default: dict) -> dict:
    if not path.is_file():
        return dict(default)
    try:
        return {**default, **json.loads(path.read_text(encoding="utf-8"))}
    except (json.JSONDecodeError, OSError):
        return dict(default)


def _fetch(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "EvoMesh-Trader/1.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    config = _load_json(CONFIG_PATH, DEFAULT_CONFIG)
    state = _load_json(STATE_PATH, {"equity_high": None})
    base = str(config["bridge_url"]).rstrip("/")

    try:
        account = _fetch(f"{base}/api/account")
        positions = _fetch(f"{base}/api/positions")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        # The bridge (or the MT5 terminal behind it) is unreachable this tick.
        # Silent, not an alert -- transient outages are expected, and the next
        # tick tries again on its own.
        return 0

    alerts: list[str] = []
    equity = account.get("equity") if isinstance(account, dict) else None
    if isinstance(equity, (int, float)):
        high = state.get("equity_high")
        if high is None or equity > high:
            high = equity
        state["equity_high"] = high

        floor = config.get("equity_floor")
        if floor is not None and equity < floor:
            alerts.append(f"Equity {equity:.2f} is below the floor of {floor:.2f}.")

        drop_percent = config.get("equity_drop_percent")
        if drop_percent and high:
            drop = (high - equity) / high * 100
            if drop >= drop_percent:
                alerts.append(
                    f"Equity dropped {drop:.1f}% from its recent high of "
                    f"{high:.2f} to {equity:.2f}."
                )

    max_loss = config.get("max_position_loss")
    if max_loss is not None and isinstance(positions, list):
        for position in positions:
            profit = position.get("profit") if isinstance(position, dict) else None
            if isinstance(profit, (int, float)) and profit <= -abs(max_loss):
                alerts.append(
                    f"{position.get('symbol')} position #{position.get('ticket')} "
                    f"is down {profit:.2f}."
                )

    try:
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass

    if alerts:
        print(" ".join(alerts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
