---
name: chrome-browser
description: Read or navigate a tab in the human's own signed-in Chrome, via a local browser extension.
command: python "{tool_dir}/scripts/chrome_browser.py"
parameters:
  - name: request
    description: >
      A JSON object, as one string. Always set "action" to one of:
      "list_tabs" (no other fields -- returns every open tab's id, url,
      title, active, window_id), "read_page" (optional "tab_id"; omit for
      the active tab -- returns that tab's url, title and visible text),
      "navigate" (requires "url"; optional "tab_id" to reuse a tab instead
      of the active one, optional "wait_seconds", default 20, for how long
      to wait for the page to finish loading -- returns the tab's id, final
      url and title once it does). Examples:
      {"action": "read_page"}
      {"action": "navigate", "url": "https://example.com"}
      {"action": "list_tabs"}
    required: true
---

Two required setup steps before this tool answers anything (see
`scripts/install-chrome-bridge.ps1`'s own header for the exact order): load
`browser-extension/` unpacked into Chrome, then run that script with the
extension id Chrome shows you. Until both are done -- or if Chrome is not
running, or the extension's own background worker has not reconnected yet --
this reports that plainly rather than hanging.

Unlike `fetch` (a separate, logged-out headless browser via Scrapling) or
`web-research` (a skill for using `fetch`), this is the human's *own*
browser, with whatever they are already signed into. Use it for a page
behind a login the mesh has no credentials of its own for; use `fetch` for
everything else, since it needs no human's browser to be open at all.

A single JSON request rather than separate `action`/`url`/`tab_id`
parameters on purpose: this project's own tool-authoring rules
(`.claude/skills/evomesh-tool-author/SKILL.md`) found that two or more
optional parameters on a custom tool are a positional-argument-ordering bug
waiting to happen, since a custom tool's parameters are appended to its
command as plain argv entries in the order TOOL.md lists them -- not passed
by name. One required JSON string sidesteps that entirely.
