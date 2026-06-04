<#
  AVIF export driver for twitch-highlights.

  Leverages the AvifTools module (github.com/NexRushVr/optimized-discord-gifs-avif)
  to encode finished clips into small, Discord-friendly animated AVIFs. This script
  does NOT reimplement any encoding — it imports AvifTools and calls Convert-ToAvif.

  It reads a JSON job file and writes a JSON result file:

    Job:    { outDir, modulePath, localRepo, maxWidth, preset, needTargetSize,
              levers: ["Quality","Resolution","Fps"], minWidth, minFps,
              variants: [{suffix, crf, fps}            # quality variant
                         {suffix, targetMb, fps}],     # target-size variant
              items:    [{input, name}] }
    Result: { results: [{input, name, variant, file}] }

  Per item it emits a host line  "AVIFPROGRESS <done>/<total> <name>-<suffix>"
  that the Python wrapper parses for progress.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$JobFile,
    [Parameter(Mandatory)][string]$ResultFile
)

$ErrorActionPreference = 'Stop'
$job = Get-Content -LiteralPath $JobFile -Raw | ConvertFrom-Json

$AvifInstallUrl = "https://raw.githubusercontent.com/NexRushVr/optimized-discord-gifs-avif/main/install.ps1"

function Install-AvifFromGitHub {
    Write-Host "Installing AvifTools from github.com/NexRushVr/optimized-discord-gifs-avif ..."
    $tmp = Join-Path $env:TEMP "aviftools_install.ps1"
    Invoke-RestMethod -Uri $AvifInstallUrl -OutFile $tmp -ErrorAction Stop
    # -NoProfile: install the module without editing the user's PowerShell profile.
    & $tmp -NoProfile
    Import-Module AvifTools -Force -ErrorAction Stop
}

function Initialize-AvifTools([string]$explicit, [string]$localRepo, [bool]$needTargetSize) {
    if (-not (Get-Command Convert-ToAvif -ErrorAction SilentlyContinue)) {
        if ($explicit -and (Test-Path $explicit)) {
            Import-Module $explicit -Force -ErrorAction Stop
        }
        elseif (Get-Module -ListAvailable -Name AvifTools) {
            Import-Module AvifTools -Force -ErrorAction Stop
        }
        elseif ($localRepo -and (Test-Path $localRepo)) {
            Import-Module $localRepo -Force -ErrorAction Stop
        }
        else {
            Install-AvifFromGitHub
        }
    }
    $cmd = Get-Command Convert-ToAvif -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "AvifTools imported but Convert-ToAvif is unavailable." }

    # Self-heal: a previously-installed or sibling copy may predate a feature we
    # need (e.g. -TargetSizeMB). Pull the latest from GitHub and re-import once.
    if ($needTargetSize -and -not $cmd.Parameters.ContainsKey('TargetSizeMB')) {
        Write-Host "Installed AvifTools is missing -TargetSizeMB; pulling the latest from GitHub ..."
        Install-AvifFromGitHub
        $cmd = Get-Command Convert-ToAvif -ErrorAction SilentlyContinue
        if (-not ($cmd -and $cmd.Parameters.ContainsKey('TargetSizeMB'))) {
            throw "AvifTools is too old for target-size mode even after updating from GitHub."
        }
    }
}

$needTarget = [bool]$job.needTargetSize
Initialize-AvifTools $job.modulePath $job.localRepo $needTarget

New-Item -ItemType Directory -Force -Path $job.outDir | Out-Null

$results = [System.Collections.Generic.List[object]]::new()
$total = $job.items.Count * $job.variants.Count
$done = 0

foreach ($it in $job.items) {
    $base = [System.IO.Path]::GetFileNameWithoutExtension($it.input)
    foreach ($v in $job.variants) {
        $done++
        Write-Host ("AVIFPROGRESS {0}/{1} {2}-{3}" -f $done, $total, $it.name, $v.suffix)
        if (-not (Test-Path -LiteralPath $it.input)) {
            Write-Warning "Missing clip: $($it.input)"
            continue
        }
        $tmp = Join-Path $env:TEMP ("avif_" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        try {
            # Splat the right knobs for a quality vs. a target-size variant.
            $p = @{ Path = $it.input; OutputDir = $tmp; Preset = $job.preset; Force = $true }
            if ($v.targetMb -gt 0) {
                $p.TargetSizeMB = [double]$v.targetMb
                $p.MinWidth = [int]$job.minWidth
                $p.MinFps = [int]$job.minFps
                if ($job.levers) { $p.Levers = @($job.levers) }
                if ($v.fps -gt 0) { $p.Fps = [int]$v.fps }      # fps cap (a "request")
                if ($job.maxWidth -gt 0) { $p.MaxWidth = [int]$job.maxWidth }
            }
            else {
                $p.Crf = [int]$v.crf
                $p.Fps = [int]$v.fps
                if ($job.maxWidth -gt 0) { $p.MaxWidth = [int]$job.maxWidth }
            }
            Convert-ToAvif @p | Out-Host

            $src = Join-Path $tmp ($base + ".avif")
            if (Test-Path -LiteralPath $src) {
                $dest = Join-Path $job.outDir ($it.name + "-" + $v.suffix + ".avif")
                Move-Item -LiteralPath $src -Destination $dest -Force
                $results.Add([pscustomobject]@{
                    input = $it.input; name = $it.name; variant = $v.suffix; file = $dest
                })
            }
            else {
                Write-Warning "No AVIF produced for $($it.input) ($($v.suffix))"
            }
        }
        finally {
            Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        }
    }
}

# Wrap in an object so single-element results still serialize as a list.
[pscustomobject]@{ results = $results } | ConvertTo-Json -Depth 6 |
    Set-Content -LiteralPath $ResultFile -Encoding utf8
Write-Host ("AVIFDONE {0} files -> {1}" -f $results.Count, $job.outDir)
