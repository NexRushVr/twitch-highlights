import argparse
import json
import os
import re

from config import load_config
from modules.source_resolver import (
    apply_time_window,
    download_twitch_vod,
    get_latest_vodvod_m3u8,
    get_latest_kick_vod_m3u8,
    resolve_local_file,
    stream_m3u8_to_file,
    _probe_duration,
)
from modules.audio_extractor import extract_audio, get_audio_peaks
from modules.transcriber import transcribe
from modules.highlight_selector import select_highlights, top_up_with_music_peaks
from modules.clip_extractor import batch_extract
from modules.subtitle_burner import caption_clip
from modules.progress import DEFAULT_PHASE_WEIGHTS, Progress, fmt_seconds


def _streamer_subdir(cfg: dict) -> str:
    """Return a per-streamer folder name (or empty string if not derivable)."""
    if cfg["source_type"] == "vodvod":
        return cfg.get("vodvod_channel", "").lstrip("@")
    if cfg["source_type"] == "kick":
        return cfg.get("kick_channel", "").lstrip("@")
    return ""


def _cached_video_ok(path: str) -> bool:
    """A cached download is reusable only if ffprobe can read a duration from it.

    An interrupted `-c copy` download has no moov atom and would crash audio
    extraction ("moov atom not found"), so a partial/corrupt cache is treated as a
    miss and re-downloaded instead of failing every run on the same bad file."""
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        return False
    try:
        return _probe_duration(path) > 0
    except Exception:
        return False


def _video_filename(vod_date: str, streamer: str) -> str:
    """Download cache name. Namespaced as `<date>-<streamer>.mp4` so the flat
    downloads/ dir can't collide across channels that streamed the same date
    (plain `<date>.mp4` when there's no streamer, e.g. a raw m3u8 URL)."""
    return f"{vod_date}-{streamer}.mp4" if streamer else f"{vod_date}.mp4"


def _make_download_cb(progress, label: str = "Downloading VOD"):
    """Build an ffmpeg-progress callback for an m3u8 pull. Reports % of the VOD
    (content time / known duration), MB downloaded, MB/s, and a sustained-rate
    ETA — to the GUI feed when one's attached, else a throttled CLI line.

    The total file size of an m3u8 isn't known until the end, so % comes from the
    muxed timestamp over the VOD's duration; the ETA projects the average rate so
    far forward ("assuming the average rate is sustained")."""
    state = {"last_cli": -10.0}

    def cb(info: dict) -> None:
        downloaded_mb = (info.get("bytes") or 0) / 1e6
        elapsed = info.get("elapsed") or 0.0
        out_time = info.get("out_time")
        duration = info.get("duration")
        rate = downloaded_mb / elapsed if elapsed > 0 else 0.0
        frac = None
        parts = [f"{downloaded_mb:.0f} MB", f"{rate:.1f} MB/s"]
        if out_time and duration:
            frac = max(0.0, min(1.0, out_time / duration))
            parts.append(f"{frac * 100:.0f}% of VOD "
                         f"({fmt_seconds(out_time)} / {fmt_seconds(duration)})")
            if frac > 0.02:
                eta = elapsed * (1.0 - frac) / frac
                parts.append(f"~{fmt_seconds(eta)} left")
        detail = " · ".join(parts)
        if progress.feed_attached:
            progress.sub(label, fraction=frac, detail=detail)
        elif elapsed - state["last_cli"] >= 2.0:
            state["last_cli"] = elapsed
            print(f"\r    {label}: {detail}          ", end="", flush=True)

    return cb


def _today_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _file_size_mb(path: str) -> float:
    """Size of a file in MB. 0.0 if missing — caller can sum freely."""
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0


def _is_inside(path: str, parent: str) -> bool:
    """True if `path` resolves under `parent` (used to gate cleanup of
    user-owned local sources — we only delete files we created)."""
    try:
        abs_path = os.path.realpath(path)
        abs_parent = os.path.realpath(parent)
    except OSError:
        return False
    rel = os.path.relpath(abs_path, abs_parent)
    return not rel.startswith("..") and not os.path.isabs(rel)


def _dir_size_mb(path: str) -> float:
    """Recursive size of `path` in MB. 0.0 if missing."""
    total = 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        return 0.0
    return total / (1024 * 1024)


