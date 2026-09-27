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
# Both run as YOUR account (asked for its password once), not LocalSystem:
# they start at boot with nobody logged in, yet still see your Ollama models,
# git credentials, uv and user environment. LocalSystem has none of those.
#
# Once installed, day-to-day control needs no elevation -- this grants your
# account start/stop rights on both services:
#   evomesh-service status | stop | start | restart | logs
# `stop` holds the mesh stopped (reboots included) until `start`, for
# development; see scripts\evomesh-service.ps1.
#
# Run this elevated (Administrator). It is not auto-elevated on purpose --
# installing a service is exactly the kind of action a human should trigger on
# purpose, not have happen as a side effect.
[CmdletBinding()]
param(
    [string] $OllamaExe,
    [pscredential] $Credential
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

if (-not $Credential) {
    $Credential = Get-Credential -UserName "$env:COMPUTERNAME\$env:USERNAME" `
        -Message 'EvoMesh runs as this account at boot, logged in or not. Its Windows password (for a Microsoft account, that account''s password -- not the PIN).'
}
if (-not $Credential) { throw 'No credential given.' }
$account = $Credential.UserName -replace '^\.\\', "$env:COMPUTERNAME\"
if ($account -notmatch '\\') { $account = "$env:COMPUTERNAME\$account" }
$password = $Credential.GetNetworkCredential().Password
Add-Type -AssemblyName System.DirectoryServices.AccountManagement
$context = New-Object System.DirectoryServices.AccountManagement.PrincipalContext('Machine')
if (-not $context.ValidateCredentials(($account -split '\\')[-1], $password)) {
    throw "Windows rejected the password for $account. A service with a wrong password fails at every boot, so nothing was installed."
}
$accountSid = (New-Object Security.Principal.NTAccount($account)).Translate([Security.Principal.SecurityIdentifier]).Value

# One mesh per control port: a console/Control Center mesh still running would
# win the port and leave the service crash-looping behind it.
$client = New-Object System.Net.Sockets.TcpClient
try {
    $client.Connect('127.0.0.1', 8765)
    $request = [Text.Encoding]::UTF8.GetBytes("{`"command`": `"/exit`"}`n")
    $client.GetStream().Write($request, 0, $request.Length)
    Write-Output '[install-services] asked the running mesh to /exit'
    Start-Sleep -Seconds 10
} catch {
} finally {
    $client.Close()
}

# The Ollama tray app starts its own `ollama serve` at login, which would
# collide with the service on port 11434. Park its Startup shortcut (restored
# by uninstall-services.ps1) and stop the one running now.
$startupLink = Join-Path ([Environment]::GetFolderPath('Startup')) 'Ollama.lnk'
if (Test-Path $startupLink) {
    Move-Item $startupLink (Join-Path $Root '.runtime\Ollama.lnk.disabled') -Force
    Write-Output '[install-services] disabled the Ollama tray app at login (the service replaces it)'
}
Get-Process -Name 'ollama app', 'ollama' -ErrorAction SilentlyContinue | Stop-Process -Force

# Ollama is started directly, not through run-supervised.ps1, so its user-level
# settings (OLLAMA_MODELS, OLLAMA_CONTEXT_LENGTH, ...) are handed over here.
$ollamaEnv = @()
$userEnv = [Environment]::GetEnvironmentVariables('User')
foreach ($name in $userEnv.Keys) {
    if ($name -like 'OLLAMA_*') { $ollamaEnv += "$name=$($userEnv[$name])" }
}

function Grant-ControlRights([string] $Name) {
    # Start (RP), stop (WP), pause (DT), query (LC/LO/RC), user controls (CR):
    # evomesh-service.ps1 works from a normal, non-elevated shell afterwards.
    $sddl = ((& sc.exe sdshow $Name) -join '').Trim()
    $ace = "(A;;RPWPDTLCLOCRRC;;;$accountSid)"
    if ($sddl.Contains($ace)) { return }
    $sddl = if ($sddl.Contains('S:')) { $sddl.Replace('S:', "${ace}S:") } else { $sddl + $ace }
    & sc.exe sdset $Name $sddl | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "sc.exe sdset $Name failed ($LASTEXITCODE)" }
}

function Install-OneService {
    param(
        [string] $Name,
        [string] $Exe,
        [string] $Arguments,
        [string] $WorkDir,
        [string] $StdoutLog,
        [string] $StderrLog,
        [string[]] $Environment = @()
    )
    $existing = & $Nssm status $Name 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Output "[install-services] $Name already exists (status: $existing) -- stopping and removing it first"
        & $Nssm stop $Name 2>$null | Out-Null
        & $Nssm remove $Name confirm | Out-Null
    }
    & $Nssm install $Name $Exe $Arguments | Out-Null
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
    # Ctrl+C first and give the mesh time to shut its agents down before NSSM
    # escalates to killing the process tree.
    & $Nssm set $Name AppStopMethodConsole 20000 | Out-Null
    # NSSM also grants the account "Log on as a service".
    & $Nssm set $Name ObjectName $account $password | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "nssm could not set $Name to run as $account" }
    if ($Environment.Count -gt 0) {
        & $Nssm set $Name AppEnvironmentExtra @Environment | Out-Null
    }
    Grant-ControlRights $Name
    Write-Output "[install-services] installed $Name (runs as $account)"
}

Install-OneService -Name 'EvoMesh-Ollama' -Exe $OllamaExe -Arguments 'serve' -WorkDir (Split-Path -Parent $OllamaExe) `
    -StdoutLog (Join-Path $logDir 'ollama-service.out.log') -StderrLog (Join-Path $logDir 'ollama-service.err.log') `
    -Environment $ollamaEnv

$psExe = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$superviseScript = Join-Path $Root 'scripts\run-supervised.ps1'
Install-OneService -Name 'EvoMesh' -Exe $psExe `
    -Arguments "-NoProfile -ExecutionPolicy Bypass -File `"$superviseScript`" -Service" -WorkDir $Root `
    -StdoutLog (Join-Path $logDir 'evomesh-service.out.log') -StderrLog (Join-Path $logDir 'evomesh-service.err.log')

& $Nssm set EvoMesh DependOnService EvoMesh-Ollama | Out-Null

# Installing means "run it": a hold left over from an earlier `stop` would
# otherwise make the fresh service end the moment it starts.
Remove-Item -Path (Join-Path $Root '.runtime\service.hold') -Force -ErrorAction SilentlyContinue

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
Write-Output "A hung mesh (alive, but not answering /ping for ~5 min) is killed and restarted"
Write-Output "by run-supervised.ps1's watchdog."
Write-Output ""
Write-Output "Day-to-day, from a normal (non-admin) shell in the EvoMesh folder:"
Write-Output "  .\evomesh-service status"
Write-Output "  .\evomesh-service stop      # for development; stays stopped across reboots"
Write-Output "  .\evomesh-service start"
Write-Output "  .\evomesh-service restart"
Write-Output "  .\evomesh-service logs"
Write-Output "  .\scripts\uninstall-services.ps1   (elevated)"
Write-Output ""
Write-Output "Also run once, so the machine does not sleep out from under a service that never"
Write-Output "asked to be woken up:"
Write-Output "  powercfg /change standby-timeout-ac 0"
Write-Output "  powercfg /change monitor-timeout-ac 0   # optional, screen only"
