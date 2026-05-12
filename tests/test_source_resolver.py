import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from modules.source_resolver import (
    download_twitch_vod,
    get_latest_vodvod_m3u8,
    get_latest_kick_vod_m3u8,
    stream_m3u8_to_file,
)


# ---------------------------------------------------------------------------
# download_twitch_vod
# ---------------------------------------------------------------------------

def test_download_twitch_vod_returns_path_and_date(tmp_path):
    video_id = "987654321"
    info_file = tmp_path / f"{video_id}.info.json"
    info_file.write_text(json.dumps({"id": video_id, "upload_date": "20260115"}))
    video_file = tmp_path / f"{video_id}.mp4"
    video_file.touch()

    with patch("modules.source_resolver.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        path, date = download_twitch_vod(
            "https://www.twitch.tv/videos/987654321", "720p", str(tmp_path)
        )

    assert path == str(video_file)
    assert date == "2026-01-15"
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert "yt-dlp" in cmd
    assert "720" in " ".join(cmd)


def test_download_twitch_vod_falls_back_to_today_for_missing_date(tmp_path):
    info_file = tmp_path / "abc.info.json"
    info_file.write_text(json.dumps({"id": "abc"}))  # no upload_date
    (tmp_path / "abc.mp4").touch()

    with patch("modules.source_resolver.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        path, date = download_twitch_vod("https://twitch.tv/videos/abc", "720p", str(tmp_path))

    # Today's date in YYYY-MM-DD
    assert len(date) == 10 and date[4] == "-" and date[7] == "-"


def test_download_twitch_vod_strips_quality_suffix(tmp_path):
    info_file = tmp_path / "abc.info.json"
    info_file.write_text(json.dumps({"id": "abc", "upload_date": "20260101"}))
    (tmp_path / "abc.mp4").touch()

    with patch("modules.source_resolver.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        download_twitch_vod("https://twitch.tv/videos/abc", "1080p", str(tmp_path))

    cmd = mock_run.call_args[0][0]
    assert "1080" in " ".join(cmd)
    assert "1080p" not in " ".join(cmd)


def test_download_twitch_vod_raises_if_no_info_json(tmp_path):
    with patch("modules.source_resolver.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        with pytest.raises(FileNotFoundError, match="No .info.json"):
            download_twitch_vod("https://twitch.tv/videos/x", "720p", str(tmp_path))


def test_download_twitch_vod_raises_if_video_missing(tmp_path):
    info_file = tmp_path / "xyz.info.json"
    info_file.write_text(json.dumps({"id": "xyz", "upload_date": "20260101"}))
    # No actual video file

    with patch("modules.source_resolver.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        with pytest.raises(FileNotFoundError, match="xyz"):
            download_twitch_vod("https://twitch.tv/videos/xyz", "720p", str(tmp_path))


def test_download_twitch_vod_finds_mkv_fallback(tmp_path):
    info_file = tmp_path / "vid.info.json"
    info_file.write_text(json.dumps({"id": "vid", "upload_date": "20260101"}))
    (tmp_path / "vid.mkv").touch()

    with patch("modules.source_resolver.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        path, _ = download_twitch_vod("https://twitch.tv/videos/vid", "720p", str(tmp_path))

    assert path.endswith("vid.mkv")


# ---------------------------------------------------------------------------
# get_latest_vodvod_m3u8
# ---------------------------------------------------------------------------

def _make_playwright_mock(items):
    """items: list of dicts {href, card_html} as the scraper's evaluate() returns."""
    mock_page = MagicMock()
    mock_browser = MagicMock()
    mock_browser.new_page.return_value = mock_page

    mock_page.evaluate.return_value = list(items)

    mock_p = MagicMock()
    mock_p.chromium.launch.return_value = mock_browser

    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_p)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    mock_ctx._page = mock_page

    return mock_ctx


def test_get_latest_vodvod_m3u8_prefers_chunked():
    items = [
        {"href": "https://cdn.example.com/regular/playlist.m3u8", "card_html": "<div>2026-04-26T22:00:00Z</div>"},
        {"href": "https://cdn.example.com/chunked/index.m3u8",   "card_html": "<div>2026-04-27T22:00:00Z</div>"},
    ]
    mock_ctx = _make_playwright_mock(items)

    with patch("modules.source_resolver.sync_playwright", return_value=mock_ctx):
        url, date = get_latest_vodvod_m3u8("@testchannel")

    assert "chunked" in url
    assert date == "2026-04-27"


def test_get_latest_vodvod_m3u8_falls_back_to_first():
    items = [{"href": "https://cdn.example.com/stream/playlist.m3u8", "card_html": "<div>2026-01-15T12:00:00Z</div>"}]
    mock_ctx = _make_playwright_mock(items)

    with patch("modules.source_resolver.sync_playwright", return_value=mock_ctx):
        url, date = get_latest_vodvod_m3u8("@testchannel")

    assert url == items[0]["href"]
    assert date == "2026-01-15"


def test_get_latest_vodvod_m3u8_uses_today_when_no_iso_date():
    items = [{"href": "https://cdn.example.com/chunked/index.m3u8", "card_html": "<div>no date here</div>"}]
    mock_ctx = _make_playwright_mock(items)

    with patch("modules.source_resolver.sync_playwright", return_value=mock_ctx):
        url, date = get_latest_vodvod_m3u8("@testchannel")

    # YYYY-MM-DD shape
    assert len(date) == 10 and date[4] == "-" and date[7] == "-"


def test_get_latest_vodvod_m3u8_raises_when_none_found():
    mock_ctx = _make_playwright_mock([])

    with patch("modules.source_resolver.sync_playwright", return_value=mock_ctx):
        with pytest.raises(ValueError, match="No m3u8"):
            get_latest_vodvod_m3u8("@emptychannel")


def test_get_latest_vodvod_m3u8_raises_without_playwright():
    with patch("modules.source_resolver.sync_playwright", None):
        with pytest.raises(ImportError, match="playwright"):
            get_latest_vodvod_m3u8("@any")


# ---------------------------------------------------------------------------
# get_latest_kick_vod_m3u8
# ---------------------------------------------------------------------------

def _make_curl_mock(status_code, payload):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = json.dumps(payload) if isinstance(payload, (list, dict)) else str(payload)
    mock = MagicMock()
    mock.get.return_value = resp
    return mock


def test_get_latest_kick_vod_m3u8_returns_first_item():
    payload = [
        {"source": "https://stream.kick.com/abc/master.m3u8", "created_at": "2026-05-06 03:31:29"},
        {"source": "https://stream.kick.com/old/master.m3u8", "created_at": "2026-05-04 03:31:29"},
    ]
    mock_curl = _make_curl_mock(200, payload)

    with patch("modules.source_resolver._curl_requests", mock_curl):
        url, date = get_latest_kick_vod_m3u8("testchannel")

    assert url == "https://stream.kick.com/abc/master.m3u8"
    assert date == "2026-05-06"
    mock_curl.get.assert_called_once()
    called_url = mock_curl.get.call_args[0][0]
    assert "testchannel" in called_url


def test_get_latest_kick_vod_m3u8_strips_at_prefix():
    payload = [{"source": "https://stream.kick.com/x/master.m3u8", "created_at": "2026-05-01 12:00:00"}]
    mock_curl = _make_curl_mock(200, payload)

    with patch("modules.source_resolver._curl_requests", mock_curl):
        get_latest_kick_vod_m3u8("@testchannel")

    called_url = mock_curl.get.call_args[0][0]
    assert "@" not in called_url.split("/channels/")[-1]


def test_get_latest_kick_vod_m3u8_falls_back_to_today_for_bad_date():
    payload = [{"source": "https://stream.kick.com/x/master.m3u8", "created_at": ""}]
    mock_curl = _make_curl_mock(200, payload)

    with patch("modules.source_resolver._curl_requests", mock_curl):
        _, date = get_latest_kick_vod_m3u8("testchannel")

    assert len(date) == 10 and date[4] == "-" and date[7] == "-"


def test_get_latest_kick_vod_m3u8_raises_on_empty_list():
    mock_curl = _make_curl_mock(200, [])

    with patch("modules.source_resolver._curl_requests", mock_curl):
        with pytest.raises(ValueError, match="No VODs"):
            get_latest_kick_vod_m3u8("testchannel")


def test_get_latest_kick_vod_m3u8_raises_on_missing_source():
    payload = [{"created_at": "2026-05-06 03:31:29"}]  # no source field
    mock_curl = _make_curl_mock(200, payload)

    with patch("modules.source_resolver._curl_requests", mock_curl):
        with pytest.raises(ValueError, match="no .*m3u8"):
            get_latest_kick_vod_m3u8("testchannel")


def test_get_latest_kick_vod_m3u8_raises_on_non_200():
    mock_curl = _make_curl_mock(403, {"error": "blocked"})

    with patch("modules.source_resolver._curl_requests", mock_curl):
        with pytest.raises(RuntimeError, match="403"):
            get_latest_kick_vod_m3u8("testchannel")


def test_get_latest_kick_vod_m3u8_raises_without_curl_cffi():
    with patch("modules.source_resolver._curl_requests", None):
        with pytest.raises(ImportError, match="curl_cffi"):
            get_latest_kick_vod_m3u8("testchannel")


# ---------------------------------------------------------------------------
# stream_m3u8_to_file
# ---------------------------------------------------------------------------

def test_stream_m3u8_to_file_calls_ffmpeg(tmp_path):
    out = str(tmp_path / "out.mp4")
    with patch("modules.source_resolver.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = stream_m3u8_to_file("https://example.com/stream.m3u8", out)

    assert result == out
    cmd = mock_run.call_args[0][0]
    assert "ffmpeg" in cmd
    assert "https://example.com/stream.m3u8" in cmd
    assert out in cmd


def test_stream_m3u8_to_file_creates_parent_dir(tmp_path):
    out = str(tmp_path / "nested" / "dir" / "out.mp4")
    with patch("modules.source_resolver.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        stream_m3u8_to_file("https://example.com/s.m3u8", out)

    assert (tmp_path / "nested" / "dir").exists()
