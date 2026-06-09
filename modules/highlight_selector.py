import json
import re
import time
from pathlib import Path

try:
    import ollama
except ImportError:
    ollama = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


# Errors worth retrying — transient network / server hiccups, not auth/format issues
_TRANSIENT_ERROR_MARKERS = ("connection", "timeout", "timed out", "refused", "reset")


def _is_transient_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "connection" in name or "timeout" in name:
        return True
    return any(m in msg for m in _TRANSIENT_ERROR_MARKERS)


def _with_retries(fn, *, attempts: int = 4, base_delay: float = 2.0):
    """Call fn() with exponential backoff on transient errors. Re-raises on final attempt."""
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if not _is_transient_error(e) or i == attempts - 1:
                raise
            delay = base_delay * (2 ** i)
            print(f"      transient {type(e).__name__} (attempt {i+1}/{attempts}), retrying in {delay:.1f}s...", flush=True)
            time.sleep(delay)
    raise last_exc  # unreachable but keeps type checkers happy

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load_prompt(filename: str) -> str:
    path = _PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


SYSTEM_BASE = _load_prompt("system_base.txt")

CLIP_MODE_PROMPTS = {
    "reaction": _load_prompt("mode_reaction.txt"),
    "dance":    _load_prompt("mode_dance.txt"),
    "hype":     _load_prompt("mode_hype.txt"),
    "all":      _load_prompt("mode_all.txt"),
}


def chunk_transcript(segments: list, max_chars: int = 6000) -> list:
    chunks: list[list] = []
    current: list = []
    current_len = 0

    for seg in segments:
        line = f"[{seg['start']:.1f}s - {seg['end']:.1f}s] {seg['text']}"
        if current_len + len(line) > max_chars and current:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(seg)
        current_len += len(line)

    if current:
        chunks.append(current)

    return chunks


def deduplicate_clips(clips: list, overlap_threshold: float = 0.5) -> list:
    kept: list = []
    for clip in clips:
        overlapping = False
        for k in kept:
            overlap_start = max(clip["start"], k["start"])
            overlap_end = min(clip["end"], k["end"])
            overlap = max(0.0, overlap_end - overlap_start)
            duration = min(clip["end"] - clip["start"], k["end"] - k["start"])
            if duration > 0 and overlap / duration > overlap_threshold:
                overlapping = True
                break
        if not overlapping:
            kept.append(clip)
    return kept


def _coerce_clip(c) -> dict | None:
    """Validate & normalize a clip dict from LLM output. Returns None if unusable."""
    if not isinstance(c, dict):
        return None
    try:
        start = float(c["start"])
        end = float(c["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if end <= start:
        return None
    out = {
        "start": start,
        "end": end,
        "reason": str(c.get("reason", "clip")) or "clip",
        "score": float(c["score"]) if isinstance(c.get("score"), (int, float)) else 0.5,
        "description": str(c.get("description", "")),
    }
    # Optional short-form metadata — carried through to the manifest when present.
    for k in ("hook", "title"):
        v = c.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    if isinstance(c.get("hashtags"), list):
        tags = [str(h).strip().lstrip("#") for h in c["hashtags"] if str(h).strip()]
        if tags:
            out["hashtags"] = tags[:8]
    if isinstance(c.get("virality"), (int, float)):
        out["virality"] = round(max(0.0, min(100.0, float(c["virality"]))), 1)
    return out


def _parse_clips_from_response(raw: str) -> list:
    candidates: list = []
    try:
        candidates = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\[.*?\]", raw, re.DOTALL)
        if match:
            try:
                candidates = json.loads(match.group())
            except json.JSONDecodeError:
                candidates = []

    if not isinstance(candidates, list):
        return []

    return [c for c in (_coerce_clip(x) for x in candidates) if c is not None]


_METADATA_PROMPT = (
    "For each clip also include, when you can: \"hook\" (a punchy 3-8 word opener "
    "for a short), \"title\" (a catchy clip title), \"hashtags\" (3-5 relevant tags, "
    "no # needed), and \"virality\" (0-100 estimate of how shareable the moment is). "
    "These are optional — never drop a clip because you're unsure of them."
)


def _build_system_prompt(config: dict) -> str:
    base = SYSTEM_BASE.replace("{max_clips}", str(config.get("max_clips", 10)))
    addendum = CLIP_MODE_PROMPTS.get(config.get("clip_mode", "all"), "")
    extra = _METADATA_PROMPT if config.get("clip_metadata", True) else ""
    return f"{base}\n\n{addendum}\n\n{extra}".strip()


# JSON schema for the clip list, passed to Ollama's `format` param so the model
# is constrained at the token level to emit a valid top-level ARRAY of clip
# objects. This removes the dependence on regex-scraping JSON out of free-form
# prose (the old failure mode that made stricter models like gemma3 return
# unparseable output). NOTE: do not pass the bare string "json" instead — that
# lets the model wrap the array in an object (e.g. {"clips":[...]}), which the
# parser below drops to zero clips.
_CLIP_LIST_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "start": {"type": "number"},
            "end": {"type": "number"},
            "reason": {"type": "string"},
            "score": {"type": "number"},
            "description": {"type": "string"},
            # Ready-to-post short-form metadata (optional; surfaced in the manifest).
            "hook": {"type": "string"},        # attention-grabbing opening line
            "title": {"type": "string"},       # punchy clip title
            "hashtags": {"type": "array", "items": {"type": "string"}},
            "virality": {"type": "number"},    # 0-100 shareability estimate
        },
        "required": ["start", "end", "reason", "score", "description"],
    },
}


