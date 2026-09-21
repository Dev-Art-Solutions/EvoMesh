"""Extract text/tabular content from a .docx, .pdf, .xlsx/.xlsm, or .csv file.

argv[1] = a JSON object: {"path": "...", "sheet": "...", "pages": "1-3",
"max_chars": 4000, "max_rows": 200}. Only "path" is required; the format is
picked from its extension. Prints one JSON object to stdout and exits 0, or
prints {"error": "..."} to stdout and exits 1 -- never a bare traceback,
since the harness feeds stdout straight back to a model that cannot inspect
a stack trace.

Runs under the isolated venv scripts/install-docs-env.ps1/.sh provisions
(python-docx, openpyxl, pypdf) -- see CLAUDE.md rule 16. Never imported by
the mesh process itself.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

DEFAULT_MAX_CHARS = 4000
DEFAULT_MAX_ROWS = 200


def _parse_pages(spec: str | None, page_count: int) -> list[int]:
    if not spec:
        return list(range(page_count))
    start_s, _, end_s = spec.partition("-")
    start = max(1, int(start_s)) - 1
    end = int(end_s) if end_s else start + 1
    end = min(end, page_count)
    return list(range(start, end))


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _truncate_rows(rows: list, max_rows: int) -> tuple[list, bool]:
    if len(rows) <= max_rows:
        return rows, False
    return rows[:max_rows], True


def _read_docx(path: Path, max_chars: int, max_rows: int) -> dict:
    from docx import Document

    document = Document(str(path))
    paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
    text = "\n".join(paragraphs)
    text, text_truncated = _truncate_text(text, max_chars)

    tables = []
    rows_truncated = False
    for table in document.tables:
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        rows, this_truncated = _truncate_rows(rows, max_rows)
        rows_truncated = rows_truncated or this_truncated
        tables.append({"rows": rows})

    return {
        "type": "docx",
        "text": text,
        "tables": tables,
        "truncated": text_truncated or rows_truncated,
    }


def _read_pdf(path: Path, pages_spec: str | None, max_chars: int) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    page_count = len(reader.pages)
    indices = _parse_pages(pages_spec, page_count)

    parts = []
    for i in indices:
        parts.append(reader.pages[i].extract_text() or "")
    text = "\n".join(parts)
    text, truncated = _truncate_text(text, max_chars)

    return {
        "type": "pdf",
        "page_count": page_count,
        "pages_read": [i + 1 for i in indices],
        "text": text,
        "truncated": truncated,
    }


def _read_xlsx(path: Path, sheet: str | None, max_rows: int) -> dict:
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    sheet_names = workbook.sheetnames
    worksheet = workbook[sheet] if sheet else workbook[sheet_names[0]]

    rows = [list(row) for row in worksheet.iter_rows(values_only=True)]
    rows = [[("" if v is None else v) for v in row] for row in rows]
    rows, truncated = _truncate_rows(rows, max_rows)

    headers = rows[0] if rows else []
    body = rows[1:] if rows else []

    return {
        "type": "xlsx",
        "sheet_names": sheet_names,
        "sheet": worksheet.title,
        "headers": headers,
        "rows": body,
        "truncated": truncated,
    }


def _read_csv(path: Path, max_rows: int) -> dict:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        rows = list(reader)
    rows, truncated = _truncate_rows(rows, max_rows)
    headers = rows[0] if rows else []
    body = rows[1:] if rows else []
    return {
        "type": "csv",
        "headers": headers,
        "rows": body,
        "truncated": truncated,
    }


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps({"error": "usage: document_read.py '<json request>'"}))
        return 2

    try:
        request = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid JSON request: {exc}"}))
        return 1

    path_str = request.get("path")
    if not path_str:
        print(json.dumps({"error": "request must include \"path\""}))
        return 1

    path = Path(path_str)
    if not path.is_file():
        print(json.dumps({"error": f"no such file: {path}"}))
        return 1

    max_chars = int(request.get("max_chars") or DEFAULT_MAX_CHARS)
    max_rows = int(request.get("max_rows") or DEFAULT_MAX_ROWS)
    suffix = path.suffix.lower()

    try:
        if suffix == ".docx":
            result = _read_docx(path, max_chars, max_rows)
        elif suffix == ".pdf":
            result = _read_pdf(path, request.get("pages"), max_chars)
        elif suffix in (".xlsx", ".xlsm"):
            result = _read_xlsx(path, request.get("sheet"), max_rows)
        elif suffix == ".csv":
            result = _read_csv(path, max_rows)
        else:
            print(json.dumps({"error": f"unsupported extension: {suffix}"}))
            return 1
    except Exception as exc:  # noqa: BLE001 - reported to the model, not raised
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1

    result["path"] = str(path)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
