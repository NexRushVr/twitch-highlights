import json
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from pipeline import run, _build_arg_parser, _cleanup_source_artifacts, _is_inside


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextmanager
def _mock_pipeline(tmp_path, sample_segments, sample_clips, video_filename="video.mp4", vod_date="2026-04-27"):
    video_path = str(tmp_path / video_filename)
    wav_path = str(tmp_path / "video.wav")
    clip_file = str(tmp_path / "clips" / "clip_001_funny_reaction.mp4")

    with patch("pipeline.download_twitch_vod", return_value=(video_path, vod_date)) as mock_dl, \
         patch("pipeline.get_latest_vodvod_m3u8", return_value=("https://cdn.example.com/chunked/index.m3u8", vod_date)) as mock_m3u8, \
         patch("pipeline.stream_m3u8_to_file", return_value=video_path) as mock_stream, \
         patch("pipeline.resolve_local_file", return_value=(video_path, vod_date)) as mock_local, \
         patch("pipeline.apply_time_window", side_effect=lambda v, *a, **kw: v) as mock_window, \
         patch("pipeline.extract_audio", return_value=wav_path) as mock_audio, \
         patch("pipeline.transcribe", return_value=sample_segments) as mock_trans, \
         patch("pipeline.select_highlights", return_value=sample_clips[:]) as mock_sel, \
         patch("pipeline.get_audio_peaks", return_value=[{"start": 8.0, "end": 25.0}]) as mock_peaks, \
         patch("pipeline.batch_extract", return_value=[{"file": clip_file, "meta": sample_clips[0]}]) as mock_extract, \
         patch("pipeline.caption_clip") as mock_caption:
        yield {
            "dl": mock_dl, "m3u8": mock_m3u8, "stream": mock_stream, "local": mock_local,
            "window": mock_window,
            "audio": mock_audio, "transcribe": mock_trans, "select": mock_sel,
            "peaks": mock_peaks, "extract": mock_extract, "caption": mock_caption,
            "vod_date": vod_date,
        }


# ---------------------------------------------------------------------------
# Twitch source
# ---------------------------------------------------------------------------

def test_run_twitch_source_calls_download(tmp_path, sample_segments, sample_clips):
    cfg = {
        "source_type": "twitch",
        "twitch_vod_url": "https://www.twitch.tv/videos/123",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "reaction",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
    }
    with _mock_pipeline(tmp_path, sample_segments, sample_clips) as mocks:
        result = run(cfg)

    mocks["dl"].assert_called_once_with(
        cfg["twitch_vod_url"], cfg["quality"], cfg["download_dir"], quiet=True,
    )
    assert len(result) >= 1


def test_run_twitch_source_writes_manifest(tmp_path, sample_segments, sample_clips):
    cfg = {
        "source_type": "twitch",
        "twitch_vod_url": "https://www.twitch.tv/videos/123",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "reaction",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
    }
    with _mock_pipeline(tmp_path, sample_segments, sample_clips, vod_date="2026-04-27") as mocks:
        run(cfg)

    # Twitch source: clips/<date>/clips_manifest.json (no streamer subdir)
    manifest = os.path.join(cfg["output_dir"], mocks["vod_date"], "clips_manifest.json")
    assert os.path.exists(manifest)
    with open(manifest) as f:
        data = json.load(f)
    assert isinstance(data, list)


# ---------------------------------------------------------------------------
# vodvod source
# ---------------------------------------------------------------------------

def test_run_vodvod_source_calls_m3u8_and_stream(tmp_path, sample_segments, sample_clips):
    cfg = {
        "source_type": "vodvod",
        "vodvod_channel": "@testchannel",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "all",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
    }
    with _mock_pipeline(tmp_path, sample_segments, sample_clips) as mocks:
        run(cfg)

    mocks["m3u8"].assert_called_once_with(cfg["vodvod_channel"])
    mocks["stream"].assert_called_once()


# ---------------------------------------------------------------------------
# m3u8 source
# ---------------------------------------------------------------------------

