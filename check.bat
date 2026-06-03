@echo off
REM Double-click me to CHECK your PC without installing anything.
REM Runs install.ps1 in dry-run mode: it reports what's present and what the
REM real installer would do, but downloads/installs/writes nothing.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Check
echo.
echo Press any key to close this window...
pause >nul
