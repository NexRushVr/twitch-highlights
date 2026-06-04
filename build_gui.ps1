<#
  Package the desktop GUI into a single clickable Windows app: dist\TwitchHighlights.exe

  Run from the repo root:
      powershell -ExecutionPolicy Bypass -File build_gui.ps1

  Notes:
  - The exe is a thin shell. It does NOT bundle torch/whisper/ffmpeg — it runs the
    project's existing .venv python against pipeline.py, so it stays small.
  - Drop dist\TwitchHighlights.exe at the repo root, next to .venv and pipeline.py,
    and double-click it. (The exe still needs the project installed via install.bat.)
  - Builds --onefile (one clickable exe). For debugging a bundling problem, swap to
    --onedir below for a readable dist\TwitchHighlights\ folder.
#>

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "No .venv found. Run install.bat first." -ForegroundColor Red
    exit 1
}

Write-Host "Installing build dependencies (pywebview + pyinstaller)..." -ForegroundColor Cyan
& $py -m pip install -r requirements-gui.txt
& $py -m pip install "pyinstaller>=6.0"

Write-Host "Building TwitchHighlights.exe..." -ForegroundColor Cyan
# --onefile: a single clickable exe (drop it at the repo root next to .venv).
# For debugging a bundling issue, swap --onefile for --onedir (faster start,
# emits dist\TwitchHighlights\ with a readable _internal folder).
& $py -m PyInstaller `
    --noconfirm `
    --clean `
    --noconsole `
    --onefile `
    --name TwitchHighlights `
    --icon "gui\icon.ico" `
    --add-data "gui\web;web" `
    --collect-all webview `
    --collect-all clr_loader `
    "gui\app.py"

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "Built: dist\TwitchHighlights.exe" -ForegroundColor Green
    Write-Host "Copy dist\TwitchHighlights.exe to the repo root (next to .venv and" -ForegroundColor Green
    Write-Host "pipeline.py) and double-click it." -ForegroundColor Green
} else {
    Write-Host "PyInstaller failed (exit $LASTEXITCODE). Scroll up for details." -ForegroundColor Red
    exit $LASTEXITCODE
}
