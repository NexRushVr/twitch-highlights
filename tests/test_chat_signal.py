from unittest.mock import patch

import pytest

from modules import chat_signal as cs
from modules.chat_signal import ChatMessage, ChatUnavailable


# ---------------------------------------------------------------------------
# message_weight (Phase 5 hype weighting)
# ---------------------------------------------------------------------------

def test_message_weight_baseline_and_hype():
    assert cs.message_weight("") == 1.0
    assert cs.message_weight("hello chat") == 1.0           # no hype tokens
    assert cs.message_weight("OMEGALUL") > 1.0
    # a flood of hype is heavier, but capped (1.0 + max 6.0)
    assert cs.message_weight("LUL " * 50) == pytest.approx(7.0)
    # phrase match contributes
    assert cs.message_weight("clip it now") > cs.message_weight("calm chat here")


# ---------------------------------------------------------------------------
# detect_chat_spikes (Phase 1 + density boundaries)
# ---------------------------------------------------------------------------

def _flat_then_spike():
    msgs = []
    # baseline: 1 msg every 5s for 300s
    for t in range(0, 300, 5):
        msgs.append(ChatMessage(float(t), "hi", 1.0))
    # spike: 40 messages around t=150-160
    for _ in range(40):
        msgs.append(ChatMessage(152.0, "OMEGALUL", cs.message_weight("OMEGALUL")))
    return msgs


def test_detect_chat_spikes_finds_the_spike():
    spikes = cs.detect_chat_spikes(_flat_then_spike(), 300.0, {
        "chat_bucket_seconds": 5, "chat_spike_z": 2.0,
        "chat_spike_min_messages": 5, "max_clips": 10,
    })
    assert spikes, "expected at least one spike"
    top = spikes[0]
    assert top["reason"] == "chat_spike"
    assert top["start"] <= 152.0 <= top["end"]
    assert 0.5 <= top["score"] <= 1.0


def test_detect_chat_spikes_empty_inputs():
    assert cs.detect_chat_spikes([], 300.0, {}) == []
    assert cs.detect_chat_spikes(_flat_then_spike(), 0.0, {}) == []


# ---------------------------------------------------------------------------
# boost + windows (Phase 2 / Phase 3 helpers)
# ---------------------------------------------------------------------------

def test_boost_clips_with_chat_bumps_overlap():
    clips = [{"start": 100.0, "end": 110.0, "score": 0.5},
             {"start": 200.0, "end": 210.0, "score": 0.5}]
    spikes = [{"start": 95.0, "end": 115.0, "_peak": 105.0}]
    cs.boost_clips_with_chat(clips, spikes, {"chat_score_boost": 0.2})
    assert clips[0]["score"] == pytest.approx(0.7)   # overlaps the spike
    assert clips[1]["score"] == pytest.approx(0.5)   # no overlap


def test_boost_clips_with_chat_interval_overlap_cases():
    """Regression: the old peak/start-only check missed spike-end-in-clip and
    spike-fully-contains-clip; proper interval overlap must catch both."""
    cfg = {"chat_score_boost": 0.2}
    # spike END falls inside the clip (spike starts before, ends inside)
    c1 = [{"start": 100.0, "end": 120.0, "score": 0.5}]
    cs.boost_clips_with_chat(c1, [{"start": 90.0, "end": 105.0}], cfg)
    assert c1[0]["score"] == pytest.approx(0.7)
    # spike fully CONTAINS the clip (peak may be outside the clip window)
    c2 = [{"start": 100.0, "end": 110.0, "score": 0.5}]
    cs.boost_clips_with_chat(c2, [{"start": 80.0, "end": 200.0, "_peak": 180.0}], cfg)
    assert c2[0]["score"] == pytest.approx(0.7)
    # genuinely disjoint -> no boost
    c3 = [{"start": 100.0, "end": 110.0, "score": 0.5}]
    cs.boost_clips_with_chat(c3, [{"start": 200.0, "end": 210.0}], cfg)
    assert c3[0]["score"] == pytest.approx(0.5)