def test_run_m3u8_source_calls_stream(tmp_path, sample_segments, sample_clips):
    cfg = {
        "source_type": "m3u8",
        "m3u8_url": "https://example.com/stream.m3u8",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "hype",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
    }
    with _mock_pipeline(tmp_path, sample_segments, sample_clips) as mocks:
        run(cfg)

    # m3u8 source uses today's date as the cache filename
    mocks["stream"].assert_called_once()
    args = mocks["stream"].call_args[0]
    assert args[0] == cfg["m3u8_url"]
    assert args[1].startswith(cfg["download_dir"])
    assert args[1].endswith(".mp4")


# ---------------------------------------------------------------------------
# Local source
# ---------------------------------------------------------------------------

def test_run_local_source_calls_resolver_with_path(tmp_path, sample_segments, sample_clips):
    cfg = {
        "source_type": "local",
        "local_path": str(tmp_path / "my_recording.mp4"),
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "all",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
    }
    with _mock_pipeline(tmp_path, sample_segments, sample_clips) as mocks:
        result = run(cfg)

    mocks["local"].assert_called_once_with(cfg["local_path"], cfg["download_dir"], quiet=True)
    assert len(result) >= 1


def test_run_local_source_without_path_raises(tmp_path, sample_segments, sample_clips):
    cfg = {
        "source_type": "local",
        "local_path": "",   # missing
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "all",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
    }
    with _mock_pipeline(tmp_path, sample_segments, sample_clips):
        with pytest.raises(ValueError, match="local"):
            run(cfg)


def test_run_local_source_derives_wav_under_download_dir(tmp_path, sample_segments, sample_clips):
    """Local source should not pollute the user's source-file directory with .wav / .transcript.json."""
    user_dir = tmp_path / "user_videos"
    user_dir.mkdir()
    source_mp4 = user_dir / "my_stream.mp4"
    source_mp4.write_bytes(b"data")
    download_dir = tmp_path / "downloads"

    cfg = {
        "source_type": "local",
        "local_path": str(source_mp4),
        "download_dir": str(download_dir),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "all",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
    }
    with _mock_pipeline(tmp_path, sample_segments, sample_clips) as mocks:
        run(cfg)

    # extract_audio's out_wav (2nd positional arg) must live under download_dir, not user_dir.
    wav_arg = mocks["audio"].call_args[0][1]
    assert str(download_dir) in wav_arg, f"wav should be under download_dir, got {wav_arg}"
    assert str(user_dir) not in wav_arg, f"wav should not be under user_dir, got {wav_arg}"


# ---------------------------------------------------------------------------
# Phrase mode
# ---------------------------------------------------------------------------

def test_run_phrase_mode_does_not_truncate_to_max_clips(tmp_path, sample_segments):
    """Phrase mode is 'catch every trigger' — max_clips must NOT cap it."""
    cfg = {
        "source_type": "twitch",
        "twitch_vod_url": "https://www.twitch.tv/videos/123",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "phrase",
        "trigger_phrase": "clip it",
        "max_clips": 2,                       # deliberately small
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
        "burn_subtitles": False,
    }
    # 6 phrase hits even though max_clips=2.
    many = [
        {"start": float(i * 100), "end": float(i * 100 + 20),
         "reason": "clip_it", "score": 1.0, "description": "clip it"}
        for i in range(6)
    ]
    clip_file = str(tmp_path / "clips" / "clip_001_clip_it.mp4")
    with patch("pipeline.download_twitch_vod", return_value=(str(tmp_path / "v.mp4"), "2026-05-13")), \
         patch("pipeline.apply_time_window", side_effect=lambda v, *a, **kw: v), \
         patch("pipeline._estimate_total_runtime", return_value=None), \
         patch("pipeline.extract_audio", return_value=str(tmp_path / "v.wav")), \
         patch("pipeline.transcribe", return_value=sample_segments), \
         patch("pipeline.select_highlights", return_value=many) as mock_sel, \
         patch("pipeline.get_audio_peaks", return_value=[]), \
         patch("pipeline.batch_extract",
               side_effect=lambda vp, clips, c, progress=None: [{"file": clip_file, "meta": cl} for cl in clips]) as mock_extract:
        run(cfg)

    # select_highlights got the phrase config; batch_extract received all 6,
    # not just max_clips=2.
    assert mock_sel.called
    extracted_clips = mock_extract.call_args[0][1]
    assert len(extracted_clips) == 6


