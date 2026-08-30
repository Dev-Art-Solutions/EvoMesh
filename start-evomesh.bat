@echo off
setlocal
cd /d "%~dp0"
set "UV_CACHE_DIR=%CD%\.runtime\uv-cache"
set "UV_PYTHON_INSTALL_DIR=%CD%\.runtime\python"

set "UV_EXE=uv"
where uv >nul 2>nul
if errorlevel 1 (
    if exist "..\.tools\uv\bin\uv.exe" (
        for %%I in ("..\.tools\uv\bin\uv.exe") do set "UV_EXE=%%~fI"
    ) else (
        echo [EvoMesh] uv was not found.
        echo Install it from https://docs.astral.sh/uv/ and run this file again.
        pause
        exit /b 1
    )
)

if not exist "evomesh.yaml" copy /y "evomesh.yaml.example" "evomesh.yaml" >nul

where dotnet >nul 2>nul
if errorlevel 1 (
    echo [EvoMesh] .NET 8 Desktop Runtime/SDK was not found.
    echo Install it from https://dotnet.microsoft.com/download/dotnet/8.0 and run again.
    pause
    exit /b 1
)

echo [EvoMesh] Synchronizing Python environment...
"%UV_EXE%" sync
if errorlevel 1 (
    echo [EvoMesh] Python environment setup failed.
    pause
    exit /b 1
)

echo [EvoMesh] Starting Windows Control Center...
dotnet run --project "desktop\EvoMesh.Desktop\EvoMesh.Desktop.csproj" -- "%CD%" "%UV_EXE%"
if errorlevel 1 (
    echo [EvoMesh] The Control Center stopped with an error.
    pause
    exit /b 1
)

endlocal
