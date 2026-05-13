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
from modules.highlight_selector import select_highlights
from modules.clip_extractor import batch_extract
from modules.subtitle_burner import caption_clip
from modules.progress import Progress, fmt_seconds


def _streamer_subdir(cfg: dict) -> str:
    """Return a per-streamer folder name (or empty string if not derivable)."""
    if cfg["source_type"] == "vodvod":
        return cfg.get("vodvod_channel", "").lstrip("@")
    if cfg["source_type"] == "kick":
        return cfg.get("kick_channel", "").lstrip("@")
    return ""


def _today_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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
    progress = Progress(verbose=verbose)

    # Step 1: Resolve source (per-VOD-date cache) + optional time-window trim
    with progress.phase("source", "Resolving source"):
        source = cfg["source_type"]
        os.makedirs(cfg["download_dir"], exist_ok=True)

        if source == "vodvod":
            m3u8_url, vod_date = get_latest_vodvod_m3u8(cfg["vodvod_channel"])
            if verbose:
                print(f"    Latest VOD: {vod_date}  m3u8: {m3u8_url}")
            video_path = os.path.join(cfg["download_dir"], f"{vod_date}.mp4")
            if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                print(f"    Using cached video: {video_path}")
            else:
                stream_m3u8_to_file(m3u8_url, video_path, quiet=quiet)
        elif source == "kick":
            m3u8_url, vod_date = get_latest_kick_vod_m3u8(cfg["kick_channel"])
            if verbose:
                print(f"    Latest VOD: {vod_date}  m3u8: {m3u8_url}")
            video_path = os.path.join(cfg["download_dir"], f"{vod_date}.mp4")
            if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                print(f"    Using cached video: {video_path}")
            else:
                stream_m3u8_to_file(m3u8_url, video_path, quiet=quiet)
        elif source == "twitch":
            video_path, vod_date = download_twitch_vod(
                cfg["twitch_vod_url"], cfg["quality"], cfg["download_dir"], quiet=quiet
            )
        elif source == "m3u8":
            vod_date = _today_iso()
            video_path = os.path.join(cfg["download_dir"], f"{vod_date}.mp4")
            if not (os.path.exists(video_path) and os.path.getsize(video_path) > 0):
                stream_m3u8_to_file(cfg["m3u8_url"], video_path, quiet=quiet)
        elif source == "local":
            if not cfg.get("local_path"):
                raise ValueError("source_type='local' requires --path or local_path in config")
            video_path, vod_date = resolve_local_file(cfg["local_path"], cfg["download_dir"], quiet=quiet)
            if verbose:
                print(f"    Local source: {video_path}  (vod_date={vod_date})")
        else:
            raise ValueError(f"Unknown source_type: '{source}'")

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

    # Now that the source (post-window) is on disk, we can estimate the total
    # wall-clock runtime so the overall-% display reflects elapsed-vs-expected
    # rather than just "fraction of phase weights done."
    estimate = _estimate_total_runtime(video_path, cfg)
    if estimate is not None:
        progress.set_estimated_total(estimate)
        print(f"    Expected total runtime: ~{fmt_seconds(estimate)} (based on source duration)")

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
            with open(manifest_path) as f:
                existing = json.load(f)
            if isinstance(existing, list) and existing:
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
    with progress.phase("transcribe", "Transcribing with Whisper"):
        if os.path.exists(transcript_path) and os.path.getsize(transcript_path) > 0:
            with open(transcript_path) as f:
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

    # Step 4: LLM highlight selection
    with progress.phase("llm", "LLM highlight selection"):
        clips = select_highlights(segments, cfg, progress=progress)
        if verbose:
            print(f"    {len(clips)} clip candidates identified")

    # Step 5: Audio peak cross-reference
    with progress.phase("peaks", "Cross-referencing audio peaks"):
        peaks = get_audio_peaks(wav_path)
        for clip in clips:
            for peak in peaks:
                if peak["start"] <= clip["start"] <= peak["end"]:
                    clip["score"] = min(1.0, clip.get("score", 0) + 0.1)
        clips.sort(key=lambda x: x.get("score", 0), reverse=True)
        clips = clips[: cfg["max_clips"]]

    # Step 6: Extract clips
    with progress.phase("clip", "Cutting clips with FFmpeg"):
        extracted = batch_extract(video_path, clips, cfg, progress=progress)

    # Step 7: Captioned variant (CapCut-style burned-in subtitles)
    if cfg.get("burn_subtitles", True):
        with progress.phase("caption", "Burning CapCut-style captions"):
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

    # Save manifest
    os.makedirs(cfg["output_dir"], exist_ok=True)
    manifest_path = os.path.join(cfg["output_dir"], "clips_manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(extracted, f, indent=2)

    print(f"\nDone. {len(extracted)} clips -> {cfg['output_dir']}")
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
    p.add_argument("--clip-mode", choices=["reaction", "dance", "hype", "all"])
    p.add_argument("--max-clips", type=int)
    p.add_argument("--llm-backend", choices=["ollama", "openai"])
    p.add_argument("--model", help="Ollama or OpenAI model name")
    p.add_argument("--force", action="store_true",
                   help="Regenerate clips even if a manifest already exists for this VOD-date")
    p.add_argument("--verbose", action="store_true",
                   help="Show full subprocess output and per-chunk LLM logs (default: compact progress display)")
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
    if args.max_clips:
        cfg["max_clips"] = args.max_clips
    if args.llm_backend:
        cfg["llm_backend"] = args.llm_backend
    if args.model:
        key = "openai_model" if cfg["llm_backend"] == "openai" else "ollama_model"
        cfg[key] = args.model
    if args.force:
        cfg["force"] = True
    if args.verbose:
        cfg["verbose"] = True

    run(cfg)
