<#
  twitch-highlights - one-shot Windows installer.

  Sets up EVERYTHING a Windows + NVIDIA machine needs to run the pipeline:
    - Python 3.12, ffmpeg, and Ollama (installed via winget if missing)
    - a local virtual environment (.venv) with CUDA-enabled PyTorch
    - all Python dependencies + the Playwright Chromium browser
    - a hardware-tuned config.json (Whisper + Ollama models picked from your VRAM)
    - the matching Ollama model, pulled and ready

  Safe to re-run: every step checks first and skips what's already done.

  Easiest way to run this: just double-click install.bat (it handles the
  PowerShell execution-policy prompt for you). Advanced users can run:
    powershell -ExecutionPolicy Bypass -File install.ps1

  Dry run (changes NOTHING - just reports what's present and what would happen):
    powershell -ExecutionPolicy Bypass -File install.ps1 -Check
  or double-click check.bat. Use this to validate a machine before the big
  downloads.
#>
param([switch]$Check)

# NOTE: 'Continue', not 'Stop'. This script shells out to native tools (winget,
# python, pip, ollama, ffmpeg) that legitimately write progress/info to stderr.
# Under 'Stop', PowerShell 5.1 turns any native stderr line into a terminating
# NativeCommandError and aborts the whole install -- even when the command
# actually succeeded. We guard every critical step explicitly instead (Test-Path
# on outputs, $LASTEXITCODE / $? checks), so 'Continue' is both safer and correct.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

# ---------------------------------------------------------------------------
# Pretty output helpers
# ---------------------------------------------------------------------------
function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK]   $msg" -ForegroundColor Green }
function Write-Info($msg) { Write-Host "  ...    $msg" -ForegroundColor Gray }
function Write-Warn2($msg){ Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Write-Err2($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red }
function Write-Would($msg){ Write-Host "  [WOULD] $msg" -ForegroundColor Cyan }

# ---------------------------------------------------------------------------
# Shared helpers (defined up front so -Check and the installer share them)
# ---------------------------------------------------------------------------
function Test-Cmd($name) {
    $null = Get-Command $name -ErrorAction SilentlyContinue
    return $?
}

# Re-read PATH from the registry so tools installed earlier in this run become
# visible without restarting the shell.
function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = @($machine, $user | Where-Object { $_ }) -join ";"
}

# Returns the path to a usable python.exe in the 3.10-3.12 range, or $null.
# All probes are wrapped so a missing launcher/interpreter never throws under
# $ErrorActionPreference = 'Stop' -- we just move on to the next candidate.
function Find-Python {
    if (Test-Cmd py) {
        foreach ($ver in @("3.12", "3.11", "3.10")) {
            try {
                $out = (& py "-$ver" -c "import sys;print(sys.executable)" 2>$null)
                if ($LASTEXITCODE -eq 0 -and $out) { return $out.Trim() }
            } catch { }
        }
    }
    if (Test-Cmd python) {
        try {
            $v = (& python -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null)
            if ($v -in @("3.10", "3.11", "3.12")) {
                return (& python -c "import sys;print(sys.executable)").Trim()
            }
        } catch { }
    }
    return $null
}

# Total VRAM in GB of the first NVIDIA GPU, or 0 if none / nvidia-smi missing.
function Get-VramGB {
    if (-not (Test-Cmd nvidia-smi)) { return 0 }
    try {
        $out = (& nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>$null)
        if ($LASTEXITCODE -ne 0 -or -not $out) { return 0 }
        $mb = ($out | Select-Object -First 1).Trim()
        if ($mb -as [int]) { return [math]::Round([int]$mb / 1024.0, 1) }
    } catch { }
    return 0
}

