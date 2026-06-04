# Guide for AI assistants (Claude, etc.)

This file tells an agentic assistant how to **install, run, and manage**
twitch-highlights for a non-technical user. Prefer these instructions over
guessing. Confirm before installing software or deleting files.

## What this is
A local, GPU-accelerated tool that turns a Twitch/Kick VOD (or local video)
into short, captioned highlight clips. Pipeline: download source -> Whisper
transcription -> LLM picks the best moments -> ffmpeg cuts + burns captions.
Everything runs locally; the default LLM backend is Ollama (no cloud cost).

## Platform assumptions
Windows 10/11 with an NVIDIA GPU. Python 3.10-3.12. `winget` is used to install
system tools. macOS/Linux work too but the helper scripts are Windows-only —
on those, follow the "Manual quickstart" in `README.md`.

## Install (do this first)
1. Ensure Git is installed (`git --version`; if missing, `winget install Git.Git`).
2. Clone: `git clone https://github.com/NexRushVr/twitch-highlights "$HOME\twitch-highlights"`.
3. From the repo folder, run the installer:
   `powershell -ExecutionPolicy Bypass -File install.ps1`
   (Double-clicking `install.bat` does the same for the user.)
   To validate a machine first without changing anything, run a dry run:
   `powershell -ExecutionPolicy Bypass -File install.ps1 -Check` (or `check.bat`).
   It reports present/missing tools, detected VRAM, the model tier it would pick,
   and whether the model is already pulled -- downloading/installing nothing.
   It is **idempotent** — safe to re-run. It installs Python/ffmpeg/Ollama via
   winget if missing, creates `.venv`, installs **CUDA** PyTorch *before* the
   other deps (critical — the default PyPI torch is CPU-only), installs the rest,
   installs Playwright Chromium, detects VRAM, writes a tuned `config.json`, and
   pulls the matching Ollama model.
4. If a step fails, read its `[FAIL]`/`[WARN]` line and fix the root cause
   (usually: stale NVIDIA driver -> `torch.cuda.is_available()` is False; or
   Ollama app not running). Then re-run the installer.

## Run
Interactive (best for the user): `powershell -ExecutionPolicy Bypass -File run.ps1`
(or double-click `run.bat`).

Desktop app (the friendliest path for non-technical users): double-click `gui.bat`
(or build a standalone `TwitchHighlights.exe` — see "GUI" below). It's a thin
pywebview window that *drives the same `pipeline.py`* — every process is still a
visible script. First launch auto-installs `pywebview` into `.venv`.

Direct, for scripted/agent use — always use the venv Python and the tuned config:
```
.\.venv\Scripts\python.exe pipeline.py --config config.json --source-type kick --channel <name> --clip-mode all --max-clips 10
```
Other sources: `--source-type twitch --url <twitch.tv/videos/...>`,
`--source-type vodvod --channel "@handle"`, `--source-type local --path <file.mp4>`.
Discover every flag with `.\.venv\Scripts\python.exe pipeline.py --help`.

Output: `clips/<streamer>/<vod_date>/` — both horizontal cuts and
`*_vertical.mp4` / captioned variants. Re-running the same VOD/date is a cached
no-op; pass `--force` to redo it.

## Configure (edit `config.json`, created by the installer)
Key knobs: `ollama_model` (e.g. `gpt-oss:20b` best, `qwen2.5:14b` lighter),
`whisper_model` (`large-v3` best, `medium`/`small` lighter/faster),
`whisper_device` (`cuda`/`cpu`), `max_clips`, `min_clips`, `clip_mode`
(`all` | `hype` | `dance` | `reaction` | `phrase` | `music`), `burn_subtitles`,
`cleanup_source`. CLI flags override config; env vars `VOD_CLIP_<KEY>` override both.
Full table is in `README.md` under "Config reference".

## Common tasks the user may ask for
- "Make N clips from <channel/URL>" -> run the pipeline with `--max-clips N`.
- "Use the bigger/smaller AI model" -> set `ollama_model`, `ollama pull` it, re-run.
- "Only clip when I say a phrase" -> `--clip-mode phrase --trigger-phrase "clip it"`.
- "It's using the CPU / it's slow" -> check `torch.cuda.is_available()`; fix the
  NVIDIA driver, ensure `whisper_device: cuda`.
