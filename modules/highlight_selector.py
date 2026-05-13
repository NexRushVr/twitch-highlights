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


def _build_system_prompt(config: dict) -> str:
    base = SYSTEM_BASE.replace("{max_clips}", str(config.get("max_clips", 10)))
    addendum = CLIP_MODE_PROMPTS.get(config.get("clip_mode", "all"), "")
    return f"{base}\n\n{addendum}".strip()


def _call_ollama(system_prompt: str, user_message: str, config: dict) -> str:
    if ollama is None:
        raise ImportError("ollama is required: pip install ollama")
    timeout = config.get("llm_timeout_seconds", 300)
    client = ollama.Client(timeout=timeout)
    return _with_retries(lambda: client.chat(
        model=config["ollama_model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Transcript:\n{user_message}"},
        ],
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


def select_highlights(segments: list, config: dict, progress=None) -> list:
    """Run the LLM clip-selection prompt over the transcript.

    `progress` is an optional `modules.progress.Progress` instance; if given,
    chunk iteration is wrapped in a tqdm bar and per-chunk console prints are
    suppressed (the bar replaces them). In verbose mode the original chatty
    output is preserved.
    """
    system_prompt = _build_system_prompt(config)
    chunks = chunk_transcript(segments, max_chars=6000)
    all_clips: list = []
    verbose = bool(config.get("verbose", False))

    iterable = enumerate(chunks, start=1)
    if progress is not None and not verbose:
        # Bar replaces the per-chunk prints; failures still print so they're
        # not silently swallowed.
        iterable = enumerate(progress.iter(chunks, total=len(chunks), desc="LLM chunks"), start=1)

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
            print(f"    LLM chunk {i} failed: {type(e).__name__}: {e}", flush=True)
            continue
        parsed = _parse_clips_from_response(raw)
        if verbose:
            print(f"    LLM chunk {i}/{len(chunks)} -> {len(parsed)} candidates", flush=True)
        all_clips.extend(parsed)

    all_clips.sort(key=lambda x: x.get("score", 0), reverse=True)
    return deduplicate_clips(all_clips)
