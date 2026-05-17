import json
import os
from pathlib import Path

DEFAULT_CONFIG = {
    # Source
    "source_type": "twitch",          # "twitch" | "m3u8" | "vodvod" | "kick" | "local"
    "twitch_vod_url": "",
    "vodvod_channel": "",             # e.g. "@your_channel"
    "kick_channel": "",               # e.g. "your_channel"
    "m3u8_url": "",
    "local_path": "",                 # path to a local .mp4 or .ts file (source_type="local")

    # Time window (optional) — restrict the pipeline to a sub-range of the source.
    # Accepts "HH:MM:SS", "MM:SS", or bare seconds. Either may be empty for "start"
    # or "end" of the video respectively.
    "start_time": "",                 # e.g. "1:00:00"
    "end_time": "",                   # e.g. "1:30:00"

    # Download
    "quality": "720p",                # "best" | "1080p" | "720p" | "480p"
    "download_dir": "./downloads",

    # Transcription
    "whisper_model": "large-v3",      # tiny / base / small / medium / large-v3
    "whisper_device": "cuda",         # "cuda" or "cpu"
    "whisper_language": "en",

    # LLM
    "llm_backend": "ollama",          # "ollama" | "openai"
    "ollama_model": "qwen2.5:14b",
    "openai_model": "gpt-4o-mini",
    "openai_api_key": "",
    "llm_timeout_seconds": 300,  # per-chunk timeout — guards against hung reasoning models

    # Clip Detection
    "clip_mode": "reaction",          # "reaction" | "dance" | "hype" | "all" | "phrase"
    "max_clips": 10,
    "min_clip_duration": 8,
    "max_clip_duration": 45,
    "clip_padding_seconds": 3,

    # Phrase mode — when clip_mode == "phrase", skip the LLM and cut a window
    # around every spot where the streamer says `trigger_phrase`. Lets you
    # "voice-mark" clips mid-stream instead of pressing hotkeys.
    "trigger_phrase": "clip it",      # case-insensitive substring match
    "phrase_pre_seconds": 60.0,       # seconds of context BEFORE the phrase
    "phrase_post_seconds": 60.0,      # seconds of context AFTER the phrase

    # Output
    "output_dir": "./clips",
    "burn_subtitles": True,

    # Display
    "verbose": False,                 # show full subprocess / per-chunk log spam

    # Runtime estimate (controls the overall-% display).
    # Expected wall-clock = source_duration * factor. Heuristic defaults:
    # CUDA ≈ 0.15, CPU ≈ 1.5 (Whisper is the dominant phase). Override here if
    # your hardware is noticeably faster or slower than the defaults assume.
    "runtime_estimate_factor": 0.0,   # 0 = auto-pick from whisper_device
}

CONFIG = DEFAULT_CONFIG.copy()


def load_config(path: str = None) -> dict:
    cfg = DEFAULT_CONFIG.copy()

    if path and Path(path).exists():
        with open(path) as f:
            overrides = json.load(f)
        cfg.update(overrides)

    # VOD_CLIP_<KEY> env var overrides
    for key, default_val in DEFAULT_CONFIG.items():
        env_key = f"VOD_CLIP_{key.upper()}"
        if env_key in os.environ:
            val = os.environ[env_key]
            if isinstance(default_val, bool):
                val = val.lower() in ("1", "true", "yes")
            elif isinstance(default_val, int):
                val = int(val)
            elif isinstance(default_val, float):
                val = float(val)
            cfg[key] = val

    return cfg
