# Nightly highlight pipeline runner (example).
#
# 1. Copy to nightly.ps1 (gitignored) and edit the channel lists below.
# 2. Register with Windows Task Scheduler:
#      schtasks /Create /SC DAILY /ST 03:00 /TN "twitch-highlights-nightly" `
#        /TR "powershell -NoProfile -ExecutionPolicy Bypass -File <full-path>\nightly.ps1"
#
# The pipeline caches per VOD-date, so re-running on the same day is a fast no-op.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logFile = Join-Path $logDir ("nightly_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

# --- Edit these lists ---
# vodvod.top channels (Twitch mirror) — prefix each with "@"
$vodvodChannels = @("@example_channel_1", "@example_channel_2")
# Kick.com channels — slug only, no "@"
$kickChannels = @("example_kick_channel")

# --- LLM settings ---
$llmBackend = "ollama"
$ollamaModel = "qwen2.5:14b"

"===== Run started: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" | Tee-Object -FilePath $logFile -Append | Out-Host

foreach ($ch in $vodvodChannels) {
    "----- vodvod $ch -----" | Tee-Object -FilePath $logFile -Append | Out-Host
    & python pipeline.py `
        --source-type vodvod `
        --channel $ch `
        --clip-mode all `
        --max-clips 10 `
        --llm-backend $llmBackend `
        --model $ollamaModel 2>&1 | Tee-Object -FilePath $logFile -Append | Out-Host
}

foreach ($ch in $kickChannels) {
    "----- kick $ch -----" | Tee-Object -FilePath $logFile -Append | Out-Host
    & python pipeline.py `
        --source-type kick `
        --channel $ch `
        --clip-mode all `
        --max-clips 15 `
        --llm-backend $llmBackend `
        --model $ollamaModel 2>&1 | Tee-Object -FilePath $logFile -Append | Out-Host
}

"===== Run finished: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" | Tee-Object -FilePath $logFile -Append | Out-Host
