@echo off
REM Double-click me to make clips.
REM Runs the interactive launcher (run.ps1) with the execution policy unblocked.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
echo.
echo Press any key to close this window...
pause >nul
