[CmdletBinding()]
param(
    [string]$Version = "v0.1.0-alpha.1",
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release",
    [string]$Runtime = "win-x64",
    [string]$OutputDirectory = "dist"
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$outputRoot = [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot $OutputDirectory))
$packageName = "EvoMesh-$Version-$Runtime"
$packageRoot = [System.IO.Path]::GetFullPath((Join-Path $outputRoot $packageName))
$zipPath = Join-Path $outputRoot "$packageName.zip"
$checksumPath = "$zipPath.sha256"

if (-not $packageRoot.StartsWith($outputRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to package outside the output directory: $packageRoot"
}

New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
if (Test-Path -LiteralPath $packageRoot) {
    Remove-Item -LiteralPath $packageRoot -Recurse -Force
}
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
if (Test-Path -LiteralPath $checksumPath) {
    Remove-Item -LiteralPath $checksumPath -Force
}

New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
$appDirectory = Join-Path $packageRoot "app"
$projectPath = Join-Path $repositoryRoot "desktop\EvoMesh.Desktop\EvoMesh.Desktop.csproj"

dotnet restore $projectPath `
    --runtime $Runtime `
    -p:PublishSingleFile=true
if ($LASTEXITCODE -ne 0) {
    throw "dotnet restore failed with exit code $LASTEXITCODE"
}

dotnet publish $projectPath `
    --configuration $Configuration `
    --runtime $Runtime `
    --self-contained true `
    --no-restore `
    -p:PublishSingleFile=true `
    -p:DebugType=None `
    --output $appDirectory
if ($LASTEXITCODE -ne 0) {
    throw "dotnet publish failed with exit code $LASTEXITCODE"
}

$releaseFiles = @(
    ".python-version",
    "CHANGELOG.md",
    "evomesh.yaml.example",
    "LICENSE",
    "pyproject.toml",
    "README.md",
    "start-evomesh.bat",
    "start-evomesh-console.bat",
    "uv.lock"
)
foreach ($relativePath in $releaseFiles) {
    Copy-Item -LiteralPath (Join-Path $repositoryRoot $relativePath) -Destination $packageRoot
}
Copy-Item -LiteralPath (Join-Path $repositoryRoot "src") -Destination $packageRoot -Recurse
Get-ChildItem -LiteralPath (Join-Path $packageRoot "src") -Directory -Filter "__pycache__" -Recurse |
    Remove-Item -Recurse -Force

Compress-Archive -LiteralPath $packageRoot -DestinationPath $zipPath -CompressionLevel Optimal
$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath $checksumPath -Value "$hash  $packageName.zip" -Encoding ascii

Write-Host "Created $zipPath"
Write-Host "SHA256 $hash"
