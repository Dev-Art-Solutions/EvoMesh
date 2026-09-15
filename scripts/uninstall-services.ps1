# Removes the EvoMesh and EvoMesh-Ollama Windows services installed by
# install-services.ps1. Run elevated (Administrator).
$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run this from an elevated PowerShell (Administrator)."
}

$Root = Split-Path -Parent $PSScriptRoot
$Nssm = Join-Path $Root '.runtime\nssm.exe'

foreach ($name in @('EvoMesh', 'EvoMesh-Ollama')) {
    & $Nssm status $name 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Output "[uninstall-services] stopping and removing $name"
        & $Nssm stop $name 2>$null | Out-Null
        & $Nssm remove $name confirm | Out-Null
    } else {
        Write-Output "[uninstall-services] $name is not installed"
    }
}
