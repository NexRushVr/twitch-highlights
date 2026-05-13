import argparse
import json
import os

from config import load_config
from modules.source_resolver import (
    download_twitch_vod,
    get_latest_vodvod_m3u8,
    get_latest_kick_vod_m3u8,
    resolve_local_file,
    stream_m3u8_to_file,
)
from modules.audio_extractor import extract_audio, get_audio_peaks
from modules.transcriber import transcribe
from modules.highlight_selector import select_highlights
from modules.clip_extractor import batch_extract
from modules.subtitle_burner import caption_clip


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

    # Step 1: Resolve source (per-VOD-date cache)
    print("[1/7] Resolving source...")
    source = cfg["source_type"]
    os.makedirs(cfg["download_dir"], exist_ok=True)

    if source == "vodvod":
        m3u8_url, vod_date = get_latest_vodvod_m3u8(cfg["vodvod_channel"])
        print(f"    Latest VOD: {vod_date}  m3u8: {m3u8_url}")
        video_path = os.path.join(cfg["download_dir"], f"{vod_date}.mp4")
        if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
            print(f"    Using cached video: {video_path}")
        else:
            stream_m3u8_to_file(m3u8_url, video_path)
    elif source == "kick":
        m3u8_url, vod_date = get_latest_kick_vod_m3u8(cfg["kick_channel"])
        print(f"    Latest VOD: {vod_date}  m3u8: {m3u8_url}")
        video_path = os.path.join(cfg["download_dir"], f"{vod_date}.mp4")
        if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
            print(f"    Using cached video: {video_path}")
        else:
            stream_m3u8_to_file(m3u8_url, video_path)
    elif source == "twitch":
        video_path, vod_date = download_twitch_vod(
            cfg["twitch_vod_url"], cfg["quality"], cfg["download_dir"]
        )
    elif source == "m3u8":
        vod_date = _today_iso()
        video_path = os.path.join(cfg["download_dir"], f"{vod_date}.mp4")
        if not (os.path.exists(video_path) and os.path.getsize(video_path) > 0):
            stream_m3u8_to_file(cfg["m3u8_url"], video_path)
    elif source == "local":
        if not cfg.get("local_path"):
            raise ValueError("source_type='local' requires --path or local_path in config")
        video_path, vod_date = resolve_local_file(cfg["local_path"], cfg["download_dir"])
        print(f"    Local source: {video_path}  (vod_date={vod_date})")
    else:
        raise ValueError(f"Unknown source_type: '{source}'")

    # Output dir: <base>/<streamer>/<vod_date>/
    cfg["output_dir"] = (
        os.path.join(base_output_dir, streamer, vod_date) if streamer
        else os.path.join(base_output_dir, vod_date)
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

    # Step 2: Extract audio (skip if cached)
    # For `local` source the video may live outside download_dir (we don't copy
    # the user's file). Anchor derived artifacts to download_dir in that case so
    # we don't pollute the user's source directory.
    print("[2/7] Extracting audio...")
    if source == "local":
        os.makedirs(cfg["download_dir"], exist_ok=True)
        derived_base = os.path.join(cfg["download_dir"], vod_date)
    else:
        derived_base = os.path.splitext(video_path)[0]
    wav_path = derived_base + ".wav"
    if os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
        print(f"    Using cached audio: {wav_path}")
    else:
        extract_audio(video_path, wav_path)

    # Step 3: Transcribe (cache transcript JSON next to the wav)
    print("[3/7] Transcribing with Whisper...")
    transcript_path = os.path.splitext(wav_path)[0] + ".transcript.json"
    if os.path.exists(transcript_path) and os.path.getsize(transcript_path) > 0:
        with open(transcript_path) as f:
            segments = json.load(f)
        print(f"    Using cached transcript: {transcript_path} ({len(segments)} segments)")
    else:
        segments = transcribe(wav_path, cfg["whisper_model"], cfg["whisper_device"])
        with open(transcript_path, "w", encoding="utf-8") as f:
            json.dump(segments, f)
        print(f"    {len(segments)} segments transcribed")

    # Step 4: LLM highlight selection
    print("[4/7] Running LLM highlight selection...")
    clips = select_highlights(segments, cfg)
    print(f"    {len(clips)} clip candidates identified")

    # Step 5: Audio peak cross-reference
    print("[5/7] Cross-referencing audio peaks...")
    peaks = get_audio_peaks(wav_path)
    for clip in clips:
        for peak in peaks:
            if peak["start"] <= clip["start"] <= peak["end"]:
                clip["score"] = min(1.0, clip.get("score", 0) + 0.1)
    clips.sort(key=lambda x: x.get("score", 0), reverse=True)
    clips = clips[: cfg["max_clips"]]

    # Step 6: Extract clips
    print("[6/7] Cutting clips with FFmpeg...")
    extracted = batch_extract(video_path, clips, cfg)

    # Step 7: Captioned variant (CapCut-style burned-in subtitles)
    if cfg.get("burn_subtitles", True):
        print("[7/7] Burning CapCut-style captions...")
        padding = float(cfg.get("clip_padding_seconds", 3.0))
        for item in extracted:
            meta = item["meta"]
            captioned = os.path.splitext(item["file"])[0] + "_captioned.mp4"
            try:
                caption_clip(item["file"], captioned, segments, meta["start"], meta["end"], padding)
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

    print(f"\nDone. {len(extracted)} clips saved to {cfg['output_dir']}/")
    print(f"   Manifest: {manifest_path}")
    return extracted


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="VOD Clip Pipeline")
    p.add_argument("--config", help="Path to JSON config file")
    p.add_argument("--source-type", choices=["twitch", "vodvod", "m3u8", "kick", "local"])
    p.add_argument("--url", help="Twitch VOD URL or direct m3u8 URL")
    p.add_argument("--path", help="Path to a local .mp4 or .ts file (use with --source-type local)")
    p.add_argument("--channel", help="vodvod.top or kick.com channel handle")
    p.add_argument("--clip-mode", choices=["reaction", "dance", "hype", "all"])
    p.add_argument("--max-clips", type=int)
    p.add_argument("--llm-backend", choices=["ollama", "openai"])
    p.add_argument("--model", help="Ollama or OpenAI model name")
    p.add_argument("--force", action="store_true",
                   help="Regenerate clips even if a manifest already exists for this VOD-date")
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

    run(cfg)
