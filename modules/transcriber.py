try:
    import whisper
except ImportError:
    whisper = None


def transcribe(audio_path: str, model_size: str = "large-v3", device: str = "cuda") -> list:
    if whisper is None:
        raise ImportError("openai-whisper is required: pip install openai-whisper")

    model = whisper.load_model(model_size, device=device)
    fp16 = device == "cuda"
    result = model.transcribe(audio_path, fp16=fp16, beam_size=5)

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
