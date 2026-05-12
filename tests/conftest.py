import pytest


@pytest.fixture
def base_config():
    return {
        "source_type": "twitch",
        "twitch_vod_url": "https://www.twitch.tv/videos/123456789",
        "vodvod_channel": "@testchannel",
        "m3u8_url": "https://example.com/stream.m3u8",
        "quality": "720p",
        "download_dir": "/tmp/downloads",
        "whisper_model": "base",
        "whisper_device": "cpu",
        "whisper_language": "en",
        "llm_backend": "ollama",
        "ollama_model": "mistral",
        "openai_model": "gpt-4o-mini",
        "openai_api_key": "test-key",
        "clip_mode": "reaction",
        "max_clips": 5,
        "min_clip_duration": 8,
        "max_clip_duration": 45,
        "clip_padding_seconds": 3,
        "output_dir": "/tmp/clips",
        "burn_subtitles": False,
    }


@pytest.fixture
def sample_segments():
    return [
        {"start": 10.0, "end": 15.0, "text": "Oh my god that's insane!", "confidence": -0.5},
        {"start": 15.0, "end": 20.0, "text": "I can't believe that just happened", "confidence": -0.4},
        {"start": 30.0, "end": 35.0, "text": "Wait wait wait no no no NOOO", "confidence": -0.3},
        {"start": 100.0, "end": 105.0, "text": "Thanks for the sub!", "confidence": -0.6},
        {"start": 200.0, "end": 210.0, "text": "Let me read chat real quick", "confidence": -0.7},
    ]


@pytest.fixture
def sample_clips():
    return [
        {"start": 8.0,  "end": 20.0,  "reason": "funny_reaction", "score": 0.9,  "description": "Shocked reaction"},
        {"start": 27.0, "end": 38.0,  "reason": "rage",            "score": 0.85, "description": "Rage moment"},
        {"start": 98.0, "end": 108.0, "reason": "hype",            "score": 0.7,  "description": "Sub hype"},
    ]
