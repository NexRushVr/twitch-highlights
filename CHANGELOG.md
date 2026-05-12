# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-05-12

Initial public release.

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