def test_json_or_unavailable_raises_chatunavailable_on_html():
    class _Resp:
        def json(self):
            raise ValueError("Expecting value")
    with pytest.raises(ChatUnavailable, match="invalid JSON"):
        cs._json_or_unavailable(_Resp(), "https://x/y")


def test_spike_windows_merges_close_windows():
    spikes = [{"start": 10.0, "end": 20.0}, {"start": 25.0, "end": 30.0},
              {"start": 200.0, "end": 210.0}]
    wins = cs.spike_windows(spikes, pad=0.0, merge_gap=10.0)
    assert wins == [(10.0, 30.0), (200.0, 210.0)]


# ---------------------------------------------------------------------------
# id / time parsing
# ---------------------------------------------------------------------------

def test_twitch_video_id_parses_url_and_bare():
    assert cs.twitch_video_id("https://www.twitch.tv/videos/2790261388") == "2790261388"
    assert cs.twitch_video_id("v2790261388") == "2790261388"
    assert cs.twitch_video_id("2790261388") == "2790261388"
    assert cs.twitch_video_id("") is None
    # highlight URL forms
    assert cs.twitch_video_id("https://www.twitch.tv/somechan/v/2790261388") == "2790261388"
    assert cs.twitch_video_id("https://www.twitch.tv/somechan/video/2790261388") == "2790261388"
    # a clip slug has no numeric id
    assert cs.twitch_video_id("https://clips.twitch.tv/SomeFunnyClipSlug") is None


def test_parse_iso_handles_kick_and_iso():
    a = cs._parse_iso("2026-06-06 23:37:37")
    b = cs._parse_iso("2026-06-06T23:37:37Z")
    assert a == b


# ---------------------------------------------------------------------------
# fetch_twitch_vod_chat (mocked GraphQL)
# ---------------------------------------------------------------------------

def _gql_page(edges, has_next):
    return [{"data": {"video": {"comments": {
        "edges": edges,
        "pageInfo": {"hasNextPage": has_next},
    }}}}]


def _edge(offset, text, cursor):
    return {"cursor": cursor, "node": {
        "contentOffsetSeconds": offset,
        "message": {"fragments": [{"text": text}]},
    }}


def test_fetch_twitch_vod_chat_pages_and_parses():
    pages = [
        _gql_page([_edge(10, "hi", "c1"), _edge(12, "OMEGALUL", "c2")], True),
        _gql_page([_edge(20, "Pog", "c3")], False),
    ]
    with patch.object(cs, "_post_json", side_effect=pages):
        msgs = cs.fetch_twitch_vod_chat("2790261388")
    assert [m.offset for m in msgs] == [10.0, 12.0, 20.0]
    assert msgs[1].weight > 1.0   # OMEGALUL weighted


def test_fetch_twitch_vod_chat_raises_when_vod_gone():
    with patch.object(cs, "_post_json", return_value=[{"data": {"video": None}}]):
        with pytest.raises(ChatUnavailable, match="unavailable"):
            cs.fetch_twitch_vod_chat("999")


# ---------------------------------------------------------------------------
# fetch_kick_vod_chat (mocked pages) + dispatch
# ---------------------------------------------------------------------------

def test_fetch_kick_vod_chat_walks_back_to_start():
    start = cs._parse_iso("2026-06-06T00:00:00Z")
    # page 1 (seek at end): empty + cursor; page 2: msgs near end; page 3: msgs at start
    def fake_page(channel_id, start_time=None, cursor=None):
        if start_time:
            return [], "cur1"
        if cursor == "cur1":
            return [{"id": "a", "created_at": "2026-06-06T00:50:00Z", "content": "late"}], "cur2"
        return [{"id": "b", "created_at": "2026-06-06T00:01:00Z", "content": "early Pog"}], "cur3"
    with patch.object(cs, "_kick_page", side_effect=fake_page):
        msgs = cs.fetch_kick_vod_chat(668, start, 3600.0)
    offs = sorted(m.offset for m in msgs)
    assert offs == [60.0, 3000.0]   # 1min and 50min into the VOD


def test_fetch_chat_for_source_rejects_vodvod():
    with pytest.raises(ChatUnavailable, match="vodvod"):
        cs.fetch_chat_for_source({"source_type": "vodvod"})