- "Run it automatically every night" -> create a `nightly.ps1` (see
  `nightly.example.ps1`) and register it with Windows Task Scheduler (the example
  file documents the `schtasks` command). Note: `nightly.ps1` is git-ignored.
- "Also make Discord AVIFs" -> add `--avif` (or tick "Also export Discord AVIFs"
  in the GUI; or use the Results-tab buttons after a run). Each clip ->
  `<streamer>-<rand>-not.avif` (HQ 480p60) + `-opt.avif` (small 480p30), written to
  `clips/<streamer>/<date>/avif/`. Encoding is the AvifTools module
  (`scripts/avif_export.ps1` + `modules/avif_exporter.py`), auto-installed from
  github.com/NexRushVr/optimized-discord-gifs-avif on first use. Needs ffmpeg with
  `libsvtav1`.
- "Make AVIFs under N MB" -> add `--avif-target <MB>` (e.g. `--avif-target 10`, or
  pick a size in the GUI's AVIF-target dropdown). Produces one
  `<streamer>-<rand>-<N>mb.avif` aimed under that size (AvifTools `-TargetSizeMB`).

Note: clip files are named `<streamer>-<random>.mp4` (e.g. `abehamm-a3f9c1.mp4`),
not `clip_NNN_<reason>.mp4`. The reason/score are in the manifest + mp4 metadata.

## Verify / troubleshoot
- `.\.venv\Scripts\python.exe -c "import torch;print(torch.cuda.is_available())"` -> must be `True` for GPU.
- `ollama list` -> daemon up + model present (start the Ollama app if it errors).
- `ffmpeg -version` -> ffmpeg on PATH.
- Tests (no GPU/network needed): `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt; .\.venv\Scripts\python.exe -m pytest -q`.
- The troubleshooting table in `README.md` maps common error messages to fixes.
- `No module named 'subprocess'` / "Failed to create .venv": a stale
  `PYTHONHOME`/`PYTHONPATH`. The installer clears these for its own process and
  retries; if creating the venv by hand, run with those env vars unset.

## GUI (optional desktop app)
A lightweight desktop front-end lives in `gui/`. It does **not** reimplement the
pipeline — it shells out to `.venv\Scripts\python.exe pipeline.py <flags>` exactly
like `run.ps1`, and monitors progress. Everything stays as inspectable scripts.
- **Launch from source:** double-click `gui.bat` (installs `pywebview` on first run).
- **Build the .exe:** `powershell -ExecutionPolicy Bypass -File build_gui.ps1` ->
  `dist\TwitchHighlights.exe` (single-file, icon included). Ship the exe at the
  repo root, next to `.venv` and `pipeline.py`. Needs the Edge WebView2 runtime
  (ships with Windows 11).
- **How monitoring works:** the GUI sets env `VOD_CLIP_PROGRESS_JSON=<file>` before
  spawning the pipeline. `modules/progress.py` then *also* appends one JSON line per
  phase event (`set_total` / `phase_start` / `phase_end`) to that file, which the GUI
  tails. This is purely additive — when the env var is unset, pipeline output is
  byte-for-byte unchanged (the CLI/`run.bat` flow is untouched).
- **Pieces:** `gui/app.py` (pywebview entry), `gui/api.py` (`JsApi` bridge — preflight
  via `install.ps1 -Check`, config round-trip, run control, results from
  `clips_manifest.json`, nightly scheduling), `gui/runner.py` (subprocess + progress
  tail + `taskkill /T` cancel), `gui/web/` (vanilla-JS UI), `gui/icon.ico`
  (regenerate with `python gui/make_icon.py`).
- The four GUI tabs mirror the CLI: Make clips (the `run.ps1` form), Results, Setup
  check, Settings (edits `config.json`, writes/register `nightly.ps1`).

## Guardrails
- Don't delete the user's `downloads/`, `clips/`, or `config.json` without asking.
- Don't commit `config.json` or `nightly.ps1` (both git-ignored — they're per-user).
- Large downloads (CUDA torch ~2.5 GB, LLM models 5-13 GB) are expected on first install.
