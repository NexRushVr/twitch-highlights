import subprocess
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

try:
    from curl_cffi import requests as _curl_requests
except ImportError:
    _curl_requests = None


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def download_twitch_vod(url: str, quality: str, out_dir: str, quiet: bool = False) -> tuple[str, str]:
    """Download a Twitch VOD. Returns (video_path, vod_date_YYYY-MM-DD)."""
    os.makedirs(out_dir, exist_ok=True)
    height = quality.rstrip("p")
    cmd = [
        "yt-dlp",
        "-f", f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
        "--merge-output-format", "mp4",
        "-o", f"{out_dir}/%(id)s.%(ext)s",
        "--write-info-json",
        "--no-playlist",
        url,
    ]
    # yt-dlp has its own --quiet flag which silences the progress bar plus most
    # other chatter; combined with stderr=PIPE the user sees nothing unless
    # the call fails.
    if quiet:
        cmd.insert(1, "--quiet")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    else:
        subprocess.run(cmd, check=True)

    info_files = sorted(
        Path(out_dir).glob("*.info.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not info_files:
        raise FileNotFoundError(f"No .info.json found in {out_dir} after download")

    with open(info_files[0]) as f:
        info = json.load(f)

    # Date: prefer upload_date (YYYYMMDD) from yt-dlp; fall back to today
    upload_date = info.get("upload_date")
    if upload_date and len(upload_date) == 8 and upload_date.isdigit():
        vod_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    else:
        vod_date = _today_iso()

    video_id = info.get("id", info_files[0].stem.replace(".info", ""))
    for ext in ("mp4", "mkv", "webm"):
        candidate = Path(out_dir) / f"{video_id}.{ext}"
        if candidate.exists():
            return str(candidate), vod_date

    raise FileNotFoundError(f"Downloaded video not found for id '{video_id}' in {out_dir}")


_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def get_latest_vodvod_m3u8(channel_handle: str) -> tuple[str, str]:
    """Scrape vodvod.top for the latest VOD. Returns (m3u8_url, vod_date_YYYY-MM-DD)."""
    if sync_playwright is None:
        raise ImportError("playwright is required: pip install playwright && playwright install chromium")

    handle = channel_handle if channel_handle.startswith("@") else f"@{channel_handle}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"https://vodvod.top/channels/{handle}", wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        # For each m3u8 anchor: walk up a few parents to capture the surrounding
        # card HTML, which contains an ISO timestamp like 2026-04-27T22:06:16Z.
        items = page.evaluate("""() => {
            return Array.from(document.querySelectorAll("a[href*='.m3u8']")).map(a => {
                let p = a;
                for (let i = 0; i < 8 && p.parentElement; i++) p = p.parentElement;
                return {href: a.getAttribute('href'), card_html: p.outerHTML};
            });
        }""")
        browser.close()

    if not items:
        raise ValueError(f"No m3u8 URL found on vodvod.top for channel '{channel_handle}'")

    # Pick first chunked / index.m3u8 (latest VOD)
    pick = next(
        (it for it in items if "chunked" in it["href"] or "index.m3u8" in it["href"]),
        items[0],
    )

    iso_match = _ISO_DATE_RE.search(pick["card_html"])
    vod_date = iso_match.group(0)[:10] if iso_match else _today_iso()

    return pick["href"], vod_date


def get_latest_kick_vod_m3u8(channel_slug: str) -> tuple[str, str]:
    """Query kick.com API for the latest VOD. Returns (m3u8_url, vod_date_YYYY-MM-DD).

    Kick fronts its API with Cloudflare, so plain `requests` gets a 403.
    We use curl_cffi with browser impersonation to get past it.
    """
    if _curl_requests is None:
        raise ImportError("curl_cffi is required: pip install 'curl_cffi>=0.10,<0.15'")

    slug = channel_slug.lstrip("@")
    api_url = f"https://kick.com/api/v2/channels/{slug}/videos"
    resp = _curl_requests.get(api_url, impersonate="chrome", timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"Kick API returned {resp.status_code} for '{slug}': {resp.text[:200]}")

    items = resp.json()
    if not items:
        raise ValueError(f"No VODs returned by Kick API for channel '{channel_slug}'")

    # API returns newest first
    pick = items[0]
    m3u8_url = pick.get("source")
    if not m3u8_url:
        raise ValueError(f"Latest Kick VOD for '{channel_slug}' has no `source` m3u8 URL")

    # created_at format: "2026-05-06 03:31:29"
    created = pick.get("created_at") or pick.get("start_time") or ""
    vod_date = created[:10] if len(created) >= 10 and created[4] == "-" else _today_iso()

    return m3u8_url, vod_date


def resolve_local_file(path: str, download_dir: str, quiet: bool = False) -> tuple[str, str]:
    """Resolve a local mp4 or .ts file. Returns (video_path, vod_date_YYYY-MM-DD).

    For `.mp4` the file is used in place (no copy). For `.ts` we stream-copy
    into `<download_dir>/<vod_date>.mp4` so the rest of the pipeline can treat
    it like any other mp4 source. Stream-copy = no re-encode: fast, lossless,
    and works for OBS-style recordings out of the box.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"local source file not found: {path}")

    mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    vod_date = mtime.strftime("%Y-%m-%d")

    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp4":
        return os.path.abspath(path), vod_date

    if ext == ".ts":
        os.makedirs(download_dir, exist_ok=True)
        out_path = os.path.join(download_dir, f"{vod_date}.mp4")
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path, vod_date
        cmd = [
            "ffmpeg", "-y",
            "-i", path,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            out_path,
        ]
        _run_ffmpeg(cmd, quiet=quiet)
        return out_path, vod_date

    raise ValueError(
        f"unsupported local file extension '{ext}' (expected .mp4 or .ts)"
    )


def stream_m3u8_to_file(m3u8_url: str, out_path: str, quiet: bool = False) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", m3u8_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        out_path,
    ]
    _run_ffmpeg(cmd, quiet=quiet)
    return out_path


def _run_ffmpeg(cmd: list, quiet: bool) -> None:
    """Run an ffmpeg command, silencing stdout/stderr when quiet."""
    if quiet:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    else:
        subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------
# Time-window trimming
# ---------------------------------------------------------------------------

def _parse_time_string(s: str) -> float:
    """Parse `HH:MM:SS`, `MM:SS`, or bare seconds into a float number of seconds."""
    s = str(s).strip()
    if not s:
        raise ValueError("time string is empty")
    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError(
            f"invalid time format: {s!r} (use HH:MM:SS, MM:SS, or seconds)"
        )
    try:
        nums = [float(p) for p in parts]
    except ValueError as e:
        raise ValueError(
            f"invalid time format: {s!r} (use HH:MM:SS, MM:SS, or seconds)"
        ) from e
    if any(n < 0 for n in nums):
        raise ValueError(f"time components must be non-negative: {s!r}")
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] * 3600 + nums[1] * 60 + nums[2]


def _format_seconds(s: float) -> str:
    """Format seconds as `H:MM:SS` for human-readable error messages."""
    total = int(round(s))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}"


def _probe_duration(video_path: str) -> float:
    """Return the duration of `video_path` in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        video_path,
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return float(result.stdout.strip())


def apply_time_window(
    video_path: str,
    start_str: str | None,
    end_str: str | None,
    download_dir: str,
    quiet: bool = False,
) -> str:
    """Trim `video_path` to `[start, end]` and return the trimmed file path.

    Either bound may be `None` / empty:
      - no start  → trim from 0
      - no end    → trim to the video's duration
      - both none → return `video_path` unchanged (no trim performed)

    Trimmed output is stream-copied (no re-encode) into `download_dir` with a
    deterministic name that encodes the window, so re-running the same window
    is a cache hit and a different window doesn't collide.

    Raises `ValueError` if either bound is out of range, inverted, or
    malformed. Raises `FileNotFoundError` if ffprobe can't read the source.
    """
    if not start_str and not end_str:
        return video_path

    duration = _probe_duration(video_path)
    start = _parse_time_string(start_str) if start_str else 0.0
    end = _parse_time_string(end_str) if end_str else duration

    if start < 0:
        raise ValueError(f"start_time cannot be negative (got {_format_seconds(start)})")
    if start >= duration:
        raise ValueError(
            f"start_time {_format_seconds(start)} is at or past the video's end "
            f"({_format_seconds(duration)} long)"
        )
    if end > duration:
        raise ValueError(
            f"end_time {_format_seconds(end)} exceeds video duration "
            f"({_format_seconds(duration)} long); drop --end-time to clip to the end"
        )
    if end <= start:
        raise ValueError(
            f"end_time {_format_seconds(end)} must be after "
            f"start_time {_format_seconds(start)}"
        )

    os.makedirs(download_dir, exist_ok=True)
    basename = os.path.splitext(os.path.basename(video_path))[0]
    out_path = os.path.join(
        download_dir,
        f"{basename}_w{int(round(start))}-{int(round(end))}.mp4",
    )

    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", video_path,
        "-t", str(end - start),
        "-c", "copy",
        out_path,
    ]
    _run_ffmpeg(cmd, quiet=quiet)
    return out_path
