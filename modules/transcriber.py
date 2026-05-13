try:
    import whisper
except ImportError:
    whisper = None


def transcribe(audio_path: str, model_size: str = "large-v3", device: str = "cuda", verbose: bool = False) -> list:
    if whisper is None:
        raise ImportError("openai-whisper is required: pip install openai-whisper")

    model = whisper.load_model(model_size, device=device)
    fp16 = device == "cuda"
    # Whisper's `verbose=None` keeps its tqdm progress bar but drops per-segment
    # console prints. `verbose=False` is fully silent. `verbose=True` prints
    # everything including each segment's text.
    whisper_verbose = True if verbose else None
    result = model.transcribe(audio_path, fp16=fp16, beam_size=5, verbose=whisper_verbose)

    segments = result.get("segments", [])
    return [
        {
            "start": round(seg["start"], 2),
            "end": round(seg["end"], 2),
            "text": seg["text"].strip(),
            "confidence": round(seg.get("avg_logprob", 0.0), 3),
        }
        for seg in segments
    ]
