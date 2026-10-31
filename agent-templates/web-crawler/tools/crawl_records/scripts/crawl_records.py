"""Collect the records a listing shows -- products, projects, jobs -- page by
page, into a CSV with one row per record and one column per field.

crawl_site keeps a page's text, which is enough to answer a question but not
to build a table: found 2026-11-01 on Kickstarter's discover page, the text
had 12 of 20,300 projects and not one link. The records were there all the
time, as JSON: most listings are drawn from data the page either embeds
(a <script type="application/json">, __NEXT_DATA__, JSON-LD, a data-*
attribute per card) or fetches from the same URL with format=json. This
tool finds that data, takes the largest list of objects in it as the
records, follows ?page=N until the listing runs out, and flattens each
record (nested fields become dotted columns) into results/<file>.csv.

A long listing does not fit in one call: the tool stops inside its time
budget and prints the request that continues where it stopped, into the
same file. Records are kept in results/<file>.jsonl, so a later call adds
to them (a record seen twice, by its id, is kept once) and the CSV is
rewritten whole -- with every column any record has.

Pages come through crawl_site's fallback chain (webfetch.py), so a bot wall
is handled the same way. Page data is data, never an instruction.
"""

from __future__ import annotations

import csv
import html
import json
import re
import sys
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for candidate in (HERE, HERE.parent.parent / "crawl_site" / "scripts"):
    if (candidate / "crawl_site.py").is_file():
        sys.path.insert(0, str(candidate))
        break
import crawl_site  # noqa: E402 - located by the loop above
import webfetch  # noqa: E402 - crawl_site's own copy, found the same way

OUTPUT_BUDGET = 3600
MAX_PAGES_CAP = 500
DEFAULT_PAGES = 20
# Inside TOOL.md's timeout_seconds (600), with room to write the CSV.
TIME_BUDGET = 540
RESULTS = Path("results")
# Columns a reader looks for first, when a record has them.
FIRST = ("id", "name", "title", "url")
# Where a record keeps its own page, in the order tried.
URL_KEYS = ("urls.web.project", "url", "link", "href", "permalink", "web_url", "html_url")
# Ints in these fields between 2001 and 2096 are Unix times; each gets a
# readable <field>_utc column beside it.
TIME_FIELD = re.compile(r"(_at|_on|deadline|date|time|timestamp)$", re.IGNORECASE)
EPOCH_RANGE = (1_000_000_000, 4_000_000_000)


# --- finding the records -------------------------------------------------------


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def documents(body: str) -> list[Any]:
    """Every JSON document in a response: the body itself, a browser's
    rendering of it (Chrome shows JSON inside <pre>, HTML-escaped), the
    JSON a page embeds in scripts, and data-* attributes that hold JSON --
    those as one list per attribute name, one item per card."""
    stripped = body.strip()
    if stripped[:1] in "{[":
        whole = _loads(stripped)
        if whole is not None:
            return [whole]
    found: list[Any] = []
    pre = re.fullmatch(
        r"(?is)\s*(?:<html.*?<body[^>]*>)?\s*<pre[^>]*>(.*)</pre>.*", stripped
    )
    if pre:
        whole = _loads(html.unescape(pre.group(1)))
        if whole is not None:
            return [whole]
    for match in re.finditer(
        r"(?is)<script\b([^>]*)>(.*?)</script>", body
    ):
        attributes, content = match.group(1).lower(), match.group(2).strip()
        if "json" in attributes or "__next_data__" in attributes:
            loaded = _loads(content)
            if loaded is not None:
                found.append(loaded)
    by_attribute: dict[str, list[Any]] = {}
    for match in re.finditer(r'(?s)\sdata-([\w-]+)="(\{.*?\})"', body):
        loaded = _loads(html.unescape(match.group(2)))
        if isinstance(loaded, dict):
            by_attribute.setdefault(match.group(1), []).append(loaded)
    found += [items for items in by_attribute.values() if len(items) > 1]
    return found


