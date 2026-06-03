<#
  twitch-highlights - interactive launcher.

  Walks you through picking a stream and cutting highlights without typing any
  command-line flags. Just answer the questions (press Enter to accept the
  [default] shown in brackets).

  Easiest way to run this: double-click run.bat.
#>

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Write-Title($msg) { Write-Host "`n$msg" -ForegroundColor Cyan }

# Prompt with a default value; pressing Enter accepts the default.
function Ask($prompt, $default) {
    if ($default) { $full = "$prompt [$default]" } else { $full = $prompt }
    $ans = Read-Host $full
    if ([string]::IsNullOrWhiteSpace($ans)) { return $default }
    return $ans.Trim()
}

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Setup hasn't been run yet." -ForegroundColor Red
    Write-Host "Double-click install.bat first, then come back here." -ForegroundColor Yellow
    exit 1
}

Write-Host "============================================================" -ForegroundColor Magenta
Write-Host "  twitch-highlights" -ForegroundColor Magenta
Write-Host "  Let's make some clips. Answer a few questions below." -ForegroundColor Magenta
Write-Host "============================================================" -ForegroundColor Magenta

# ---------------------------------------------------------------------------
# 1. Where's the stream coming from?
# ---------------------------------------------------------------------------
Write-Title "Where is the stream?"
Write-Host "  1) Kick channel            (most reliable - Kick keeps VODs forever)"
Write-Host "  2) Twitch VOD link         (paste a twitch.tv/videos/... URL)"
Write-Host "  3) vodvod.top channel      (Twitch mirror, for expired Twitch VODs)"
Write-Host "  4) A video file on my PC   (.mp4 or .ts)"

$cliArgs = @()
$choice = Ask "Pick 1-4" "1"

switch ($choice) {
    "1" {
        $channel = Ask "Kick channel name (e.g. abehamm)" $null
        $cliArgs += @("--source-type", "kick", "--channel", $channel)
    }
    "2" {
        $url = Ask "Twitch VOD URL" $null
        $cliArgs += @("--source-type", "twitch", "--url", $url)
    }
    "3" {
        $channel = Ask "vodvod.top channel handle (e.g. @eevi)" $null
        $cliArgs += @("--source-type", "vodvod", "--channel", $channel)
    }
    "4" {
        $path = Ask "Full path to the video file" $null
        $cliArgs += @("--source-type", "local", "--path", $path)
    }
    default {
        Write-Host "Didn't understand '$choice'. Defaulting to Kick." -ForegroundColor Yellow
        $channel = Ask "Kick channel name (e.g. abehamm)" $null
        $cliArgs += @("--source-type", "kick", "--channel", $channel)
    }
}

# ---------------------------------------------------------------------------
# 2. How should it pick clips?
# ---------------------------------------------------------------------------
Write-Title "How should it choose the moments?"
Write-Host "  1) AI picks the best moments     (recommended)"
Write-Host "  2) Only where you said a phrase  (e.g. you say 'clip it' on stream)"

$modeChoice = Ask "Pick 1-2" "1"
if ($modeChoice -eq "2") {
    $cliArgs += @("--clip-mode", "phrase")
    $phrase = Ask "Trigger phrase to look for" "clip it"
    $cliArgs += @("--trigger-phrase", $phrase)
} else {
    $cliArgs += @("--clip-mode", "all")
    $maxClips = Ask "How many clips at most?" "10"
    $cliArgs += @("--max-clips", $maxClips)
}

# ---------------------------------------------------------------------------
# 3. Use the tuned config if the installer made one
# ---------------------------------------------------------------------------
$configPath = Join-Path $PSScriptRoot "config.json"
if (Test-Path $configPath) {
    $cliArgs += @("--config", "config.json")
}

# ---------------------------------------------------------------------------
# Run it
# ---------------------------------------------------------------------------
Write-Title "Starting up. A long VOD can take 20-40 minutes - mostly transcription."
Write-Host "Command: python pipeline.py $($cliArgs -join ' ')" -ForegroundColor DarkGray
Write-Host ""

& $venvPy pipeline.py @cliArgs
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
    Write-Host "Done! Your clips are in the 'clips' folder:" -ForegroundColor Green
    $clipsDir = Join-Path $PSScriptRoot "clips"
    if (Test-Path $clipsDir) {
        Write-Host "  $clipsDir" -ForegroundColor Green
        $open = Ask "Open the clips folder now? (y/n)" "y"
        if ($open -eq "y") { Start-Process explorer.exe $clipsDir }
    }
} else {
    Write-Host "The run ended with an error (code $code). Scroll up for details." -ForegroundColor Red
    Write-Host "Common fixes: make sure the Ollama app is running, and that the" -ForegroundColor Yellow
    Write-Host "channel name / URL is correct." -ForegroundColor Yellow
}
