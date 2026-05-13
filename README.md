# twitch-highlights

[![tests](https://github.com/NexRushVr/twitch-highlights/actions/workflows/tests.yml/badge.svg)](https://github.com/NexRushVr/twitch-highlights/actions/workflows/tests.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Local, GPU-accelerated highlight extractor for Twitch / Kick VODs and raw m3u8 streams.

**What you get:** vertical, CapCut-style captioned short clips (`*_captioned.mp4`) plus the horizontal source cuts, ready to upload to TikTok / Shorts / Reels. Everything runs on your own machine — no cloud APIs required if you use Ollama.

**Who it's for:** streamers, clippers, and editors who want a "pick last night's best 10 moments and burn captions" command, not a manual scrub-and-cut workflow.

**Cost:** free (MIT). The only paid path is optional OpenAI usage; the default (`ollama` + a local Whisper model) costs $0 once installed.

**This is not:** a hosted service, a Twitch clip uploader, or a one-click TikTok publisher. It produces files locally — what you do with them is up to you.

## Quickstart

Assumes Python 3.10+, ffmpeg on `PATH`, an NVIDIA GPU (CPU works but is much slower), and Ollama running locally. Detailed install + GPU notes are in the [Install](#install) section.

```bash
git clone https://github.com/NexRushVr/twitch-highlights.git
cd twitch-highlights
python -m venv .venv && . .venv/Scripts/Activate.ps1     # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
ollama pull qwen2.5:14b

# Cut highlights from the most recent Kick stream by a channel:
python pipeline.py --source-type kick --channel abehamm --clip-mode all

# Or via vodvod.top (Twitch mirror), for Twitch streamers who archive there:
python pipeline.py --source-type vodvod --channel "@eevi" --clip-mode all
```

Output lands in `clips/<streamer>/<vod_date>/`. Re-running on the same VOD is a fast no-op — every step is cached per VOD-date.

> **Picking a source type:** Use `twitch` when you have a still-live VOD URL — perfect for clipping your own streams. Use `kick` for any Kick channel. Use `vodvod` for older Twitch streams whose original VODs have expired (Twitch only retains them 14–60 days depending on the streamer's tier). Use `local` when you already have the recording on disk (OBS output, downloaded VOD, etc.) as an `.mp4` or `.ts`.

## How it works

```
pipeline.py
├── modules/source_resolver.py    # yt-dlp, vodvod.top scrape, Kick API, m3u8 download
├── modules/audio_extractor.py    # ffmpeg WAV + librosa loudness peaks
├── modules/transcriber.py        # openai-whisper (CUDA)
├── modules/highlight_selector.py # LLM call + JSON parse + dedupe
├── modules/clip_extractor.py     # ffmpeg clip cutting
└── modules/subtitle_burner.py    # ASS generation + ffmpeg subtitle burn-in
prompts/                          # base + per-mode LLM prompts (edit freely)
```

The pipeline pulls the source VOD, transcribes the audio with Whisper, asks an LLM to pick the best clip windows from the transcript, cross-references those picks against audio-loudness peaks, cuts with ffmpeg, and burns CapCut-style captions on top.

## Requirements

System tools (must be on `PATH`):

- `ffmpeg`
- `yt-dlp` (only for the `twitch` source type — `pip install yt-dlp` installs the CLI)
- **CUDA-capable NVIDIA GPU** for Whisper (the default model is `large-v3`, which needs ~10 GB VRAM). CPU is supported but is roughly 30–50× slower and not recommended for full VODs.
- [Ollama](https://ollama.com) running locally if you use the local LLM backend (default).

Python: **3.10, 3.11, or 3.12**.

### Installing ffmpeg

| Platform | Command |
| --- | --- |
| Windows (winget) | `winget install Gyan.FFmpeg` |
| Windows (choco) | `choco install ffmpeg` |
| macOS (brew) | `brew install ffmpeg` |
| Debian/Ubuntu | `sudo apt-get install -y ffmpeg` |
| Arch | `sudo pacman -S ffmpeg` |

Verify with `ffmpeg -version`.

### Installing Ollama (default LLM backend)

Install from [ollama.com](https://ollama.com), make sure the daemon is running (`ollama serve` or the tray app), then pull the default model:

```bash
ollama pull qwen2.5:14b
```

`qwen2.5:14b` is ~9 GB. If you have <16 GB RAM/VRAM, pull a smaller model like `llama3.1:8b` and set `--model llama3.1:8b` (or `ollama_model` in config).

## Install

```bash
git clone https://github.com/NexRushVr/twitch-highlights.git
cd twitch-highlights
python -m venv .venv

# Activate the venv:
. .venv/Scripts/Activate.ps1     # Windows PowerShell
.venv\Scripts\activate.bat       # Windows cmd.exe
source .venv/bin/activate        # macOS / Linux

pip install -r requirements.txt
playwright install chromium      # only needed for the vodvod.top scraper
```

### GPU and Whisper setup

`pip install openai-whisper` pulls in `torch` from PyPI, which on Windows/Linux installs the **CPU-only** wheel by default. To actually use your NVIDIA GPU you need a CUDA-enabled torch build. Install it *before* the rest:

```bash
# CUDA 12.1 wheels (check https://pytorch.org/get-started/locally/ for your CUDA version)
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install -r requirements.txt
```

If you don't have a GPU, override the device:

```bash
python pipeline.py ... --config config.json   # set "whisper_device": "cpu"
# or env var:
$env:VOD_CLIP_WHISPER_DEVICE = "cpu"          # PowerShell
export VOD_CLIP_WHISPER_DEVICE=cpu            # bash
```

Leaving `whisper_device: "cuda"` on a CPU-only machine will hard-crash at transcription time.

### Smoke test

After install, verify the pieces are wired up before kicking off a full VOD run:

```bash
ffmpeg -version            # ffmpeg on PATH
yt-dlp --version           # yt-dlp on PATH
ollama list                # Ollama running, default model present (start the daemon first if this errors)
python -c "import whisper, torch; print('cuda:', torch.cuda.is_available())"
pip install -r requirements-dev.txt && pytest -q   # 192 unit tests (no GPU/network needed)
```

If `torch.cuda.is_available()` prints `False`, see the GPU section above. If `ollama list` errors with a connection refused, start the daemon (`ollama serve` or launch the tray app) and retry.

## Usage

### Pick a source

```bash
# Latest VOD from a Kick channel — Kick keeps VODs indefinitely, so this is
# the most reliable source for channels that stream on Kick.
python pipeline.py --source-type kick --channel abehamm --clip-mode all --max-clips 10

# Latest VOD from a vodvod.top channel (Twitch mirror).
# Useful for Twitch streamers whose own VODs have expired or weren't archived.
python pipeline.py --source-type vodvod --channel "@eevi" --clip-mode all --max-clips 10

# A specific Twitch VOD URL — best path for your own streams, or for popular
# streamers (e.g. shroud) whose VODs stay available longer. Won't help with
# channels like eevi who delete their VODs — use the vodvod path for those.
python pipeline.py --source-type twitch --url https://www.twitch.tv/videos/2345678901

# A raw m3u8 stream — for when you already have a manifest URL in hand.
python pipeline.py --source-type m3u8 --url https://example.com/stream.m3u8

# A local mp4 you already recorded (OBS, screen capture, downloaded VOD, etc.).
# No re-download, no scraping — pipeline runs straight on your file.
python pipeline.py --source-type local --path "C:/Users/you/Videos/last_night.mp4"

# A local .ts (OBS's default replay-buffer format works fine).
# Stream-copied to mp4 once into ./downloads/ then cached for re-runs.
python pipeline.py --source-type local --path "C:/Users/you/Videos/last_night.ts"
```

### Mix in flags

```bash
# Just the 1:00:00 — 1:30:00 window of a Kick stream, in `dance` mode.
python pipeline.py --source-type kick --channel abehamm --clip-mode dance `
    --start-time 1:00:00 --end-time 1:30:00

# Only the last 30 minutes of a local OBS recording (no end-time = "to the end").
python pipeline.py --source-type local --path "./obs.mp4" --start-time 1:30:00

# Only the first 10 minutes of a vodvod stream (no start-time = "from the start").
python pipeline.py --source-type vodvod --channel "@eevi" --end-time 0:10:00

# Tighter clip cap + hype mode + OpenAI backend (uses VOD_CLIP_OPENAI_API_KEY).
python pipeline.py --source-type kick --channel abehamm --clip-mode hype `
    --max-clips 5 --llm-backend openai --model gpt-4o-mini

# Re-run today's vodvod clip pass with a different window — `--force` bypasses
# the manifest skip-guard.
python pipeline.py --source-type vodvod --channel "@eevi" `
    --start-time 0:30:00 --end-time 1:00:00 --force

# Drive everything from a JSON config (CLI flags and VOD_CLIP_* env vars still
# override what's in the file).
python pipeline.py --config config.json
```

> PowerShell line-continuation is shown as ` `` `; on macOS/Linux use `\` instead.

### What you'll see while it runs

By default the display is compact — phase headers, progress bars on iterating phases, and an overall % anchored to an upfront wall-clock estimate. Example for a 2-hour VOD on a CUDA GPU:

```
[1/7] Resolving source...  (overall 0%)
    Expected total runtime: ~18:00 (based on source duration)
       v done in 0:02  (overall 0%, ~17:58 left of ~18:00)
[2/7] Extracting audio...  (overall 0%, ~17:58 left of ~18:00)
       v done in 0:08  (overall 1%, ~17:50 left of ~18:00)
[3/7] Transcribing with Whisper...  (overall 1%, ~17:50 left of ~18:00)
       [Whisper's own per-segment progress bar prints here]
       v done in 15:22  (overall 86%, ~2:36 left of ~18:00)
[4/7] LLM highlight selection...  (overall 86%, ~2:36 left of ~18:00)
       LLM chunks: 100%|███████████| 4/4 [01:12<00:00, 18.0s/it]
       v done in 1:12  (overall 92%, ~1:24 left of ~18:00)
[5/7] Cross-referencing audio peaks...  (overall 92%, ~1:24 left of ~18:00)
       v done in 0:03  (overall 92%, ~1:21 left of ~18:00)
[6/7] Cutting clips with FFmpeg...  (overall 92%, ~1:21 left of ~18:00)
       clips: 100%|████████████████| 10/10 [00:15<00:00, 1.5s/it]
       v done in 0:15  (overall 93%, ~1:06 left of ~18:00)
[7/7] Burning CapCut-style captions...  (overall 93%, ~1:06 left of ~18:00)
       captions: 100%|█████████████| 10/10 [00:32<00:00, 3.2s/it]
       v done in 0:32  (overall 94%, ~1:02 left of ~18:00)

Done. 10 clips -> clips/abehamm/2026-05-13
   Manifest: clips/abehamm/2026-05-13/clips_manifest.json
   Total time: 17:34
```

Overall % is `elapsed / expected_total`, where expected is the source duration × a hardware factor (~0.15 on CUDA, ~1.5 on CPU). It's clamped to 99% mid-run so a faster-than-expected pass doesn't oscillate past 100%. Override the factor with `runtime_estimate_factor` in config if your hardware is significantly different.

Pass `--verbose` to get the old chatty behavior: ffmpeg / yt-dlp / Whisper / per-LLM-chunk logs all stream through. Useful when something's failing and you want to see the raw output.

Clip modes: `reaction`, `dance`, `hype`, `all`.

LLM backend: `--llm-backend ollama` (default) or `--llm-backend openai` plus `--model <name>`. OpenAI requires `openai_api_key` in your config or `VOD_CLIP_OPENAI_API_KEY` in your env.

### Time window details

`--start-time` and `--end-time` accept `HH:MM:SS`, `MM:SS`, or bare seconds (`3600`). Either may be omitted; the missing one defaults to "start of video" or "end of video" respectively.

The window is applied with an ffmpeg stream-copy (no re-encode), then the rest of the pipeline runs on the trimmed file. Output lands in `clips/<streamer>/<vod_date>_w<start>-<end>/` so different windows on the same VOD-date don't collide. Out-of-range bounds raise a clear error *before* any heavy work starts:

```
ValueError: end_time 4:00:00 exceeds video duration (2:45:00 long);
            drop --end-time to clip to the end
```

Requires `ffprobe` (ships with ffmpeg) so the pipeline can read the source's duration.

Full CLI surface (`python pipeline.py --help`):

```
usage: pipeline.py [-h] [--config CONFIG]
                   [--source-type {twitch,vodvod,m3u8,kick,local}] [--url URL]
                   [--path PATH] [--channel CHANNEL]
                   [--start-time START_TIME] [--end-time END_TIME]
                   [--clip-mode {reaction,dance,hype,all}]
                   [--max-clips MAX_CLIPS] [--llm-backend {ollama,openai}]
                   [--model MODEL] [--force] [--verbose]
```

**Expected runtime** (RTX 3090, 4-hour 1080p VOD): ~20–40 min total — most of it Whisper transcription. Output is roughly 10 × 15–45 s mp4s, a few hundred MB total.

Output lands in `clips/<streamer>/<vod_date>/`:

```
clip_001_funny_reaction.mp4
clip_001_funny_reaction_captioned.mp4
clip_001_funny_reaction_captioned.ass    # edit + re-burn without re-running the pipeline
clips_manifest.json
```

Every output mp4 also carries embedded attribution metadata: a `comment` tag pointing at this repo, plus the LLM-generated `title` (reason) and `description` for that specific clip. Visible via `ffprobe -show_format <file>`, mediainfo, VLC → Tools → Codec Information, or Windows Explorer → right-click → Properties → Details. It's there as honest provenance — not a DRM lock and not visible on the video itself.

A `clips_manifest.json` entry looks like:

```json
[
  {
    "file": "clips/shroud/2026-05-10/clip_001_funny_reaction.mp4",
    "captioned": "clips/shroud/2026-05-10/clip_001_funny_reaction_captioned.mp4",
    "meta": {
      "start": 1842.5,
      "end": 1867.2,
      "reason": "funny_reaction",
      "score": 0.92,
      "description": "Shroud reacts to an unexpected teamkill"
    }
  }
]
```

Pass `--force` to regenerate clips for a VOD-date that already has a manifest.

## Configuration

Precedence (highest wins): **CLI flags > `VOD_CLIP_<KEY>` env vars > `config.json` > defaults in `config.py`**.

```bash
cp config.example.json config.json
# edit config.json
python pipeline.py --config config.json
```

Env-var example: `VOD_CLIP_OLLAMA_MODEL=llama3.1:8b`, `VOD_CLIP_WHISPER_DEVICE=cpu`.

### Common settings

| Key | Default | Notes |
| --- | --- | --- |
| `source_type` | `twitch` | `twitch` \| `vodvod` \| `kick` \| `m3u8` \| `local` |
| `clip_mode` | `reaction` | `reaction` \| `dance` \| `hype` \| `all` |
| `max_clips` | `10` | hard cap on output count |
| `whisper_device` | `cuda` | **`cuda` will crash on a CPU-only machine — set `cpu` explicitly if no GPU** |
| `whisper_model` | `large-v3` | `tiny` \| `base` \| `small` \| `medium` \| `large-v3` |
| `llm_backend` | `ollama` | `ollama` \| `openai` |
| `ollama_model` | `qwen2.5:14b` | any model pulled into your local Ollama |
| `burn_subtitles` | `true` | also produce a `*_captioned.mp4` per clip |
| `verbose` | `false` | stream all subprocess output + per-chunk LLM logs (compact display is the default) |
| `runtime_estimate_factor` | `0.0` | overall-% predictor; `0.0` = auto (~0.15 for CUDA, ~1.5 for CPU). Set explicitly if your hardware is unusually fast/slow. |

<details>
<summary><b>Advanced settings</b></summary>

| Key | Default | Notes |
| --- | --- | --- |
| `twitch_vod_url` | `""` | used when `source_type=twitch` |
| `vodvod_channel` | `""` | e.g. `@shroud` |
| `kick_channel` | `""` | slug, no `@` |
| `m3u8_url` | `""` | direct stream URL |
| `local_path` | `""` | path to a local `.mp4` or `.ts` (used when `source_type=local`) |
| `start_time` | `""` | trim the source to start at this point (`HH:MM:SS`, `MM:SS`, or seconds) |
| `end_time` | `""` | trim the source to end at this point (same formats as `start_time`) |
| `quality` | `720p` | yt-dlp height cap: `best` \| `1080p` \| `720p` \| `480p` |
| `download_dir` | `./downloads` | source VOD + extracted WAV cache |
| `whisper_language` | `en` | Whisper language hint |
| `openai_model` | `gpt-4o-mini` | used when `llm_backend=openai` |
| `openai_api_key` | `""` | or set `VOD_CLIP_OPENAI_API_KEY` |
| `llm_timeout_seconds` | `300` | per-chunk timeout — guards against hung reasoning models |
| `min_clip_duration` | `8` | seconds |
| `max_clip_duration` | `45` | seconds |
| `clip_padding_seconds` | `3` | head/tail padding around the LLM's chosen window |
| `output_dir` | `./clips` | where `<streamer>/<vod_date>/` lands |

</details>

## Nightly scheduling

Copy `nightly.example.ps1` to `nightly.ps1`, edit the channel lists, then register a daily Task Scheduler job (the example file has the `schtasks` command). `nightly.ps1` is gitignored.

On macOS / Linux, the equivalent is a `cron` entry pointing at `python pipeline.py --source-type vodvod --channel @whoever --clip-mode all`.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `RuntimeError: CUDA error` / `Torch not compiled with CUDA` | torch CPU wheel installed, or no GPU | Install CUDA torch (see GPU and Whisper setup), or set `whisper_device: "cpu"`. |
| `whisper.load_model` OOM on GPU | `large-v3` needs ~10 GB VRAM | Use `whisper_model: "medium"` or `"small"`. |
| `httpx.ConnectError` / `connection refused` to `localhost:11434` | Ollama daemon not running | Launch the Ollama tray app, or run `ollama serve`. |
| `model "qwen2.5:14b" not found` | Default model not pulled | `ollama pull qwen2.5:14b` (or whatever you set as `ollama_model`). |
| `playwright._impl._errors.Error: Executable doesn't exist` | Chromium not downloaded | `playwright install chromium`. |
| vodvod scraper returns no VOD / wrong page | vodvod.top layout changed (it's a third-party scraper, see Caveats) | Pin to a Twitch VOD URL with `--source-type twitch --url ...` while you wait for a fix. |
| `yt-dlp: ERROR: Unable to download ... 403` | yt-dlp out of date vs Twitch changes | `pip install -U yt-dlp`. |
| Pipeline says "skipping, manifest exists" but you want a re-run | Per-VOD-date cache hit | Pass `--force`. |

If the run dies, check `pipeline_run.log` next to `pipeline.py` for the full traceback.

## Caveats

This project depends on third-party services that change without warning. The scrapers in [modules/source_resolver.py](modules/source_resolver.py) are best-effort:

- **vodvod.top** is an unaffiliated, community-run Twitch mirror. Its legal standing is unclear, the site itself could disappear, and the DOM is scraped via Playwright — any layout change breaks the `vodvod` source until [modules/source_resolver.py:68](modules/source_resolver.py#L68) is updated. The `twitch` direct path (yt-dlp) is the most stable when you have a live VOD URL — Twitch's 14–60 day retention just means you can't reach back to old streams that way.
- **Kick.com** has no official public API. The `kick` source uses an undocumented endpoint fronted by Cloudflare and accessed via `curl_cffi` browser impersonation. It can rotate or rate-limit at any time.
- **yt-dlp** ships extractor fixes constantly — keep it updated (`pip install -U yt-dlp`).
- This project does not host, redistribute, or mirror anyone's stream content. It only orchestrates tools you've installed locally. If a streamer or platform asks you to stop processing their content, stop.

## Legal / ethical

- **Respect platform Terms of Service.** Twitch, Kick, and vodvod.top each have rules about automated access. Don't hammer endpoints — the nightly cron pattern in `nightly.example.ps1` is fine; scraping a streamer's full back-catalogue in a loop is not. If you don't understand a platform's ToS, read it before running this.
- **VOD content belongs to the streamer.** Get explicit permission before reuploading clips of someone else's stream to TikTok / YouTube / Reels / etc. Many streamers explicitly allow clipping; others don't. The default assumption should be "ask first." This tool is intended for personal archival and for clippers working with streamers who've okayed it.
- **The MIT license covers the code, not the content you run it on.** You are responsible for what you do with the output.

---

*Disclaimer: Generated with agentic AI.*

## License

MIT — see [LICENSE](LICENSE).
