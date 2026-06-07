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
