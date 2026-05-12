# Contributing

Thanks for your interest. This is a small CLI tool maintained in spare time, so
keep PRs focused and contained. Bug fixes, scraper-resilience patches, and
prompt tuning are the easiest contributions to merge.

## Quick start

```bash
git clone https://github.com/NexRushVr/twitch-highlights.git
cd twitch-highlights
python -m venv .venv
. .venv/Scripts/Activate.ps1   # or: source .venv/bin/activate

pip install -r requirements-dev.txt
pytest -q                       # 132 tests, ~3 s, no GPU / network needed
```

The full install path (Whisper + CUDA torch + Ollama + Playwright Chromium) is
only needed to run the pipeline end-to-end against a real VOD. Tests mock every
external boundary.

## Before opening a PR

1. **`pytest` is green** locally. The CI matrix is Python 3.10 / 3.11 / 3.12 on
   ubuntu-latest — if you use 3.13-only syntax, CI will fail.
2. **No personal data** in commits: real channel handles, VOD URLs containing
   IDs, your `nightly.ps1`, `config.json`, `downloads/`, or `clips/`. The
   `.gitignore` covers most of this, but double-check `git diff` before pushing.
3. **Tests for new behavior.** If you add a new source resolver or a new clip
   mode, add a unit test in `tests/` that mocks the external boundary the same
   way the existing ones do.
4. **Don't bump dependency floors** unless you genuinely need a newer API. The
   existing floors in `requirements.txt` and `pyproject.toml` are intentionally
   conservative to keep installs working on older systems.

## Scope: what belongs in this repo

- Source resolvers for VOD platforms.
- LLM prompts and selection logic.
- ffmpeg-based clip / caption generation.
- Tests for all of the above.

## Scope: what doesn't

- Hosting / SaaS wrappers — keep this a local CLI.
- Direct uploaders to TikTok / YouTube — out of scope, license-sensitive, and
  platform-specific. Users can chain their own publishing step.
- Anything that touches a streamer's content without their consent. See the
  Legal / ethical section in [README.md](README.md).

## If you fork this

The repo URL (`github.com/NexRushVr/twitch-highlights`) appears in a few
places: `pyproject.toml` `[project.urls]`, the badges + clone command in
`README.md`, and this file. If you republish under your own name, swap them
all in one pass — `git grep NexRushVr` will list every hit.

## Reporting issues

Open an issue with:

- What you ran (`python pipeline.py ...` exact command).
- What you expected.
- What happened — including the relevant lines from `pipeline_run.log`.
- Python version, OS, GPU (if any), and the output of `pip freeze | grep -iE 'whisper|torch|librosa|yt-dlp|playwright|curl_cffi|ollama|openai'`.

Scraper-broken-by-layout-change issues are welcome; please include a sample
URL that fails so the fix can be tested.
