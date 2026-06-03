from unittest.mock import MagicMock, patch

import pytest

from modules.transcriber import transcribe


def _make_whisper_mock(segments_data):
    """Build a mock for `whisper` module: load_model returns a model whose .transcribe returns dict."""
    mock_model = MagicMock()
    mock_model.transcribe.return_value = {"segments": segments_data}
    mock_whisper = MagicMock()
    mock_whisper.load_model.return_value = mock_model
    return mock_whisper, mock_model


def test_transcribe_returns_segment_list():
    segs_data = [
        {"start": 0.0,  "end": 5.0,  "text": " Hello world",  "avg_logprob": -0.5},
        {"start": 5.0,  "end": 10.0, "text": " How are you",  "avg_logprob": -0.4},
    ]
    mock_whisper, _ = _make_whisper_mock(segs_data)

    with patch("modules.transcriber.whisper", mock_whisper):
        result = transcribe("/tmp/audio.wav", "base", "cpu")

    assert len(result) == 2
    assert result[0] == {"start": 0.0, "end": 5.0, "text": "Hello world", "confidence": -0.5, "no_speech_prob": 0.0}
    assert result[1] == {"start": 5.0, "end": 10.0, "text": "How are you", "confidence": -0.4, "no_speech_prob": 0.0}


def test_transcribe_strips_whitespace_from_text():
    segs_data = [{"start": 1.0, "end": 3.0, "text": "  lots of spaces   ", "avg_logprob": -0.3}]
    mock_whisper, _ = _make_whisper_mock(segs_data)

    with patch("modules.transcriber.whisper", mock_whisper):
        result = transcribe("/tmp/audio.wav", "base", "cpu")

    assert result[0]["text"] == "lots of spaces"


def test_transcribe_uses_fp16_on_cuda():
    mock_whisper, mock_model = _make_whisper_mock([])

    with patch("modules.transcriber.whisper", mock_whisper):
        transcribe("/tmp/audio.wav", "large-v3", "cuda")

    mock_whisper.load_model.assert_called_once_with("large-v3", device="cuda")
    _, kwargs = mock_model.transcribe.call_args
    assert kwargs["fp16"] is True


def test_transcribe_disables_fp16_on_cpu():
    mock_whisper, mock_model = _make_whisper_mock([])

    with patch("modules.transcriber.whisper", mock_whisper):
        transcribe("/tmp/audio.wav", "base", "cpu")

    mock_whisper.load_model.assert_called_once_with("base", device="cpu")
    _, kwargs = mock_model.transcribe.call_args
    assert kwargs["fp16"] is False


def test_transcribe_rounds_timestamps():
    segs_data = [{"start": 0.123456, "end": 4.987654, "text": "hi", "avg_logprob": -0.123456}]
    mock_whisper, _ = _make_whisper_mock(segs_data)

    with patch("modules.transcriber.whisper", mock_whisper):
        result = transcribe("/tmp/audio.wav", "base", "cpu")

    assert result[0]["start"] == round(0.123456, 2)
    assert result[0]["end"] == round(4.987654, 2)
    assert result[0]["confidence"] == round(-0.123456, 3)


def test_transcribe_returns_empty_list_for_no_segments():
    mock_whisper, _ = _make_whisper_mock([])

    with patch("modules.transcriber.whisper", mock_whisper):
        result = transcribe("/tmp/audio.wav", "base", "cpu")

    assert result == []


def test_transcribe_handles_missing_avg_logprob():
    segs_data = [{"start": 0.0, "end": 1.0, "text": "x"}]  # no avg_logprob
    mock_whisper, _ = _make_whisper_mock(segs_data)

    with patch("modules.transcriber.whisper", mock_whisper):
        result = transcribe("/tmp/audio.wav", "base", "cpu")

    assert result[0]["confidence"] == 0.0


def test_transcribe_raises_without_whisper():
    with patch("modules.transcriber.whisper", None):
        with pytest.raises(ImportError, match="openai-whisper"):
            transcribe("/tmp/audio.wav")
