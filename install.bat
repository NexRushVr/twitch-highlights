@echo off
REM Double-click me to install twitch-highlights.
REM This just runs install.ps1 with the execution policy unblocked, so you
REM don't have to fight PowerShell's "running scripts is disabled" message.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
echo Press any key to close this window...
pause >nul
