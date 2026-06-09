import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from modules.source_resolver import (
    apply_time_window,
    download_twitch_vod,
    get_latest_vodvod_m3u8,
    get_latest_kick_vod_m3u8,
    resolve_local_file,
    stream_m3u8_to_file,
    _parse_time_string,
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
    """items: list of dicts {href, text} as the per-card evaluate() returns
    (text = "<title>\\n<ISO date>\\n|\\n<duration>...")."""
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


def test_get_latest_vodvod_m3u8_picks_newest_by_date_and_returns_title():
    items = [
        {"href": "https://api.vodvod.top/m3u8/100/a/index.m3u8",
         "text": "Old stream\n2026-04-26T22:00:00Z\n|\n1:00:00"},
        {"href": "https://api.vodvod.top/m3u8/200/b/index.m3u8",
         "text": "New stream night\n2026-04-27T22:00:00Z\n|\n2:00:00"},
    ]
    mock_ctx = _make_playwright_mock(items)

    with patch("modules.source_resolver.sync_playwright", return_value=mock_ctx):
        url, date, title, duration = get_latest_vodvod_m3u8("@testchannel")

    assert "/200/" in url
    assert date == "2026-04-27"
    assert title == "New stream night"
    assert duration == 2 * 3600   # "2:00:00" from the winning card


def test_get_latest_vodvod_m3u8_newest_even_when_older_is_first():
    """Regression: vodvod's DOM is not reliably newest-first. Picking the first
    anchor used to grab an older (already-cached) VOD; pick by date instead."""
    items = [
        {"href": "https://api.vodvod.top/m3u8/100/a/index.m3u8",
         "text": "Older\n2026-05-01T10:00:00Z\n|\n3:00:00"},   # first in DOM, but OLD
        {"href": "https://api.vodvod.top/m3u8/200/b/index.m3u8",
         "text": "Newer\n2026-05-10T10:00:00Z\n|\n4:00:00"},
    ]
    mock_ctx = _make_playwright_mock(items)

    with patch("modules.source_resolver.sync_playwright", return_value=mock_ctx):
        url, date, title, duration = get_latest_vodvod_m3u8("@testchannel")

    assert "/200/" in url and date == "2026-05-10" and title == "Newer"
    assert duration == 4 * 3600


def test_get_latest_vodvod_m3u8_uses_today_when_no_iso_date():
    items = [{"href": "https://api.vodvod.top/m3u8/1/a/index.m3u8", "text": "No date here"}]
    mock_ctx = _make_playwright_mock(items)

    with patch("modules.source_resolver.sync_playwright", return_value=mock_ctx):
        url, date, title, duration = get_latest_vodvod_m3u8("@testchannel")

    assert len(date) == 10 and date[4] == "-" and date[7] == "-"
    assert title == "No date here"
    assert duration is None


def test_parse_card_duration_handles_hms_mmss_and_ignores_iso():
    from modules.source_resolver import _parse_card_duration
    # Real card shape: title, ISO timestamp (has T...Z), '|', then the length.
    card = "[18+] TIGER STREAM\n2026-06-04T22:04:30Z\n|\n5:01:51\n819\n|\nmoonbuvr"
    assert _parse_card_duration(card) == 5 * 3600 + 1 * 60 + 51
    assert _parse_card_duration("Short\n2026-06-04T22:04:30Z\n|\n45:12") == 45 * 60 + 12
    assert _parse_card_duration("No length\n2026-06-04T22:04:30Z") is None


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

def _fake_ffmpeg_writes_part(cmd, **kwargs):
    # ffmpeg writes to the .part target (cmd's last arg); mimic a finished file.
    with open(cmd[-1], "wb") as f:
        f.write(b"downloaded data")
    return MagicMock(returncode=0)


def test_stream_m3u8_to_file_calls_ffmpeg(tmp_path):
    out = str(tmp_path / "out.mp4")
    with patch("modules.source_resolver.subprocess.run", side_effect=_fake_ffmpeg_writes_part) as mock_run:
        result = stream_m3u8_to_file("https://example.com/stream.m3u8", out)

    assert result == out
    cmd = mock_run.call_args[0][0]
    assert "ffmpeg" in cmd
    assert "https://example.com/stream.m3u8" in cmd
    assert out + ".part" in cmd            # downloads to .part, renamed on success
    # The .part extension can't tell ffmpeg the muxer, so the format must be forced
    # (regression: without `-f mp4` ffmpeg errors "Unable to choose an output format").
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "mp4"
    assert cmd.index("-f") < cmd.index(out + ".part")   # -f applies to the output
    assert os.path.exists(out)             # atomically moved into place
    assert not os.path.exists(out + ".part")


def test_stream_m3u8_to_file_creates_parent_dir(tmp_path):
    out = str(tmp_path / "nested" / "dir" / "out.mp4")
    with patch("modules.source_resolver.subprocess.run", side_effect=_fake_ffmpeg_writes_part):
        stream_m3u8_to_file("https://example.com/s.m3u8", out)

    assert (tmp_path / "nested" / "dir").exists()


# ---------------------------------------------------------------------------
# resolve_local_file
# ---------------------------------------------------------------------------

def test_resolve_local_file_mp4_returns_path_in_place(tmp_path):
    src = tmp_path / "stream_recording.mp4"
    src.write_bytes(b"\x00" * 16)
    download_dir = tmp_path / "downloads"

    with patch("modules.source_resolver.subprocess.run") as mock_run:
        path, date = resolve_local_file(str(src), str(download_dir))

    # mp4 path: no ffmpeg invocation, no copy.
    mock_run.assert_not_called()
    assert path == str(src.resolve()) or path == str(src.absolute())
    assert len(date) == 10 and date[4] == "-" and date[7] == "-"


def test_resolve_local_file_mp4_does_not_create_download_dir(tmp_path):
    src = tmp_path / "stream.mp4"
    src.write_bytes(b"data")
    download_dir = tmp_path / "should_not_exist"

    with patch("modules.source_resolver.subprocess.run"):
        resolve_local_file(str(src), str(download_dir))

    assert not download_dir.exists(), "mp4 path should not touch download_dir"


def test_resolve_local_file_ts_transcodes_to_mp4(tmp_path):
    src = tmp_path / "stream.ts"
    src.write_bytes(b"\x47\x40\x00" * 100)  # arbitrary bytes; ffmpeg is mocked
    download_dir = tmp_path / "downloads"

    with patch("modules.source_resolver.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        path, date = resolve_local_file(str(src), str(download_dir))

    cmd = mock_run.call_args[0][0]
    assert "ffmpeg" in cmd
    assert str(src) in cmd
    # Stream-copy: no re-encode.
    assert "-c" in cmd and "copy" in cmd
    assert "aac_adtstoasc" in cmd
    # Output mp4 lands inside download_dir keyed by date.
    assert path.endswith(f"{date}.mp4")
    assert download_dir.name in path


def test_resolve_local_file_ts_skips_transcode_when_cached(tmp_path):
    src = tmp_path / "stream.ts"
    src.write_bytes(b"data")
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    # Pre-create a non-empty cached mp4 keyed by today / src mtime date.
    from datetime import datetime, timezone
    mtime = datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc)
    cached = download_dir / f"{mtime.strftime('%Y-%m-%d')}.mp4"
    cached.write_bytes(b"cached")

    with patch("modules.source_resolver.subprocess.run") as mock_run:
        path, _ = resolve_local_file(str(src), str(download_dir))

    mock_run.assert_not_called()
    assert path == str(cached)


def test_resolve_local_file_missing_raises(tmp_path):
    missing = tmp_path / "nope.mp4"
    with pytest.raises(FileNotFoundError, match="local source file"):
        resolve_local_file(str(missing), str(tmp_path))


def test_resolve_local_file_unsupported_extension_raises(tmp_path):
    src = tmp_path / "stream.mkv"
    src.write_bytes(b"data")
    with pytest.raises(ValueError, match="unsupported local file extension"):
        resolve_local_file(str(src), str(tmp_path))


def test_resolve_local_file_uses_mtime_for_vod_date(tmp_path):
    import os as _os
    from datetime import datetime, timezone

    src = tmp_path / "stream.mp4"
    src.write_bytes(b"data")
    target_ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    _os.utime(src, (target_ts, target_ts))

    with patch("modules.source_resolver.subprocess.run"):
        _, date = resolve_local_file(str(src), str(tmp_path / "dl"))

    assert date == "2026-01-15"


# ---------------------------------------------------------------------------
# _parse_time_string
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("s,expected", [
    ("0", 0.0),
    ("30", 30.0),
    ("3600", 3600.0),
    ("30:00", 1800.0),
    ("1:30", 90.0),
    ("1:00:00", 3600.0),
    ("1:30:00", 5400.0),
    ("0:00:01.5", 1.5),
    ("  1:00:00  ", 3600.0),
])
def test_parse_time_string_valid(s, expected):
    assert _parse_time_string(s) == pytest.approx(expected)


@pytest.mark.parametrize("s", [
    "",
    "abc",
    "1:bad:00",
    "1:2:3:4",       # too many components
    "-1",            # negative bare seconds
    "1:-30:00",      # negative component
])
def test_parse_time_string_invalid(s):
    with pytest.raises(ValueError):
        _parse_time_string(s)


# ---------------------------------------------------------------------------
# apply_time_window
# ---------------------------------------------------------------------------

def _mock_probe_then_trim(duration: float):
    """Mock subprocess.run so the first call (ffprobe) returns `duration` and
    subsequent calls (ffmpeg trim) succeed silently."""
    def side_effect(cmd, *a, **kw):
        if cmd[0] == "ffprobe":
            return MagicMock(returncode=0, stdout=f"{duration}\n")
        return MagicMock(returncode=0)
    return side_effect


def test_apply_time_window_returns_input_when_both_bounds_empty(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"data")
    with patch("modules.source_resolver.subprocess.run") as mock_run:
        out = apply_time_window(str(src), None, None, str(tmp_path / "dl"))
    mock_run.assert_not_called()
    assert out == str(src)


def test_apply_time_window_trims_to_both_bounds(tmp_path):
    src = tmp_path / "vod.mp4"
    src.write_bytes(b"data")
    download_dir = tmp_path / "dl"

    with patch("modules.source_resolver.subprocess.run", side_effect=_mock_probe_then_trim(7200.0)) as mock_run:
        out = apply_time_window(str(src), "1:00:00", "1:30:00", str(download_dir))

    # 1st call = ffprobe, 2nd = ffmpeg trim
    assert mock_run.call_count == 2
    trim_cmd = mock_run.call_args_list[1][0][0]
    assert trim_cmd[0] == "ffmpeg"
    assert trim_cmd[trim_cmd.index("-ss") + 1] == "3600.0"
    assert trim_cmd[trim_cmd.index("-t") + 1] == "1800.0"
    assert "-c" in trim_cmd and "copy" in trim_cmd  # stream-copy
    assert out.endswith("vod_w3600-5400.mp4")
    assert str(download_dir) in out


def test_apply_time_window_only_start_trims_to_end(tmp_path):
    src = tmp_path / "vod.mp4"
    src.write_bytes(b"data")

    with patch("modules.source_resolver.subprocess.run", side_effect=_mock_probe_then_trim(3600.0)) as mock_run:
        out = apply_time_window(str(src), "0:30:00", None, str(tmp_path / "dl"))

    trim_cmd = mock_run.call_args_list[1][0][0]
    assert trim_cmd[trim_cmd.index("-ss") + 1] == "1800.0"
    assert trim_cmd[trim_cmd.index("-t") + 1] == "1800.0"  # 3600 - 1800 = 1800
    assert out.endswith("vod_w1800-3600.mp4")


def test_apply_time_window_only_end_trims_from_zero(tmp_path):
    src = tmp_path / "vod.mp4"
    src.write_bytes(b"data")

    with patch("modules.source_resolver.subprocess.run", side_effect=_mock_probe_then_trim(3600.0)) as mock_run:
        out = apply_time_window(str(src), None, "0:10:00", str(tmp_path / "dl"))

    trim_cmd = mock_run.call_args_list[1][0][0]
    assert trim_cmd[trim_cmd.index("-ss") + 1] == "0.0"
    assert trim_cmd[trim_cmd.index("-t") + 1] == "600.0"
    assert out.endswith("vod_w0-600.mp4")


def test_apply_time_window_skips_trim_when_cached(tmp_path):
    src = tmp_path / "vod.mp4"
    src.write_bytes(b"data")
    download_dir = tmp_path / "dl"
    download_dir.mkdir()
    cached = download_dir / "vod_w0-600.mp4"
    cached.write_bytes(b"cached")

    with patch("modules.source_resolver.subprocess.run", side_effect=_mock_probe_then_trim(3600.0)) as mock_run:
        out = apply_time_window(str(src), None, "0:10:00", str(download_dir))

    # ffprobe still runs to validate bounds, but ffmpeg trim is skipped.
    assert mock_run.call_count == 1
    assert mock_run.call_args_list[0][0][0][0] == "ffprobe"
    assert out == str(cached)


def test_apply_time_window_rejects_start_past_video_end(tmp_path):
    src = tmp_path / "vod.mp4"
    src.write_bytes(b"data")

    with patch("modules.source_resolver.subprocess.run", side_effect=_mock_probe_then_trim(1800.0)):
        with pytest.raises(ValueError, match="start_time .* past the video's end"):
            apply_time_window(str(src), "2:00:00", None, str(tmp_path / "dl"))


def test_apply_time_window_rejects_end_past_video_duration(tmp_path):
    src = tmp_path / "vod.mp4"
    src.write_bytes(b"data")

    with patch("modules.source_resolver.subprocess.run", side_effect=_mock_probe_then_trim(1800.0)):
        with pytest.raises(ValueError, match="end_time .* exceeds video duration"):
            apply_time_window(str(src), None, "1:00:00", str(tmp_path / "dl"))


def test_apply_time_window_rejects_inverted_bounds(tmp_path):
    src = tmp_path / "vod.mp4"
    src.write_bytes(b"data")

    with patch("modules.source_resolver.subprocess.run", side_effect=_mock_probe_then_trim(7200.0)):
        with pytest.raises(ValueError, match="must be after"):
            apply_time_window(str(src), "1:30:00", "1:00:00", str(tmp_path / "dl"))


def test_apply_time_window_accepts_bare_seconds(tmp_path):
    src = tmp_path / "vod.mp4"
    src.write_bytes(b"data")

    with patch("modules.source_resolver.subprocess.run", side_effect=_mock_probe_then_trim(3600.0)) as mock_run:
        apply_time_window(str(src), "60", "120", str(tmp_path / "dl"))

    trim_cmd = mock_run.call_args_list[1][0][0]
    assert trim_cmd[trim_cmd.index("-ss") + 1] == "60.0"
    assert trim_cmd[trim_cmd.index("-t") + 1] == "60.0"


# ---------------------------------------------------------------------------
# stream_m3u8_to_file — atomic download (.part -> rename)
# ---------------------------------------------------------------------------

def test_stream_m3u8_atomic_rename_on_success(tmp_path):
    import modules.source_resolver as sr
    out = str(tmp_path / "vid.mp4")

    def fake_run(cmd, quiet):
        part = cmd[-1]
        assert part.endswith(".part")          # ffmpeg writes to the .part target
        with open(part, "wb") as f:
            f.write(b"complete download")

    with patch.object(sr, "_run_ffmpeg", side_effect=fake_run):
        res = stream_m3u8_to_file("http://x/index.m3u8", out)

    assert res == out
    assert os.path.exists(out)                 # renamed into place
    assert not os.path.exists(out + ".part")   # .part consumed
    with open(out, "rb") as f:
        assert f.read() == b"complete download"


def test_stream_m3u8_removes_part_and_leaves_no_cache_on_failure(tmp_path):
    """An interrupted download must not leave a valid-named (corrupt) cache."""
    import modules.source_resolver as sr
    out = str(tmp_path / "vid.mp4")

    def fake_run(cmd, quiet):
        with open(cmd[-1], "wb") as f:
            f.write(b"truncated, no moov atom")   # partial
        raise subprocess.CalledProcessError(1, cmd)

    with patch.object(sr, "_run_ffmpeg", side_effect=fake_run):
        with pytest.raises(subprocess.CalledProcessError):
            stream_m3u8_to_file("http://x/index.m3u8", out)

    assert not os.path.exists(out)             # no corrupt file masquerading as cache
    assert not os.path.exists(out + ".part")   # partial cleaned up