def test_run_reaction_mode_still_truncates_to_max_clips(tmp_path, sample_segments):
    """Sanity counter-test: non-phrase modes still honor max_clips."""
    cfg = {
        "source_type": "twitch",
        "twitch_vod_url": "https://www.twitch.tv/videos/123",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "reaction",
        "max_clips": 2,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
        "burn_subtitles": False,
    }
    many = [
        {"start": float(i * 100), "end": float(i * 100 + 20),
         "reason": "hype", "score": 1.0 - i * 0.1, "description": "x"}
        for i in range(6)
    ]
    clip_file = str(tmp_path / "clips" / "clip_001_hype.mp4")
    with patch("pipeline.download_twitch_vod", return_value=(str(tmp_path / "v.mp4"), "2026-05-13")), \
         patch("pipeline.apply_time_window", side_effect=lambda v, *a, **kw: v), \
         patch("pipeline._estimate_total_runtime", return_value=None), \
         patch("pipeline.extract_audio", return_value=str(tmp_path / "v.wav")), \
         patch("pipeline.transcribe", return_value=sample_segments), \
         patch("pipeline.select_highlights", return_value=many), \
         patch("pipeline.get_audio_peaks", return_value=[]), \
         patch("pipeline.batch_extract",
               side_effect=lambda vp, clips, c, progress=None: [{"file": clip_file, "meta": cl} for cl in clips]) as mock_extract:
        run(cfg)

    extracted_clips = mock_extract.call_args[0][1]
    assert len(extracted_clips) == 2


# ---------------------------------------------------------------------------
# Time window
# ---------------------------------------------------------------------------

def _twitch_cfg(tmp_path, **overrides):
    base = {
        "source_type": "twitch",
        "twitch_vod_url": "https://www.twitch.tv/videos/123",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "reaction",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
    }
    base.update(overrides)
    return base


def test_run_with_no_time_window_skips_trim(tmp_path, sample_segments, sample_clips):
    cfg = _twitch_cfg(tmp_path)
    with _mock_pipeline(tmp_path, sample_segments, sample_clips) as mocks:
        run(cfg)
    mocks["window"].assert_not_called()


def test_run_with_start_and_end_calls_window(tmp_path, sample_segments, sample_clips):
    cfg = _twitch_cfg(tmp_path, start_time="1:00:00", end_time="1:30:00")
    with _mock_pipeline(tmp_path, sample_segments, sample_clips) as mocks:
        run(cfg)
    mocks["window"].assert_called_once()
    args = mocks["window"].call_args[0]
    assert args[1] == "1:00:00"   # start_str
    assert args[2] == "1:30:00"   # end_str
    assert args[3] == cfg["download_dir"]


def test_run_with_only_start_time_calls_window(tmp_path, sample_segments, sample_clips):
    cfg = _twitch_cfg(tmp_path, start_time="0:30:00")
    with _mock_pipeline(tmp_path, sample_segments, sample_clips) as mocks:
        run(cfg)
    mocks["window"].assert_called_once()
    args = mocks["window"].call_args[0]
    assert args[1] == "0:30:00"
    assert args[2] is None


def test_run_with_only_end_time_calls_window(tmp_path, sample_segments, sample_clips):
    cfg = _twitch_cfg(tmp_path, end_time="0:45:00")
    with _mock_pipeline(tmp_path, sample_segments, sample_clips) as mocks:
        run(cfg)
    mocks["window"].assert_called_once()
    args = mocks["window"].call_args[0]
    assert args[1] is None
    assert args[2] == "0:45:00"


def test_run_propagates_window_validation_error(tmp_path, sample_segments, sample_clips):
    cfg = _twitch_cfg(tmp_path, start_time="2:00:00", end_time="1:00:00")
    with _mock_pipeline(tmp_path, sample_segments, sample_clips) as mocks:
        mocks["window"].side_effect = ValueError("end_time must be after start_time")
        with pytest.raises(ValueError, match="end_time must be after"):
            run(cfg)


# ---------------------------------------------------------------------------
# Unknown source
# ---------------------------------------------------------------------------

