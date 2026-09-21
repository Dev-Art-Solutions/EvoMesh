#!/usr/bin/env sh
# Provisions python-docx, openpyxl, pypdf and reportlab in their own venv,
# never in .venv. CLAUDE.md rule 16 keeps this project's own runtime
# dependencies at five (aiosqlite, httpx, pydantic, pydantic-settings,
# pyyaml); document parsing/generation is exactly the kind of extra weight
# that stays out. tools/document_read and tools/document_write shell out to
# whatever this script builds; neither is ever imported into the mesh
# process itself -- same relationship this project already has with
# Scrapling (install-scrapling.sh) and MetaEditor.
set -eu

root="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
venv_path="$root/.runtime/docs"

uv venv "$venv_path" --python 3.12
uv pip install --python "$venv_path/bin/python" "python-docx>=1.1" "openpyxl>=3.1" "pypdf>=5.0" "reportlab>=4.2"

venv_python="$venv_path/bin/python"

echo ""
echo "Document environment installed at $venv_python"
echo "tools/document_read/TOOL.md and tools/document_write/TOOL.md already point at it."
echo "If this checkout lives somewhere other than 'D:/Projects/Dev-art solutions/EvoMesh',"
echo "edit the 'command:' line in both TOOL.md files to match $venv_python"