def _cleanup_source_artifacts(
    original_video_path: str,
    windowed_video_path: str | None,
    wav_path: str,
    download_dir: str,
) -> float:
    """Delete downloaded VOD, windowed trim, and WAV after a successful run.

    Returns total MB freed. Skips any path that lives outside `download_dir`
    so a user's local source file is never touched. Keeps the transcript JSON
    so a later `--force` re-run can skip Whisper after re-downloading.
    """
    candidates = []
    if windowed_video_path and windowed_video_path != original_video_path:
        candidates.append(windowed_video_path)
    candidates.append(original_video_path)
    candidates.append(wav_path)

    freed = 0.0
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        if not _is_inside(path, download_dir):
            # User-owned (e.g. source_type=local .mp4 outside download_dir).
            continue
        size = _file_size_mb(path)
        try:
            os.remove(path)
            freed += size
        except OSError as e:
            print(f"    cleanup: could not remove {path}: {e}")
    return freed


def _estimate_total_runtime(video_path: str, cfg: dict) -> float | None:
    """Best-effort wall-clock estimate for the whole pipeline, in seconds.

    Returns None if we can't probe the source. The estimate drives the
    `overall N%` display so the user sees real progress vs expected time.
    """
    factor = float(cfg.get("runtime_estimate_factor") or 0.0)
    if factor <= 0:
        # Auto-pick by device. CPU Whisper is roughly 10× slower than CUDA.
        factor = 1.5 if cfg.get("whisper_device") == "cpu" else 0.15
    try:
        duration = _probe_duration(video_path)
    except Exception:
        return None
    return duration * factor


def _export_avifs(extracted: list, cfg: dict, progress, manifest_path: str = None) -> None:
    """Encode the run's clips to AVIF. Used in the normal flow AND on a cache-hit
    re-run (so re-running with a different --avif-target still produces new files
    instead of being swallowed by the skip-guard). Each variant has a distinct
    suffix (-opt/-not or -<N>mb), so different sizes never collide. Attaches the
    paths to `extracted` (merging, so multiple sizes accumulate) and rewrites the
    manifest when `manifest_path` is given."""
    from modules.avif_exporter import clips_from_manifest, export_clips_to_avif
    with progress.phase("avif", "Exporting AVIFs (AvifTools)", spinner=False):
        avif_source = cfg.get("avif_source", "captioned")
        clip_files = clips_from_manifest(extracted, avif_source)
        avif_dir = os.path.join(
            cfg["output_dir"], "avif" if avif_source == "captioned" else "avif-clean")

        def _avif_prog(done, total, label):
            progress.sub("Exporting AVIFs",
                         fraction=(done / total if total else None), detail=label)

        avif_results = export_clips_to_avif(clip_files, avif_dir, cfg, on_progress=_avif_prog)
        by_name = {r["name"]: r for r in avif_results}
        for item in extracted:
            base = os.path.splitext(
                os.path.basename(item.get("captioned") or item.get("file") or ""))[0]
            if base.endswith("_captioned"):
                base = base[: -len("_captioned")]
            if base in by_name:
                item.setdefault("avif", {}).update(by_name[base]["files"])
        made = sum(len(r.get("files") or {}) for r in avif_results)
        print(f"    Exported {made} AVIFs -> {avif_dir}")
    if manifest_path:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(extracted, f, indent=2)


