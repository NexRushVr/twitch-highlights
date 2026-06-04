<#
  AVIF export driver for twitch-highlights.

  Leverages the AvifTools module (github.com/NexRushVr/optimized-discord-gifs-avif)
  to encode finished clips into small, Discord-friendly animated AVIFs. This script
  does NOT reimplement any encoding — it imports AvifTools and calls Convert-ToAvif.

  It reads a JSON job file and writes a JSON result file:

    Job:    { outDir, modulePath, localRepo, maxWidth, preset,
              variants: [{suffix, crf, fps}, ...],
              items:    [{input, name}, ...] }
    Result: { results: [{input, name, variant, file}, ...] }

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

function Ensure-AvifTools([string]$explicit, [string]$localRepo) {
    if (Get-Command Convert-ToAvif -ErrorAction SilentlyContinue) { return }
    # 1) explicit module path from config
    if ($explicit -and (Test-Path $explicit)) {
        Import-Module $explicit -Force -ErrorAction Stop
    }
    # 2) already installed in the PS module path
    elseif (Get-Module -ListAvailable -Name AvifTools) {
        Import-Module AvifTools -Force -ErrorAction Stop
    }
    # 3) a sibling local checkout of the repo (dev machines)
    elseif ($localRepo -and (Test-Path $localRepo)) {
        Import-Module $localRepo -Force -ErrorAction Stop
    }
    # 4) pull it from GitHub as a tool (install to the user module path; -NoProfile
    #    so we don't edit the user's PowerShell profile)
    else {
        Write-Host "Installing AvifTools from github.com/NexRushVr/optimized-discord-gifs-avif ..."
        $tmp = Join-Path $env:TEMP "aviftools_install.ps1"
        Invoke-RestMethod -Uri "https://raw.githubusercontent.com/NexRushVr/optimized-discord-gifs-avif/main/install.ps1" -OutFile $tmp -ErrorAction Stop
        & $tmp -NoProfile
        Import-Module AvifTools -Force -ErrorAction Stop
    }
    if (-not (Get-Command Convert-ToAvif -ErrorAction SilentlyContinue)) {
        throw "AvifTools imported but Convert-ToAvif is unavailable."
    }
}

Ensure-AvifTools $job.modulePath $job.localRepo

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
            Convert-ToAvif -Path $it.input -OutputDir $tmp `
                -Fps $v.fps -Crf $v.crf -MaxWidth $job.maxWidth -Preset $job.preset -Force | Out-Host

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
