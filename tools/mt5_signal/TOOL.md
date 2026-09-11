---
name: mt5_signal
description: Submit a risk-based trading signal (BUY or SELL) to the local MT5 Execution Bridge. The Bridge enforces its own risk checks (spread, daily loss, position limits, stop loss) before ever sending an order -- this tool only asks.
command: python "{tool_dir}/scripts/mt5_signal.py"
parameters:
  - name: payload
    description: >
      JSON object describing the trade, e.g. {"symbol": "XAUUSD", "action":
      "BUY", "risk_percent": 1.0, "stop_loss": 1900.0, "take_profit": 1950.0}.
      Required: symbol, action (BUY or SELL), risk_percent. Optional:
      stop_loss, take_profit, strategy, comment.
    required: true
  - name: bridge_url
    description: "Base URL of the MT5 Execution Bridge (default http://127.0.0.1:8200)"
    required: false
---

Never call this on your own initiative -- only when a human explicitly asked
for a trade or a strategy to be executed. The Bridge is the risk authority;
report whatever it decides plainly, and never retry a rejected signal.