def run(cfg: dict = None) -> list:
    if cfg is None:
        cfg = load_config()

    # First-pass per-streamer namespacing (date appended after we resolve the VOD)
    streamer = _streamer_subdir(cfg)
    base_download_dir = cfg["download_dir"]
    base_output_dir = cfg["output_dir"]
    cfg = dict(cfg)
    if streamer:
        cfg["download_dir"] = os.path.join(base_download_dir, streamer)
        cfg["output_dir"] = os.path.join(base_output_dir, streamer)

    verbose = bool(cfg.get("verbose", False))
    quiet = not verbose
    weights = dict(DEFAULT_PHASE_WEIGHTS)
    if cfg.get("avif_export"):
        weights["avif"] = 0.04   # adds an 8th phase to the overall-% display
    progress = Progress(verbose=verbose, weights=weights)

    # Step 1: Resolve source (per-VOD-date cache) + optional time-window trim
    with progress.phase("source", "Resolving source"):
        source = cfg["source_type"]
        os.makedirs(cfg["download_dir"], exist_ok=True)

        if source == "vodvod":
            m3u8_url, vod_date, vod_title, vod_duration = get_latest_vodvod_m3u8(cfg["vodvod_channel"])
            length = f", {fmt_seconds(vod_duration)}" if vod_duration else ""
            print(f"    Latest VOD: {vod_title or '(untitled)'}  ({vod_date}{length})")
            if verbose:
                print(f"    m3u8: {m3u8_url}")
            video_path = os.path.join(cfg["download_dir"], _video_filename(vod_date, streamer))
            if _cached_video_ok(video_path):
                print(f"    Using cached video: {video_path}")
            else:
                progress.sub(f"Found VOD: {vod_title} ({vod_date}) — downloading"
                             if vod_title else f"Found VOD {vod_date} — downloading")
                stream_m3u8_to_file(m3u8_url, video_path, quiet=quiet,
                                    duration=vod_duration,
                                    on_progress=_make_download_cb(progress))
                if not progress.feed_attached:
                    print()  # close the \r download line
        elif source == "kick":
            m3u8_url, vod_date = get_latest_kick_vod_m3u8(cfg["kick_channel"])
            if verbose:
                print(f"    Latest VOD: {vod_date}  m3u8: {m3u8_url}")
            video_path = os.path.join(cfg["download_dir"], _video_filename(vod_date, streamer))
            if _cached_video_ok(video_path):
                print(f"    Using cached video: {video_path}")
            else:
                progress.sub(f"Found VOD {vod_date} — downloading")
                stream_m3u8_to_file(m3u8_url, video_path, quiet=quiet,
                                    on_progress=_make_download_cb(progress))
                if not progress.feed_attached:
                    print()  # close the \r download line
        elif source == "twitch":
            progress.sub("Found Twitch VOD — downloading")
            with progress.download_monitor(cfg["download_dir"]):
                video_path, vod_date = download_twitch_vod(
                    cfg["twitch_vod_url"], cfg["quality"], cfg["download_dir"], quiet=quiet
                )
        elif source == "m3u8":
            vod_date = _today_iso()
            video_path = os.path.join(cfg["download_dir"], f"{vod_date}.mp4")
            if not _cached_video_ok(video_path):
                progress.sub("Downloading stream")
                with progress.download_monitor(video_path):
                    stream_m3u8_to_file(cfg["m3u8_url"], video_path, quiet=quiet)
        elif source == "local":
            if not cfg.get("local_path"):
                raise ValueError("source_type='local' requires --path or local_path in config")
            video_path, vod_date = resolve_local_file(cfg["local_path"], cfg["download_dir"], quiet=quiet)
            if verbose:
                print(f"    Local source: {video_path}  (vod_date={vod_date})")
        else:
            raise ValueError(f"Unknown source_type: '{source}'")

        # Capture the source path *before* any window trim — cleanup needs to
        # delete both the original download and any windowed derivative.
        original_video_path = video_path

        # Optional time window — trim the source to [start_time, end_time] before
        # any downstream work. The whole pipeline then sees a shorter video and
        # behaves identically, so no time-offset bookkeeping is needed.
        start_str = cfg.get("start_time") or None
        end_str = cfg.get("end_time") or None
        windowed = bool(start_str or end_str)
        if windowed:
            print(f"    Trimming to window: start={start_str or '0'}  end={end_str or 'EOF'}")
            video_path = apply_time_window(
                video_path, start_str, end_str, cfg["download_dir"], quiet=quiet
            )

    # Now that the source (post-window) is on disk we know its duration, so we
    # can anchor a real wall-clock estimate. The download time is already in
    # `total_elapsed()`, so the model adds it to the remaining-phases estimate
    # (and re-anchors at every phase boundary). See modules/timing.py.
    run_duration = None
    try:
        run_duration = _probe_duration(video_path)
    except Exception:
        run_duration = None
    if run_duration:
        override = float(cfg.get("runtime_estimate_factor") or 0.0)
        if override > 0:
            # Legacy escape hatch: a fixed factor, but still anchored on elapsed.
            progress.set_estimated_total(progress.total_elapsed() + run_duration * override)
        else:
            from modules.timing import build_phase_plan, factors_for
            progress.set_time_model(run_duration, build_phase_plan(cfg, factors_for(cfg)))
        print(f"    Expected total runtime: ~{fmt_seconds(progress.estimated_total())} "
              f"(refines as it runs)")

    # Output dir: <base>/<streamer>/<vod_date>[_w<start>-<end>]/
    # When a window is applied we suffix the subdir with the same `_w<s>-<e>`
    # tag that `apply_time_window` puts on the trimmed file, so two different
    # windows on the same VOD-date don't trip each other's manifest skip-guard.
    output_subdir = vod_date
    if windowed:
        win_basename = os.path.splitext(os.path.basename(video_path))[0]
        m = re.search(r"_w\d+-\d+$", win_basename)
        if m:
            output_subdir += m.group(0)
    cfg["output_dir"] = (
        os.path.join(base_output_dir, streamer, output_subdir) if streamer
        else os.path.join(base_output_dir, output_subdir)
    )

    # Skip guard: if a non-empty manifest already exists for this VOD-date, no-op.
    manifest_path = os.path.join(cfg["output_dir"], "clips_manifest.json")
    if not cfg.get("force") and os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, list) and existing:
                if cfg.get("avif_export"):
                    # Clips are cached, but the user asked for AVIFs (likely a
                    # different --avif-target) — make those from the existing
                    # clips instead of no-op'ing. Distinct suffixes mean new
                    # sizes never overwrite old ones.
                    print(f"\n[skip] {manifest_path} already has {len(existing)} clips — "
                          f"exporting the requested AVIFs from them (use --force to redo clips).")
                    _export_avifs(existing, cfg, progress, manifest_path)
                else:
                    print(f"\n[skip] {manifest_path} already has {len(existing)} clips. Use --force to regenerate.")
                return existing
        except (OSError, json.JSONDecodeError):
            pass

    # Anchor derived artifacts. `local` source without a window may have a
    # video_path outside download_dir (we don't copy the user's file); send
    # derived files into download_dir to avoid polluting the user's directory.
    if source == "local" and not windowed:
        os.makedirs(cfg["download_dir"], exist_ok=True)
        derived_base = os.path.join(cfg["download_dir"], vod_date)
    else:
        derived_base = os.path.splitext(video_path)[0]
    wav_path = derived_base + ".wav"
    transcript_path = os.path.splitext(wav_path)[0] + ".transcript.json"

    # Step 2: Extract audio
    with progress.phase("audio", "Extracting audio"):
        if os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
            print(f"    Using cached audio: {wav_path}")
        else:
            extract_audio(video_path, wav_path, quiet=quiet)

    # Step 3: Transcribe (cache transcript JSON next to the wav)
    # spinner=False: Whisper prints its own per-segment progress bar.
    with progress.phase("transcribe", "Transcribing with Whisper", spinner=False):
        if os.path.exists(transcript_path) and os.path.getsize(transcript_path) > 0:
            with open(transcript_path, encoding="utf-8") as f:
                segments = json.load(f)
            print(f"    Using cached transcript: {len(segments)} segments")
        else:
            segments = transcribe(
                wav_path, cfg["whisper_model"], cfg["whisper_device"], verbose=verbose
            )
            with open(transcript_path, "w", encoding="utf-8") as f:
                json.dump(segments, f)
            if verbose:
                print(f"    {len(segments)} segments transcribed")

    # Step 4: Clip selection (LLM, or transcript phrase scan, or music peaks)
    # spinner=False: select_highlights drives its own tqdm bar via progress.iter.
    if cfg.get("clip_mode") == "phrase":
        sel_label = f"Scanning transcript for '{cfg.get('trigger_phrase', 'clip it')}'"
    elif cfg.get("clip_mode") == "music":
        sel_label = "Detecting musical onsets"
    else:
        sel_label = "LLM highlight selection"
    with progress.phase("llm", sel_label, spinner=False):
        clips = select_highlights(segments, cfg, wav_path=wav_path, progress=progress)
        if verbose:
            print(f"    {len(clips)} clip candidates identified")

    # Step 5: Audio peak cross-reference + min_clips top-up
    with progress.phase("peaks", "Cross-referencing audio peaks"):
        peaks = get_audio_peaks(wav_path)
        for clip in clips:
            for peak in peaks:
                if peak["start"] <= clip["start"] <= peak["end"]:
                    clip["score"] = min(1.0, clip.get("score", 0) + 0.1)
        clips.sort(key=lambda x: x.get("score", 0), reverse=True)
        # Phrase mode is "catch every time I said the trigger" — don't truncate
        # or top up; the whole point is fidelity to the streamer's marks.
        if cfg.get("clip_mode") != "phrase":
            max_clips = int(cfg["max_clips"])
            min_clips_cfg = int(cfg.get("min_clips") or 0)
            min_clips = min_clips_cfg if min_clips_cfg > 0 else max(1, max_clips // 2)
            min_clips = min(min_clips, max_clips)
            if len(clips) < min_clips and cfg.get("clip_mode") != "music":
                # LLM under-delivered: top up with musical onsets so the user
                # gets at least `min_clips`. Music mode already drew from
                # peaks, so topping up there would be redundant.
                before = len(clips)
                clips = top_up_with_music_peaks(clips, wav_path, cfg, target_count=min_clips)
                added = len(clips) - before
                if added > 0:
                    print(f"    LLM returned {before} clips (<{min_clips} floor) — added {added} music-peak candidates")
                clips.sort(key=lambda x: x.get("score", 0), reverse=True)
            clips = clips[: max_clips]

    # Step 6: Extract clips
    # spinner=False: batch_extract drives its own per-clip tqdm bar.
    with progress.phase("clip", "Cutting clips with FFmpeg", spinner=False):
        extracted = batch_extract(video_path, clips, cfg, progress=progress, streamer=streamer)

    # Step 7: Captioned variant (CapCut-style burned-in subtitles)
    if cfg.get("burn_subtitles", True):
        # spinner=False: the caption loop drives its own per-item tqdm bar.
        with progress.phase("caption", "Burning CapCut-style captions", spinner=False):
            padding = float(cfg.get("clip_padding_seconds", 3.0))
            for item in progress.iter(extracted, total=len(extracted), desc="captions"):
                meta = item["meta"]
                captioned = os.path.splitext(item["file"])[0] + "_captioned.mp4"
                try:
                    caption_clip(
                        item["file"], captioned, segments,
                        meta["start"], meta["end"], padding, quiet=quiet,
                    )
                    item["captioned"] = captioned
                except Exception as e:
                    print(f"    captioning failed for {item['file']}: {type(e).__name__}: {e}")
    else:
        print("[7/7] Skipping captions (burn_subtitles disabled).")

    # Optional AVIF export — encode finished clips to Discord-friendly animated
    # AVIFs via the AvifTools module (auto-installed on first use). Each clip ->
    # <streamer>-<rand>-not.avif (high-q 480p60) + <streamer>-<rand>-opt.avif.
    if cfg.get("avif_export"):
        _export_avifs(extracted, cfg, progress)

    # Save manifest
    os.makedirs(cfg["output_dir"], exist_ok=True)
    manifest_path = os.path.join(cfg["output_dir"], "clips_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(extracted, f, indent=2)

    # Step 8: Reclaim disk — delete the multi-GB VOD + WAV now that clips are
    # produced. Transcript JSON stays (small + cheap re-run if --force later).
    # Manifest cache-hit path returned earlier, so we only get here on a real
    # run that actually produced clips.
    if cfg.get("cleanup_source", True):
        freed_mb = _cleanup_source_artifacts(
            original_video_path,
            video_path if windowed else None,
            wav_path,
            cfg["download_dir"],
        )
        if freed_mb > 0:
            print(f"   Cleaned up source files: freed {freed_mb:.1f} MB "
                  f"(pass --keep-vod to retain)")

    # Learn this run's per-phase factors so the next estimate is tighter.
    if run_duration:
        try:
            from modules.timing import record_run
            record_run(cfg, run_duration, progress.phase_times)
        except Exception:
            pass

    clips_dir_mb = _dir_size_mb(cfg["output_dir"])
    print(f"\nDone. {len(extracted)} clips ({clips_dir_mb:.1f} MB) -> {cfg['output_dir']}")
    print(f"   Manifest: {manifest_path}")
    print(f"   Total time: {fmt_seconds(progress.total_elapsed())}")
    return extracted


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="VOD Clip Pipeline")
    p.add_argument("--config", help="Path to JSON config file")
    p.add_argument("--source-type", choices=["twitch", "vodvod", "m3u8", "kick", "local"])
    p.add_argument("--url", help="Twitch VOD URL or direct m3u8 URL")
    p.add_argument("--path", help="Path to a local .mp4 or .ts file (use with --source-type local)")
    p.add_argument("--channel", help="vodvod.top or kick.com channel handle")
    p.add_argument("--start-time", help="Trim the source to start at this point (HH:MM:SS, MM:SS, or seconds)")
    p.add_argument("--end-time", help="Trim the source to end at this point (HH:MM:SS, MM:SS, or seconds)")
    p.add_argument("--clip-mode", choices=["reaction", "dance", "hype", "all", "phrase", "music"])
    p.add_argument("--trigger-phrase",
                   help="Phrase that marks a clip when clip-mode=phrase (default: 'clip it')")
    p.add_argument("--phrase-pre", type=float,
                   help="Seconds of context before the trigger phrase (default: 60)")
    p.add_argument("--phrase-post", type=float,
                   help="Seconds of context after the trigger phrase (default: 60)")
    p.add_argument("--max-clips", type=int)
    p.add_argument("--min-clips", type=int,
                   help="Floor on returned clips. Defaults to max_clips // 2. "
                        "If the LLM returns fewer, tops up from musical onsets.")
    p.add_argument("--llm-backend", choices=["ollama", "openai"])
    p.add_argument("--model", help="Ollama or OpenAI model name")
    p.add_argument("--force", action="store_true",
                   help="Regenerate clips even if a manifest already exists for this VOD-date")
    p.add_argument("--keep-vod", dest="keep_vod", action="store_true",
                   help="Keep the downloaded VOD + WAV after a successful run "
                        "(default: delete to reclaim disk; transcript JSON is always kept)")
    p.add_argument("--verbose", action="store_true",
                   help="Show full subprocess output and per-chunk LLM logs (default: compact progress display)")
    p.add_argument("--avif", action="store_true",
                   help="Also export each clip to Discord-friendly animated AVIFs "
                        "(<streamer>-<rand>-not/opt.avif) via the AvifTools module")
    p.add_argument("--avif-source", choices=["captioned", "raw"],
                   help="Which clip to encode to AVIF (default: captioned)")
    p.add_argument("--avif-target", type=float,
                   help="Target AVIF size in MB — one <streamer>-<rand>-<N>mb.avif per "
                        "clip (auto bitrate + downscale) instead of the quality not/opt pair")
    return p


if __name__ == "__main__":
    parser = _build_arg_parser()
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.source_type:
        cfg["source_type"] = args.source_type
    if args.url:
        if cfg["source_type"] == "twitch":
            cfg["twitch_vod_url"] = args.url
        else:
            cfg["m3u8_url"] = args.url
    if args.path:
        cfg["local_path"] = args.path
    if args.start_time:
        cfg["start_time"] = args.start_time
    if args.end_time:
        cfg["end_time"] = args.end_time
    if args.channel:
        if cfg["source_type"] == "kick":
            cfg["kick_channel"] = args.channel
        else:
            cfg["vodvod_channel"] = args.channel
    if args.clip_mode:
        cfg["clip_mode"] = args.clip_mode
    if args.trigger_phrase:
        cfg["trigger_phrase"] = args.trigger_phrase
    if args.phrase_pre is not None:
        cfg["phrase_pre_seconds"] = args.phrase_pre
    if args.phrase_post is not None:
        cfg["phrase_post_seconds"] = args.phrase_post
    if args.max_clips:
        cfg["max_clips"] = args.max_clips
    if args.min_clips is not None:
        cfg["min_clips"] = args.min_clips
    if args.llm_backend:
        cfg["llm_backend"] = args.llm_backend
    if args.model:
        key = "openai_model" if cfg["llm_backend"] == "openai" else "ollama_model"
        cfg[key] = args.model
    if args.force:
        cfg["force"] = True
    if args.keep_vod:
        cfg["cleanup_source"] = False
    if args.verbose:
        cfg["verbose"] = True
    if args.avif:
        cfg["avif_export"] = True
    if args.avif_source:
        cfg["avif_source"] = args.avif_source
    if args.avif_target is not None:
        cfg["avif_export"] = True
        cfg["avif_target_mb"] = args.avif_target

    run(cfg)
