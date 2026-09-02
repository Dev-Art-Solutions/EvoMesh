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

REM Exit code 86 is EvoMesh asking to be brought back up on a generation it just
REM landed in the tree. Anything else -- a clean /exit, a crash -- ends the run.
:run
"%UV_EXE%" run evomesh --config "%CD%\evomesh.yaml"
if %errorlevel% equ 86 (
    echo [EvoMesh] A new generation landed. Restarting into it...
    REM The new code may need dependencies the old one did not have.
    "%UV_EXE%" sync --locked --no-dev
    goto run
)

endlocal