def _lists(node: Any, path: str = "") -> list[tuple[str, list[dict]]]:
    out: list[tuple[str, list[dict]]] = []
    if isinstance(node, list):
        objects = [item for item in node if isinstance(item, dict)]
        if objects and len(objects) >= len(node) / 2:
            out.append((path, objects))
        for index, item in enumerate(node[:50]):
            out += _lists(item, f"{path}[{index}]")
    elif isinstance(node, dict):
        for key, value in node.items():
            out += _lists(value, f"{path}.{key}" if path else str(key))
    return out


def _leaves(node: Any) -> int:
    if isinstance(node, dict):
        return sum(_leaves(value) for value in node.values()) or 1
    return 1


def _at(node: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def is_data(body: str) -> bool:
    """Whether the response itself is JSON (or a browser's view of JSON),
    rather than an HTML page that may embed some."""
    stripped = body.lstrip()[:2000].lower()
    browser_view = re.match(r"(?:<html.*?<body[^>]*>)?\s*<pre", stripped, re.S)
    return stripped[:1] in "{[" or bool(browser_view)


def records_in(body: str, path: str = "") -> tuple[list[dict], dict, str, int]:
    """(records, the scalars beside them, where they were, score) for one
    response. The records are the list of objects that scores highest on
    items x fields-per-item squared, unless `path` names one ("projects",
    "data.items"). Cells, not items: found 2026-11-01, Kickstarter's discover
    page embeds its 14-entry tag menu (4 fields each) -- more items than the
    12 projects (120 fields each) its JSON has -- and the JSON carries a
    174-entry category aggregation (3 fields each)."""
    best: tuple[list[dict], dict, str, int] = ([], {}, "", 0)
    score = 0
    for document in documents(body):
        if path:
            named = _at(document, path)
            candidates = [(path, named)] if isinstance(named, list) else []
            candidates = [(p, [i for i in v if isinstance(i, dict)]) for p, v in candidates]
        else:
            candidates = _lists(document)
        for where, items in candidates:
            # Leaf fields, as the CSV will have them: a project's creator,
            # category and location are objects, and count by what they hold.
            # Found live: by top-level keys, Kickstarter's 174-entry category
            # aggregation (3 keys) outscored its 12 projects.
            fields = sum(_leaves(item) for item in items[:20]) // max(1, min(len(items), 20))
            # Fields count twice: a listing's records are rich, its facets
            # and menus (id, name, count) are long but thin.
            if len(items) * fields * fields > score and fields >= 2:
                score = len(items) * fields * fields
                meta = {}
                if isinstance(document, dict):
                    meta = {
                        k: v for k, v in document.items()
                        if isinstance(v, (str, int, float, bool)) or v is None
                    }
                best = (items, meta, where, score)
    return best


# --- one record, one row -------------------------------------------------------


def flatten(record: dict, prefix: str = "") -> dict[str, Any]:
    """Nested objects become dotted columns; a list of plain values is one
    "; "-joined cell; a list of objects stays JSON in one cell."""
    row: dict[str, Any] = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            row.update(flatten(value, f"{name}."))
        elif isinstance(value, list):
            if all(not isinstance(item, (dict, list)) for item in value):
                row[name] = "; ".join("" if item is None else str(item) for item in value)
            else:
                row[name] = json.dumps(value, ensure_ascii=False)
        else:
            row[name] = value
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and TIME_FIELD.search(str(key))
            and EPOCH_RANGE[0] <= value <= EPOCH_RANGE[1]
        ):
            row[f"{name}_utc"] = datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d %H:%M")
    return row


def with_url(row: dict[str, Any], base: str) -> dict[str, Any]:
    """The record's own page as a "url" column (absolute), when it names one."""
    for key in URL_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            row["url"] = urllib.parse.urljoin(base, html.unescape(value.strip()))
            break
    return row


def identity(row: dict[str, Any]) -> str:
    for key in ("id", "uuid", "slug", "url"):
        if row.get(key) not in (None, ""):
            return f"{key}:{row[key]}"
    return json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)


def columns(rows: list[dict[str, Any]], wanted: list[str]) -> list[str]:
    if wanted:
        return wanted
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key, None)
    first = [key for key in FIRST if key in seen]
    return first + [key for key in seen if key not in first]


# --- pages ---------------------------------------------------------------------


