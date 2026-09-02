# Runs EvoMesh headless and brings it back up when it asks to be restarted.
#
# Exit code 86 is the mesh saying "start me again, I have new code to run" after
# a generation landed in the tree. This is the same contract the Control Center
# and start-evomesh-console.bat honour; this script is the headless equivalent,
# for running the mesh without a window.
[CmdletBinding()]
param(
    [string] $Root,
    [int]    $ControlPort = 8765
)

$ErrorActionPreference = 'Stop'

# Resolved here, not as a parameter default: Windows PowerShell 5.1 evaluates
# param() defaults before $PSScriptRoot is populated, so the default silently
# came out empty and Split-Path failed before the script logged anything.
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }

# `uv run` finds the project in the working directory, not from the paths it is
# handed, so a script launched from anywhere else -- a shortcut, a scheduled
# task, an admin shell sitting in system32 -- got "program not found: evomesh"
# and a supervisor that gave up on the spot. start-evomesh.bat has always done
# the same thing with `cd /d "%~dp0"`; this was the one launcher that resolved a
# root and then never stood in it.
Set-Location -LiteralPath $Root

$RestartExitCode = 86

$env:UV_CACHE_DIR = Join-Path $Root '.runtime\uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Root '.runtime\python'

$uv = 'uv'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $bundled = Join-Path (Split-Path -Parent $Root) '.tools\uv\bin\uv.exe'
    if (-not (Test-Path $bundled)) { throw "uv was not found, and $bundled does not exist." }
    $uv = $bundled
}

$config = Join-Path $Root 'evomesh.yaml'
$meshLog = Join-Path $Root '.runtime\logs\mesh.log'
$supervisorLog = Join-Path $Root '.runtime\logs\supervisor.log'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $meshLog) | Out-Null

function Write-Log([string] $Message) {
    $line = "$(Get-Date -Format o) $Message"
    Add-Content -Path $supervisorLog -Value $line -Encoding utf8
    Write-Output $line
}

while ($true) {
    Write-Log '[supervisor] starting EvoMesh'
    & $uv run --locked --no-dev evomesh `
        --config $config `
        --headless `
        --control-host 127.0.0.1 `
        --control-port $ControlPort `
        --log-file $meshLog
    $code = $LASTEXITCODE

    if ($code -ne $RestartExitCode) {
        Write-Log "[supervisor] EvoMesh exited with code $code; not restarting"
        break
    }

    Write-Log '[supervisor] a new generation landed; restarting into it'
    # The new code may need dependencies the old one did not have, and the old
    # process needs a moment to release the control port.
    & $uv sync --locked --no-dev
    Start-Sleep -Seconds 2
}
