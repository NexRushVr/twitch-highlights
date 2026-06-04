"""Self-calibrating runtime estimation for the pipeline.

The old estimate was a single `duration * 0.15` set *after* the download — which
ignored the (often multi-minute) download itself and badly under-weighted the LLM
phase, so the ETA routinely hit 0 with the job still running.

This models each phase's time as `duration * factor`, has the pipeline anchor the
total on the ACTUAL elapsed time at every phase boundary (so the download counts
and drift self-corrects), and learns the factors from your own runs (EMA, keyed
by whisper/LLM model + device) so the estimate tightens over time.
"""

from __future__ import annotations

import json
import os

# Per-phase time as a fraction of the source DURATION (wall-clock seconds per
# second of video). Seeded from measured runs (medium Whisper + qwen2.5:14b on a
# fast GPU): the LLM and transcription dominate; the rest are small. `source`
# (the download) is intentionally absent — once it finishes it's part of the
# measured elapsed time, never estimated ahead.
DEFAULT_FACTORS = {
    "audio": 0.001,
    "transcribe": 0.08,
    "llm": 0.17,
    "peaks": 0.003,
    "clip": 0.015,
    "caption": 0.02,
    "avif": 0.04,
}

_EMA_ALPHA = 0.4   # weight of the newest run when updating a learned factor


def _store_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "logs", "timing_calibration.json")


def _profile_key(cfg: dict) -> str:
    """A run's timing depends on the heavy models + device, so calibrate per
    (device, whisper model, LLM backend, LLM model)."""
    backend = cfg.get("llm_backend", "ollama")
    model_key = "openai_model" if backend == "openai" else "ollama_model"
    return "|".join([
        str(cfg.get("whisper_device", "")),
        str(cfg.get("whisper_model", "")),
        str(backend),
        str(cfg.get(model_key, "")),
    ])


def _load() -> dict:
    try:
        with open(_store_path(), encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_store_path()), exist_ok=True)
        with open(_store_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def factors_for(cfg: dict) -> dict:
    """Per-phase factors, blending defaults with what we've learned for this
    machine/model profile."""
    factors = dict(DEFAULT_FACTORS)
    learned = _load().get(_profile_key(cfg), {}).get("factors", {})
    for k, v in learned.items():
        if isinstance(v, (int, float)) and v > 0:
            factors[k] = float(v)
    return factors


def build_phase_plan(cfg: dict, factors: dict) -> list:
    """Ordered [(key, factor)] for the phases that will actually run. `source` is
    included with factor 0 — its time is measured, not estimated ahead."""
    plan = [
        ("source", 0.0),
        ("audio", factors["audio"]),
        ("transcribe", factors["transcribe"]),
        ("llm", factors["llm"]),
        ("peaks", factors["peaks"]),
        ("clip", factors["clip"]),
    ]
    if cfg.get("burn_subtitles", True):
        plan.append(("caption", factors["caption"]))
    if cfg.get("avif_export"):
        plan.append(("avif", factors["avif"]))
    return plan


def record_run(cfg: dict, duration: float, phase_times: dict) -> None:
    """After a real run, fold the observed per-phase factors into the learned
    store (EMA). `phase_times`: {phase_key: seconds}. The download (`source`) is
    skipped — it scales with file size / network, not video duration."""
    if not duration or duration <= 0 or not phase_times:
        return
    data = _load()
    key = _profile_key(cfg)
    entry = data.get(key, {"factors": {}, "runs": 0})
    factors = entry.get("factors", {})
    for phase, secs in phase_times.items():
        if phase == "source" or not secs or secs <= 0:
            continue
        observed = secs / duration
        prev = factors.get(phase)
        factors[phase] = observed if prev is None else \
            (1 - _EMA_ALPHA) * prev + _EMA_ALPHA * observed
    entry["factors"] = factors
    entry["runs"] = int(entry.get("runs", 0)) + 1
    data[key] = entry
    _save(data)
