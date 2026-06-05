# TODO — download progress (% of VOD) + `date-<streamer>` cache naming

**Status:** ✅ **DONE — released as v1.4.3 (2026-06-05).** Offline tests green (248 pass);
live-verified against moonbuvr's real m3u8 (ffmpeg `out_time` parsed, `% of VOD` computed
off the card's 5:01:51 length); exe rebuilt + GitHub release published. Kept below as a
record of what shipped.

This captures two changes requested on 2026-06-05:
1. Show **% of the VOD downloaded** (+ MB, MB/s, and a sustained-rate ETA) during the
   m3u8 pull — not just "X MB at X MB/s".
2. Name the download cache `**<date>-<streamer>.mp4**` instead of flat `<date>.mp4`.

---

## What's already implemented (committed)

| File | Change |
|---|---|
| `modules/source_resolver.py` | `get_latest_vodvod_m3u8` now returns **4-tuple** `(url, date, title, duration_seconds)`; `_parse_card_duration` reads the VOD length (`H:MM:SS`/`M:SS`) from the card. `stream_m3u8_to_file(..., duration=, on_progress=)` runs ffmpeg with `-progress pipe:1 -nostats -loglevel warning`, parses each block, and calls `on_progress({out_time, bytes, elapsed, duration})`. stderr drained on a thread so a long pull can't deadlock. |
| `modules/progress.py` | `Progress.feed_attached` property (GUI feed present? → else CLI fallback). |
| `pipeline.py` | `_video_filename(date, streamer)` → `<date>-<streamer>.mp4`. `_make_download_cb(progress)` formats `% of VOD (out_time/duration)`, MB, MB/s, `~ETA left` (ETA assumes the average rate holds). vodvod + kick branches stream with the callback; vodvod also passes `duration`. CLI prints a throttled `\r` line; GUI gets `sub_progress` with a `fraction`. |
| `gui/web/app.js` | `sub_progress` with a numeric `fraction` makes the overall bar **determinate** and fills 0→100% of the VOD during download; `tick()` reclaims it for the whole run once `set_total` arrives. |
| `tests/` | resolver tests unpack the 4-tuple + assert duration; `_parse_card_duration` test; two `_make_download_cb` tests (pct/MB/rate/ETA, and the no-duration case). |

### Why % comes from time, not bytes
An m3u8's **total size is unknown until the end**, so a byte-based % is impossible. The
VOD's **duration** is on the vodvod card (e.g. `5:01:51`); ffmpeg reports the muxed
content timestamp (`out_time`) as it pulls, so `out_time / duration` is the true fraction.

---

## Run later (needs usage) — verification checklist

1. **Live vodvod download** (the real check — pick a VOD not already cached):
   ```powershell
   .\.venv\Scripts\python.exe pipeline.py --config config.json `
     --source-type vodvod --channel "@moonbuvr" --max-clips 3
   ```
   - Console shows `Latest VOD: <title>  (<date>, H:MM:SS)`.
   - During download, a throttled line updates:
     `Downloading VOD: 612 MB · 22.0 MB/s · 43% of VOD (2:11:00 / 5:01:51) · ~6m left`.
   - Cache file is `downloads/<date>-moonbuvr.mp4`.
2. **GUI**: launch `gui.bat`, start a vodvod run, confirm the overall bar **fills 0→100%
   during download** (no longer indeterminate) with "43%" + the detail line; after download
   it switches to the whole-run ETA.
3. **CLI ETA sanity**: the `~left` should shrink roughly linearly if the rate is steady.
4. **Edge**: a VOD with no length line on the card → falls back to MB + MB/s only (no %),
   no crash. (Covered offline by `test_make_download_cb_without_duration_omits_pct`.)

## Release (after verification)
1. Move `CHANGELOG.md` `[Unreleased]` → `[1.4.3]` (entry drafted below).
2. `git tag -a v1.4.3 -m "..."`, push branch + tag.
3. `powershell -ExecutionPolicy Bypass -File build_gui.ps1` (bakes the tag), copy
   `dist\TwitchHighlights.exe` to repo root, restore `gui/version.py` `_BAKED="dev"`.
4. `gh release create v1.4.3 ... dist\TwitchHighlights.exe#...`.

### CHANGELOG draft (Added)
- Live **download progress**: during an m3u8 pull the GUI/CLI now show **% of the VOD**
  downloaded (from content time over the VOD's known length), plus MB, MB/s, and a
  sustained-rate ETA — the GUI bar fills 0→100% instead of sitting indeterminate.
- Download cache is named `**<date>-<streamer>.mp4**` so the flat `downloads/` folder
  can't collide across channels that streamed on the same date.

---

## Caveats / notes
- **One-time re-download:** existing caches are `downloads/<date>.mp4`; the new name is
  `<date>-<streamer>.mp4`, so the first run per channel re-pulls the VOD. Intended — the
  old flat name couldn't tell two channels' same-day VODs apart. (Also clears the stale
  mislabeled `2026-06-04` moonbu file from the pre-v1.4.2 resolver bug.)
- **Clips folder unchanged:** still `clips/<streamer>/<date>/` (already namespaced). Only
  the download cache filename changed.
- **Same-day two-VODs** collision (both → one `<date>`) is still possible but out of scope
  per "not worried about conflicts going forward." VOD-id keying would close it if needed.
- kick downloads use the callback too but without a duration → MB + MB/s only (no %). Wiring
  kick's duration (from its API) is a small follow-up if wanted.
