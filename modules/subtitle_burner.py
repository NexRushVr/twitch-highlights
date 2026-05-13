import os
import subprocess

from modules.clip_extractor import ATTRIBUTION_COMMENT


# Yellow Hype: Impact 80, yellow fill, thick black outline, lower-third.
# (ASS color is &HAABBGGRR — yellow = 0000FFFF.)
# .ass files land next to each `*_captioned.mp4` so styles can be tweaked
# and re-burned with ffmpeg without re-running the pipeline.
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CapCut,Impact,80,&H0000FFFF,&H0000FFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,7,3,2,120,120,150,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _format_ass_time(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - h * 3600 - m * 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _escape_ass_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", "\\N")
    )


def _split_long_segment(start: float, end: float, text: str, max_chunk_s: float = 3.5) -> list[tuple[float, float, str]]:
    """If a Whisper segment is long, split into ~max_chunk_s sub-cues so captions feel CapCut-snappy."""
    duration = end - start
    if duration <= max_chunk_s:
        return [(start, end, text)]

    words = text.split()
    if len(words) <= 3:
        return [(start, end, text)]

    n_chunks = max(2, int(round(duration / max_chunk_s)))
    words_per_chunk = max(1, len(words) // n_chunks)
    chunks: list[tuple[float, float, str]] = []
    for i in range(n_chunks):
        w_start = i * words_per_chunk
        w_end = len(words) if i == n_chunks - 1 else (i + 1) * words_per_chunk
        if w_start >= w_end:
            continue
        chunk_text = " ".join(words[w_start:w_end])
        t0 = start + (i / n_chunks) * duration
        t1 = start + ((i + 1) / n_chunks) * duration
        chunks.append((t0, t1, chunk_text))
    return chunks


def build_ass(segments: list, clip_start: float, clip_end: float, padding: float) -> str:
    """Build a CapCut-style ASS subtitle file for a single clip.

    Caller already extracted clip = source[clip_start - padding : clip_end + padding].
    The clip's local timeline starts at zero, so segments need to be re-timed by
    subtracting (clip_start - padding).
    """
    extract_start = max(0.0, clip_start - padding)
    extract_end = clip_end + padding
    clip_duration = extract_end - extract_start

    lines = [ASS_HEADER]
    for seg in segments:
        seg_start = float(seg["start"])
        seg_end = float(seg["end"])
        if seg_end <= extract_start or seg_start >= extract_end:
            continue
        local_start = max(0.0, seg_start - extract_start)
        local_end = min(seg_end - extract_start, clip_duration)
        if local_end <= local_start:
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        for sub_start, sub_end, sub_text in _split_long_segment(local_start, local_end, text):
            lines.append(
                f"Dialogue: 0,{_format_ass_time(sub_start)},{_format_ass_time(sub_end)},"
                f"CapCut,,0,0,0,,{_escape_ass_text(sub_text)}"
            )
    return "\n".join(lines) + "\n"


def burn_captions(input_video: str, output_video: str, ass_path: str, quiet: bool = False) -> str:
    """Burn an ASS subtitle file into a video via ffmpeg."""
    # ffmpeg's vf parser uses ':' as a separator, so Windows drive letters need escaping.
    # Single quote the path for the filter, then escape ':' inside it.
    ass_arg = ass_path.replace("\\", "/").replace(":", "\\:")
    cmd = [
        "ffmpeg", "-y",
        "-i", input_video,
        "-vf", f"ass='{ass_arg}'",
        "-c:v", "libx264",
        "-crf", "20",
        "-c:a", "copy",
        "-metadata", f"comment={ATTRIBUTION_COMMENT}",
        output_video,
    ]
    kwargs = {"check": True}
    if quiet:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.PIPE
    subprocess.run(cmd, **kwargs)
    return output_video


def caption_clip(
    input_video: str,
    output_video: str,
    segments: list,
    clip_start: float,
    clip_end: float,
    padding: float,
    quiet: bool = False,
) -> str:
    """End-to-end: build ASS for the clip's transcript window, burn it in. Returns output path."""
    ass_path = os.path.splitext(output_video)[0] + ".ass"
    ass_text = build_ass(segments, clip_start, clip_end, padding)
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_text)
    burn_captions(input_video, output_video, ass_path, quiet=quiet)
    return output_video
