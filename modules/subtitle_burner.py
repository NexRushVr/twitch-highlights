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
Style: Karaoke,Impact,80,&H0000FFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,7,3,2,120,120,150,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
# Karaoke style: words start white (SecondaryColour) and pop yellow (PrimaryColour)
# as they're spoken, via {\k} fills — the modern word-by-word caption look.


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


def _karaoke_lines(words: list, words_per_line: int = 5) -> list[str]:
    """Group word-timed cues into short lines and emit one karaoke Dialogue per
    line: each word is a `{\\k<cs>}word` run that fills (pops) as it's spoken."""
    lines: list[str] = []
    for i in range(0, len(words), words_per_line):
        group = words[i:i + words_per_line]
        if not group:
            continue
        line_start = group[0][0]
        line_end = group[-1][1]
        parts = []
        prev_end = line_start
        for w_start, w_end, w_text in group:
            # A small lead-gap before the word also fills, keeping timing honest.
            gap_cs = max(0, int(round((w_start - prev_end) * 100)))
            dur_cs = max(1, int(round((w_end - w_start) * 100)))
            if gap_cs:
                parts.append(f"{{\\k{gap_cs}}}")
            parts.append(f"{{\\k{dur_cs}}}{_escape_ass_text(w_text)} ")
            prev_end = w_end
        lines.append(
            f"Dialogue: 0,{_format_ass_time(line_start)},{_format_ass_time(line_end)},"
            f"Karaoke,,0,0,0,,{''.join(parts).rstrip()}"
        )
    return lines


def build_ass(segments: list, clip_start: float, clip_end: float, padding: float,
              style: str = "karaoke") -> str:
    """Build a CapCut-style ASS subtitle file for a single clip.

    Caller already extracted clip = source[clip_start - padding : clip_end + padding].
    The clip's local timeline starts at zero, so segments need to be re-timed by
    subtracting (clip_start - padding).

    When `style != "simple"` and Whisper word timestamps are present, captions are
    word-by-word "karaoke" (each word pops as spoken); otherwise they fall back to
    evenly-split segment cues.
    """
    extract_start = max(0.0, clip_start - padding)
    extract_end = clip_end + padding
    clip_duration = extract_end - extract_start

    # Collect in-window words (re-timed to the clip) for karaoke, if available.
    karaoke_words: list[tuple[float, float, str]] = []
    have_words = False
    if style != "simple":
        for seg in segments:
            for w in (seg.get("words") or []):
                have_words = True
                try:
                    ws = float(w["start"]); we = float(w["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                if we <= extract_start or ws >= extract_end:
                    continue
                ls = max(0.0, ws - extract_start)
                le = min(we - extract_start, clip_duration)
                wt = (w.get("word") or "").strip()
                if le > ls and wt:
                    karaoke_words.append((ls, le, wt))

    lines = [ASS_HEADER]
    if have_words and karaoke_words:
        karaoke_words.sort(key=lambda x: x[0])
        lines.extend(_karaoke_lines(karaoke_words))
        return "\n".join(lines) + "\n"

    # Fallback: segment-level cues with even time-splitting.
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
    style: str = "karaoke",
) -> str:
    """End-to-end: build ASS for the clip's transcript window, burn it in. Returns output path."""
    ass_path = os.path.splitext(output_video)[0] + ".ass"
    ass_text = build_ass(segments, clip_start, clip_end, padding, style=style)
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_text)
    burn_captions(input_video, output_video, ass_path, quiet=quiet)
    return output_video