# SINGLE SOURCE OF TRUTH for model selection. Whisper's CUDA memory can still be
# resident when Ollama (a separate process) loads the LLM, so the two can stack
# -- these tiers keep the combined footprint under the card so the whole run
# stays on-GPU and fast. (16 GB cards report ~15.9 GB, so the top tier triggers
# on 24 GB cards and 16 GB lands one tier down.) Ollama spills to system RAM
# rather than fail if a model is a touch too big, so these tune speed not crashes.
function Get-ModelTier($vram) {
    if ($vram -ge 22)     { return [pscustomobject]@{ Whisper="large-v3"; Ollama="gpt-oss:20b";  Device="cuda" } }  # 24 GB
    elseif ($vram -ge 15) { return [pscustomobject]@{ Whisper="medium";   Ollama="qwen2.5:14b"; Device="cuda" } }  # 16 GB
    elseif ($vram -ge 11) { return [pscustomobject]@{ Whisper="small";    Ollama="qwen2.5:14b"; Device="cuda" } }  # 12 GB
    elseif ($vram -ge 8)  { return [pscustomobject]@{ Whisper="small";    Ollama="llama3.1:8b"; Device="cuda" } }  # 8-10 GB
    elseif ($vram -gt 0)  { return [pscustomobject]@{ Whisper="base";     Ollama="llama3.1:8b"; Device="cuda" } }  # weak GPU
    else                  { return [pscustomobject]@{ Whisper="base";     Ollama="llama3.1:8b"; Device="cpu"  } }  # no GPU
}

# Is an Ollama model already pulled? (Best-effort; returns $false if Ollama down.)
function Test-OllamaModel($model) {
    if (-not (Test-Cmd ollama)) { return $false }
    try {
        $list = (& ollama list 2>$null)
        if ($LASTEXITCODE -ne 0) { return $false }
        return [bool]($list | Select-String -SimpleMatch $model -Quiet)
    } catch { return $false }
}

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

# A stale PYTHONHOME / PYTHONPATH makes a freshly-located Python look for its
# standard library in the wrong place. That is the classic cause of
# "No module named 'subprocess'" when running `python -m venv`. Clear them for
# THIS process only (does not touch the user's saved environment) so every
# Python we spawn below uses its own bundled stdlib.
foreach ($poison in 'PYTHONHOME', 'PYTHONPATH') {
    if (Test-Path "Env:$poison") {
        Write-Warn2 "Ignoring $poison for this run (a stale value breaks venv creation)"
        Remove-Item "Env:$poison" -ErrorAction SilentlyContinue
    }
}

# ===========================================================================
# DRY RUN (-Check): report only, change nothing, then exit.
# ===========================================================================
if ($Check) {
    Write-Host "============================================================" -ForegroundColor Magenta
    Write-Host "  twitch-highlights installer - DRY RUN (-Check)" -ForegroundColor Magenta
    Write-Host "  Nothing will be installed, downloaded, or written." -ForegroundColor Magenta
    Write-Host "============================================================" -ForegroundColor Magenta
    $missing = 0

    Write-Step "System tools"
    if (Test-Cmd winget) { Write-Ok "winget available" }
    else { Write-Warn2 "winget MISSING - update 'App Installer' from the Microsoft Store (needed to auto-install tools)" }

    if (Test-Cmd git) { Write-Ok "git available" }
    else { Write-Warn2 "git not found (only needed to clone the repo; would install Git.Git)" }

    $py = Find-Python
    if ($py) { Write-Ok "Python 3.10-3.12: $py" }
    else { Write-Warn2 "Python 3.10-3.12 MISSING -> would install Python.Python.3.12"; $missing++ }

    if (Test-Cmd ffmpeg) { Write-Ok "ffmpeg on PATH" }
    else { Write-Warn2 "ffmpeg MISSING -> would install Gyan.FFmpeg"; $missing++ }

    if (Test-Cmd ollama) {
        Write-Ok "Ollama installed"
        & ollama list *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Ollama daemon responding" }
        else { Write-Warn2 "Ollama installed but not running (open the Ollama app)" }
    } else { Write-Warn2 "Ollama MISSING -> would install Ollama.Ollama"; $missing++ }

    Write-Step "Python environment"
    if (Test-Path $venvPy) {
        Write-Ok ".venv exists"
        & $venvPy -c "import whisper, torch" 2>$null
        if ($?) { Write-Ok "Python dependencies importable" }
        else { Write-Warn2 "deps not fully installed yet -> would run pip install -r requirements.txt" }
        $cuda = (& $venvPy -c "import torch;print(torch.cuda.is_available())" 2>$null)
        if ($cuda -eq "True") { Write-Ok "CUDA PyTorch active" }
        else { Write-Warn2 "CUDA PyTorch not active -> would install GPU torch (update NVIDIA driver if it stays CPU)" }
    } else {
        Write-Would "create .venv, install CUDA PyTorch (~2.5 GB) + deps + Playwright Chromium"
    }

    Write-Step "GPU and model selection"
    $vram = Get-VramGB
    if ($vram -gt 0) { Write-Ok ("Detected ~{0} GB VRAM" -f $vram) }
    else { Write-Warn2 "No NVIDIA GPU detected via nvidia-smi -> would run on CPU (much slower)" }
    $tier = Get-ModelTier $vram
    Write-Info "Would use Whisper '$($tier.Whisper)' + Ollama '$($tier.Ollama)' on '$($tier.Device)'"
    if (Test-OllamaModel $tier.Ollama) { Write-Ok "Ollama model '$($tier.Ollama)' already pulled" }
    else { Write-Would "pull Ollama model '$($tier.Ollama)' (one-time download)" }

    Write-Step "Config"
    if (Test-Path (Join-Path $PSScriptRoot "config.json")) { Write-Ok "config.json exists - would be left untouched" }
    else { Write-Would "write a tuned config.json" }

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Magenta
    if ($missing -eq 0) { Write-Host "  Dry run OK - this machine looks ready. Run install.bat to finish." -ForegroundColor Green }
    else { Write-Host "  Dry run found $missing missing tool(s) above - install.bat will fetch them." -ForegroundColor Yellow }
    Write-Host "============================================================" -ForegroundColor Magenta
    exit 0
}

