import subprocess
import json
import os
import re
import threading
import time
from collections import deque
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

    # yt-dlp writes the .info.json as UTF-8 with ensure_ascii=False, so it can
    # contain raw non-ASCII (emoji titles, accented/CJK/Cyrillic names) and may
    # carry a BOM. A bare open() defaults to cp1252 on Windows and crashes on
    # those bytes; utf-8-sig decodes correctly and strips any leading BOM.
    with open(info_files[0], encoding="utf-8-sig") as f:
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


def _parse_card_duration(text: str) -> "float | None":
    """Pull the VOD length (seconds) from a vodvod card's text.

    The card reads "<title>\\n<ISO date>\\n|\\n<H:MM:SS>\\n...": the duration is the
    first standalone `H:MM:SS` (or `M:SS`) line. The ISO line carries a `T...Z`, so
    it never full-matches. Returns None when no length line is present.
    """
    for line in (ln.strip() for ln in (text or "").splitlines()):
        m = re.fullmatch(r"(\d{1,2}):([0-5]\d):([0-5]\d)", line)
        if m:
            h, mm, ss = (int(x) for x in m.groups())
            return h * 3600 + mm * 60 + ss
        m = re.fullmatch(r"(\d{1,2}):([0-5]\d)", line)
        if m:
            mm, ss = (int(x) for x in m.groups())
            return mm * 60 + ss
    return None


def get_latest_vodvod_m3u8(channel_handle: str) -> tuple[str, str, str, "float | None"]:
    """Scrape vodvod.top for the NEWEST VOD.

    Returns (m3u8_url, vod_date_YYYY-MM-DD, title, duration_seconds). Picks by latest
    date — not DOM order, which vodvod does not reliably sort newest-first (picking
    the first anchor used to grab an older, already-cached VOD). duration_seconds is
    None when the card has no length line.
    """
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

        # Each VOD is its own card: the smallest ancestor of the .m3u8 link that
        # still contains exactly that one link (climbing stops at the list, which
        # holds them all). The card text reads "<title>\n<ISO date>\n|\n<dur>...".
        items = page.evaluate("""() => {
            return Array.from(document.querySelectorAll("a[href*='.m3u8']")).map(a => {
                let card = a;
                while (card.parentElement &&
                       card.parentElement.querySelectorAll("a[href*='.m3u8']").length === 1) {
                    card = card.parentElement;
                }
                return {href: a.getAttribute('href'), text: (card.innerText || '').trim()};
            });
        }""")
        browser.close()

    if not items:
        raise ValueError(f"No m3u8 URL found on vodvod.top for channel '{channel_handle}'")

    def _parse(it: dict) -> dict:
        text = it.get("text") or ""
        m = _ISO_DATE_RE.search(text)
        vid = re.search(r"/m3u8/(\d+)/", it.get("href") or "")
        return {
            "href": it.get("href"),
            "date": m.group(0)[:10] if m else "",
            "title": text.splitlines()[0].strip() if text else "",
            "vid": int(vid.group(1)) if vid else 0,
            "duration": _parse_card_duration(text),
        }

    parsed = [_parse(it) for it in items if it.get("href")]
    # Newest = latest ISO date, then highest VOD id (ids grow over time).
    best = max(parsed, key=lambda x: (x["date"], x["vid"]))
    vod_date = best["date"] or _today_iso()
    return best["href"], vod_date, best["title"], best["duration"]


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


def stream_m3u8_to_file(m3u8_url: str, out_path: str, quiet: bool = False,
                        duration: "float | None" = None, on_progress=None) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    # Download to a .part file and atomically rename on success. An interrupted
    # `-c copy` mp4 has no moov atom and is unreadable; writing straight to
    # out_path would leave that corrupt file looking like a valid cache and crash
    # every later run (audio extraction fails on "moov atom not found"). A killed
    # process just leaves a .part, which the next download overwrites.
    part_path = out_path + ".part"
    head = ["ffmpeg", "-y"]
    if on_progress is not None:
        # Machine-readable progress on stdout; keep stderr quiet+drained so it
        # can't deadlock during a multi-GB / multi-hour pull.
        head += ["-loglevel", "warning", "-progress", "pipe:1", "-nostats"]
    cmd = head + ["-i", m3u8_url, "-c", "copy", "-bsf:a", "aac_adtstoasc", part_path]
    try:
        if on_progress is None:
            _run_ffmpeg(cmd, quiet=quiet)
        else:
            _stream_ffmpeg_with_progress(cmd, duration, on_progress)
    except BaseException:
        _quiet_remove(part_path)
        raise
    os.replace(part_path, out_path)
    return out_path


def _quiet_remove(path: str) -> None:
    """Best-effort delete; never raises (used in cleanup paths)."""
    try:
        os.remove(path)
    except OSError:
        pass


def _run_ffmpeg(cmd: list, quiet: bool) -> None:
    """Run an ffmpeg command, silencing stdout/stderr when quiet."""
    if quiet:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    else:
        subprocess.run(cmd, check=True)


def _ffmpeg_seconds(out_time: "str | None") -> "float | None":
    """Parse ffmpeg's out_time ('HH:MM:SS.micro' or 'N/A') into float seconds."""
    if not out_time or out_time == "N/A":
        return None
    try:
        return _parse_time_string(out_time)
    except (ValueError, TypeError):
        return None


def _stream_ffmpeg_with_progress(cmd: list, duration, on_progress) -> None:
    """Run ffmpeg, parsing its -progress blocks and reporting download position.

    Calls on_progress({out_time, bytes, elapsed, duration}) once per block. ffmpeg
    emits key=value lines terminated by `progress=continue|end`; `out_time` is the
    muxed-content timestamp, so out_time/duration is the true fraction of the VOD
    pulled — bytes alone can't give that, since the total size is unknown until the
    end. stderr is drained on a thread so a chatty pull can't deadlock the pipe.
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )
    err_tail: deque = deque(maxlen=40)

    def _drain_err() -> None:
        for line in proc.stderr:
            err_tail.append(line.rstrip())

    err_thread = threading.Thread(target=_drain_err, daemon=True)
    err_thread.start()

    t0 = time.monotonic()
    cur: dict = {}
    try:
        for line in proc.stdout:
            key, _, val = line.strip().partition("=")
            if key != "progress":
                cur[key] = val
                continue
            try:
                size = int(cur.get("total_size"))
            except (TypeError, ValueError):
                size = None
            try:
                on_progress({
                    "out_time": _ffmpeg_seconds(cur.get("out_time")),
                    "bytes": size,
                    "elapsed": time.monotonic() - t0,
                    "duration": duration,
                })
            except Exception:
                pass
            cur = {}
            if val == "end":
                break
    finally:
        rc = proc.wait()
        err_thread.join(timeout=1.0)
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd, stderr="\n".join(err_tail))


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