def test_run_unknown_source_raises(tmp_path, sample_segments, sample_clips):
    cfg = {
        "source_type": "unknown_source",
        "download_dir": str(tmp_path / "downloads"),
        "output_dir": str(tmp_path / "clips"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "all",
        "max_clips": 5,
        "clip_padding_seconds": 3,
    }
    with _mock_pipeline(tmp_path, sample_segments, sample_clips):
        with pytest.raises(ValueError, match="unknown_source"):
            run(cfg)


# ---------------------------------------------------------------------------
# Caption burn-in
# ---------------------------------------------------------------------------

def test_run_with_burn_subtitles_calls_caption_clip(tmp_path, sample_segments, sample_clips):
    cfg = {
        "source_type": "twitch",
        "twitch_vod_url": "https://www.twitch.tv/videos/123",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "reaction",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
        "burn_subtitles": True,
    }
    with _mock_pipeline(tmp_path, sample_segments, sample_clips) as mocks:
        result = run(cfg)

    mocks["caption"].assert_called_once()
    assert "captioned" in result[0]


def test_run_with_burn_subtitles_off_skips_caption(tmp_path, sample_segments, sample_clips):
    cfg = {
        "source_type": "twitch",
        "twitch_vod_url": "https://www.twitch.tv/videos/123",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "reaction",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
        "burn_subtitles": False,
    }
    with _mock_pipeline(tmp_path, sample_segments, sample_clips) as mocks:
        result = run(cfg)

    mocks["caption"].assert_not_called()
    assert "captioned" not in result[0]


# ---------------------------------------------------------------------------
# Audio peak score boosting
# ---------------------------------------------------------------------------

def test_run_boosts_clip_score_on_audio_peak(tmp_path, sample_segments, sample_clips):
    cfg = {
        "source_type": "twitch",
        "twitch_vod_url": "https://www.twitch.tv/videos/123",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "reaction",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
    }
    # Clip at start=8.0 overlaps with peak 0.0-15.0
    clips_with_known_score = [{"start": 8.0, "end": 20.0, "reason": "hype", "score": 0.5, "description": "test"}]

    with patch("pipeline.download_twitch_vod", return_value=(str(tmp_path / "v.mp4"), "2026-04-27")), \
         patch("pipeline.extract_audio"), \
         patch("pipeline.transcribe", return_value=sample_segments), \
         patch("pipeline.select_highlights", return_value=clips_with_known_score), \
         patch("pipeline.get_audio_peaks", return_value=[{"start": 0.0, "end": 15.0}]), \
         patch("pipeline.batch_extract", return_value=[{"file": str(tmp_path / "c.mp4"), "meta": clips_with_known_score[0]}]):
        run(cfg)

    # Score should have been boosted to 0.6
    assert clips_with_known_score[0]["score"] == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def test_arg_parser_defaults():
    parser = _build_arg_parser()
    args = parser.parse_args([])
    assert args.source_type is None
    assert args.max_clips is None


def test_arg_parser_sets_source_type():
    parser = _build_arg_parser()
    args = parser.parse_args(["--source-type", "vodvod"])
    assert args.source_type == "vodvod"


def test_arg_parser_sets_max_clips():
    parser = _build_arg_parser()
    args = parser.parse_args(["--max-clips", "20"])
    assert args.max_clips == 20


def test_arg_parser_force_flag():
    parser = _build_arg_parser()
    args = parser.parse_args([])
    assert args.force is False
    args = parser.parse_args(["--force"])
    assert args.force is True


# ---------------------------------------------------------------------------
# Skip-if-already-processed guard
# ---------------------------------------------------------------------------

def test_run_skips_when_manifest_already_exists(tmp_path, sample_segments, sample_clips):
    cfg = {
        "source_type": "vodvod",
        "vodvod_channel": "@somestreamer",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "all",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
    }
    vod_date = "2026-06-01"
    # Pre-seed the dated manifest with content
    output_subdir = tmp_path / "clips" / "somestreamer" / vod_date
    output_subdir.mkdir(parents=True, exist_ok=True)
    manifest = output_subdir / "clips_manifest.json"
    seeded = [{"file": "x.mp4", "meta": {"reason": "hype"}}]
    manifest.write_text(json.dumps(seeded))

    with _mock_pipeline(tmp_path, sample_segments, sample_clips, vod_date=vod_date) as mocks:
        result = run(cfg)

    # Heavy work should have been skipped
    mocks["transcribe"].assert_not_called()
    mocks["select"].assert_not_called()
    mocks["extract"].assert_not_called()
    assert result == seeded


def test_run_force_regenerates_even_if_manifest_exists(tmp_path, sample_segments, sample_clips):
    cfg = {
        "source_type": "vodvod",
        "vodvod_channel": "@somestreamer",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "all",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
        "force": True,
    }
    vod_date = "2026-06-01"
    output_subdir = tmp_path / "clips" / "somestreamer" / vod_date
    output_subdir.mkdir(parents=True, exist_ok=True)
    (output_subdir / "clips_manifest.json").write_text(json.dumps([{"file": "x.mp4", "meta": {}}]))

    with _mock_pipeline(tmp_path, sample_segments, sample_clips, vod_date=vod_date) as mocks:
        run(cfg)

    # Heavy work runs because force=True
    mocks["transcribe"].assert_called_once()
    mocks["select"].assert_called_once()


def test_run_skip_ignores_empty_manifest(tmp_path, sample_segments, sample_clips):
    """An empty manifest file should NOT trigger the skip guard."""
    cfg = {
        "source_type": "vodvod",
        "vodvod_channel": "@somestreamer",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "all",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
    }
    vod_date = "2026-06-01"
    output_subdir = tmp_path / "clips" / "somestreamer" / vod_date
    output_subdir.mkdir(parents=True, exist_ok=True)
    (output_subdir / "clips_manifest.json").write_text("[]")

    with _mock_pipeline(tmp_path, sample_segments, sample_clips, vod_date=vod_date) as mocks:
        run(cfg)

    mocks["transcribe"].assert_called_once()


# ---------------------------------------------------------------------------
# Per-streamer namespacing
# ---------------------------------------------------------------------------

def test_run_vodvod_namespaces_output_by_channel_and_date(tmp_path, sample_segments, sample_clips):
    cfg = {
        "source_type": "vodvod",
        "vodvod_channel": "@somestreamer",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "all",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
    }
    vod_date = "2026-06-01"
    namespaced_clips = tmp_path / "clips" / "somestreamer" / vod_date
    namespaced_downloads = tmp_path / "downloads" / "somestreamer"

    with _mock_pipeline(tmp_path, sample_segments, sample_clips, vod_date=vod_date) as mocks:
        run(cfg)

    # Downloads stay at <streamer>/ (one mp4 per streamer is fine)
    stream_args = mocks["stream"].call_args[0]
    assert str(namespaced_downloads) in stream_args[1]

    # Manifest at clips/<streamer>/<date>/clips_manifest.json
    manifest = namespaced_clips / "clips_manifest.json"
    assert manifest.exists()


def test_run_uses_cached_transcript_if_present(tmp_path, sample_segments, sample_clips):
    """If <wav_basename>.transcript.json exists, skip the transcribe call entirely."""
    cfg = {
        "source_type": "twitch",
        "twitch_vod_url": "https://www.twitch.tv/videos/123",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "all",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
    }
    os.makedirs(cfg["output_dir"], exist_ok=True)

    # Pre-seed a cached transcript next to the wav path the pipeline will derive.
    wav_basename = str(tmp_path / "video")
    transcript_path = wav_basename + ".transcript.json"
    cached_segments = [{"start": 0.0, "end": 1.0, "text": "cached", "confidence": -0.1}]
    with open(transcript_path, "w") as f:
        json.dump(cached_segments, f)

    with _mock_pipeline(tmp_path, sample_segments, sample_clips, video_filename="video.mp4") as mocks:
        run(cfg)

    # transcribe should NOT have been called — cached transcript was used
    mocks["transcribe"].assert_not_called()


def test_run_writes_transcript_cache_after_transcribe(tmp_path, sample_segments, sample_clips):
    cfg = {
        "source_type": "twitch",
        "twitch_vod_url": "https://www.twitch.tv/videos/123",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "all",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
    }
    os.makedirs(cfg["output_dir"], exist_ok=True)

    with _mock_pipeline(tmp_path, sample_segments, sample_clips, video_filename="video.mp4") as mocks:
        run(cfg)

    transcript_path = str(tmp_path / "video.transcript.json")
    assert os.path.exists(transcript_path)
    with open(transcript_path) as f:
        cached = json.load(f)
    assert cached == sample_segments


def test_run_twitch_does_not_namespace(tmp_path, sample_segments, sample_clips):
    """Twitch source has no auto-namespace — output_dir is used as-is."""
    cfg = {
        "source_type": "twitch",
        "twitch_vod_url": "https://www.twitch.tv/videos/123",
        "quality": "720p",
        "download_dir": str(tmp_path / "downloads"),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "all",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
    }
    os.makedirs(cfg["output_dir"], exist_ok=True)

    with _mock_pipeline(tmp_path, sample_segments, sample_clips, vod_date="2026-04-27") as mocks:
        run(cfg)

    manifest = os.path.join(cfg["output_dir"], mocks["vod_date"], "clips_manifest.json")
    assert os.path.exists(manifest)


# ---------------------------------------------------------------------------
# Source cleanup (disk reclaim)
# ---------------------------------------------------------------------------

def test_cleanup_removes_vod_and_wav_in_download_dir(tmp_path):
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    vod = download_dir / "2026-05-13.mp4"
    wav = download_dir / "2026-05-13.wav"
    vod.write_bytes(b"x" * 1024 * 1024)     # 1 MB
    wav.write_bytes(b"x" * 512 * 1024)      # 0.5 MB

    freed = _cleanup_source_artifacts(str(vod), None, str(wav), str(download_dir))

    assert not vod.exists()
    assert not wav.exists()
    assert freed > 1.0   # at least the VOD's MB


def test_cleanup_removes_windowed_trim_too(tmp_path):
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    vod = download_dir / "2026-05-13.mp4"
    trim = download_dir / "2026-05-13_w3600-5400.mp4"
    wav = download_dir / "2026-05-13_w3600-5400.wav"
    vod.write_bytes(b"x" * 1024)
    trim.write_bytes(b"x" * 1024)
    wav.write_bytes(b"x" * 1024)

    _cleanup_source_artifacts(str(vod), str(trim), str(wav), str(download_dir))

    assert not vod.exists()
    assert not trim.exists()
    assert not wav.exists()


def test_cleanup_skips_files_outside_download_dir(tmp_path):
    """User-owned local source (outside download_dir) must never be deleted."""
    user_dir = tmp_path / "user_videos"
    user_dir.mkdir()
    user_mp4 = user_dir / "my_stream.mp4"
    user_mp4.write_bytes(b"precious")

    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    wav = download_dir / "my_stream.wav"
    wav.write_bytes(b"derived")

    freed = _cleanup_source_artifacts(str(user_mp4), None, str(wav), str(download_dir))

    assert user_mp4.exists(), "user's source file must not be touched"
    assert not wav.exists(), "wav (we created it in download_dir) should be deleted"
    assert freed > 0


def test_cleanup_keeps_transcript_json(tmp_path):
    """Transcript is small and enables fast re-runs — must survive cleanup."""
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    vod = download_dir / "2026-05-13.mp4"
    wav = download_dir / "2026-05-13.wav"
    transcript = download_dir / "2026-05-13.transcript.json"
    vod.write_bytes(b"x")
    wav.write_bytes(b"x")
    transcript.write_text("[]")

    _cleanup_source_artifacts(str(vod), None, str(wav), str(download_dir))

    assert transcript.exists(), "transcript.json must NOT be deleted by cleanup"


def test_cleanup_handles_missing_files(tmp_path):
    """Cleanup must not crash when an expected file isn't on disk (mock paths,
    cached-skip paths, etc)."""
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    freed = _cleanup_source_artifacts(
        str(download_dir / "does_not_exist.mp4"),
        None,
        str(download_dir / "also_missing.wav"),
        str(download_dir),
    )
    assert freed == 0.0


def test_is_inside_detects_path_membership(tmp_path):
    parent = tmp_path / "downloads"
    parent.mkdir()
    inside = parent / "video.mp4"
    inside.write_bytes(b"x")
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"x")

    assert _is_inside(str(inside), str(parent))
    assert not _is_inside(str(outside), str(parent))


