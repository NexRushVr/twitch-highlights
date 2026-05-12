from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from modules.audio_extractor import extract_audio, get_audio_peaks


# ---------------------------------------------------------------------------
# extract_audio
# ---------------------------------------------------------------------------

def test_extract_audio_returns_wav_path(tmp_path):
    video = str(tmp_path / "video.mp4")
    wav = str(tmp_path / "audio.wav")

    with patch("modules.audio_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = extract_audio(video, wav)

    assert result == wav


def test_extract_audio_uses_correct_ffmpeg_args(tmp_path):
    video = str(tmp_path / "video.mp4")
    wav = str(tmp_path / "audio.wav")

    with patch("modules.audio_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        extract_audio(video, wav)

    cmd = mock_run.call_args[0][0]
    assert "ffmpeg" in cmd
    assert "-vn" in cmd
    assert "-ar" in cmd
    assert "16000" in cmd
    assert "-ac" in cmd
    assert "1" in cmd
    assert "-f" in cmd
    assert "wav" in cmd
    assert video in cmd
    assert wav in cmd


def test_extract_audio_passes_y_flag(tmp_path):
    video = str(tmp_path / "v.mp4")
    wav = str(tmp_path / "a.wav")

    with patch("modules.audio_extractor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        extract_audio(video, wav)

    cmd = mock_run.call_args[0][0]
    assert "-y" in cmd


# ---------------------------------------------------------------------------
# get_audio_peaks
# ---------------------------------------------------------------------------

def _mock_librosa(y_values, db_values, frame_times):
    mock_lib = MagicMock()
    mock_lib.load.return_value = (y_values, 16000)
    mock_lib.feature.rms.return_value = [np.array(db_values)]
    mock_lib.amplitude_to_db.return_value = np.array(db_values)
    mock_lib.frames_to_time.return_value = np.array(frame_times)
    return mock_lib


def test_get_audio_peaks_detects_loud_segment():
    db = [-30, -30, -10, -10, -10, -30, -30]
    times = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    mock_lib = _mock_librosa(np.zeros(100), db, times)

    with patch("modules.audio_extractor.librosa", mock_lib):
        peaks = get_audio_peaks("/fake/audio.wav", threshold_db=-20.0)

    assert len(peaks) == 1
    assert peaks[0]["start"] == 1.0
    assert peaks[0]["end"] == 2.5


def test_get_audio_peaks_detects_multiple_peaks():
    db = [-30, -10, -10, -30, -30, -10, -30]
    times = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    mock_lib = _mock_librosa(np.zeros(100), db, times)

    with patch("modules.audio_extractor.librosa", mock_lib):
        peaks = get_audio_peaks("/fake/audio.wav", threshold_db=-20.0)

    assert len(peaks) == 2


def test_get_audio_peaks_returns_empty_for_quiet_audio():
    db = [-40, -40, -40, -40]
    times = [0.0, 0.5, 1.0, 1.5]
    mock_lib = _mock_librosa(np.zeros(100), db, times)

    with patch("modules.audio_extractor.librosa", mock_lib):
        peaks = get_audio_peaks("/fake/audio.wav", threshold_db=-20.0)

    assert peaks == []


def test_get_audio_peaks_closes_open_peak_at_eof():
    db = [-30, -10, -10, -10]  # peak never drops below threshold
    times = [0.0, 0.5, 1.0, 1.5]
    mock_lib = _mock_librosa(np.zeros(100), db, times)

    with patch("modules.audio_extractor.librosa", mock_lib):
        peaks = get_audio_peaks("/fake/audio.wav", threshold_db=-20.0)

    assert len(peaks) == 1
    assert peaks[0]["end"] == 1.5


def test_get_audio_peaks_raises_without_librosa():
    with patch("modules.audio_extractor.librosa", None):
        with pytest.raises(ImportError, match="librosa"):
            get_audio_peaks("/fake/audio.wav")
