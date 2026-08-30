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
        echo [EvoMesh] uv was not found. Install it from https://docs.astral.sh/uv/.
        pause
        exit /b 1
    )
)

if not exist "evomesh.yaml" copy /y "evomesh.yaml.example" "evomesh.yaml" >nul
"%UV_EXE%" sync --locked --no-dev
if errorlevel 1 exit /b 1
"%UV_EXE%" run evomesh --config "%CD%\evomesh.yaml"

endlocal
