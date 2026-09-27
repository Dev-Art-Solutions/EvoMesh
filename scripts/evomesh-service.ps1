# Day-to-day control of the EvoMesh Windows service (see install-services.ps1).
# Needs no elevation once the services are installed: the installer grants the
# installing account start/stop rights on both.
#
#   evomesh-service status    services, hold flag, /ping, last supervisor lines
#   evomesh-service stop      graceful /exit, then keep it stopped -- through a
#                             reboot too -- until `start` (for development)
#   evomesh-service start     clear the hold and start (Ollama first)
#   evomesh-service restart   /restart through the control port, or restart the service
#   evomesh-service logs      follow mesh.log
#
# `-Ollama` on stop also stops EvoMesh-Ollama; by default it keeps running,
# since a mesh run from source during development still needs it.
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('status', 'start', 'stop', 'restart', 'logs')]
    [string] $Command = 'status',
    [switch] $Ollama,
    [int]    $ControlPort = 8765
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$holdFile = Join-Path $Root '.runtime\service.hold'
$logDir = Join-Path $Root '.runtime\logs'

function Send-MeshCommand([string] $Text, [int] $TimeoutMs = 10000) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connect = $client.BeginConnect('127.0.0.1', $ControlPort, $null, $null)
        if (-not $connect.AsyncWaitHandle.WaitOne(3000)) { return $null }
        $client.EndConnect($connect)
        $stream = $client.GetStream()
        $stream.ReadTimeout = $TimeoutMs
        $request = [Text.Encoding]::UTF8.GetBytes((@{ command = $Text } | ConvertTo-Json -Compress) + "`n")
        $stream.Write($request, 0, $request.Length)
        $line = (New-Object System.IO.StreamReader($stream)).ReadLine()
        if ($line) { return $line | ConvertFrom-Json }
        return $null
    } catch {
        return $null
    } finally {
        $client.Close()
    }
}

function Get-Svc([string] $Name) {
    Get-Service -Name $Name -ErrorAction SilentlyContinue
}

function Wait-Stopped([string] $Name, [int] $Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $svc = Get-Svc $Name
        if (-not $svc -or $svc.Status -eq 'Stopped') { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

switch ($Command) {
    'status' {
        foreach ($name in @('EvoMesh', 'EvoMesh-Ollama')) {
            $svc = Get-Svc $name
            if ($svc) {
                Write-Output ("{0,-16} {1,-8} (start: {2})" -f $name, $svc.Status, $svc.StartType)
            } else {
                Write-Output ("{0,-16} not installed -- run scripts\install-services.ps1 elevated" -f $name)
            }
        }
        if (Test-Path $holdFile) {
            Write-Output "hold             ON since $((Get-Item $holdFile).LastWriteTime) -- stays stopped until 'start'"
        } else {
            Write-Output 'hold             off'
        }
        $ping = Send-MeshCommand '/ping'
        if ($ping) {
            $stuck = @($ping.stuck) -join ', '
            Write-Output ("control port     answering on {0}{1}" -f $ControlPort, $(if ($stuck) { " (stuck: $stuck)" } else { '' }))
        } else {
            Write-Output "control port     no answer on $ControlPort"
        }
        $supervisorLog = Join-Path $logDir 'supervisor.log'
        if (Test-Path $supervisorLog) {
            Write-Output ''
            Get-Content $supervisorLog -Tail 5 -Encoding utf8
        }
    }

    'stop' {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $holdFile) | Out-Null
        Set-Content -Path $holdFile -Value "held by $env:USERNAME at $(Get-Date -Format o)" -Encoding utf8
        # /exit first: the mesh shuts its agents down cleanly and exits 0, the
        # supervisor sees the hold and ends, NSSM treats exit 0 as a stop.
        if (Send-MeshCommand '/exit') {
            Write-Output '[evomesh-service] sent /exit'
        }
        $svc = Get-Svc 'EvoMesh'
        if ($svc -and -not (Wait-Stopped 'EvoMesh' 60)) {
            Write-Output '[evomesh-service] still running after 60s; stopping the service'
            Stop-Service -Name 'EvoMesh' -Force
        }
        if ($Ollama -and (Get-Svc 'EvoMesh-Ollama')) {
            Stop-Service -Name 'EvoMesh-Ollama' -Force
            Write-Output '[evomesh-service] stopped EvoMesh-Ollama'
        }
        Write-Output '[evomesh-service] EvoMesh stopped and held (also across reboots). Resume with: evomesh-service start'
    }

    'start' {
        Remove-Item -Path $holdFile -Force -ErrorAction SilentlyContinue
        if (-not (Get-Svc 'EvoMesh')) {
            throw 'The EvoMesh service is not installed. Run scripts\install-services.ps1 from an elevated PowerShell.'
        }
        if (Send-MeshCommand '/ping') {
            if ((Get-Svc 'EvoMesh').Status -ne 'Running') {
                throw "Something else already answers on port $ControlPort (a console or dev mesh?). Stop it first -- two meshes cannot share the port."
            }
            Write-Output '[evomesh-service] already running'
            return
        }
        if ((Get-Svc 'EvoMesh-Ollama') -and (Get-Svc 'EvoMesh-Ollama').Status -ne 'Running') {
            Start-Service -Name 'EvoMesh-Ollama'
        }
        Start-Service -Name 'EvoMesh'
        Write-Output '[evomesh-service] EvoMesh started (hold cleared)'
    }

    'restart' {
        if (Send-MeshCommand '/restart') {
            Write-Output '[evomesh-service] sent /restart; the supervisor brings it back in ~15s'
        } elseif (Get-Svc 'EvoMesh') {
            Remove-Item -Path $holdFile -Force -ErrorAction SilentlyContinue
            Restart-Service -Name 'EvoMesh' -Force
            Write-Output '[evomesh-service] restarted the EvoMesh service'
        } else {
            throw 'Nothing answers on the control port and the service is not installed.'
        }
    }

    'logs' {
        Get-Content (Join-Path $logDir 'mesh.log') -Tail 50 -Wait -Encoding utf8
    }
}
