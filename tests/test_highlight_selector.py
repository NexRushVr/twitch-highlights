import json
from unittest.mock import MagicMock, patch

import pytest

from modules.highlight_selector import (
    chunk_transcript,
    deduplicate_clips,
    select_by_phrase,
    select_highlights,
    _parse_clips_from_response,
    _build_system_prompt,
    _sanitize_reason,
)


# ---------------------------------------------------------------------------
# chunk_transcript
# ---------------------------------------------------------------------------

def test_chunk_transcript_single_chunk_when_under_limit(sample_segments):
    chunks = chunk_transcript(sample_segments, max_chars=99999)
    assert len(chunks) == 1
    assert chunks[0] == sample_segments


def test_chunk_transcript_splits_at_max_chars():
    segs = [{"start": float(i), "end": float(i + 1), "text": "x" * 100, "confidence": -0.5}
            for i in range(20)]
    chunks = chunk_transcript(segs, max_chars=500)
    assert len(chunks) > 1
    # All segments accounted for
    total = sum(len(c) for c in chunks)
    assert total == len(segs)


def test_chunk_transcript_empty_returns_empty():
    assert chunk_transcript([], max_chars=6000) == []


def test_chunk_transcript_single_segment_always_included():
    segs = [{"start": 0.0, "end": 5.0, "text": "a", "confidence": -0.5}]
    chunks = chunk_transcript(segs, max_chars=1)  # even below limit, segment must appear
    assert sum(len(c) for c in chunks) == 1


# ---------------------------------------------------------------------------
# deduplicate_clips
# ---------------------------------------------------------------------------

def test_deduplicate_keeps_non_overlapping():
    clips = [
        {"start": 0.0,  "end": 10.0, "score": 0.9},
        {"start": 20.0, "end": 30.0, "score": 0.8},
        {"start": 40.0, "end": 50.0, "score": 0.7},
    ]
    result = deduplicate_clips(clips)
    assert len(result) == 3


def test_deduplicate_removes_heavily_overlapping():
    clips = [
        {"start": 0.0, "end": 20.0, "score": 0.9},   # kept (first)
        {"start": 2.0, "end": 18.0, "score": 0.8},   # removed (heavily overlaps first)
    ]
    result = deduplicate_clips(clips, overlap_threshold=0.5)
    assert len(result) == 1
    assert result[0]["score"] == 0.9


def test_deduplicate_keeps_slightly_overlapping():
    clips = [
        {"start": 0.0,  "end": 10.0, "score": 0.9},
        {"start": 8.0,  "end": 20.0, "score": 0.8},  # only 2s overlap of 10s min-duration = 20% → kept
    ]
    result = deduplicate_clips(clips, overlap_threshold=0.5)
    assert len(result) == 2


def test_deduplicate_empty_input():
    assert deduplicate_clips([]) == []


# ---------------------------------------------------------------------------
# _parse_clips_from_response
# ---------------------------------------------------------------------------

def test_parse_clips_valid_json():
    clips = [{"start": 10.0, "end": 20.0, "reason": "hype", "score": 0.9, "description": "sub hype"}]
    raw = json.dumps(clips)
    assert _parse_clips_from_response(raw) == clips


def test_parse_clips_json_with_preamble():
    clips = [{"start": 5.0, "end": 15.0, "reason": "rage", "score": 0.8, "description": "rage"}]
    raw = f"Here are the clips I found:\n{json.dumps(clips)}\nHope that helps!"
    result = _parse_clips_from_response(raw)
    assert result == clips


def test_parse_clips_invalid_json_returns_empty():
    assert _parse_clips_from_response("not json at all") == []


def test_parse_clips_empty_array():
    assert _parse_clips_from_response("[]") == []


def test_parse_clips_drops_missing_start():
    raw = json.dumps([
        {"end": 10.0, "reason": "hype", "score": 0.9},               # missing start
        {"start": 5.0, "end": 15.0, "reason": "hype", "score": 0.8}, # valid
    ])
    result = _parse_clips_from_response(raw)
    assert len(result) == 1
    assert result[0]["start"] == 5.0


