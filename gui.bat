@echo off
rem Twitch Highlights - desktop GUI launcher.
rem Double-click this to open the app. First run installs the GUI dependency.
setlocal
cd /d "%~dp0"

set "VENVPY=%~dp0.venv\Scripts\python.exe"
set "VENVPYW=%~dp0.venv\Scripts\pythonw.exe"

if not exist "%VENVPY%" (
  echo Setup hasn't been run yet.
  echo Double-click install.bat first, then come back here.
  pause
  exit /b 1
)

rem Install pywebview the first time (idempotent thereafter).
"%VENVPY%" -c "import webview" 1>nul 2>nul
if errorlevel 1 (
  echo Installing the GUI dependency ^(first run only^)...
  "%VENVPY%" -m pip install -r "%~dp0requirements-gui.txt"
  if errorlevel 1 (
    echo Failed to install pywebview. See the messages above.
    pause
    exit /b 1
  )
)

rem Launch windowed (pythonw = no console). Falls back to python if pythonw absent.
if exist "%VENVPYW%" (
  start "Twitch Highlights" "%VENVPYW%" "%~dp0gui\app.py"
) else (
  start "Twitch Highlights" "%VENVPY%" "%~dp0gui\app.py"
)
endlocal