def _call_ollama(system_prompt: str, user_message: str, config: dict) -> str:
    if ollama is None:
        raise ImportError("ollama is required: pip install ollama")
    timeout = config.get("llm_timeout_seconds", 300)
    client = ollama.Client(timeout=timeout)
    # Cap the context window: our transcript chunks are ~6000 chars, so the model's
    # default (e.g. 32K for qwen2.5) just bloats VRAM — at 32K a 14B model needs
    # ~15 GB and spills to CPU on a 16 GB card. 8192 fits a chunk + schema output
    # with margin and keeps the model fully on-GPU.
    num_ctx = int(config.get("ollama_num_ctx", 8192) or 8192)
    return _with_retries(lambda: client.chat(
        model=config["ollama_model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Transcript:\n{user_message}"},
        ],
        format=_CLIP_LIST_SCHEMA,
        options={"num_ctx": num_ctx},
    )["message"]["content"])


def _call_openai(system_prompt: str, user_message: str, config: dict) -> str:
    if OpenAI is None:
        raise ImportError("openai is required: pip install openai")
    client = OpenAI(api_key=config.get("openai_api_key") or None)
    return _with_retries(lambda: client.chat.completions.create(
        model=config["openai_model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Transcript:\n{user_message}"},
        ],
    ).choices[0].message.content)


def _call_llm(system_prompt: str, user_message: str, config: dict) -> str:
    if config.get("llm_backend") == "openai":
        return _call_openai(system_prompt, user_message, config)
    return _call_ollama(system_prompt, user_message, config)


def _sanitize_reason(phrase: str) -> str:
    """Turn a trigger phrase into a filename-safe clip reason tag."""
    tag = re.sub(r"[^a-z0-9]+", "_", phrase.strip().lower()).strip("_")
    return tag or "phrase"


def detect_content_type(segments: list) -> tuple[str, float]:
    """Classify a transcript as "music", "mixed", or "commentary" using
    Whisper's per-segment `no_speech_prob`. Music-only streams (mute VRChat
    dancers, instrumental DJ sets) score high because Whisper can tell the
    audio is not speech; talking streamers score near zero. Returns the label
    and the duration-weighted mean for logging.

    Thresholds were picked from the dataset Whisper publishes — 0.4 is the
    same cutoff Whisper itself uses to drop "no speech" segments.
    """
    total_dur = 0.0
    weighted = 0.0
    for s in segments:
        try:
            d = max(0.0, float(s.get("end", 0)) - float(s.get("start", 0)))
        except (TypeError, ValueError):
            continue
        total_dur += d
        weighted += d * float(s.get("no_speech_prob", 0.0))

    if total_dur <= 0:
        return "unknown", 0.0
    mean = weighted / total_dur
    if mean >= 0.4:
        return "music", mean
    if mean >= 0.2:
        return "mixed", mean
    return "commentary", mean


