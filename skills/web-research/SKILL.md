---
name: web-research
description: Fetch a web page and use its content to answer a question, instead of guessing from memory.
---

Use the `fetch` tool. Do not answer from memory when a URL is available or the
answer depends on something that could have changed since training.

1. Call `fetch` with the URL. It returns the page's main content as Markdown
   -- navigation, ads and scripts already stripped.
2. If the answer is buried in a large page, pass `css_selector` to narrow it
   to one section instead of reading the whole thing (for example
   `"article"`, `"#content"`, or a class selector from the page's own HTML).
3. If the result is empty or missing the content you expected, the page is
   probably rendering it with JavaScript. Retry the same call with
   `dynamic: true` -- it uses a real browser instead of a plain HTTP request.
   It is slower, and only works if a browser was installed for it
   (`scripts/install-scrapling.ps1 -WithBrowser`); if it was not, the tool
   says so plainly rather than returning nothing.
4. Quote or closely paraphrase what the page actually said, and name the URL
   it came from. Do not blend a page's content with what you already
   "knew" without saying which is which.
5. One fetch per distinct question. If the first page does not answer it,
   say so rather than fetching several more pages speculatively -- the same
   character budget that applies to everything else in this project applies
   to what `fetch` returns.
