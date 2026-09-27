# Runs EvoMesh headless and brings it back up when it asks to be restarted.
#
# Exit code 86 is the mesh saying "start me again, I have new code to run" after
# a generation landed in the tree. This is the same contract the Control Center
# and start-evomesh-console.bat honour; this script is the headless equivalent,
# for running the mesh without a window.
[CmdletBinding()]
param(
    [string] $Root,
    [int]    $ControlPort = 8765,
    # Set by the EvoMesh Windows service (install-services.ps1). Honours the
    # hold file that `scripts\evomesh-service.ps1 stop` leaves behind, and
    # loads the account's user-level environment a service never inherits.
    [switch] $Service,
    # Hang watchdog: a mesh whose process is alive but whose control port stops
    # answering /ping is killed and restarted like a crash. /ping is answered
    # straight from the event loop, so no answer means the loop is wedged.
    [int]    $PingIntervalSeconds = 60,
    [int]    $MaxMissedPings = 5,
    [int]    $StartupGraceSeconds = 600
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
# Found live: exit code 1 from a stale process still holding the control port
# used to make this loop `break` and log "not restarting" -- silencing the
# whole mesh (and every agent's Telegram bot) until a human noticed the
# window looked idle and relaunched it by hand, once for 5+ hours straight.
# A crash is not a reason to give up forever, only a reason to back off so a
# genuinely broken build doesn't spin the CPU retrying every few milliseconds.
$consecutiveFailures = 0
$maxBackoffSeconds = 300

if ($Service) {
    # A service gets the machine environment only. OLLAMA_*, API keys and the
    # user's PATH (uv among it) live in the account's user scope, which a
    # console launch inherits from Explorer and a service never sees.
    $userEnv = [Environment]::GetEnvironmentVariables('User')
    foreach ($name in $userEnv.Keys) {
        if ($name -eq 'Path') {
            $env:Path = "$env:Path;$($userEnv[$name])"
        } elseif (-not [Environment]::GetEnvironmentVariable($name, 'Process')) {
            [Environment]::SetEnvironmentVariable($name, $userEnv[$name], 'Process')
        }
    }
}

# `scripts\evomesh-service.ps1 stop` writes this so a stopped mesh stays
# stopped -- through a reboot too -- until `start` removes it. Development
# needs the port and the tree to itself; a watchdog fighting that is worse
# than none.
$holdFile = Join-Path $Root '.runtime\service.hold'

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
    # Found live: this whole script ran under $ErrorActionPreference = 'Stop',
    # so a transient Add-Content failure (the log file briefly locked by
    # something reading it) turned into a terminating error that silently
    # killed the entire supervisor loop -- the one thing that exists
    # specifically to never give up. Logging a restart must never be able to
    # prevent one.
    try { Add-Content -Path $supervisorLog -Value $line -Encoding utf8 -ErrorAction Stop } catch {}
    # Host, not the output stream: called from inside Invoke-Mesh, Write-Output
    # would become part of that function's return value -- the exit code.
    Write-Host $line
}

function Test-MeshAlive {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connect = $client.BeginConnect('127.0.0.1', $ControlPort, $null, $null)
        if (-not $connect.AsyncWaitHandle.WaitOne(5000)) { return $false }
        $client.EndConnect($connect)
        $stream = $client.GetStream()
        $stream.ReadTimeout = 20000
        $request = [Text.Encoding]::UTF8.GetBytes("{`"command`": `"/ping`"}`n")
        $stream.Write($request, 0, $request.Length)
        $line = (New-Object System.IO.StreamReader($stream)).ReadLine()
        return [bool]($line -and $line.Contains('"running"'))
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Invoke-Mesh {
    # A Process object rather than `& uv run`, so this loop keeps control while
    # the mesh runs and can ping it. No redirection: output still goes to the
    # console, or to the service's log files under NSSM.
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $uv
    $info.Arguments = "run --locked --no-dev evomesh --config `"$config`" --headless " +
        "--control-host 127.0.0.1 --control-port $ControlPort --log-file `"$meshLog`""
    $info.WorkingDirectory = $Root
    $info.UseShellExecute = $false
    $process = [System.Diagnostics.Process]::Start($info)

    $started = Get-Date
    $everAnswered = $false
    $missed = 0
    while (-not $process.WaitForExit($PingIntervalSeconds * 1000)) {
        if (Test-MeshAlive) {
            $everAnswered = $true
            $missed = 0
            continue
        }
        if (-not $everAnswered -and ((Get-Date) - $started).TotalSeconds -lt $StartupGraceSeconds) {
            continue
        }
        $missed++
        Write-Log "[supervisor] watchdog: no answer to /ping ($missed/$MaxMissedPings)"
        if ($missed -ge $MaxMissedPings) {
            Write-Log "[supervisor] watchdog: EvoMesh is hung; killing process tree $($process.Id)"
            & taskkill.exe /PID $process.Id /T /F | Out-Null
            $process.WaitForExit()
            return -1
        }
    }
    $process.WaitForExit()
    return $process.ExitCode
}

while ($true) {
    if ($Service -and (Test-Path $holdFile)) {
        Write-Log '[supervisor] held for development (.runtime\service.hold); not starting'
        break
    }
    Write-Log '[supervisor] starting EvoMesh'
    $code = Invoke-Mesh

    if ($code -eq $RestartExitCode) {
        Write-Log '[supervisor] a new generation landed; restarting into it'
        $consecutiveFailures = 0
        # The new code may need dependencies the old one did not have, and the
        # old process needs a moment to release the control port.
        & $uv sync --locked --no-dev
        Start-Sleep -Seconds 2
        continue
    }

    if ($code -eq 0) {
        # A clean exit is a stop somebody asked for: /exit from the Control
        # Center's Stop button, the console, Telegram. Rule 13 -- exit code 86
        # is "start me again", deliberately not 0, so a plain /exit is never
        # mistaken for one. Found live 2026-09-24: this loop restarted every
        # one of them ("exited with code 0; restarting in 5s", ten times in
        # supervisor.log), so the Control Center's Stop never stopped anything.
        # A crash is still a non-zero code, and still comes back below.
        Write-Log '[supervisor] EvoMesh stopped on request (exit code 0); not restarting'
        break
    }

    $consecutiveFailures++
    $backoff = [Math]::Min($maxBackoffSeconds, 5 * [Math]::Pow(2, $consecutiveFailures - 1))
    Write-Log "[supervisor] EvoMesh exited with code $code; restarting in ${backoff}s (failure #$consecutiveFailures)"
    Start-Sleep -Seconds $backoff
}
