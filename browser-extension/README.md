# EvoMesh Browser Bridge

Lets EvoMesh agents read and drive Chrome tabs through the `browser` tool.
Everything runs on your machine. Messages pass only between this extension,
Chrome's native-messaging pipe and the EvoMesh process on this computer.

There are two kinds of session:

- **EvoMesh's automation Chrome** (the default). This is a separate Chrome
  process on its own profile under `data/browser/profile`. It has no logins
  of yours. `/browser start` launches it and `/browser stop` stops only that
  process.
- **Your own Chrome.** An agent reaches it only if you grant it explicitly
  with `/browser grant <agent> read|interact user`. It then sees whatever you
  are signed into, so grant this sparingly.

For a page that needs neither (a public article, a search result), the
`fetch` tool and the Crawler's `fetch_page` need no browser at all.

## Setup on Windows (one time)

1. **Register the native-messaging host.** Run this from the repo root, in an
   ordinary (not elevated) PowerShell:

   ```powershell
   .\scripts\install-chrome-bridge.ps1
   ```

   It writes `scripts/com.evomesh.browser_bridge.json` and registers it under
   `HKCU:\Software\Google\Chrome\NativeMessagingHosts`. That is per user, needs
   no admin rights, and serves every Chrome profile. The extension's manifest
   carries a fixed public key, so its id is always
   `fgefcelipaiabacacgjcdkgachkdchcm` and no id needs copying.

2. **Start the automation Chrome:** `/browser start` in the EvoMesh console.

3. **Load the extension in that window, once.** Open `chrome://extensions`,
   turn on *Developer mode*, click *Load unpacked* and pick this
   `browser-extension/` folder. Branded Chrome 137 and later ignores the
   `--load-extension` switch EvoMesh passes, and EvoMesh will not install an
   extension any other way. The profile keeps the extension from then on.

4. **Check:** `/browser status` should say `extension connected: yes`.

5. **Grant an agent:** `/browser grant <agent> read` or
   `/browser grant <agent> interact`. Nothing is granted by default.

To use your own Chrome as well, load the extension there too (step 3, in your
profile) and grant with `user`.

## What an agent can do

The agent sends one JSON request per call: `{"action": ..., ...}`.

| Needs | Actions |
|---|---|
| `read` | `snapshot`, `read_page`, `get_links`, `get_elements`, `get_page_structure`, `get_page_metadata`, `get_element_attributes`, `list_tabs`, `get_active_tab`, `screenshot`, `element_screenshot`, `wait_for_element`, `wait_for_navigation`, `browser_status` |
| `interact` | `start_browser`, `stop_browser`, `navigate`, `back`, `forward`, `reload`, `new_tab`, `switch_tab`, `close_tab`, `click`, `double_click`, `fill`, `type_text`, `clear`, `press_key`, `scroll`, `select_option`, `check`, `uncheck` |

There is no action that runs JavaScript of the agent's choosing.

**Targets.** A target is one of:
- a ref from a snapshot: `{"ref": "k3x9a1:e12"}`
- an accessible role and name: `{"role": "button", "name": "Search"}`
- `label`, `placeholder`, `text` or `css`

Names match case-insensitively by substring, or exactly with `"exact": true`. When several elements match, add `"nth": 2`; otherwise the action fails with `ambiguous_target` and lists the candidates. A ref goes stale (`stale_ref`) when the page navigates or the element leaves the DOM. Hidden, disabled and covered elements are reported as `element_hidden`, `element_disabled` and `element_obscured`, never clicked anyway. Waits are bounded: `"timeout_seconds"`, at most 30.

```json
{"action": "fill", "target": {"role": "searchbox", "name": "Search"}, "value": "python tutorials"}
{"action": "press_key", "target": {"role": "searchbox", "name": "Search"}, "key": "Enter"}
{"action": "wait_for_element", "target": {"css": "#results a"}, "timeout_seconds": 10}
{"action": "get_links", "contains": "tutorial", "limit": 10}
{"action": "new_tab", "url": "https://docs.python.org/3/tutorial/"}
{"action": "screenshot"}
```

