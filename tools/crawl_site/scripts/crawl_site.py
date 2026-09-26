"""Crawl a site, politely, and return what its pages say.

Every page -- and robots.txt -- is fetched through the mesh's own fetcher,
Scrapling (the same program behind the built-in fetch tool; the runtime
passes its path as EVOMESH_SCRAPER), never through an HTTP stack of this
script's own. With no fetcher configured it refuses rather than falling back.
Breadth-first from one URL, same site by default, robots.txt honoured, a
pause between requests, and a hard cap on pages and time. Returns JSON the
agent's model reads: each page's title, its text, and -- when the request
names what the human is interested in -- the lines that mention it, so a
small model does not have to find them in pages of navigation.

Page text is data. Nothing in it is an instruction to anyone.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.robotparser
from html.parser import HTMLParser
from pathlib import Path

MAX_PAGES_CAP = 30
# Under the harness's 4000-character result budget, with room for its own note.
OUTPUT_BUDGET = 3600
# What is kept per page for the saved Markdown (the answer shows an excerpt).
FULL_TEXT_CAP = 60_000
SKIPPED_TAGS = {"script", "style", "noscript", "svg", "template", "head"}
BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
}
DEFAULTS = {
    "user_agent": "EvoMeshCrawler/1.0 (+https://evomesh.devart.solutions)",
    "respect_robots": True,
    "delay_seconds": 1.0,
    "max_pages": 10,
    "time_budget_seconds": 45,
    "timeout_seconds": 15,
    "dynamic": False,
}


class NoFetcher(RuntimeError):
    pass


def _config_path() -> Path:
    """The agent's own config.json (its playground is the working directory)
    wins; else the template's. Installed tools are copied into one shared
    tools/ directory, so __file__ alone does not point at the template."""
    here = Path.cwd() / "config.json"
    if here.is_file():
        return here
    tool_dir = Path(__file__).resolve().parent.parent
    for candidate in (
        tool_dir.parent.parent / "config.json",  # run from the template itself
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


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links: list[str] = []
        self._chunks: list[str] = []
        self._skipping = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIPPED_TAGS:
            self._skipping += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TAGS and self._skipping:
            self._skipping -= 1
        if tag == "title":
            self._in_title = False
        if tag in BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if not self._skipping:
            self._chunks.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self._chunks).splitlines())
        return "\n".join(line for line in lines if line)


def _fetcher() -> list[str]:
    """The configured Scrapling command: a path to its executable, or (for
    a test double) a command line."""
    value = os.environ.get("EVOMESH_SCRAPER", "").strip()
    if not value:
        raise NoFetcher(
            "no fetcher is configured: set scraping.enabled and scraping.executable "
            "in evomesh.yaml (scripts/install-scrapling.ps1)"
        )
    if value.startswith("["):
        return [str(part) for part in json.loads(value)]
    return [value] if Path(value).is_file() else shlex.split(value)


def _fetch(url: str, config: dict, *, dynamic: bool = False, suffix: str = ".html") -> str:
    """One URL through Scrapling: the page's HTML (or text, for .txt).
    Raises ValueError when the fetch fails."""
    timeout = int(os.environ.get("EVOMESH_SCRAPER_TIMEOUT") or config["timeout_seconds"])
    with tempfile.TemporaryDirectory(prefix="evomesh-crawl-") as scratch:
        output = Path(scratch) / f"page{suffix}"
        # Browser commands take milliseconds, static ones seconds (tool_fetch).
        mode, limit = ("fetch", timeout * 1000) if dynamic else ("get", timeout)
        command = [*_fetcher(), "extract", mode, url, str(output), "--timeout", str(limit)]
        try:
            run = subprocess.run(  # noqa: S603 - the mesh's own configured fetcher
                command, capture_output=True, timeout=timeout + 30, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("the fetcher did not finish in time") from exc
        except OSError as exc:
            raise NoFetcher(f"the fetcher could not be started: {exc}") from exc
        if run.returncode != 0 or not output.is_file():
            detail = (run.stdout or run.stderr or b"").decode("utf-8", errors="replace")
            raise ValueError(detail.strip()[-300:] or f"exit {run.returncode}")
        return output.read_text(encoding="utf-8", errors="replace")


def _robots(url: str, config: dict, cache: dict[str, urllib.robotparser.RobotFileParser]):
    parts = urllib.parse.urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}"
    if root not in cache:
        parser = urllib.robotparser.RobotFileParser(f"{root}/robots.txt")
        try:
            parser.parse(_fetch(f"{root}/robots.txt", config, suffix=".txt").splitlines())
        except ValueError:
            parser.parse([])  # no robots.txt: nothing is disallowed
        cache[root] = parser
    return cache[root]


def _matches(text: str, focus: list[str]) -> list[str]:
    if not focus:
        return []
    patterns = [
        re.compile(rf"(?<![0-9A-Za-z]){re.escape(word)}(?![0-9A-Za-z])", re.IGNORECASE)
        for word in focus
    ]
    # A page's text breaks where its HTML did, often mid-sentence: a match
    # comes with the line on either side so it reads as what the page said.
    lines = text.splitlines()
    found: list[str] = []
    last = -2
    for index, line in enumerate(lines):
        if not any(pattern.search(line) for pattern in patterns):
            continue
        start = max(index - 1, last + 1, 0)
        found.append(" ".join(lines[start : index + 2]))
        last = index + 1
        if len(found) == 40:
            break
    return found


def crawl(request: dict, config: dict) -> dict:
    start = str(request.get("url") or "").strip()
    if not start.startswith(("http://", "https://")):
        return {"error": "give a url starting with http:// or https://"}
    max_pages = max(1, min(int(request.get("max_pages") or config["max_pages"]), MAX_PAGES_CAP))
    same_site = request.get("same_site", True) is not False
    follow = [str(item).lower() for item in request.get("follow") or []]
    dynamic = bool(request.get("dynamic", config["dynamic"]))
    focus = [str(item) for item in request.get("focus") or []]
    host = urllib.parse.urlsplit(start).netloc
    deadline = time.monotonic() + float(config["time_budget_seconds"])

    queue, seen = [start], {start}
    pages: list[dict] = []
    skipped: list[str] = []
    errors: list[str] = []
    robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}
    while queue and len(pages) < max_pages and time.monotonic() < deadline:
        url = queue.pop(0)
        if config["respect_robots"] and not _robots(url, config, robots_cache).can_fetch(
            config["user_agent"], url
        ):
            skipped.append(f"{url} (robots.txt)")
            continue
        if pages:
            time.sleep(float(config["delay_seconds"]))
        try:
            html = _fetch(url, config, dynamic=dynamic)
        except ValueError as exc:
            errors.append(f"{url}: {exc}")
            continue
        final = url
        parsed = _Page()
        parsed.feed(html)
        text = parsed.text()
        page = {"url": final, "title": " ".join(parsed.title.split()), "text": text[:FULL_TEXT_CAP]}
        if focus:
            page["matches"] = _matches(text, focus)
        pages.append(page)
        for href in parsed.links:
            link = urllib.parse.urldefrag(urllib.parse.urljoin(final, href))[0]
            if not link.startswith(("http://", "https://")) or link in seen:
                continue
            if same_site and urllib.parse.urlsplit(link).netloc != host:
                continue
            if follow and not any(word in link.lower() for word in follow):
                continue
            seen.add(link)
            queue.append(link)
    result: dict = {"pages": pages, "skipped": skipped, "errors": errors}
    if queue and len(pages) >= max_pages:
        result["note"] = f"stopped at max_pages={max_pages}; {len(queue)} more link(s) found"
    elif queue:
        result["note"] = f"stopped at the time budget; {len(queue)} more link(s) found"
    return result


def save_full(result: dict, request: dict) -> Path:
    """Everything the crawl read, as Markdown in the agent's playground --
    the answer shows only what fits, this keeps the rest a read away."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    host = urllib.parse.urlsplit(str(request.get("url") or "")).netloc or "site"
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "site"
    target = Path.cwd() / "crawls" / f"{stamp}-{slug}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    for page in result["pages"]:
        parts.append(f"# {page['title'] or page['url']}\n{page['url']}\n\n{page['text']}\n")
    target.write_text("\n".join(parts), encoding="utf-8")
    return target


