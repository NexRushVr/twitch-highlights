# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [Unreleased]

### Added
- `clip_mode=phrase`: skip the LLM and cut a window around every spot the
  streamer says a configurable trigger phrase (`--trigger-phrase`, default
  `clip it`; `--phrase-pre` / `--phrase-post`, default 60s each). Voice-mark
  clips mid-stream instead of pressing hotkeys. Overlapping triggers merge;
  every match is kept (phrase mode ignores `max_clips`). Configurable via CLI,
  `config.json`, or `VOD_CLIP_TRIGGER_PHRASE` etc.
- Live elapsed-time ticker during output-silent phases (source download,
  ffmpeg audio extract, librosa peaks). Rewrites one line in place so a long
  quiet step visibly counts up instead of looking hung — this was the
  "it froze on Resolving source" confusion. TTY-only; silent in pipes/CI.
- Compact progress display: per-phase headers, tqdm bars for iterating phases
  (LLM chunks / clip cuts / caption burns), and an overall % that reports
  `elapsed / expected_total` instead of `phases_completed`. Expected total
  comes from `ffprobe`-ing the source duration and multiplying by a hardware
  factor (~0.15 for CUDA, ~1.5 for CPU; configurable via
  `runtime_estimate_factor`). Subprocess output (ffmpeg, yt-dlp, Whisper's
  per-segment prints, per-chunk LLM logs) is suppressed by default; pass
  `--verbose` to restore the old chatty mode. Whisper's own progress bar
  still prints in non-verbose mode since it's a real progress signal, not
  log spam. Stderr stays captured so failures still surface diagnostics.
- `--start-time` / `--end-time` (also `start_time` / `end_time` in config) trim
  the source video to a sub-range before any downstream work. Either bound may
  be omitted (no start = 0, no end = video duration). Accepts `HH:MM:SS`,
  `MM:SS`, or bare seconds. Out-of-range or inverted bounds raise a clear
  error before audio/transcription/LLM costs are incurred. Windowed runs land
  in `clips/<streamer>/<vod_date>_w<start>-<end>/` so different windows on the
  same date have independent manifests. Requires `ffprobe` (ships with ffmpeg).
- New `local` source type for files already on disk. `--path file.mp4` runs
  the pipeline directly against the recording with no copy; `--path file.ts`
  stream-copies (no re-encode) to `mp4` once into `download_dir` and then
  reuses the cached output on subsequent runs. Use case: streamers who
  recorded their own session in OBS and want to clip from the local
  recording instead of re-pulling from a VOD service.
- Output mp4s now carry embedded attribution metadata (`comment`, `title`,
  `description`) so files can be identified as pipeline output after they've
  been moved or shared. Visible in `ffprobe`, mediainfo, VLC's Codec
  Information, and Windows Explorer Properties → Details. Not a visible
  watermark — viewers see no change to the video itself.

## [1.0.0] - 2026-05-12

Initial public release. A local CLI that turns Twitch / Kick / m3u8 VODs into
short, captioned highlight clips using Whisper transcription and an LLM
(Ollama by default) for moment selection — no cloud APIs, no per-clip cost.

### Added
- End-to-end VOD highlight pipeline: source resolve → audio extract →
  Whisper transcription → LLM clip selection → audio-peak rerank →
  ffmpeg cut → CapCut-style burned subtitles.
- Source resolvers for: Twitch (yt-dlp), vodvod.top (Playwright scrape),
  Kick.com (curl_cffi API call), and raw m3u8 streams.
- LLM backends: Ollama (default) and OpenAI, with per-chunk timeout +
  exponential backoff on transient errors.
- Clip modes: `reaction`, `dance`, `hype`, `all`.
- Per-VOD-date caching of downloaded video, extracted WAV, transcript
  JSON, and clip manifest — re-running on the same day is a fast no-op.
- `--force` flag to bypass the manifest skip-guard.
- CLI + JSON config + `VOD_CLIP_<KEY>` env-var overrides.
- Nightly scheduler example for Windows Task Scheduler
  (`nightly.example.ps1`).
- GitHub Actions workflow: tests on push / PR / daily cron, Python
  3.10 / 3.11 / 3.12 matrix on ubuntu-latest.
- 132 unit tests covering source resolvers, audio extraction, transcriber
  wrapper, highlight selector (parsing + retries), clip extractor, and
  subtitle burner. All external boundaries (subprocess, librosa, whisper,
  ollama, openai, playwright, curl_cffi) are mocked.
