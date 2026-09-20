# Registers scripts/chrome-native-host.bat as a Chrome native-messaging host,
# so browser-extension/background.js's chrome.runtime.connectNative(...) call
# has something to launch. Per-user (HKCU), no elevation needed -- unlike
# install-services.ps1, this changes nothing system-wide.
#
# Order matters: load browser-extension/ unpacked in Chrome FIRST
# (chrome://extensions -> Developer mode -> Load unpacked), copy the
# extension id Chrome shows you, then run this with it:
#
#   .\scripts\install-chrome-bridge.ps1 -ExtensionId <the id>
#
# The id has to come first because Chrome only assigns one once the
# extension is actually loaded, and the native-messaging manifest this
# writes has to name it in allowed_origins -- a native host with no
# extension id allowed cannot be connected to by anything.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $ExtensionId
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
    description     = 'EvoMesh browser bridge -- lets agents read and navigate tabs in your own Chrome.'
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
Write-Output 'Add "python" and "chrome-browser" tools to an agent template to let it use this.'
