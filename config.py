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
    "clip_mode": "reaction",          # "reaction" | "dance" | "hype" | "all"
    "max_clips": 10,
    "min_clip_duration": 8,
    "max_clip_duration": 45,
    "clip_padding_seconds": 3,

    # Output
    "output_dir": "./clips",
    "burn_subtitles": True,
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
