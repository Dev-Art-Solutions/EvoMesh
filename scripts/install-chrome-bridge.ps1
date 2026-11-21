# Registers scripts/chrome-native-host.bat as a Chrome native-messaging host,
# so browser-extension/background.js's chrome.runtime.connectNative(...) call
# has something to launch. Per-user (HKCU), no elevation needed -- unlike
# install-services.ps1, this changes nothing system-wide.
#
#   .\scripts\install-chrome-bridge.ps1
#
# The native-messaging manifest this writes has to name the extension in
# allowed_origins -- a host with no extension allowed cannot be connected to
# by anything. The extension's manifest carries a fixed public key, so its id
# is the same in every profile and on every machine:
# fgefcelipaiabacacgjcdkgachkdchcm. -ExtensionId is only needed for a copy
# whose key was changed. The registration is per user, so it serves
# EvoMesh's automation profile (/browser start) and your own Chrome alike;
# the extension still has to be loaded unpacked in each profile once
# (chrome://extensions -> Developer mode -> Load unpacked -> browser-extension).
[CmdletBinding()]
param(
    [string] $ExtensionId = 'fgefcelipaiabacacgjcdkgachkdchcm'
)

$ErrorActionPreference = 'Stop'

if ($ExtensionId -notmatch '^[a-p]{32}$') {
    throw "'$ExtensionId' does not look like a Chrome extension id (32 lowercase letters a-p). " +
        "Copy it from chrome://extensions with Developer mode on."
}

$Root = Split-Path -Parent $PSScriptRoot
$HostName = 'com.evomesh.browser_bridge'
$LauncherPath = Join-Path $PSScriptRoot 'chrome-native-host.bat'
if (-not (Test-Path $LauncherPath)) {
    throw "Launcher not found: $LauncherPath"
}
$ManifestPath = Join-Path $PSScriptRoot "$HostName.json"

$Manifest = [ordered]@{
    name            = $HostName
    description     = 'EvoMesh browser bridge -- lets granted agents read and drive Chrome tabs.'
    path            = $LauncherPath
    type            = 'stdio'
    allowed_origins = @("chrome-extension://$ExtensionId/")
}
$Manifest | ConvertTo-Json -Depth 4 | Set-Content -Path $ManifestPath -Encoding utf8
Write-Output "[install-chrome-bridge] wrote $ManifestPath"

$RegistryKey = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$HostName"
New-Item -Path $RegistryKey -Force | Out-Null
Set-ItemProperty -Path $RegistryKey -Name '(Default)' -Value $ManifestPath
Write-Output "[install-chrome-bridge] registered $RegistryKey -> $ManifestPath"

Write-Output ''
Write-Output '[install-chrome-bridge] Done. Reload the extension (chrome://extensions -> the'
Write-Output 'reload icon on EvoMesh Browser Bridge) so it picks up a fresh connectNative call.'
Write-Output 'Then give an agent the browser tool: /browser grant <agent> read|interact'
Write-Output 'and start EvoMesh''s own Chrome with /browser start (load the extension there once).'
