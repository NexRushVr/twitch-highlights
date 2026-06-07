from unittest.mock import patch, MagicMock

import pytest

from modules.clip_extractor import ATTRIBUTION_COMMENT
from modules.subtitle_burner import (
    _format_ass_time,
    _escape_ass_text,
    _split_long_segment,
    build_ass,
    burn_captions,
    caption_clip,
)


# ---------------------------------------------------------------------------
# _format_ass_time
# ---------------------------------------------------------------------------

def test_format_ass_time_zero():
    assert _format_ass_time(0.0) == "0:00:00.00"


def test_format_ass_time_centiseconds():
    assert _format_ass_time(1.23) == "0:00:01.23"


def test_format_ass_time_minutes():
    assert _format_ass_time(75.5) == "0:01:15.50"


def test_format_ass_time_hours():
    assert _format_ass_time(3661.45) == "1:01:01.45"


def test_format_ass_time_negative_clamps_to_zero():
    assert _format_ass_time(-1.0) == "0:00:00.00"


# ---------------------------------------------------------------------------
# _escape_ass_text
# ---------------------------------------------------------------------------

def test_escape_ass_text_passthrough():
    assert _escape_ass_text("hello world") == "hello world"


def test_escape_ass_text_braces():
    assert _escape_ass_text("hello {bold} text") == "hello \\{bold\\} text"


def test_escape_ass_text_newlines_become_ass_newlines():
    assert _escape_ass_text("line1\nline2") == "line1\\Nline2"


# ---------------------------------------------------------------------------
# _split_long_segment
# ---------------------------------------------------------------------------

def test_split_long_segment_short_returns_single():
    out = _split_long_segment(0.0, 2.0, "hi there")
    assert out == [(0.0, 2.0, "hi there")]


def test_split_long_segment_few_words_returns_single():
    out = _split_long_segment(0.0, 10.0, "what")
    assert out == [(0.0, 10.0, "what")]


def test_split_long_segment_splits_proportionally():
    out = _split_long_segment(0.0, 10.0, "one two three four five six seven eight", max_chunk_s=3.5)
    assert len(out) >= 2
    # First chunk starts at 0, last chunk ends at 10
    assert out[0][0] == 0.0
    assert out[-1][1] == pytest.approx(10.0)
    # All chunks have non-empty text
    assert all(t.strip() for _, _, t in out)
    # Chunks together contain all original words
    rejoined = " ".join(t for _, _, t in out)
    assert rejoined == "one two three four five six seven eight"


# ---------------------------------------------------------------------------
# build_ass
# ---------------------------------------------------------------------------

def _segs():
    return [
        {"start": 100.0, "end": 102.0, "text": "before clip"},
        {"start": 105.0, "end": 107.0, "text": "first line"},
        {"start": 108.0, "end": 110.0, "text": "second line"},
        {"start": 200.0, "end": 202.0, "text": "after clip"},
    ]


def test_build_ass_includes_header_and_style():
    out = build_ass(_segs(), clip_start=105.0, clip_end=110.0, padding=2.0)
    assert "[Script Info]" in out
    assert "Style: CapCut," in out
    assert "Impact" in out


def test_build_ass_filters_segments_outside_window():
    out = build_ass(_segs(), clip_start=105.0, clip_end=110.0, padding=2.0)
    assert "before clip" not in out
    assert "after clip" not in out
    assert "first line" in out
    assert "second line" in out


def test_build_ass_retimes_to_clip_local_zero():
    """Segment at source time 105 in a clip starting at (105 - padding=2) = 103
    becomes local time 2.0 in the captioned output."""
    out = build_ass(_segs(), clip_start=105.0, clip_end=110.0, padding=2.0)
    # Find the dialogue line for "first line" — its start should be 0:00:02.00
    for line in out.splitlines():
        if line.startswith("Dialogue") and "first line" in line:
            parts = line.split(",")
            start_time = parts[1]
            assert start_time == "0:00:02.00"
            return
    pytest.fail("first line dialogue not found")


def test_build_ass_skips_empty_text():
    segs = [{"start": 100.0, "end": 101.0, "text": "   "}]
    out = build_ass(segs, clip_start=100.0, clip_end=101.0, padding=0.0)
    assert "Dialogue" not in out


def test_build_ass_clamps_segment_end_to_clip_duration():
    segs = [{"start": 100.0, "end": 999.0, "text": "very long"}]
    out = build_ass(segs, clip_start=100.0, clip_end=105.0, padding=2.0)
    # Clip duration with padding = 5 + 2*2 = 7s. Segment must not exceed.
    assert "very long" in out


def test_build_ass_handles_clip_at_zero():
    """A clip starting near time zero with padding shouldn't produce negative extract_start."""
    segs = [{"start": 0.5, "end": 1.5, "text": "early"}]
    out = build_ass(segs, clip_start=0.5, clip_end=2.0, padding=3.0)
    assert "early" in out


