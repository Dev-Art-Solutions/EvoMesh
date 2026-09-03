# Provisions Scrapling (https://github.com/D4Vinci/Scrapling) in its own venv,
# never in .venv. Scrapling's fetchers extra alone pulls in a dozen packages --
# a browser automation stack among them -- and CLAUDE.md rule 16 keeps this
# project's own runtime dependencies at five. The Web.Fetch skill shells out to
# whatever this script builds; it never imports scrapling directly.
[CmdletBinding()]
param(
    [string]$Root,
    # Pulls down Chromium via Playwright -- hundreds of MB, one time -- so the
    # fetch tool's dynamic=true (a real browser, for JavaScript-rendered
    # pages) works. Off by default: the static fetcher alone already handles
    # most pages, and nobody should get a download that size without asking
    # for it. Re-run with this switch later; it is additive, not a reinstall.
    [switch]$WithBrowser
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

if ($WithBrowser) {
    & $executable install
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Output ""
Write-Output "Scrapling installed at $executable"
Write-Output "Set in evomesh.yaml:"
Write-Output "  scraping:"
Write-Output "    enabled: true"
Write-Output "    executable: '$($executable.Replace('\', '/'))'"
Write-Output ""
if ($WithBrowser) {
    Write-Output "Browser installed: dynamic=true on the fetch tool works too."
} else {
    Write-Output "This installed the static fetcher only (curl_cffi -- no browser download)."
    Write-Output "The fetch tool's dynamic=true needs a real browser; re-run with -WithBrowser"
    Write-Output "to add it (hundreds of MB, one time), or by hand from this venv:"
    Write-Output "  & '$executable' install"
}