def test_run_cleans_up_by_default(tmp_path, sample_segments, sample_clips):
    """End-to-end: a successful run deletes the VOD and WAV it created."""
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    # Pre-create the files the mocks "produced" so cleanup has something to delete.
    vod = download_dir / "video.mp4"
    wav = download_dir / "video.wav"
    vod.write_bytes(b"x" * 2048)
    wav.write_bytes(b"x" * 1024)

    cfg = {
        "source_type": "twitch",
        "twitch_vod_url": "https://www.twitch.tv/videos/123",
        "quality": "720p",
        "download_dir": str(download_dir),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "reaction",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
        "burn_subtitles": False,
        "cleanup_source": True,
    }

    clip_file = str(tmp_path / "clips" / "clip_001.mp4")
    with patch("pipeline.download_twitch_vod", return_value=(str(vod), "2026-04-27")), \
         patch("pipeline.extract_audio", return_value=str(wav)), \
         patch("pipeline.transcribe", return_value=sample_segments), \
         patch("pipeline.select_highlights", return_value=sample_clips[:]), \
         patch("pipeline.get_audio_peaks", return_value=[]), \
         patch("pipeline.batch_extract",
               return_value=[{"file": clip_file, "meta": sample_clips[0]}]):
        run(cfg)

    assert not vod.exists(), "VOD should be deleted after a successful run"
    assert not wav.exists(), "WAV should be deleted after a successful run"