def select_by_music_peaks(wav_path: str, config: dict) -> list:
    """Build clip candidates from musical onsets (beat drops, song transitions)
    using librosa. Used when the transcript is mostly lyrics/music — the LLM
    has nothing to work with, but the audio waveform does.

    Onsets are detected globally, ranked by onset-envelope strength, then
    greedily picked with a minimum gap so two adjacent drops don't both make
    the cut. Each onset becomes a [-pre, +post] window.
    """
    try:
        import librosa
        import numpy as np
    except ImportError:
        raise ImportError("librosa is required for music-peak selection: pip install librosa")

    max_clips = int(config.get("max_clips", 10))
    pre = float(config.get("music_peak_pre_seconds", 5.0))
    post = float(config.get("music_peak_post_seconds", 12.0))
    min_gap = float(config.get("music_peak_min_gap_seconds", 20.0))

    y, sr = librosa.load(wav_path, sr=None)
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    onset_frames = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr)
    if len(onset_frames) == 0:
        return []

    strengths = onset_env[onset_frames]
    onset_times = librosa.frames_to_time(onset_frames, sr=sr)

    # Rank by strength descending, then greedily accept onsets that are
    # `min_gap` seconds away from every onset already kept. This spreads
    # picks across the whole VOD instead of clustering them in one song.
    order = np.argsort(strengths)[::-1]
    duration = float(len(y)) / float(sr)
    kept_times: list[float] = []
    kept_strengths: list[float] = []
    for idx in order:
        t = float(onset_times[idx])
        if any(abs(t - k) < min_gap for k in kept_times):
            continue
        kept_times.append(t)
        kept_strengths.append(float(strengths[idx]))
        if len(kept_times) >= max_clips:
            break

    if not kept_strengths:
        return []
    max_strength = max(kept_strengths) or 1.0
    clips: list = []
    for t, s in zip(kept_times, kept_strengths):
        clips.append({
            "start": max(0.0, t - pre),
            "end": min(duration, t + post),
            "reason": "music_peak",
            "score": round(0.5 + 0.5 * (s / max_strength), 3),  # 0.5..1.0
            "description": f"Musical onset at {t:.1f}s (strength {s:.1f})",
        })

    clips.sort(key=lambda c: c["start"])
    return deduplicate_clips(clips)


def top_up_with_music_peaks(existing: list, wav_path: str, config: dict, target_count: int) -> list:
    """Pad `existing` clips with music-peak candidates until length >= target_count.
    Only adds peaks that don't overlap existing clips. Used as a floor when the
    LLM returns fewer clips than `min_clips`.
    """
    if len(existing) >= target_count:
        return existing
    fill_config = dict(config)
    # Ask for plenty of candidates so we can filter overlaps with existing picks.
    fill_config["max_clips"] = max(target_count * 3, 30)
    # Best-effort floor: a missing/unreadable WAV or a missing librosa must not
    # crash an otherwise-successful run. Topping up is a nicety, so degrade to
    # "return what the LLM gave us" instead of raising. (clip_mode="music" calls
    # select_by_music_peaks directly and keeps its strict error there.)
    try:
        candidates = select_by_music_peaks(wav_path, fill_config)
    except Exception as e:
        print(f"    music-peak top-up unavailable ({type(e).__name__}: {e}) — keeping {len(existing)} clips")
        return existing
    if not candidates:
        return existing
    merged = list(existing)
    for c in candidates:
        if len(merged) >= target_count:
            break
        overlap = False
        for k in merged:
            o_start = max(c["start"], k["start"])
            o_end = min(c["end"], k["end"])
            if o_end - o_start > 0:
                overlap = True
                break
        if not overlap:
            merged.append(c)
    return merged


