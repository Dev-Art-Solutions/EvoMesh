@echo off
REM Chrome spawns this directly (see scripts/install-chrome-bridge.ps1's
REM generated native-messaging host manifest, whose "path" points here) the
REM moment browser-extension/background.js calls connectNative. Nothing
REM about it needs a console window or user interaction -- stdin/stdout are
REM Chrome's own pipes to the extension, not a terminal.
setlocal
cd /d "%~dp0.."
set "UV_CACHE_DIR=%CD%\.runtime\uv-cache"
set "UV_PYTHON_INSTALL_DIR=%CD%\.runtime\python"

set "UV_EXE=uv"
where uv >nul 2>nul
if errorlevel 1 (
    if exist "..\.tools\uv\bin\uv.exe" (
        for %%I in ("..\.tools\uv\bin\uv.exe") do set "UV_EXE=%%~fI"
    ) else (
        exit /b 1
    )
)

"%UV_EXE%" run --locked --no-dev python -m evomesh.browser_bridge
endlocal
