---
name: mt5_query
description: Read the MT5 account's current state through the local Execution Bridge -- open positions, pending orders, or account balance/equity/margin. Never places or changes a trade.
command: python "{tool_dir}/scripts/mt5_query.py"
parameters:
  - name: resource
    description: "One of: positions, orders, account"
    required: true
  - name: bridge_url
    description: "Base URL of the MT5 Execution Bridge (default http://127.0.0.1:8200)"
    required: false
---

Read-only. Ask for `resource: account` before placing a signal, to see
current equity; `resource: positions` to see what is already open;
`resource: orders` for resting orders.