def select_by_phrase(segments: list, config: dict) -> list:
    """Cut a window around every transcript segment that contains the
    configured trigger phrase. No LLM call — this is the "voice-mark your
    own clips" path.

    Window = [match_start - pre, match_end + post], floored at 0. Overlapping
    windows (e.g. the streamer says "clip it" twice within a minute) are
    merged by `deduplicate_clips`, so a burst of triggers yields one clip.
    Every match is returned — phrase mode is intentionally not capped by
    `max_clips`; the whole point is to catch them all.
    """
    phrase = str(config.get("trigger_phrase", "clip it")).strip().lower()
    if not phrase:
        return []
    pre = float(config.get("phrase_pre_seconds", 60.0))
    post = float(config.get("phrase_post_seconds", 60.0))
    reason = _sanitize_reason(phrase)

    hits: list = []
    for seg in segments:
        text = (seg.get("text") or "")
        if phrase not in text.lower():
            continue
        try:
            seg_start = float(seg["start"])
            seg_end = float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        hits.append({
            "start": max(0.0, seg_start - pre),
            "end": seg_end + post,
            "reason": reason,
            "score": 1.0,
            "description": text.strip(),
        })

    hits.sort(key=lambda c: c["start"])
    return deduplicate_clips(hits)


def select_highlights(segments: list, config: dict, wav_path: str | None = None, progress=None) -> list:
    """Run the LLM clip-selection prompt over the transcript.

    `wav_path` is required when `clip_mode == "music"` or when auto-detection
    routes a mostly-music VOD to peak-based selection (mute VRChat dancers,
    DJ sets — anywhere the transcript is just lyrics).

    `progress` is an optional `modules.progress.Progress` instance; if given,
    chunk iteration is wrapped in a tqdm bar and per-chunk console prints are
    suppressed (the bar replaces them). In verbose mode the original chatty
    output is preserved.
    """
    if config.get("clip_mode") == "phrase":
        return select_by_phrase(segments, config)

    # Music mode: skip the LLM entirely and clip around audio onsets.
    if config.get("clip_mode") == "music":
        if not wav_path:
            raise ValueError("clip_mode='music' requires wav_path")
        return select_by_music_peaks(wav_path, config)

    # Auto-route: if Whisper says the audio is mostly not-speech (lyrics or
    # silence dominate), use the music-peak path instead of pumping song
    # lyrics through the LLM.
    label, score = detect_content_type(segments)
    config["_content_label"] = label
    config["_content_score"] = score
    if label == "music" and wav_path:
        print(f"    Auto-detected music content (no_speech_prob={score:.2f}) — using audio-peak selection")
        return select_by_music_peaks(wav_path, config)

    system_prompt = _build_system_prompt(config)
    chunks = chunk_transcript(segments, max_chars=6000)
    all_clips: list = []
    verbose = bool(config.get("verbose", False))

    iterable = enumerate(chunks, start=1)
    if progress is not None and not verbose:
        # Bar replaces the per-chunk prints; failures still print so they're
        # not silently swallowed.
        iterable = enumerate(progress.iter(chunks, total=len(chunks), desc="LLM chunks"), start=1)

    failures = 0
    for i, chunk in iterable:
        chunk_text = "\n".join(
            f"[{s['start']:.1f}s - {s['end']:.1f}s] {s['text']}"
            for s in chunk
        )
        if verbose:
            print(f"    LLM chunk {i}/{len(chunks)} ({len(chunk)} segments)...", flush=True)
        try:
            raw = _call_llm(system_prompt, chunk_text, config)
        except Exception as e:
            failures += 1
            print(f"    LLM chunk {i} failed: {type(e).__name__}: {e}", flush=True)
            continue
        parsed = _parse_clips_from_response(raw)
        if verbose:
            print(f"    LLM chunk {i}/{len(chunks)} -> {len(parsed)} candidates", flush=True)
        all_clips.extend(parsed)

    # Distinguish "the model found nothing" from "every call errored" — the latter
    # (model down, bad key) would otherwise look identical to an empty result.
    if chunks and failures == len(chunks):
        print(f"    WARNING: all {len(chunks)} LLM chunks failed — check the LLM "
              f"backend/model; falling back to audio/chat signals only.", flush=True)

    all_clips.sort(key=lambda x: x.get("score", 0), reverse=True)
    return deduplicate_clips(all_clips)
