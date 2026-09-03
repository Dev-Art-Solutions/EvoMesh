# Provisions Scrapling (https://github.com/D4Vinci/Scrapling) in its own venv,
# never in .venv. Scrapling's fetchers extra alone pulls in a dozen packages --
# a browser automation stack among them -- and CLAUDE.md rule 16 keeps this
# project's own runtime dependencies at five. The Web.Fetch skill shells out to
# whatever this script builds; it never imports scrapling directly.
[CmdletBinding()]
param(
    [string]$Root
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }

$venvPath = Join-Path $Root ".runtime\scrapling"
$uv = "uv"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $bundled = Join-Path (Split-Path -Parent $Root) ".tools\uv\bin\uv.exe"
    if (-not (Test-Path $bundled)) { throw "uv was not found, and $bundled does not exist." }
    $uv = $bundled
}

& $uv venv $venvPath --python 3.12
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$venvPython = Join-Path $venvPath "Scripts\python.exe"
& $uv pip install --python $venvPython "scrapling[rag]"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$executable = Join-Path $venvPath "Scripts\scrapling.exe"
Write-Output ""
Write-Output "Scrapling installed at $executable"
Write-Output "Set in evomesh.yaml:"
Write-Output "  scraping:"
Write-Output "    enabled: true"
Write-Output "    executable: '$($executable.Replace('\', '/'))'"
Write-Output ""
Write-Output "This installs the static fetcher only (curl_cffi -- no browser download)."
Write-Output "Web.Fetch works with that alone. For JS-rendered pages, a real browser"
Write-Output "is needed too; run once more, from this venv:"
Write-Output "  & '$executable' install"