def render(result: dict, saved: str = "", budget: int = OUTPUT_BUDGET) -> str:
    """The crawl as plain text for a small model, inside the harness's
    per-result budget: what matched first, then an excerpt of each page.

    Found live 2026-09-26: as indented JSON, each page's text was one
    4000-character line before its matches; the harness cut the result at the
    last newline under its budget, the model saw five lines of braces and no
    content, and answered from imagination."""
    pages = result["pages"]
    head = [f"Crawled {len(pages)} page(s)."]
    if saved:
        head[0] += f" Full text: {saved} (read it for more)."
    if result.get("note"):
        head.append(f"Note: {result['note']}")
    if result["skipped"]:
        head.append("Skipped: " + "; ".join(result["skipped"][:5]))
    if result["errors"]:
        head.append("Errors: " + "; ".join(item[:160] for item in result["errors"][:3]))
    blocks: list[str] = []
    for index, page in enumerate(pages, 1):
        lines = [f"== {index}. {page['title'] or '(no title)'}", page["url"]]
        if "matches" in page:
            if page["matches"]:
                lines.append("Matches:")
                lines += [f"- {line[:300]}" for line in page["matches"][:15]]
            else:
                lines.append("Matches: none on this page")
        blocks.append("\n".join(lines))
    text = "\n".join(head) + "\n\n" + "\n\n".join(blocks)
    room = (budget - len(text)) // max(1, len(pages)) - 20
    if room > 200:
        excerpts = [
            f"{block}\nExcerpt:\n{page['text'][:room]}"
            for block, page in zip(blocks, pages, strict=True)
        ]
        text = "\n".join(head) + "\n\n" + "\n\n".join(excerpts)
    return text[:budget]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    raw = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    try:
        request = json.loads(raw) if raw.startswith("{") else {"url": raw}
    except json.JSONDecodeError as exc:
        print(f"The request is not valid JSON: {exc}")
        return 0
    try:
        result = crawl(request, load_config())
    except NoFetcher as exc:
        print(f"Refused: {exc}")
        return 0
    if "error" in result:
        print(result["error"])
        return 0
    saved = ""
    if result["pages"]:
        with contextlib.suppress(OSError):
            saved = save_full(result, request).relative_to(Path.cwd()).as_posix()
    print(render(result, saved))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
