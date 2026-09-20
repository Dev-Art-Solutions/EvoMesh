# EvoMesh Browser Bridge

Lets an EvoMesh agent read or navigate a tab in **your own signed-in
Chrome** -- for a page behind a login the mesh has no credentials of its
own for. Everything here runs on your machine; nothing is sent anywhere
except between this extension, Chrome's native-messaging pipe, and the
EvoMesh process already running on this computer.

For anything that does *not* need your own session (a public page, an
article, a search result), the mesh's existing `fetch` tool (a separate,
headless, logged-out browser via Scrapling) is the right tool and needs
none of this setup.

## What it can do

An agent with the `chrome-browser` tool (see `tools/chrome-browser/TOOL.md`)
can ask this bridge to:

- **`list_tabs`** -- every open tab's id, url, title.
- **`read_page`** -- the active tab's (or a named tab's) url, title, and
  visible text.
- **`navigate`** -- point a tab at a URL and wait for it to finish loading.

It cannot click, type, or fill in a form. That is a deliberate first cut,
not an oversight: reading and navigating cover most of what "check this
page I'm signed into" needs, and a click/type surface is real additional
risk (a wrong click on a real, signed-in banking or admin page is not a
"try again" in the way a read is) worth adding only once the read-only
half has actually proven itself useful.

## Setup (one time, by a human -- this cannot be automated from here)

1. **Load the extension.** Open `chrome://extensions`, turn on *Developer
   mode* (top right), click *Load unpacked*, and select this
   `browser-extension/` folder. Chrome will show you the extension's id --
   a 32-character string starting with a lowercase letter -- copy it.

2. **Register the native-messaging host**, from this repo's root, in an
   ordinary (non-elevated) PowerShell:

   ```powershell
   .\scripts\install-chrome-bridge.ps1 -ExtensionId <the id from step 1>
   ```

   This writes `scripts/com.evomesh.browser_bridge.json` (naming the
   extension id as the only origin allowed to connect) and registers it
   under `HKCU:\Software\Google\Chrome\NativeMessagingHosts`. Per-user, no
   admin rights needed, and nothing system-wide.

3. **Reload the extension** (the circular reload icon on its card in
   `chrome://extensions`) so its background worker makes a fresh
   `connectNative` call against the manifest you just registered.

4. **Add the tool to an agent.** `tools: [chrome-browser]` in an
   `AGENT.md`, with `python` in `evomesh.yaml`'s `harness.shell_allow` (off
   by default -- see `evomesh.yaml.example`), is what actually lets an
   agent call it. Installing the extension alone grants nothing to any
   agent that was not also told the tool exists.

## How it fits together

```
harness job --(one JSON line over TCP, 127.0.0.1:8799)--> browser_bridge.py
                                                                |
                                                    (Chrome's native-messaging
                                                     protocol on stdin/stdout;
                                                     Chrome itself spawns this
                                                     process from the registry
                                                     entry above)
                                                                |
                                                                v
                                                    background.js (this extension)
                                                                |
                                                    chrome.tabs / chrome.scripting
                                                                |
                                                                v
                                                        your actual Chrome tab
```

`browser_bridge.py` (`src/evomesh/browser_bridge.py`, run as
`python -m evomesh.browser_bridge` by `scripts/chrome-native-host.bat`) is
a short-lived process: Chrome starts it when the extension connects and
expects it to exit when the extension disconnects, so it is not something
you run yourself or add to `evomesh.yaml` -- Chrome owns its lifecycle
entirely.

## Troubleshooting

- **"the browser bridge is not reachable on 127.0.0.1:8799"** -- Chrome has
  not spawned the native host yet. Check that the extension is loaded and
  enabled, and that step 2 above actually completed (look for
  `scripts/com.evomesh.browser_bridge.json` on disk).
- **"the browser extension is not connected"** -- the host process is
  running (something is listening on the port) but Chrome's own connection
  to it has dropped, most often because the extension's service worker
  went idle. Reloading the extension reconnects it; it also retries once a
  minute on its own (see the alarm in `background.js`).
- **A page never seems to finish loading in `navigate`** -- the tool still
  returns once its `wait_seconds` (default 20) elapses, with whatever the
  tab's state is at that point; a page that never reaches Chrome's
  "complete" status (a long-polling dashboard, for instance) is not a bug
  in this bridge.