# ===========================================================================
# REAL INSTALL
# ===========================================================================
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host "  twitch-highlights installer" -ForegroundColor Magenta
Write-Host "  Windows + NVIDIA GPU setup. This can take 10-30 minutes" -ForegroundColor Magenta
Write-Host "  the first time (large downloads: PyTorch + the LLM model)." -ForegroundColor Magenta
Write-Host "  Tip: run a dry run first with  check.bat" -ForegroundColor Magenta
Write-Host "============================================================" -ForegroundColor Magenta

# ---------------------------------------------------------------------------
# 0. winget (Windows Package Manager) - needed to auto-install the system tools
# ---------------------------------------------------------------------------
Write-Step "Checking Windows Package Manager (winget)"
$haveWinget = Test-Cmd winget
if ($haveWinget) {
    Write-Ok "winget is available"
} else {
    Write-Warn2 "winget not found. It ships with Windows 10 (1809+) and Windows 11."
    Write-Warn2 "Update 'App Installer' from the Microsoft Store, then re-run this script."
    Write-Warn2 "The script will continue, but it cannot auto-install missing tools."
}

# Installs a winget package only if the given command is missing. Returns $true
# if the command is present (already, or after install).
function Install-ToolIfMissing($cmd, $wingetId, $label) {
    if (Test-Cmd $cmd) { Write-Ok "$label already installed"; return $true }
    if (-not $haveWinget) { Write-Err2 "$label is missing and winget is unavailable. Install it manually."; return $false }
    Write-Info "Installing $label via winget ($wingetId)..."
    winget install --id $wingetId -e --accept-source-agreements --accept-package-agreements --silent
    Update-SessionPath
    if (Test-Cmd $cmd) { Write-Ok "$label installed"; return $true }
    Write-Warn2 "$label installed but '$cmd' isn't on PATH yet. You may need to close and re-open this window, then re-run."
    return (Test-Cmd $cmd)
}

