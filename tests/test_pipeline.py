import json
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from pipeline import run, _build_arg_parser


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
         patch("pipeline.extract_audio", return_value=wav_path) as mock_audio, \
         patch("pipeline.transcribe", return_value=sample_segments) as mock_trans, \
         patch("pipeline.select_highlights", return_value=sample_clips[:]) as mock_sel, \
         patch("pipeline.get_audio_peaks", return_value=[{"start": 8.0, "end": 25.0}]) as mock_peaks, \
         patch("pipeline.batch_extract", return_value=[{"file": clip_file, "meta": sample_clips[0]}]) as mock_extract, \
         patch("pipeline.caption_clip") as mock_caption:
        yield {
            "dl": mock_dl, "m3u8": mock_m3u8, "stream": mock_stream,
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

    mocks["dl"].assert_called_once_with(cfg["twitch_vod_url"], cfg["quality"], cfg["download_dir"])
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
