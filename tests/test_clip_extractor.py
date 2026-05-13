import os
from unittest.mock import MagicMock, patch

import pytest

from modules.clip_extractor import ATTRIBUTION_COMMENT, extract_clip, batch_extract


def _find_metadata_value(cmd, key):
    """Return the value of a -metadata key=value pair in cmd, or None if not present."""
    prefix = f"{key}="
    for i, arg in enumerate(cmd):
        if arg == "-metadata" and i + 1 < len(cmd) and cmd[i + 1].startswith(prefix):
            return cmd[i + 1][len(prefix):]
    return None


# ---------------------------------------------------------------------------
# extract_clip
# ---------------------------------------------------------------------------

def test_extract_clip_returns_output_path(tmp_path):
    out = str(tmp_path / "clip.mp4")
    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = extract_clip("/video.mp4", 30.0, 50.0, out, padding=3.0)
    assert result == out


def test_extract_clip_start_and_duration_with_padding():
    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        extract_clip("/video.mp4", 30.0, 50.0, "/out.mp4", padding=3.0)

    cmd = mock_run.call_args[0][0]
    ss_idx = cmd.index("-ss")
    t_idx = cmd.index("-t")
    assert float(cmd[ss_idx + 1]) == pytest.approx(27.0)
    assert float(cmd[t_idx + 1]) == pytest.approx(26.0)  # (50+3) - (30-3) = 26


def test_extract_clip_clamps_start_to_zero():
    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        extract_clip("/video.mp4", 1.0, 10.0, "/out.mp4", padding=5.0)

    cmd = mock_run.call_args[0][0]
    ss_idx = cmd.index("-ss")
    assert float(cmd[ss_idx + 1]) == 0.0


def test_extract_clip_zero_padding():
    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        extract_clip("/video.mp4", 10.0, 20.0, "/out.mp4", padding=0.0)

    cmd = mock_run.call_args[0][0]
    ss_idx = cmd.index("-ss")
    t_idx = cmd.index("-t")
    assert float(cmd[ss_idx + 1]) == 10.0
    assert float(cmd[t_idx + 1]) == 10.0


def test_extract_clip_uses_libx264():
    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        extract_clip("/v.mp4", 0.0, 10.0, "/o.mp4")

    cmd = mock_run.call_args[0][0]
    assert "libx264" in cmd
    assert "aac" in cmd


# ---------------------------------------------------------------------------
# batch_extract
# ---------------------------------------------------------------------------

def test_batch_extract_creates_output_dir(tmp_path, sample_clips, base_config):
    base_config["output_dir"] = str(tmp_path / "new_clips")
    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        batch_extract("/video.mp4", sample_clips, base_config)
    assert os.path.isdir(base_config["output_dir"])


def test_batch_extract_returns_file_and_meta(tmp_path, sample_clips, base_config):
    base_config["output_dir"] = str(tmp_path / "clips")
    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = batch_extract("/video.mp4", sample_clips, base_config)

    assert len(result) == len(sample_clips)
    for item in result:
        assert "file" in item
        assert "meta" in item


def test_batch_extract_filenames_include_reason(tmp_path, sample_clips, base_config):
    base_config["output_dir"] = str(tmp_path / "clips")
    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = batch_extract("/video.mp4", sample_clips, base_config)

    for item in result:
        assert item["meta"]["reason"] in item["file"]


def test_batch_extract_filenames_are_numbered(tmp_path, sample_clips, base_config):
    base_config["output_dir"] = str(tmp_path / "clips")
    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = batch_extract("/video.mp4", sample_clips, base_config)

    assert "001" in result[0]["file"]
    assert "002" in result[1]["file"]
    assert "003" in result[2]["file"]


def test_batch_extract_passes_padding_from_config(tmp_path, base_config):
    base_config["output_dir"] = str(tmp_path / "clips")
    base_config["clip_padding_seconds"] = 5.0
    clips = [{"start": 20.0, "end": 30.0, "reason": "hype", "score": 0.9}]

    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        batch_extract("/video.mp4", clips, base_config)

    cmd = mock_run.call_args[0][0]
    ss_idx = cmd.index("-ss")
    assert float(cmd[ss_idx + 1]) == pytest.approx(15.0)  # 20 - 5 padding


def test_batch_extract_empty_clips_returns_empty(tmp_path, base_config):
    base_config["output_dir"] = str(tmp_path / "clips")
    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = batch_extract("/video.mp4", [], base_config)
    assert result == []


# ---------------------------------------------------------------------------
# Attribution metadata
# ---------------------------------------------------------------------------

def test_extract_clip_embeds_attribution_comment():
    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        extract_clip("/v.mp4", 0.0, 10.0, "/o.mp4")

    cmd = mock_run.call_args[0][0]
    assert _find_metadata_value(cmd, "comment") == ATTRIBUTION_COMMENT


def test_extract_clip_embeds_title_and_description_when_provided():
    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        extract_clip(
            "/v.mp4", 0.0, 10.0, "/o.mp4",
            title="funny_reaction",
            description="Streamer reacts to surprise jumpscare",
        )

    cmd = mock_run.call_args[0][0]
    assert _find_metadata_value(cmd, "title") == "funny_reaction"
    assert _find_metadata_value(cmd, "description") == "Streamer reacts to surprise jumpscare"


def test_extract_clip_omits_optional_metadata_when_absent():
    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        extract_clip("/v.mp4", 0.0, 10.0, "/o.mp4")

    cmd = mock_run.call_args[0][0]
    assert _find_metadata_value(cmd, "title") is None
    assert _find_metadata_value(cmd, "description") is None


def test_batch_extract_passes_reason_as_title_and_description(tmp_path, base_config):
    base_config["output_dir"] = str(tmp_path / "clips")
    clips = [{"start": 0.0, "end": 10.0, "reason": "hype", "score": 0.9,
              "description": "Crowd goes wild as streamer hits a clutch shot"}]

    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        batch_extract("/video.mp4", clips, base_config)

    cmd = mock_run.call_args[0][0]
    assert _find_metadata_value(cmd, "title") == "hype"
    assert _find_metadata_value(cmd, "description") == "Crowd goes wild as streamer hits a clutch shot"
    assert _find_metadata_value(cmd, "comment") == ATTRIBUTION_COMMENT


def test_batch_extract_skips_blank_description(tmp_path, base_config):
    base_config["output_dir"] = str(tmp_path / "clips")
    clips = [{"start": 0.0, "end": 10.0, "reason": "hype", "score": 0.9, "description": ""}]

    with patch("modules.clip_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        batch_extract("/video.mp4", clips, base_config)

    cmd = mock_run.call_args[0][0]
    assert _find_metadata_value(cmd, "description") is None