# ---------------------------------------------------------------------------
# 1. Python 3.10-3.12
# ---------------------------------------------------------------------------
Write-Step "Checking Python (need 3.10, 3.11, or 3.12)"
$python = Find-Python
if (-not $python) {
    Write-Info "No supported Python found. Installing Python 3.12..."
    Install-ToolIfMissing "py" "Python.Python.3.12" "Python 3.12" | Out-Null
    Update-SessionPath
    $python = Find-Python
}
if (-not $python) {
    Write-Err2 "Could not locate a working Python 3.10-3.12. Install it from python.org (check 'Add to PATH'), then re-run."
    exit 1
}
Write-Ok "Using Python: $python"

# ---------------------------------------------------------------------------
# 2. ffmpeg + Ollama
# ---------------------------------------------------------------------------
Write-Step "Checking ffmpeg"
Install-ToolIfMissing "ffmpeg" "Gyan.FFmpeg" "ffmpeg" | Out-Null

Write-Step "Checking Ollama (local LLM runtime)"
Install-ToolIfMissing "ollama" "Ollama.Ollama" "Ollama" | Out-Null

# ---------------------------------------------------------------------------
# 3. Virtual environment
# ---------------------------------------------------------------------------
Write-Step "Creating Python virtual environment (.venv)"
if (Test-Path $venvPy) {
    Write-Ok ".venv already exists"
} else {
    & $python -m venv .venv 2>$null
    if (-not (Test-Path $venvPy)) {
        # Most common cause is a broken/partial Python install or stale
        # PYTHONHOME/PYTHONPATH (cleared above). Try a clean winget (re)install
        # of Python and retry once before giving up.
        Write-Warn2 "venv creation failed with $python - attempting a clean Python 3.12 (re)install..."
        if ($haveWinget) {
            winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements --silent
            Update-SessionPath
            $retryPy = Find-Python
            if ($retryPy) {
                & $retryPy -m venv .venv 2>$null
                if (Test-Path $venvPy) { $python = $retryPy }
            }
        }
    }
    if (-not (Test-Path $venvPy)) {
        Write-Err2 "Could not create the virtual environment (.venv)."
        Write-Warn2 "This points to a broken Python install or a leftover PYTHONHOME / PYTHONPATH."
        Write-Warn2 "Try one of these, then re-run install.bat:"
        Write-Warn2 "  1. Reinstall Python 3.12 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')."
        Write-Warn2 "  2. Remove any PYTHONHOME / PYTHONPATH entries from your System Environment Variables."
        Write-Warn2 "  3. Open a brand-new terminal so the changes take effect."
        exit 1
    }
    Write-Ok ".venv created"
}
& $venvPy -m pip install --upgrade pip --quiet
Write-Ok "pip upgraded"

# ---------------------------------------------------------------------------
# 4. CUDA-enabled PyTorch (must come BEFORE requirements, or pip pulls the
#    CPU-only wheel and the GPU silently goes unused).
# ---------------------------------------------------------------------------
Write-Step "Installing GPU (CUDA) PyTorch"
& $venvPy -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>$null
$cudaOk = ($LASTEXITCODE -eq 0)
if ($cudaOk) {
    Write-Ok "CUDA-enabled PyTorch already present"
} else {
    Write-Info "Downloading CUDA 12.1 PyTorch wheels (this is the big one ~2.5 GB)..."
    & $venvPy -m pip install torch --index-url https://download.pytorch.org/whl/cu121
    & $venvPy -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>$null
    $cudaOk = ($LASTEXITCODE -eq 0)
    if ($cudaOk) { Write-Ok "CUDA PyTorch installed" }
    else { Write-Warn2 "PyTorch installed but torch.cuda.is_available() is False. Update your NVIDIA driver; the pipeline will fall back to CPU (slow) until then." }
}

# ---------------------------------------------------------------------------
# 5. Python dependencies + Playwright browser
# ---------------------------------------------------------------------------
Write-Step "Installing Python dependencies"
& $venvPy -m pip install -r requirements.txt
Write-Ok "Dependencies installed"

Write-Step "Installing Playwright Chromium (for the vodvod.top scraper)"
& $venvPy -m playwright install chromium
Write-Ok "Chromium installed"

