import json
import os
import pytest

from config import DEFAULT_CONFIG, load_config


REQUIRED_KEYS = {
    "source_type", "twitch_vod_url", "vodvod_channel", "kick_channel", "m3u8_url",
    "quality", "download_dir",
    "whisper_model", "whisper_device", "whisper_language",
    "llm_backend", "ollama_model", "openai_model", "openai_api_key",
    "clip_mode", "max_clips", "min_clip_duration", "max_clip_duration", "clip_padding_seconds",
    "output_dir", "burn_subtitles",
}


def test_default_config_has_all_keys():
    assert REQUIRED_KEYS.issubset(DEFAULT_CONFIG.keys())


def test_default_config_types():
    assert isinstance(DEFAULT_CONFIG["max_clips"], int)
    assert isinstance(DEFAULT_CONFIG["min_clip_duration"], int)
    assert isinstance(DEFAULT_CONFIG["max_clip_duration"], int)
    assert isinstance(DEFAULT_CONFIG["clip_padding_seconds"], int)
    assert isinstance(DEFAULT_CONFIG["burn_subtitles"], bool)
    assert isinstance(DEFAULT_CONFIG["source_type"], str)


def test_load_config_returns_defaults_when_no_path():
    cfg = load_config()
    assert cfg == DEFAULT_CONFIG
    assert cfg is not DEFAULT_CONFIG  # must be a copy


def test_load_config_merges_json_file(tmp_path):
    override = {"max_clips": 99, "clip_mode": "dance", "burn_subtitles": False}
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(override))

    cfg = load_config(str(config_file))

    assert cfg["max_clips"] == 99
    assert cfg["clip_mode"] == "dance"
    assert cfg["burn_subtitles"] is False
    # Unoverridden keys should still be defaults
    assert cfg["whisper_model"] == DEFAULT_CONFIG["whisper_model"]


def test_load_config_missing_file_uses_defaults(tmp_path):
    cfg = load_config(str(tmp_path / "nonexistent.json"))
    assert cfg == DEFAULT_CONFIG


def test_load_config_handles_utf8_bom(tmp_path):
    # The Windows installer writes config.json via PowerShell, which prepends a
    # UTF-8 BOM. Regression guard: load_config must read it, not choke on the BOM.
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"max_clips": 7}), encoding="utf-8-sig")
    cfg = load_config(str(config_file))
    assert cfg["max_clips"] == 7


def test_load_config_raises_clear_error_on_bad_json(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text("{not valid json")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_config(str(config_file))


def test_load_config_env_var_string_override(monkeypatch):
    monkeypatch.setenv("VOD_CLIP_OLLAMA_MODEL", "llama3")
    cfg = load_config()
    assert cfg["ollama_model"] == "llama3"


def test_load_config_env_var_int_override(monkeypatch):
    monkeypatch.setenv("VOD_CLIP_MAX_CLIPS", "25")
    cfg = load_config()
    assert cfg["max_clips"] == 25


def test_load_config_env_var_bool_override_true(monkeypatch):
    monkeypatch.setenv("VOD_CLIP_BURN_SUBTITLES", "true")
    cfg = load_config()
    assert cfg["burn_subtitles"] is True


def test_load_config_env_var_bool_override_false(monkeypatch):
    monkeypatch.setenv("VOD_CLIP_BURN_SUBTITLES", "0")
    cfg = load_config()
    assert cfg["burn_subtitles"] is False


def test_load_config_env_takes_precedence_over_file(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"max_clips": 5}))
    monkeypatch.setenv("VOD_CLIP_MAX_CLIPS", "42")

    cfg = load_config(str(config_file))
    assert cfg["max_clips"] == 42


# ---------------------------------------------------------------------------
# validation added by the 10-lens review
# ---------------------------------------------------------------------------

def test_invalid_caption_style_raises():
    with pytest.raises(ValueError, match="caption_style"):
        load_config_with({"caption_style": "karake"})


def test_bad_env_override_raises_clear_error(monkeypatch):
    monkeypatch.setenv("VOD_CLIP_CHAT_BUCKET_SECONDS", "not-a-number")
    with pytest.raises(ValueError, match="VOD_CLIP_CHAT_BUCKET_SECONDS"):
        load_config()


def test_clip_metadata_is_registered_default():
    # so the env-override loop + docs/example pick it up
    assert DEFAULT_CONFIG.get("clip_metadata") is True


def load_config_with(overrides, tmp_path=None):
    """Write overrides to a temp config and load it (helper for validation tests)."""
    import json, tempfile, os
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(overrides, f)
    try:
        return load_config(path)
    finally:
        os.remove(path)
