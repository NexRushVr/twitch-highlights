import subprocess
import os


def extract_clip(
    video_path: str,
    start: float,
    end: float,
    out_path: str,
    padding: float = 3.0,
) -> str:
    actual_start = max(0.0, start - padding)
    actual_end = end + padding
    duration = actual_end - actual_start

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(actual_start),
        "-i", video_path,
        "-t", str(duration),
        "-c:v", "libx264",
        "-crf", "18",
        "-c:a", "aac",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    return out_path


def batch_extract(video_path: str, clips: list, config: dict) -> list:
    os.makedirs(config["output_dir"], exist_ok=True)
    output_files = []

    for i, clip in enumerate(clips):
        reason = clip.get("reason", "clip")
        fname = f"clip_{i + 1:03d}_{reason}.mp4"
        out = os.path.join(config["output_dir"], fname)
        extract_clip(
            video_path,
            clip["start"],
            clip["end"],
            out,
            config.get("clip_padding_seconds", 3.0),
        )
        output_files.append({"file": out, "meta": clip})

    return output_files