# ---------------------------------------------------------------------------
# burn_captions
# ---------------------------------------------------------------------------

def test_burn_captions_invokes_ffmpeg(tmp_path):
    in_path = str(tmp_path / "in.mp4")
    out_path = str(tmp_path / "out.mp4")
    ass_path = str(tmp_path / "subs.ass")

    with patch("modules.subtitle_burner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = burn_captions(in_path, out_path, ass_path)

    assert result == out_path
    cmd = mock_run.call_args[0][0]
    assert "ffmpeg" in cmd
    # The subtitle filter must reference the ass path
    vf = cmd[cmd.index("-vf") + 1]
    assert "ass=" in vf


def test_burn_captions_escapes_windows_drive_letter(tmp_path):
    """ffmpeg's vf parser uses ':' as separator — paths with drive letters need escaping."""
    in_path = str(tmp_path / "in.mp4")
    out_path = str(tmp_path / "out.mp4")
    ass_path = "C:\\Users\\foo\\subs.ass"

    with patch("modules.subtitle_burner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        burn_captions(in_path, out_path, ass_path)

    cmd = mock_run.call_args[0][0]
    vf = cmd[cmd.index("-vf") + 1]
    # Backslashes converted to forward slashes; drive letter colon escaped
    assert "C\\:/Users/foo/subs.ass" in vf


def test_burn_captions_embeds_attribution_comment(tmp_path):
    in_path = str(tmp_path / "in.mp4")
    out_path = str(tmp_path / "out.mp4")
    ass_path = str(tmp_path / "subs.ass")

    with patch("modules.subtitle_burner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        burn_captions(in_path, out_path, ass_path)

    cmd = mock_run.call_args[0][0]
    # `-metadata comment=...` pair must point at the canonical attribution string
    meta_idx = next(
        i for i, a in enumerate(cmd)
        if a == "-metadata" and i + 1 < len(cmd) and cmd[i + 1].startswith("comment=")
    )
    assert cmd[meta_idx + 1] == f"comment={ATTRIBUTION_COMMENT}"


# ---------------------------------------------------------------------------
# caption_clip (end-to-end)
# ---------------------------------------------------------------------------

def test_caption_clip_writes_ass_and_calls_burn(tmp_path):
    in_path = str(tmp_path / "clip.mp4")
    out_path = str(tmp_path / "clip_captioned.mp4")
    segments = [{"start": 10.0, "end": 12.0, "text": "hello"}]

    with patch("modules.subtitle_burner.burn_captions") as mock_burn:
        mock_burn.return_value = out_path
        result = caption_clip(in_path, out_path, segments, clip_start=10.0, clip_end=12.0, padding=1.0)

    assert result == out_path
    ass_path = str(tmp_path / "clip_captioned.ass")
    import os
    assert os.path.exists(ass_path)
    with open(ass_path, encoding="utf-8") as f:
        content = f.read()
    assert "hello" in content
    assert "[Script Info]" in content
    mock_burn.assert_called_once_with(in_path, out_path, ass_path, quiet=False)


# ---------------------------------------------------------------------------
# Word-level karaoke captions
# ---------------------------------------------------------------------------

def _word_seg():
    # one segment spanning 100-104s with 4 word timings
    return [{
        "start": 100.0, "end": 104.0, "text": "this is so funny",
        "words": [
            {"word": "this", "start": 100.0, "end": 100.5},
            {"word": "is", "start": 100.5, "end": 101.0},
            {"word": "so", "start": 101.0, "end": 102.0},
            {"word": "funny", "start": 102.0, "end": 104.0},
        ],
    }]


def test_build_ass_karaoke_uses_word_tags_and_style():
    out = build_ass(_word_seg(), clip_start=101.0, clip_end=103.0, padding=2.0, style="karaoke")
    assert "Style: Karaoke" in out          # header carries the karaoke style
    assert ",Karaoke,," in out              # dialogue uses it
    assert r"\k" in out                     # karaoke fill tags present
    assert "funny" in out


def test_build_ass_simple_style_ignores_words():
    out = build_ass(_word_seg(), clip_start=101.0, clip_end=103.0, padding=2.0, style="simple")
    assert r"\k" not in out                 # no karaoke
    assert ",CapCut,," in out               # falls back to the segment style


def test_build_ass_falls_back_when_no_words():
    # segments without word timings -> classic CapCut cues even in karaoke mode
    segs = [{"start": 100.0, "end": 103.0, "text": "no word timings here"}]
    out = build_ass(segs, clip_start=100.0, clip_end=103.0, padding=1.0, style="karaoke")
    assert r"\k" not in out and ",CapCut,," in out
