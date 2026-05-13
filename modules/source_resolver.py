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


def download_twitch_vod(url: str, quality: str, out_dir: str) -> tuple[str, str]:
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


def resolve_local_file(path: str, download_dir: str) -> tuple[str, str]:
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
        subprocess.run(cmd, check=True)
        return out_path, vod_date

    raise ValueError(
        f"unsupported local file extension '{ext}' (expected .mp4 or .ts)"
    )


def stream_m3u8_to_file(m3u8_url: str, out_path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", m3u8_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    return out_path
