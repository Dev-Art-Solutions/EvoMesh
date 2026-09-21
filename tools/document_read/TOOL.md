---
name: document_read
description: Read and extract text or tabular content from a document file -- .docx, .pdf, .xlsx/.xlsm, or .csv. Returns JSON. Needs the doc-processing venv from scripts/install-docs-env.ps1/.sh (see tools/document_read/README.md); if that has not been run yet, this tool fails with a clear "python-docx/pypdf/openpyxl not found" error instead of silently doing nothing.
command: '"D:/Projects/Dev-art solutions/EvoMesh/.runtime/docs/Scripts/python.exe" "{tool_dir}/scripts/document_read.py"'
parameters:
  - name: request
    description: >
      JSON object, e.g. {"path": "reports/q1.xlsx", "sheet": "Summary",
      "max_rows": 200}. Required: path (relative to the calling agent's own
      playground, or absolute). Optional: "sheet" (xlsx, name; defaults to
      the first sheet), "pages" (pdf, e.g. "1-3"; defaults to all pages),
      "max_chars" (docx/pdf text, default 4000), "max_rows" (xlsx/csv/docx
      tables, default 200) -- output past either limit is truncated and
      "truncated": true is set, rather than silently dropped.
    required: true
---

Read-only. Always returns JSON: `{"path", "type", ...}` plus format-specific
fields -- `text`/`tables` for docx, `page_count`/`pages_read`/`text` for
pdf, `sheet_names`/`sheet`/`headers`/`rows` for xlsx, `headers`/`rows` for
csv -- or `{"error": "..."}` on any failure (missing file, unsupported
extension, corrupt document), never a bare traceback.

Pairs with `document_write` for the other direction. Both shell out to a
venv under `.runtime/docs/`, never the project's own `.venv` -- see
CLAUDE.md rule 16 and `scripts/install-docs-env.ps1`/`.sh`. `python` must
also be in `harness.shell_allow` in `evomesh.yaml` (it already is, for
`check-site`/`news_fetch`/etc.).