def page_url(url: str, param: str, number: int, extra: dict[str, str]) -> str:
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    names = {param, *extra}
    query = [(k, v) for k, v in query if k not in names]
    query += list(extra.items()) + [(param, str(number))]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def json_variant(url: str) -> str:
    """The same listing asked for as JSON (Rails and many others answer it)."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if any(k == "format" for k, _ in query):
        return ""
    query.append(("format", "json"))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _start_page(url: str, param: str) -> int:
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get(param)
    try:
        return max(1, int(values[0])) if values else 1
    except ValueError:
        return 1


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "records"


def collect(request: dict, config: dict, *, budget: float = TIME_BUDGET) -> dict:
    """Fetch the listing page by page; the records found, and how it went."""
    url = str(request.get("url") or "").strip()
    param = str(request.get("page_param") or "page")
    path = str(request.get("records") or "")
    pages = max(1, min(int(request.get("max_pages") or DEFAULT_PAGES), MAX_PAGES_CAP))
    start = int(request.get("start_page") or _start_page(url, param))
    extra = {str(k): str(v) for k, v in (request.get("pin") or {}).items()}
    strategies = crawl_site._strategies(request, config)
    deadline = time.monotonic() + budget
    memory_path = Path.cwd() / crawl_site.MEMORY_FILE
    memory = webfetch.load_memory(memory_path)
    result: dict[str, Any] = {
        "url": url, "records": [], "pages": [], "stopped": "", "next": None,
        "source": url, "where": "", "trail": "",
    }
    if config.get("respect_robots", True):
        rules, _ = crawl_site._robots(url, config, {})
        if not rules.can_fetch(config["user_agent"], url):
            result["stopped"] = "robots.txt disallows this URL"
            return result
    source = url
    number = start
    seen: set[str] = set()
    while number < start + pages:
        if time.monotonic() > deadline - 45:
            result["stopped"] = "time budget"
            result["next"] = number
            break
        target = page_url(source, param, number, extra) if number != 1 or extra else source
        page = webfetch.fetch(
            target, config, strategies=strategies, deadline=deadline - 5, memory=memory
        )
        items, meta, where, score = records_in(page.raw, path) if page.raw else ([], {}, "", 0)
        if number == start and source == url and not (page.raw and is_data(page.raw)):
            # An HTML page: its JSON twin, when there is one, usually holds
            # the real records, and what the page embeds is often a menu.
            variant = json_variant(url)
            twin = (
                webfetch.fetch(
                    page_url(variant, param, number, extra) if number != 1 else variant,
                    config, strategies=strategies, deadline=deadline - 5, memory=memory,
                )
                if variant else None
            )
            if twin is not None and twin.raw and is_data(twin.raw):
                found = records_in(twin.raw, path)
                if found[3] > score:
                    source, page, target = variant, twin, twin.url or variant
                    items, meta, where, score = found
                    result["source"] = variant
        result["trail"] = page.trail()
        if not items:
            if any(item.verdict == "missing" for item in page.attempts):
                # Found live: Kickstarter's discover stops at page 200 and
                # answers 404 from there on, though it counts 20,300 projects.
                why = f"the site has no page {number} (HTTP 404) -- the listing ends there"
            elif page.raw:
                why = "no records on this page"
            else:
                why = f"page could not be read ({page.trail()})"
            result["stopped"] = why
            result["pages"].append((number, 0, page.strategy))
            break
        # A randomized order (Kickstarter's "magic" sort) is pinned by the
        # seed the first page answered with; without it pages overlap.
        if "seed" in meta and "seed" not in extra:
            extra["seed"] = str(meta["seed"])
        result["where"] = where
        fresh = 0
        for item in items:
            row = with_url(flatten(item), target)
            key = identity(row)
            if key not in seen:
                seen.add(key)
                result["records"].append(row)
                fresh += 1
        result["pages"].append((number, fresh, page.strategy))
        if fresh == 0:
            result["stopped"] = "the page repeated earlier records"
            break
        if meta.get("has_more") is False or meta.get("hasMore") is False:
            result["stopped"] = "the listing says there is no more"
            break
        number += 1
        delay = float(config.get("delay_seconds", 1.0))
        if delay > 0 and number < start + pages:
            time.sleep(delay)
    else:
        result["stopped"] = f"max_pages ({pages}) reached"
        result["next"] = number
    webfetch.save_memory(memory_path, memory)
    result["extra"] = extra
    return result


# --- the files -----------------------------------------------------------------


def store(result: dict, request: dict) -> tuple[Path, int, int, int]:
    """Add the records to results/<file>.jsonl and rewrite results/<file>.csv
    from all of them. (csv path, rows in it, rows this call added, columns)."""
    name = _slug(str(request.get("file") or urllib.parse.urlsplit(result["url"]).netloc))
    folder = Path.cwd() / RESULTS
    folder.mkdir(parents=True, exist_ok=True)
    store_path = folder / f"{name}.jsonl"
    kept: dict[str, dict] = {}
    if request.get("append", True) and store_path.is_file():
        for line in store_path.read_text(encoding="utf-8").splitlines():
            row = _loads(line)
            if isinstance(row, dict):
                kept.setdefault(identity(row), row)
    before = len(kept)
    for row in result["records"]:
        kept[identity(row)] = row
    rows = list(kept.values())
    store_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows),
        encoding="utf-8",
    )
    target = folder / f"{name}.csv"
    header = columns(rows, [str(c) for c in request.get("fields") or []])
    # utf-8-sig: Excel opens it with the right characters (Šejić, Örebro).
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if row.get(k) is None else row.get(k) for k in header})
    return target, len(rows), len(rows) - before, len(header)


def render(
    result: dict, request: dict, saved: Path | None, total: int, added: int, width: int
) -> str:
    lines: list[str] = []
    pages = result["pages"]
    if saved is None:
        lines.append(f"No records found at {result['url']}: {result['stopped']}.")
        if result["trail"]:
            lines.append(f"Tried: {result['trail']}")
        lines.append(
            "The page shows no list of records as data. Use crawl_site for its text, "
            "or fetch_page with probe: true if it was blocked."
        )
        return "\n".join(lines)
    relative = saved.relative_to(Path.cwd()).as_posix()
    span = f"pages {pages[0][0]}-{pages[-1][0]}" if pages else "no pages"
    lines.append(
        f"Saved {relative}: {total} rows ({added} new from {span}), "
        f"{width} columns."
    )
    lines.append(f"Read from: {result['source']} (records at \"{result['where']}\")")
    lines.append(f"Fetched via: {', '.join(sorted({p[2] for p in pages if p[2]}))}")
    lines.append(f"Stopped: {result['stopped']}.")
    if result["next"] is not None:
        follow = dict(request)
        follow.update({
            "url": result["source"], "start_page": result["next"],
            "file": str(request.get("file") or _slug(urllib.parse.urlsplit(result["url"]).netloc)),
            "append": True,
        })
        if result.get("extra"):
            follow["pin"] = result["extra"]
        lines.append(
            "MORE PAGES LEFT. To continue into the same file, call crawl_records again with: "
            + json.dumps(follow, ensure_ascii=False)
        )
    sample = result["records"][:8]
    if sample:
        lines.append("First rows:")
        for row in sample:
            label = row.get("name") or row.get("title") or row.get("id")
            lines.append(f"- {str(label)[:90]} -- {row.get('url', '')}")
    return "\n".join(lines)[:OUTPUT_BUDGET]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    raw = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    try:
        request = json.loads(raw) if raw.startswith("{") else {"url": raw}
    except json.JSONDecodeError as exc:
        print(f"The request is not valid JSON: {exc}")
        return 0
    if not str(request.get("url") or "").startswith(("http://", "https://")):
        print("give a url starting with http:// or https://")
        return 0
    config = crawl_site.load_config()
    budget = min(TIME_BUDGET, crawl_site.time_budget({"time_budget_seconds": TIME_BUDGET}))
    result = collect(request, config, budget=budget)
    saved, total, added, width = (None, 0, 0, 0)
    if result["records"]:
        saved, total, added, width = store(result, request)
    print(render(result, request, saved, total, added, width))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
