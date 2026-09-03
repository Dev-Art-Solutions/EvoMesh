---
name: check-site
description: Check whether a URL responds, and how fast.
command: python "{tool_dir}/scripts/check.py"
parameters:
  - name: url
    description: The URL to check, including scheme (https://...).
    required: true
---

A worked example of a custom tool: standard-library-only, so it needs no
dependency and nothing but `python` in `harness.shell_allow` to activate.

Unlike the `fetch` tool (via Scrapling, page content as Markdown) or the
`web-research` skill (a procedure for using `fetch`), this answers a
narrower, cheaper question -- is it up, and how long did it take -- as a
single named call: `check-site(url)` rather than a shell command a model has
to construct correctly from prose. Good for a goal on a cron schedule that
only needs a yes/no, not a page's content.
