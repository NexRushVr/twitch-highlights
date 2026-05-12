# Security

This is a small personal-scale CLI tool, not a hosted service. It has no
network-facing components of its own; the attack surface is essentially
"what third-party Python deps + ffmpeg + a local Ollama daemon do on your
machine."

## Reporting a vulnerability

If you find something genuinely security-relevant (e.g. a way the scrapers
could be coerced into running attacker-controlled commands, or a dependency
chain compromise), please **open a private security advisory** via GitHub's
"Report a vulnerability" button on the Security tab rather than filing a
public issue.

For ordinary bug reports, use the regular issue tracker.

## Scope notes

- Configuration values from `config.json` and `VOD_CLIP_*` environment
  variables are trusted (they come from the user running the tool).
- VOD URLs, m3u8 URLs, and channel handles are passed to `ffmpeg`, `yt-dlp`,
  Playwright, and `curl_cffi`. Treat any URL you feed this tool as code —
  it will be opened in headless Chromium and / or downloaded by ffmpeg.
- LLM output is parsed as JSON and only `start` / `end` / `reason` / `score`
  / `description` keys are kept. `reason` is used in output filenames; the
  pipeline does **not** sanitize it beyond what the OS filesystem rejects.
  If you're running untrusted prompts, audit `modules/clip_extractor.py`.
