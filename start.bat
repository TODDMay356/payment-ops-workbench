@echo off
setlocal enabledelayedexpansion

set "HERE=%~dp0"
set "PYTHON=%HERE%venv\Scripts\python.exe"
set "APP=%HERE%app.py"

if not exist "%PYTHON%" (
    echo [ERROR] venv not found. Run install.bat first.
    pause
    exit /b 1
)

if not exist "%HERE%config.json" (
    echo [WARN] config.json not found, using example.
)

echo.
echo =====================================
echo   Workbench starting...
echo   Open http://127.0.0.1:5050
echo   Close this window to stop.
echo =====================================
echo.

REM Open browser after 2 seconds
start "" http://127.0.0.1:5050

"%PYTHON%" "%APP%"

pause
