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
    "ollama_model": "gpt-oss:20b",
    "openai_model": "gpt-4o-mini",
    "openai_api_key": "",
    "llm_timeout_seconds": 300,  # per-chunk timeout — guards against hung reasoning models

    # Clip Detection
    "clip_mode": "reaction",          # "reaction" | "dance" | "hype" | "all" | "phrase" | "music"
    "max_clips": 10,
    # Floor on returned clips. 0 = auto = max_clips // 2. If the chosen
    # selector returns fewer than this, the pipeline tops up from music-peak
    # candidates so a quiet-LLM-day still produces a usable reel.
    "min_clips": 0,
    "min_clip_duration": 8,
    "max_clip_duration": 45,
    "clip_padding_seconds": 3,

    # Music mode (also used for top-up when min_clips isn't met) — window
    # built around each detected musical onset and the minimum gap between
    # successive picks so two drops in the same chorus don't both qualify.
    "music_peak_pre_seconds": 5.0,
    "music_peak_post_seconds": 12.0,
    "music_peak_min_gap_seconds": 20.0,

    # Phrase mode — when clip_mode == "phrase", skip the LLM and cut a window
    # around every spot where the streamer says `trigger_phrase`. Lets you
    # "voice-mark" clips mid-stream instead of pressing hotkeys.
    "trigger_phrase": "clip it",      # case-insensitive substring match
    "phrase_pre_seconds": 60.0,       # seconds of context BEFORE the phrase
    "phrase_post_seconds": 60.0,      # seconds of context AFTER the phrase

    # Output
    "output_dir": "./clips",
    "burn_subtitles": True,

    # AVIF export — encode finished clips to small, Discord-friendly animated
    # AVIFs using the AvifTools module (github.com/NexRushVr/optimized-discord-gifs-avif),
    # which is auto-installed/imported on first use. Each clip yields two files:
    #   <streamer>-<rand>-not.avif  high quality (480p60, low CRF — "not optimized")
    #   <streamer>-<rand>-opt.avif  optimized   (480p30, higher CRF — small for chat)
    "avif_export": False,             # also export AVIFs after a run
    "avif_source": "captioned",       # "captioned" | "raw" — which clip to encode
    "avif_max_width": 854,            # downscale cap in px (854 ≈ 480p for 16:9)
    "avif_hq_crf": 18,                # "-not" (high quality) CRF (lower = better)
    "avif_hq_fps": 60,                # "-not" frame rate
    "avif_opt_crf": 30,               # "-opt" (optimized) CRF
    "avif_opt_fps": 30,               # "-opt" frame rate
    "avif_preset": 6,                 # SVT-AV1 speed/quality (0 slow/best .. 13 fast)
    "avif_module_path": "",           # optional explicit path to the AvifTools module
    # Target-size mode: when > 0, encode ONE <streamer>-<rand>-<N>mb.avif per clip
    # aimed under N MB (auto bitrate + downscale/fps-drop within the floors below),
    # instead of the fixed-quality not/opt pair. Uses AvifTools' -TargetSizeMB.
    "avif_target_mb": 0,              # 0 = quality mode (not+opt); e.g. 10 = aim < 10 MB
    "avif_levers": "Quality,Resolution,Fps",  # knobs target mode may turn (subset locks rest)
    "avif_min_width": 480,            # target-mode resolution floor (px)
    "avif_min_fps": 24,               # target-mode frame-rate floor

    # Chat signal — use the VOD's chat replay (Twitch GraphQL / Kick API) as a
    # cheap, crowd-sourced highlight cue. Velocity + hype-emote spikes become clip
    # candidates and a score boost. Only works for Twitch VODs still up + Kick;
    # any failure falls back to the audio+LLM flow. vodvod is unsupported (its IDs
    # aren't Twitch video IDs and those VODs are usually gone from Twitch).
    "use_chat_signal": True,          # fetch chat + detect spikes when supported
    "chat_bucket_seconds": 5.0,       # velocity bucket width
    "chat_spike_z": 2.5,              # z-score over baseline to call a spike
    "chat_spike_min_messages": 5,     # min raw msgs in a bucket to qualify
    "chat_pre_seconds": 6.0,          # padding before a spike window
    "chat_post_seconds": 6.0,         # padding after a spike window
    "chat_elevated_fraction": 0.5,    # envelope cut (× std) for density boundaries
    "chat_score_boost": 0.15,         # bump for a clip overlapping a chat spike
    "chat_add_candidates": True,      # fold chat spikes in as their own candidates
    "chat_max_candidates": 0,         # 0 = max_clips × 3
    "chat_max_messages": 0,           # 0 = unlimited (cap very long VODs if needed)
    "chat_gate": False,               # Phase 3: only LLM-score transcript near spikes
    "chat_gate_pad_seconds": 15.0,    # window padding when gating the LLM

    # Disk cleanup — after a successful run, delete the downloaded VOD, any
    # windowed trim, and the derived WAV to reclaim multi-GB of space. The
    # transcript JSON is kept (it's small and lets re-runs skip Whisper if you
    # later re-download). Never touches a user's local source file. Skipped
    # when the manifest skip-guard fires (nothing new was produced).
    "cleanup_source": True,

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
        # utf-8-sig: the installer writes config.json via PowerShell, which adds a
        # UTF-8 BOM; a hand-edit in Notepad can too. utf-8-sig strips a leading BOM
        # if present and is a no-op on plain UTF-8, so json.load never sees the BOM
        # bytes (a bare open() defaults to cp1252 on Windows and crashes on them).
        try:
            with open(path, encoding="utf-8-sig") as f:
                overrides = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(
                f"Config file {path} is not valid JSON ({e}). Fix it, or delete it "
                f"and re-run the installer to regenerate a fresh config.json."
            ) from e
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
