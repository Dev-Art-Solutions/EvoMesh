"""Generate a .docx, .pdf, .xlsx, or .csv file from a JSON description.

argv[1] = a JSON object:
{
  "path": "report.docx",                 required; the format is picked
                                          from this extension
  "title": "Quarterly Report",           optional heading (docx/pdf; the
                                          xlsx/csv writers have no header
                                          concept and ignore it)
  "paragraphs": ["First para", "..."],   optional body text (docx/pdf)
  "headers": ["Col A", "Col B"],         optional single table (any format)
  "rows": [["1", "2"], ["3", "4"]],
  "sheets": {                            xlsx only; overrides headers/rows
    "Summary": {"headers": [...], "rows": [[...]]},
    "Detail":  {"headers": [...], "rows": [[...]]}
  }
}

Prints {"path": ..., "type": ..., "bytes_written": N} to stdout and exits 0,
or {"error": "..."} and exits 1 -- never a bare traceback, since the harness
feeds stdout straight back to a model that cannot inspect a stack trace.

Runs under the isolated venv scripts/install-docs-env.ps1/.sh provisions
(python-docx, openpyxl, reportlab) -- see CLAUDE.md rule 16. Never imported
by the mesh process itself.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


def _write_docx(path: Path, request: dict) -> None:
    from docx import Document

    document = Document()
    title = request.get("title")
    if title:
        document.add_heading(str(title), level=1)
    for paragraph in request.get("paragraphs") or []:
        document.add_paragraph(str(paragraph))

    headers = request.get("headers")
    rows = request.get("rows") or []
    if headers:
        table = document.add_table(rows=1, cols=len(headers))
        table.style = "Light Grid Accent 1"
        for cell, value in zip(table.rows[0].cells, headers, strict=True):
            cell.text = str(value)
        for row in rows:
            cells = table.add_row().cells
            for cell, value in zip(cells, row, strict=True):
                cell.text = str(value)

    document.save(str(path))


def _write_pdf(path: Path, request: dict) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    story = []

    title = request.get("title")
    if title:
        story.append(Paragraph(str(title), styles["Title"]))
        story.append(Spacer(1, 12))

    for paragraph in request.get("paragraphs") or []:
        story.append(Paragraph(str(paragraph), styles["BodyText"]))
        story.append(Spacer(1, 6))

    headers = request.get("headers")
    rows = request.get("rows") or []
    if headers:
        data = [[str(h) for h in headers]] + [[str(v) for v in row] for row in rows]
        table = Table(data)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbe5f1")),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ]
            )
        )
        story.append(table)

    SimpleDocTemplate(str(path), pagesize=letter).build(story)


def _write_xlsx(path: Path, request: dict) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)

    sheets = request.get("sheets")
    if not sheets:
        sheets = {"Sheet1": {"headers": request.get("headers"), "rows": request.get("rows") or []}}

    for name, spec in sheets.items():
        worksheet = workbook.create_sheet(title=str(name)[:31])
        headers = spec.get("headers")
        if headers:
            worksheet.append([str(h) for h in headers])
        for row in spec.get("rows") or []:
            worksheet.append(list(row))

    workbook.save(str(path))


def _write_csv(path: Path, request: dict) -> None:
    headers = request.get("headers")
    rows = request.get("rows") or []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if headers:
            writer.writerow(headers)
        writer.writerows(rows)


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps({"error": "usage: document_write.py '<json request>'"}))
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
    suffix = path.suffix.lower()
    if suffix not in (".docx", ".pdf", ".xlsx", ".csv"):
        print(json.dumps({"error": f"unsupported extension: {suffix}"}))
        return 1

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if suffix == ".docx":
            _write_docx(path, request)
        elif suffix == ".pdf":
            _write_pdf(path, request)
        elif suffix == ".xlsx":
            _write_xlsx(path, request)
        else:
            _write_csv(path, request)
    except Exception as exc:  # noqa: BLE001 - reported to the model, not raised
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1

    print(
        json.dumps(
            {"path": str(path), "type": suffix.lstrip("."), "bytes_written": path.stat().st_size}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