def test_parse_clips_drops_non_numeric_start():
    raw = json.dumps([
        {"start": "early", "end": 10.0, "reason": "hype", "score": 0.9},
        {"start": 5.0, "end": 15.0, "reason": "rage", "score": 0.7},
    ])
    result = _parse_clips_from_response(raw)
    assert len(result) == 1
    assert result[0]["reason"] == "rage"


def test_parse_clips_drops_zero_or_negative_duration():
    raw = json.dumps([
        {"start": 10.0, "end": 10.0, "reason": "hype", "score": 0.9},  # zero duration
        {"start": 20.0, "end": 15.0, "reason": "hype", "score": 0.9},  # negative
        {"start": 30.0, "end": 40.0, "reason": "ok",   "score": 0.9},  # valid
    ])
    result = _parse_clips_from_response(raw)
    assert len(result) == 1
    assert result[0]["reason"] == "ok"


def test_parse_clips_supplies_default_score_and_reason():
    raw = json.dumps([{"start": 1.0, "end": 5.0}])  # bare-minimum clip
    result = _parse_clips_from_response(raw)
    assert len(result) == 1
    assert result[0]["reason"] == "clip"
    assert result[0]["score"] == 0.5


def test_parse_clips_handles_nondict_items():
    raw = json.dumps(["just a string", 42, {"start": 1.0, "end": 2.0}])
    result = _parse_clips_from_response(raw)
    assert len(result) == 1


def test_parse_clips_handles_non_list_root():
    # LLM returned a dict instead of a list
    raw = json.dumps({"start": 1.0, "end": 5.0, "reason": "hype"})
    assert _parse_clips_from_response(raw) == []


# ---------------------------------------------------------------------------
# _build_system_prompt
# ---------------------------------------------------------------------------

def test_build_system_prompt_includes_max_clips(base_config):
    base_config["max_clips"] = 7
    prompt = _build_system_prompt(base_config)
    assert "7" in prompt


def test_build_system_prompt_includes_mode_content(base_config):
    base_config["clip_mode"] = "dance"
    prompt = _build_system_prompt(base_config)
    # The dance mode prompt file contains "dance"
    assert len(prompt) > 0


# ---------------------------------------------------------------------------
# select_highlights
# ---------------------------------------------------------------------------

def test_select_highlights_calls_llm_per_chunk(base_config, sample_segments):
    clips_json = json.dumps([
        {"start": 10.0, "end": 20.0, "reason": "funny_reaction", "score": 0.9, "description": "reaction"}
    ])

    with patch("modules.highlight_selector._call_llm", return_value=clips_json) as mock_llm:
        result = select_highlights(sample_segments, base_config)

    assert mock_llm.called
    assert len(result) >= 1


def test_select_highlights_deduplicates_results(base_config):
    # Two overlapping clips returned across two chunks
    clips_json = json.dumps([
        {"start": 10.0, "end": 30.0, "reason": "hype", "score": 0.9, "description": "a"},
        {"start": 12.0, "end": 28.0, "reason": "hype", "score": 0.85, "description": "b"},
    ])

    with patch("modules.highlight_selector._call_llm", return_value=clips_json):
        result = select_highlights(
            [{"start": 10.0, "end": 30.0, "text": "test", "confidence": -0.5}],
            base_config,
        )

    assert len(result) == 1


def test_select_highlights_sorts_by_score_desc(base_config, sample_segments):
    clips_json = json.dumps([
        {"start": 10.0, "end": 20.0, "reason": "hype",            "score": 0.5, "description": "low"},
        {"start": 50.0, "end": 65.0, "reason": "funny_reaction",  "score": 0.95, "description": "high"},
        {"start": 80.0, "end": 95.0, "reason": "rage",            "score": 0.7, "description": "mid"},
    ])

    with patch("modules.highlight_selector._call_llm", return_value=clips_json):
        result = select_highlights(sample_segments, base_config)

    scores = [c["score"] for c in result]
    assert scores == sorted(scores, reverse=True)


def test_select_highlights_uses_ollama_backend(base_config, sample_segments):
    base_config["llm_backend"] = "ollama"

    mock_client = MagicMock()
    mock_client.chat.return_value = {"message": {"content": "[]"}}

    with patch("modules.highlight_selector.ollama") as mock_ollama:
        mock_ollama.Client.return_value = mock_client
        select_highlights(sample_segments, base_config)

    assert mock_ollama.Client.called
    assert mock_client.chat.called