# ---------------------------------------------------------------------------
# 6. Detect VRAM and pick models that fit, then write a tuned config.json
# ---------------------------------------------------------------------------
Write-Step "Detecting GPU and tuning model choices"
$vram = Get-VramGB
$tier = Get-ModelTier $vram
$whisperModel = $tier.Whisper
$ollamaModel  = $tier.Ollama
$device       = $tier.Device

if ($vram -gt 0) { Write-Ok ("Detected ~{0} GB VRAM" -f $vram) }
else { Write-Warn2 "No NVIDIA GPU detected via nvidia-smi. Falling back to CPU (much slower)." }
Write-Info "Whisper model: $whisperModel   |   Ollama model: $ollamaModel   |   device: $device"

$configPath = Join-Path $PSScriptRoot "config.json"
if (Test-Path $configPath) {
    Write-Ok "config.json already exists - leaving your settings untouched"
} else {
    try {
        $cfg = Get-Content (Join-Path $PSScriptRoot "config.example.json") -Raw | ConvertFrom-Json
        $cfg.whisper_model = $whisperModel
        $cfg.whisper_device = $device
        $cfg.ollama_model = $ollamaModel
        # Write UTF-8 WITHOUT a BOM. PowerShell 5.1's `Out-File -Encoding utf8`
        # prepends a BOM (EF BB BF), which Python's json.load chokes on. The
        # UTF8Encoding($false) ctor arg disables the BOM.
        [System.IO.File]::WriteAllText($configPath, ($cfg | ConvertTo-Json -Depth 10), (New-Object System.Text.UTF8Encoding $false))
        Write-Ok "Wrote tuned config.json"
    } catch {
        Write-Warn2 "Couldn't auto-write config.json ($_). Copy config.example.json to config.json manually if needed."
    }
}

# ---------------------------------------------------------------------------
# 7. Start Ollama and pull the model
# ---------------------------------------------------------------------------
Write-Step "Starting Ollama and pulling the LLM model ($ollamaModel)"
if (Test-Cmd ollama) {
    # Make sure the daemon is up; the desktop app usually starts it, but a fresh
    # install in this session may not have it running yet.
    & ollama list *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Info "Starting the Ollama background service..."
        Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
        Start-Sleep -Seconds 5
    }
    Write-Info "Pulling $ollamaModel (large download, one time)..."
    & ollama pull $ollamaModel
    if ($LASTEXITCODE -eq 0) { Write-Ok "$ollamaModel ready" }
    else { Write-Warn2 "Could not pull $ollamaModel automatically. Run 'ollama pull $ollamaModel' once the Ollama app is running." }
} else {
    Write-Warn2 "Ollama isn't on PATH yet. Open the Ollama app once, then run 'ollama pull $ollamaModel'."
}

# ---------------------------------------------------------------------------
# 8. Smoke test
# ---------------------------------------------------------------------------
Write-Step "Verifying the install"
$allGood = $true

& $venvPy -c "import whisper, torch" 2>$null
if ($?) { Write-Ok "Python deps import cleanly" } else { Write-Err2 "Python deps failed to import"; $allGood = $false }

$cudaReport = (& $venvPy -c "import torch;print(torch.cuda.is_available())" 2>$null)
if ($cudaReport -eq "True") { Write-Ok "GPU acceleration available (CUDA)" }
else { Write-Warn2 "CUDA not available - runs will use CPU (slow). Update your NVIDIA driver to fix." }

if (Test-Cmd ffmpeg) { Write-Ok "ffmpeg on PATH" } else { Write-Err2 "ffmpeg not found on PATH"; $allGood = $false }

& ollama list *> $null
if ($LASTEXITCODE -eq 0) { Write-Ok "Ollama responding" } else { Write-Warn2 "Ollama not responding yet (start the Ollama app)." }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Magenta
if ($allGood) {
    Write-Host "  Install complete!" -ForegroundColor Green
} else {
    Write-Host "  Install finished with warnings (see [FAIL]/[WARN] above)." -ForegroundColor Yellow
}
Write-Host "  Next step: double-click  run.bat  to make some clips." -ForegroundColor Magenta
Write-Host "============================================================" -ForegroundColor Magenta