**A snapshot** is the compact view to start from. It holds:
- the URL, title and load state
- a window of the visible text (`text_offset` pages through it)
- numbered interactive elements (`element_offset` pages through them)
- links, forms with their fields, alert and error messages, open dialogs
- `human_required` when the page shows a CAPTCHA or a "verify you are human" check

The values of password, hidden and payment fields are never returned.

**Screenshots** are saved as files in the agent's own playground, under `screenshots/`, and the tool returns the path, not the image. They are capped by `browser.screenshot_max_bytes`, and only the newest `browser.screenshot_keep` are kept. Only the visible tab can be captured.

**Asking about a screenshot.** Add a question and the agent's own model looks at the image:

```json
{"action": "screenshot", "question": "Which quarter has the tallest bar?"}
```

- The image goes to the agent's own provider and model, in one call of its own, outside the job's transcript. Nothing else is sent anywhere.
- On Ollama, the model must list `vision` in `/api/show`. A text-only model is never sent the image, so it cannot describe a page it did not see. The reply says `vision_unsupported`.
- The answer is page content: it is marked untrusted, checked for instruction-like phrases, cut to `browser.vision_answer_chars`, and a reasoning model's thinking is removed.
- A failed or slow answer costs the answer, never the saved file. `browser.vision: false` refuses questions.

## What needs you

Some actions stop with `confirmation_required` no matter what the agent's grant says:
- payments
- sending, posting or publishing
- deleting things or changing account settings
- submitting a password
- typing into password or card fields
- installing software

The check runs on what the page says the element is, inside the mesh process. EvoMesh tells you, and `/browser pending` lists the request. `/browser approve <n>` lets that one exact action through once. `/browser deny <n>` refuses it. After an approval the agent must repeat the identical request with `"confirmation": <n>`. Approvals expire after `browser.confirmation_minutes`.

A CAPTCHA or bot check is never worked around. The snapshot says `human_required`, and the agent stops and reports.

Web page text is untrusted. Every result carrying it starts with a line saying so, and instruction-override phrases in it are flagged under `warnings`.

## How it fits together

```
agent's harness job --(browser tool, in the mesh process: grant, confirmation)-->
    browser.py --(one JSON line + token, loopback TCP, free port)--> browser_bridge.py
        --(Chrome native messaging, stdin/stdout)--> background.js (tabs, navigation, screenshots)
            --(chrome.scripting)--> page_agent.js (targeting, snapshots, interaction) --> the tab
```

Chrome starts `browser_bridge.py` (via `scripts/chrome-native-host.bat`) when the extension connects, and the bridge exits when it disconnects. Each bridge listens on a free loopback port and writes its port, a random token and the Chrome process that started it to `data/browser/bridges/<pid>.json`. A request without that token is refused. That record is how EvoMesh tells its automation Chrome from yours. Native messages stay under Chrome's 1 MB limit; a larger reply, such as a screenshot, is sent in pieces and reassembled.

## Troubleshooting

`/browser status` names the missing piece. The usual ones:

- **`browser_not_running`**: the automation Chrome is not running. Use `/browser start`, or `{"action": "start_browser"}` from an agent with an interact grant.
- **`extension_not_connected`**: Chrome is running but the extension is not loaded in that profile (step 3), or the native host is not registered (step 1). After registering, reload the extension.
- **`browser_disconnected`**: the bridge went away, for example because Chrome closed. The next request uses the new bridge once the extension reconnects; it retries once a minute.
- **`page_not_scriptable`**: `chrome://` pages and the Web Store cannot be read or driven.
- **`navigation_failed`**: Chrome's own error, such as `net::ERR_NAME_NOT_RESOLVED`.

## Known limits

- Clicks and typing are DOM events (`isTrusted: false`). A few sites ignore them; those need a human.
- File uploads are not supported.
- Elements inside cross-origin iframes and closed shadow roots are not reached.
- A screenshot is looked at only when the request asks a question. The job's transcript stays text, so the model sees the answer, not the image.
- Cancelling a job stops the waiting. A page action already running in the tab still finishes.