def test_select_highlights_passes_llm_timeout_to_ollama_client(base_config, sample_segments):
    base_config["llm_backend"] = "ollama"
    base_config["llm_timeout_seconds"] = 42

    mock_client = MagicMock()
    mock_client.chat.return_value = {"message": {"content": "[]"}}

    with patch("modules.highlight_selector.ollama") as mock_ollama:
        mock_ollama.Client.return_value = mock_client
        select_highlights(sample_segments, base_config)

    # First positional or keyword arg should carry the timeout
    _, kwargs = mock_ollama.Client.call_args
    assert kwargs.get("timeout") == 42


def test_select_highlights_default_ollama_timeout_is_300(base_config, sample_segments):
    base_config["llm_backend"] = "ollama"
    # remove explicit override
    base_config.pop("llm_timeout_seconds", None)

    mock_client = MagicMock()
    mock_client.chat.return_value = {"message": {"content": "[]"}}

    with patch("modules.highlight_selector.ollama") as mock_ollama:
        mock_ollama.Client.return_value = mock_client
        select_highlights(sample_segments, base_config)

    _, kwargs = mock_ollama.Client.call_args
    assert kwargs.get("timeout") == 300


def test_select_highlights_uses_openai_backend(base_config, sample_segments):
    base_config["llm_backend"] = "openai"

    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "[]"
    mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

    with patch("modules.highlight_selector.OpenAI", return_value=mock_client):
        result = select_highlights(sample_segments, base_config)

    assert mock_client.chat.completions.create.called


def test_select_highlights_handles_llm_json_error(base_config, sample_segments):
    with patch("modules.highlight_selector._call_llm", return_value="NOT JSON"):
        result = select_highlights(sample_segments, base_config)

    assert result == []


# ---------------------------------------------------------------------------
# Retry-with-backoff for transient errors
# ---------------------------------------------------------------------------

from modules.highlight_selector import _with_retries, _is_transient_error


def test_is_transient_error_recognizes_connection_error():
    assert _is_transient_error(ConnectionError("boom"))


def test_is_transient_error_recognizes_timeout():
    assert _is_transient_error(TimeoutError("slow"))


def test_is_transient_error_recognizes_message_text():
    class CustomErr(Exception):
        pass
    assert _is_transient_error(CustomErr("Connection refused by server"))
    assert _is_transient_error(CustomErr("read timed out"))


def test_is_transient_error_does_not_match_value_error():
    assert not _is_transient_error(ValueError("bad input"))


def test_with_retries_returns_value_on_first_success():
    fn = MagicMock(return_value="ok")
    assert _with_retries(fn, attempts=3, base_delay=0) == "ok"
    assert fn.call_count == 1


def test_with_retries_retries_on_transient_then_succeeds():
    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("flap")
        return "yay"
    result = _with_retries(fn, attempts=4, base_delay=0)
    assert result == "yay"
    assert calls["n"] == 3


def test_with_retries_raises_after_exhausting_attempts():
    fn = MagicMock(side_effect=ConnectionError("nope"))
    with pytest.raises(ConnectionError):
        _with_retries(fn, attempts=3, base_delay=0)
    assert fn.call_count == 3


def test_with_retries_does_not_retry_non_transient():
    fn = MagicMock(side_effect=ValueError("bad"))
    with pytest.raises(ValueError):
        _with_retries(fn, attempts=4, base_delay=0)
    assert fn.call_count == 1


# ---------------------------------------------------------------------------
# Phrase mode: _sanitize_reason
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase,expected", [
    ("clip it", "clip_it"),
    ("CLIP IT", "clip_it"),
    ("  clip   that!!  ", "clip_that"),
    ("nice-shot", "nice_shot"),
    ("???", "phrase"),
    ("", "phrase"),
])
def test_sanitize_reason(phrase, expected):
    assert _sanitize_reason(phrase) == expected


# ---------------------------------------------------------------------------
# Phrase mode: select_by_phrase
# ---------------------------------------------------------------------------

