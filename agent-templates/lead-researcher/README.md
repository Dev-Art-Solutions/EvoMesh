# Lead Researcher setup

Use the matching [LeadCollector Windows setup guide](https://github.com/Dev-Art-Solutions/EvoMesh-LeadCollector/blob/main/docs/WINDOWS_SETUP.md) to build the API and extension, provision separate agent and worker credentials and set the export directory before starting the backend.

From an ordinary PowerShell in EvoMesh, register the Browser Bridge with
`./scripts/install-chrome-bridge.ps1`. Run these in the EvoMesh console:

```text
/agent-template spawn lead-researcher lead-researcher
/browser start
/browser grant lead-researcher interact
/mcp add lead-researcher leadcollector node "C:/Dev/EvoMesh-LeadCollector/apps/api/mcp-server.mjs"
/mcp check leadcollector
/browser status
```

Use the real absolute script path with forward slashes. Use an absolute
`node.exe` path if the EvoMesh process cannot find Node on PATH. The MCP
entrypoint reads the git-ignored `apps/api/.env.agent` beside it, so the console
command and persisted agent config carry no token. Edit the port there if the
operator uses a different loopback port.

In the dedicated window opened by `/browser start`, use `chrome://extensions`
with Developer mode to load both unpacked folders: EvoMesh’s
`browser-extension/` and LeadCollector’s `apps/extension/dist/`. In
LeadCollector’s popup Settings, configure its loopback backend URL and worker
token. Give Lead Researcher a few seconds to check in, allowing up to a full
alarm interval. The authenticated MCP health probe reports worker version and
last seen time. Use `chrome://extensions` to distinguish a missing extension
from one that is installed and cannot check in.

The grant above lets the agent inspect the automation profile for preflight.
If the operator wants the agent to manage the dedicated Chrome lifecycle,
use `/browser grant lead-researcher interact`. Keep the session as automation.
Stop the dedicated browser from the console with `/browser stop`.

For a tool allowlist, copy the template into a separate local bundle, change
its frontmatter name to `lead-researcher-local`, and insert the
`mcp_servers` block from [the Windows example](mcp.windows.yaml.example).
Install the local directory with `/agent-template install <directory>` and
spawn `lead-researcher-local` as `lead-researcher`. The template’s private
server is attached during spawn. Skip `/mcp add` when using this method.
Keep any operator paths and credentials local.

The default export path for the standard agent name is
`workspace/agents/lead-researcher/playground/exports` under the EvoMesh
checkout. Pass its absolute path as `-ExportDirectory` when initializing
the LeadCollector backend. Use the actual workspace root if customized.
The agent needs access to the export directory inside its playground.
The database and credential files remain outside it.

## Verify

Ask the agent to run preflight. It should report Chrome, bridge, MCP, API,
SQLite and the worker independently. Then use a small operator-approved
job as described in the collector smoke test. The agent uses the normal
EvoMesh harness and MCP client. No mesh-wide MCP grant is needed.

`uv run pytest tests/test_lead_researcher.py -q` tests template and grant
resolution. Set `EVOMESH_LEADCOLLECTOR_TEST_REPO` to a built and installed
LeadCollector checkout to also exercise the real Node MCP adapter from
EvoMesh against a fixture API.
