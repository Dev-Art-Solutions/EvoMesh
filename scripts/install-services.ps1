# Installs EvoMesh (and the Ollama it depends on) as real Windows services via
# NSSM, so both survive logoff, an closed console window, and a crash -- not
# just a landed generation asking for exit code 86.
#
# Two services, in a dependency chain:
#   EvoMesh-Ollama -- `ollama.exe serve`, because the Windows Ollama installer
#                      only starts it from the user's Startup folder, which
#                      never fires for a service running with nobody logged in.
#   EvoMesh        -- scripts\run-supervised.ps1, which already loops on exit
#                      code 86 (a generation landed) -- see that script's own
#                      header. NSSM adds the second layer: if the whole
#                      supervisor process dies (a crash, not a clean /exit),
#                      the service itself restarts, which the previous
#                      console/Control Center launch path had nobody doing
#                      once the window or session was gone.
#
# Run this elevated (Administrator). It is not auto-elevated on purpose --
# installing a service is exactly the kind of action a human should trigger on
# purpose, not have happen as a side effect.
[CmdletBinding()]
param(
    [string] $OllamaExe
)

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run this from an elevated PowerShell (Administrator). Installing a service needs it."
}

$Root = Split-Path -Parent $PSScriptRoot
$Nssm = Join-Path $Root '.runtime\nssm.exe'
if (-not (Test-Path $Nssm)) {
    Write-Output "[install-services] nssm.exe not found, downloading nssm 2.24"
    New-Item -ItemType Directory -Force -Path (Join-Path $Root '.runtime') | Out-Null
    $zipPath = Join-Path $Root '.runtime\nssm.zip'
    Invoke-WebRequest -Uri 'https://nssm.cc/release/nssm-2.24.zip' -OutFile $zipPath
    $extractDir = Join-Path $Root '.runtime\nssm-extract'
    Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force
    Copy-Item (Join-Path $extractDir 'nssm-2.24\win64\nssm.exe') $Nssm -Force
    Remove-Item $zipPath, $extractDir -Recurse -Force
}

if (-not $OllamaExe) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
        (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')
    )
    $OllamaExe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $OllamaExe -or -not (Test-Path $OllamaExe)) {
    throw "Could not find ollama.exe. Pass it explicitly: -OllamaExe 'C:\path\to\ollama.exe'"
}

$logDir = Join-Path $Root '.runtime\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Install-OneService {
    param(
        [string] $Name,
        [string] $Exe,
        [string] $Args,
        [string] $WorkDir,
        [string] $StdoutLog,
        [string] $StderrLog
    )
    $existing = & $Nssm status $Name 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Output "[install-services] $Name already exists (status: $existing) -- stopping and removing it first"
        & $Nssm stop $Name 2>$null | Out-Null
        & $Nssm remove $Name confirm | Out-Null
    }
    & $Nssm install $Name $Exe $Args | Out-Null
    & $Nssm set $Name AppDirectory $WorkDir | Out-Null
    & $Nssm set $Name AppStdout $StdoutLog | Out-Null
    & $Nssm set $Name AppStderr $StderrLog | Out-Null
    & $Nssm set $Name AppRotateFiles 1 | Out-Null
    & $Nssm set $Name AppRotateBytes 10485760 | Out-Null
    & $Nssm set $Name AppRotateOnline 1 | Out-Null
    & $Nssm set $Name Start SERVICE_AUTO_START | Out-Null
    # Default action is Restart (covers a crash or a hang the process itself
    # never asked to be brought back from); exit code 0 (a deliberate /exit or
    # a clean stop) is left alone rather than fought.
    & $Nssm set $Name AppExit Default Restart | Out-Null
    & $Nssm set $Name AppExit 0 Exit | Out-Null
    & $Nssm set $Name AppRestartDelay 10000 | Out-Null
    Write-Output "[install-services] installed $Name"
}

Install-OneService -Name 'EvoMesh-Ollama' -Exe $OllamaExe -Args 'serve' -WorkDir (Split-Path -Parent $OllamaExe) `
    -StdoutLog (Join-Path $logDir 'ollama-service.out.log') -StderrLog (Join-Path $logDir 'ollama-service.err.log')

$psExe = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$superviseScript = Join-Path $Root 'scripts\run-supervised.ps1'
Install-OneService -Name 'EvoMesh' -Exe $psExe `
    -Args "-NoProfile -ExecutionPolicy Bypass -File `"$superviseScript`"" -WorkDir $Root `
    -StdoutLog (Join-Path $logDir 'evomesh-service.out.log') -StderrLog (Join-Path $logDir 'evomesh-service.err.log')

& $Nssm set EvoMesh DependOnService EvoMesh-Ollama | Out-Null

Write-Output "[install-services] starting EvoMesh-Ollama"
& $Nssm start EvoMesh-Ollama | Out-Null
Start-Sleep -Seconds 3
Write-Output "[install-services] starting EvoMesh"
& $Nssm start EvoMesh | Out-Null

Write-Output ""
Write-Output "Done. Both services are set to start automatically at boot, no login required,"
Write-Output "and NSSM restarts either one 10s after any non-zero exit (a landed generation's"
Write-Output "exit code 86 is still handled first, inside run-supervised.ps1's own loop)."
Write-Output ""
Write-Output "Useful commands:"
Write-Output "  Get-Service EvoMesh, EvoMesh-Ollama"
Write-Output "  Stop-Service EvoMesh; Stop-Service EvoMesh-Ollama"
Write-Output "  .\scripts\uninstall-services.ps1"
Write-Output ""
Write-Output "Also run once, so the machine does not sleep out from under a service that never"
Write-Output "asked to be woken up:"
Write-Output "  powercfg /change standby-timeout-ac 0"
Write-Output "  powercfg /change monitor-timeout-ac 0   # optional, screen only"