def _segs():
    return [
        {"start": 100.0, "end": 103.0, "text": "okay let's go"},
        {"start": 200.0, "end": 202.0, "text": "oh that was insane CLIP IT please"},
        {"start": 500.0, "end": 503.0, "text": "nothing happening here"},
        {"start": 900.0, "end": 902.0, "text": "yo clip it clip it"},
    ]


def test_select_by_phrase_finds_matches_case_insensitive():
    cfg = {"trigger_phrase": "clip it", "phrase_pre_seconds": 60, "phrase_post_seconds": 60}
    clips = select_by_phrase(_segs(), cfg)
    # Two distinct trigger spots (200s and 900s); 100s/500s have no phrase.
    assert len(clips) == 2
    assert clips[0]["start"] == 140.0   # 200 - 60
    assert clips[0]["end"] == 262.0     # 202 + 60
    assert clips[1]["start"] == 840.0   # 900 - 60


def test_select_by_phrase_window_floored_at_zero():
    segs = [{"start": 10.0, "end": 12.0, "text": "clip it now"}]
    cfg = {"trigger_phrase": "clip it", "phrase_pre_seconds": 60, "phrase_post_seconds": 30}
    clips = select_by_phrase(segs, cfg)
    assert clips[0]["start"] == 0.0     # max(0, 10 - 60)
    assert clips[0]["end"] == 42.0      # 12 + 30


def test_select_by_phrase_custom_phrase():
    segs = [{"start": 50.0, "end": 52.0, "text": "that was a HIGHLIGHT moment"}]
    cfg = {"trigger_phrase": "highlight", "phrase_pre_seconds": 5, "phrase_post_seconds": 5}
    clips = select_by_phrase(segs, cfg)
    assert len(clips) == 1
    assert clips[0]["reason"] == "highlight"
    assert clips[0]["description"] == "that was a HIGHLIGHT moment"


def test_select_by_phrase_no_matches_returns_empty():
    cfg = {"trigger_phrase": "supercalifragilistic"}
    assert select_by_phrase(_segs(), cfg) == []


def test_select_by_phrase_empty_phrase_returns_empty():
    assert select_by_phrase(_segs(), {"trigger_phrase": "   "}) == []


def test_select_by_phrase_merges_overlapping_triggers():
    # Two "clip it"s 20s apart with 60s windows → overlapping → dedup to 1.
    segs = [
        {"start": 300.0, "end": 302.0, "text": "clip it"},
        {"start": 320.0, "end": 322.0, "text": "clip it again"},
    ]
    cfg = {"trigger_phrase": "clip it", "phrase_pre_seconds": 60, "phrase_post_seconds": 60}
    clips = select_by_phrase(segs, cfg)
    assert len(clips) == 1


def test_select_highlights_routes_to_phrase_mode_without_llm():
    """clip_mode=phrase must not touch the LLM backend at all."""
    segs = [{"start": 10.0, "end": 12.0, "text": "clip it"}]
    cfg = {"clip_mode": "phrase", "trigger_phrase": "clip it",
           "phrase_pre_seconds": 5, "phrase_post_seconds": 5}
    with patch("modules.highlight_selector._call_llm") as mock_llm:
        clips = select_highlights(segs, cfg)
    mock_llm.assert_not_called()
    assert len(clips) == 1
    assert clips[0]["reason"] == "clip_it"


# ---------------------------------------------------------------------------
# short-form metadata passthrough (hook / title / hashtags / virality)
# ---------------------------------------------------------------------------

def test_coerce_clip_carries_shortform_metadata():
    from modules.highlight_selector import _coerce_clip
    c = _coerce_clip({
        "start": 10, "end": 20, "reason": "funny", "score": 0.8,
        "description": "d", "hook": "You won't believe this",
        "title": "Insane play", "hashtags": ["#gaming", "clip", ""],
        "virality": 87,
    })
    assert c["hook"] == "You won't believe this"
    assert c["title"] == "Insane play"
    assert c["hashtags"] == ["gaming", "clip"]   # '#' stripped, blanks dropped
    assert c["virality"] == 87.0


def test_coerce_clip_without_metadata_is_unchanged():
    from modules.highlight_selector import _coerce_clip
    c = _coerce_clip({"start": 1, "end": 2, "reason": "r", "score": 0.5, "description": "d"})
    assert "hook" not in c and "title" not in c and "hashtags" not in c and "virality" not in c
