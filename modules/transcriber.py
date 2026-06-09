try:
    import whisper
except ImportError:
    whisper = None


def transcribe(audio_path: str, model_size: str = "large-v3", device: str = "cuda",
               verbose: bool = False, word_timestamps: bool = False) -> list:
    if whisper is None:
        raise ImportError("openai-whisper is required: pip install openai-whisper")

    model = whisper.load_model(model_size, device=device)
    fp16 = device == "cuda"
    # Whisper's `verbose=None` keeps its tqdm progress bar but drops per-segment
    # console prints. `verbose=False` is fully silent. `verbose=True` prints
    # everything including each segment's text.
    whisper_verbose = True if verbose else None
    # word_timestamps powers word-level "karaoke" captions (each word pops as it's
    # spoken). Cheap add-on to the same decode; off by default for callers that
    # only need segment text.
    try:
        result = model.transcribe(audio_path, fp16=fp16, beam_size=5,
                                  verbose=whisper_verbose, word_timestamps=word_timestamps)

        out = []
        for seg in result.get("segments", []):
            entry = {
                "start": round(seg["start"], 2),
                "end": round(seg["end"], 2),
                "text": seg["text"].strip(),
                "confidence": round(seg.get("avg_logprob", 0.0), 3),
                "no_speech_prob": round(float(seg.get("no_speech_prob", 0.0)), 3),
            }
            if word_timestamps:
                entry["words"] = [
                    {"word": (w.get("word") or "").strip(),
                     "start": round(float(w["start"]), 2),
                     "end": round(float(w["end"]), 2)}
                    for w in (seg.get("words") or [])
                    if w.get("start") is not None and w.get("end") is not None and (w.get("word") or "").strip()
                ]
            out.append(entry)
        return out
    finally:
        # Release Whisper's VRAM before the LLM phase. The pipeline shares one GPU
        # with the Ollama server; a 14B model (~15 GB) won't co-fit with a resident
        # Whisper model on a 16 GB card, so Ollama would spill to CPU and time out.
        # PyTorch's caching allocator holds the memory until we drop the model and
        # empty the cache.
        del model
        try:
            import gc
            import torch
            gc.collect()
            if device == "cuda" and torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