def test_run_keeps_vod_when_cleanup_disabled(tmp_path, sample_segments, sample_clips):
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    vod = download_dir / "video.mp4"
    wav = download_dir / "video.wav"
    vod.write_bytes(b"x" * 1024)
    wav.write_bytes(b"x" * 1024)

    cfg = {
        "source_type": "twitch",
        "twitch_vod_url": "https://www.twitch.tv/videos/123",
        "quality": "720p",
        "download_dir": str(download_dir),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "reaction",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
        "burn_subtitles": False,
        "cleanup_source": False,   # opt out
    }

    clip_file = str(tmp_path / "clips" / "clip_001.mp4")
    with patch("pipeline.download_twitch_vod", return_value=(str(vod), "2026-04-27")), \
         patch("pipeline.extract_audio", return_value=str(wav)), \
         patch("pipeline.transcribe", return_value=sample_segments), \
         patch("pipeline.select_highlights", return_value=sample_clips[:]), \
         patch("pipeline.get_audio_peaks", return_value=[]), \
         patch("pipeline.batch_extract",
               return_value=[{"file": clip_file, "meta": sample_clips[0]}]):
        run(cfg)

    assert vod.exists()
    assert wav.exists()


def test_run_cleanup_skipped_on_manifest_cache_hit(tmp_path, sample_segments, sample_clips):
    """Skip-guard returns early — cleanup must not touch the cached VOD."""
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    streamer_dir = download_dir / "somestreamer"
    streamer_dir.mkdir()
    vod = streamer_dir / "2026-06-01.mp4"
    vod.write_bytes(b"x" * 1024)

    cfg = {
        "source_type": "vodvod",
        "vodvod_channel": "@somestreamer",
        "quality": "720p",
        "download_dir": str(download_dir),
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "clip_mode": "all",
        "max_clips": 5,
        "clip_padding_seconds": 3,
        "output_dir": str(tmp_path / "clips"),
        "cleanup_source": True,
    }
    # Seed manifest so the skip-guard fires.
    output_subdir = tmp_path / "clips" / "somestreamer" / "2026-06-01"
    output_subdir.mkdir(parents=True, exist_ok=True)
    (output_subdir / "clips_manifest.json").write_text(
        json.dumps([{"file": "x.mp4", "meta": {"reason": "hype"}}])
    )

    with _mock_pipeline(tmp_path, sample_segments, sample_clips, vod_date="2026-06-01"):
        run(cfg)

    assert vod.exists(), "cached VOD must not be deleted on a skip-guard return"


def test_arg_parser_keep_vod_flag():
    parser = _build_arg_parser()
    args = parser.parse_args([])
    assert args.keep_vod is False
    args = parser.parse_args(["--keep-vod"])
    assert args.keep_vod is True
