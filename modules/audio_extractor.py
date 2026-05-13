import subprocess

try:
    import librosa
    import numpy as np
except ImportError:
    librosa = None
    np = None


def extract_audio(video_path: str, out_wav: str, quiet: bool = False) -> str:
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",
        "-ar", "16000",
        "-ac", "1",
        "-f", "wav",
        out_wav,
    ]
    kwargs = {"check": True}
    if quiet:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.PIPE  # captured so errors are still surfaced
    subprocess.run(cmd, **kwargs)
    return out_wav


def get_audio_peaks(wav_path: str, threshold_db: float = -20.0) -> list:
    if librosa is None:
        raise ImportError("librosa is required: pip install librosa")

    y, sr = librosa.load(wav_path, sr=None)
    rms = librosa.feature.rms(y=y)[0]
    db = librosa.amplitude_to_db(rms)
    frame_times = librosa.frames_to_time(range(len(db)), sr=sr)

    peaks = []
    in_peak = False
    start = 0.0

    for t, d in zip(frame_times, db):
        if d >= threshold_db and not in_peak:
            in_peak = True
            start = float(t)
        elif d < threshold_db and in_peak:
            in_peak = False
            peaks.append({"start": round(start, 2), "end": round(float(t), 2)})

    if in_peak and len(frame_times):
        peaks.append({"start": round(start, 2), "end": round(float(frame_times[-1]), 2)})

    return peaks
