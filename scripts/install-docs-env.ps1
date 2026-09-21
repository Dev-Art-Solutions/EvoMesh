# Provisions python-docx, openpyxl, pypdf and reportlab in their own venv,
# never in .venv. CLAUDE.md rule 16 keeps this project's own runtime
# dependencies at five (aiosqlite, httpx, pydantic, pydantic-settings,
# pyyaml); document parsing/generation is exactly the kind of extra weight
# that stays out. tools/document_read and tools/document_write shell out to
# whatever this script builds; neither is ever imported into the mesh
# process itself -- same relationship this project already has with
# Scrapling (install-scrapling.ps1) and MetaEditor.
[CmdletBinding()]
param(
    [string]$Root
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }

$venvPath = Join-Path $Root ".runtime\docs"
$uv = "uv"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $bundled = Join-Path (Split-Path -Parent $Root) ".tools\uv\bin\uv.exe"
    if (-not (Test-Path $bundled)) { throw "uv was not found, and $bundled does not exist." }
    $uv = $bundled
}

& $uv venv $venvPath --python 3.12
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$venvPython = Join-Path $venvPath "Scripts\python.exe"
& $uv pip install --python $venvPython "python-docx>=1.1" "openpyxl>=3.1" "pypdf>=5.0" "reportlab>=4.2"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Output ""
Write-Output "Document environment installed at $venvPython"
Write-Output "tools/document_read/TOOL.md and tools/document_write/TOOL.md already point at it."
Write-Output "If this checkout lives somewhere other than 'D:/Projects/Dev-art solutions/EvoMesh',"
Write-Output "edit the 'command:' line in both TOOL.md files to match $($venvPython.Replace('\', '/'))"
