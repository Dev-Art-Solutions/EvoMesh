---
name: document_write
description: Generate a document file -- .docx, .pdf, .xlsx, or .csv -- from a JSON description (title, paragraphs, a table, or xlsx sheets). Returns JSON. Needs the doc-processing venv from scripts/install-docs-env.ps1/.sh (see tools/document_write/README.md); if that has not been run yet, this tool fails with a clear "python-docx/reportlab/openpyxl not found" error instead of silently doing nothing.
command: '"D:/Projects/Dev-art solutions/EvoMesh/.runtime/docs/Scripts/python.exe" "{tool_dir}/scripts/document_write.py"'
parameters:
  - name: request
    description: >
      JSON object, e.g. {"path": "reports/q1.docx", "title": "Q1 Report",
      "paragraphs": ["Revenue was up 4%."], "headers": ["Metric", "Value"],
      "rows": [["Revenue", "1.2M"]]}. Required: path (its extension picks
      the format; relative to the calling agent's own playground, or
      absolute -- parent directories are created as needed). Optional:
      "title" and "paragraphs" (docx/pdf only; ignored for xlsx/csv),
      "headers" + "rows" (one table, any format), "sheets" (xlsx only, e.g.
      {"Summary": {"headers": [...], "rows": [[...]]}, "Detail": {...}} --
      overrides headers/rows when both are given).
    required: true
---

Overwrites `path` if it already exists. Always returns JSON:
`{"path", "type", "bytes_written"}` on success, or `{"error": "..."}` on
any failure (bad extension, malformed request), never a bare traceback.

Pairs with `document_read` for the other direction. Both shell out to a
venv under `.runtime/docs/`, never the project's own `.venv` -- see
CLAUDE.md rule 16 and `scripts/install-docs-env.ps1`/`.sh`. `python` must
also be in `harness.shell_allow` in `evomesh.yaml` (it already is, for
`check-site`/`news_fetch`/etc.).
