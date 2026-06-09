"""Chat-velocity highlight signals.

Chat message rate and hype-emote spikes are a cheap, crowd-sourced predictor of
clip-worthy moments — no GPU, no LLM. This module fetches a VOD's chat *replay*
(Twitch GraphQL or Kick's API), turns it into time-bucketed velocity, and emits
spike windows as clip candidates / score boosts.

It only works for VODs whose chat is still hosted: Twitch VODs the streamer left
up (chat is server-side data attached to the video object — gone when the VOD is
deleted), and Kick VODs. vodvod.top rehosts VODs that are usually *gone* from
Twitch, so its IDs are not Twitch video IDs and chat is unavailable there — the
caller falls back to the audio+Whisper+LLM flow via the `ChatUnavailable` signal.

Mechanism mirrors lay295/TwitchDownloader's ChatDownloader (persisted GraphQL
query `VideoCommentsByOffsetOrCursor`, cursor pagination, `content_offset_seconds`
per message); the Kick path uses `/api/v2/channels/<id>/messages` (seek by
`start_time`, page backward by `cursor`, `created_at` -> offset).
"""
import re
import statistics
from collections import namedtuple
from datetime import datetime, timezone

try:  # curl_cffi gets past Kick's Cloudflare and works fine for Twitch too
    from curl_cffi import requests as _rq
    _IMPERSONATE = {"impersonate": "chrome"}
except ImportError:
    try:  # plain requests is a fine fallback for Twitch (Kick needs curl_cffi)
        import requests as _rq
        _IMPERSONATE = {}
    except ImportError:  # neither installed (e.g. CI's minimal test deps) — the
        _rq = None       # pure functions still import; fetchers raise ChatUnavailable
        _IMPERSONATE = {}


# A single chat line anchored to its position in the VOD.
#   offset: seconds from the start of the VOD
#   text:   message body (concatenated fragments)
#   weight: 1.0 baseline + capped hype bonus (see message_weight)
ChatMessage = namedtuple("ChatMessage", ["offset", "text", "weight"])


class ChatUnavailable(Exception):
    """Chat replay can't be fetched for this source (deleted/expired VOD, an
    unsupported source like vodvod or a raw m3u8, or a network failure). Callers
    catch this and fall back to the normal audio+LLM pipeline."""


# ---------------------------------------------------------------------------
# Hype weighting (Phase 5)
# ---------------------------------------------------------------------------
# Token -> extra weight. A message flooded with OMEGALUL/Pog/"clip it" is a much
# stronger highlight signal than generic chatter, so we weight the velocity curve
# by hype content rather than counting every message equally.
_HYPE_TOKENS = {
    "omegalul": 2.0, "lulw": 1.6, "lul": 1.0, "kekw": 1.6, "kekwait": 1.4,
    "kek": 0.9, "lmao": 1.0, "lmfao": 1.1, "pepelaugh": 1.4, "icant": 1.2,
    "pog": 1.5, "pogchamp": 1.6, "poggers": 1.6, "pogu": 1.5, "pogcrazy": 1.7,
    "letsgo": 1.4, "letsgoo": 1.6, "ez": 0.9, "ezclap": 1.4, "gg": 0.7,
    "clip": 2.0, "clipit": 2.5, "clipthat": 2.5, "clipped": 1.5, "clipper": 1.2,
    "sheesh": 1.5, "sheeesh": 1.6, "monkas": 1.4, "monkaw": 1.5, "monkahmm": 1.0,
    "based": 0.9, "dub": 0.8, "w": 0.4, "ws": 0.6, "omg": 1.0, "omgg": 1.2,
    "holy": 1.1, "insane": 1.4, "cracked": 1.4, "gigachad": 1.1, "chad": 0.7,
    "yooo": 1.2, "yoo": 1.0, "wtf": 1.0, "noway": 1.5, "hypers": 1.5, "hype": 1.3,
    "peepoclap": 1.2, "catkiss": 0.6, "ratjam": 0.8, "vibe": 0.6, "actualcontent": 1.8,
}
_HYPE_PHRASES = {
    "no way": 1.5, "oh my": 0.9, "let's go": 1.5, "lets go": 1.5,
    "clip it": 2.5, "clip that": 2.5, "what the": 0.7, "holy shit": 1.5,
    "oh my god": 1.2, "actual content": 1.8,
}
_TOKEN_RE = re.compile(r"[a-z0-9+]+")


def message_weight(text: str) -> float:
    """1.0 baseline plus a capped bonus for hype emotes/keywords/phrases."""
    if not text:
        return 1.0
    t = text.lower()
    bonus = 0.0
    for phrase, w in _HYPE_PHRASES.items():
        if phrase in t:
            bonus += w
    for tok in _TOKEN_RE.findall(t):
        bonus += _HYPE_TOKENS.get(tok, 0.0)
    return 1.0 + min(bonus, 6.0)


# ---------------------------------------------------------------------------
# Spike detection (Phase 1) + density boundaries (Phase 5)
# ---------------------------------------------------------------------------

def detect_chat_spikes(messages: list, vod_duration: float, config: dict) -> list:
    """Turn chat into clip candidates by finding buckets where the (hype-weighted)
    message rate spikes above the VOD's baseline.

    Buckets the messages by `chat_bucket_seconds`, z-scores the weighted rate, and
    emits a clip for each spike region — its bounds follow the *density envelope*
    (expand across contiguous elevated buckets around the peak, then pad), rather
    than a fixed window, so the clip hugs the actual hype. Returns clip dicts in
    the same `{start,end,reason,score,description}` shape the LLM path produces.
    """
    bucket = float(config.get("chat_bucket_seconds", 5.0))
    z_thresh = float(config.get("chat_spike_z", 2.5))
    min_msgs = int(config.get("chat_spike_min_messages", 5))
    pre = float(config.get("chat_pre_seconds", 6.0))
    post = float(config.get("chat_post_seconds", 6.0))
    elevated_frac = float(config.get("chat_elevated_fraction", 0.5))  # of the peak
    max_clips = int(config.get("chat_max_candidates") or 0) or int(config.get("max_clips", 10)) * 3

    if not messages or vod_duration <= 0 or bucket <= 0:
        return []

    n = int(vod_duration // bucket) + 1
    weighted = [0.0] * n
    raw = [0] * n
    for m in messages:
        i = int(m.offset // bucket)
        if 0 <= i < n:
            weighted[i] += m.weight
            raw[i] += 1

    mean = statistics.fmean(weighted)
    std = statistics.pstdev(weighted) or 1.0
    z = [(weighted[i] - mean) / std for i in range(n)]

    # Spike buckets: a real message floor AND a z above threshold.
    is_spike = [raw[i] >= min_msgs and z[i] >= z_thresh for i in range(n)]
    elevated_cut = mean + max(0.0, elevated_frac) * std  # weak elevation for envelope

    clips: list = []
    i = 0
    while i < n:
        if not is_spike[i]:
            i += 1
            continue
        # Peak bucket of this spike cluster (walk while still spiking or merged
        # by a short gap of merely-elevated buckets).
        j = i
        peak = i
        gap = 0
        while j + 1 < n and (is_spike[j + 1] or (weighted[j + 1] >= elevated_cut and gap < 2)):
            j += 1
            gap = 0 if is_spike[j] else gap + 1
            if weighted[j] > weighted[peak]:
                peak = j
        # Density envelope: expand outward from the cluster while buckets stay
        # above the weak elevation cut — this sets natural in/out points.
        lo = i
        while lo > 0 and weighted[lo - 1] >= elevated_cut:
            lo -= 1
        hi = j
        while hi + 1 < n and weighted[hi + 1] >= elevated_cut:
            hi += 1

        start = max(0.0, lo * bucket - pre)
        end = min(vod_duration, (hi + 1) * bucket + post)
        peak_t = peak * bucket
        # Score 0.5..1.0 from how far the peak stands above baseline.
        score = round(min(1.0, 0.5 + 0.1 * z[peak]), 3)
        msgs_in = sum(raw[k] for k in range(lo, hi + 1))
        clips.append({
            "start": start,
            "end": end,
            "reason": "chat_spike",
            "score": score,
            "description": f"Chat spike at {_fmt(peak_t)} ({msgs_in} msgs, {z[peak]:.1f}σ)",
            "_peak": peak_t,
            "_z": z[peak],
        })
        i = j + 1

    clips.sort(key=lambda c: c["score"], reverse=True)
    return clips[:max_clips]


def boost_clips_with_chat(clips: list, spikes: list, config: dict) -> list:
    """Bump the score of any clip whose window overlaps a chat spike (Phase 2).

    Mirrors the audio-peak cross-reference: a moment both the chat and the model
    flagged is a stronger pick. Mutates and returns `clips`."""
    if not clips or not spikes:
        return clips
    boost = float(config.get("chat_score_boost", 0.15))
    for clip in clips:
        cs, ce = clip.get("start", 0.0), clip.get("end", 0.0)
        for sp in spikes:
            # Proper interval overlap — the old peak/start-only check missed
            # spike-end-in-clip and spike-contains-clip, so earned boosts were
            # silently skipped.
            if cs < sp["end"] and sp["start"] < ce:
                clip["score"] = min(1.0, clip.get("score", 0.0) + boost)
                break
    return clips


def spike_windows(spikes: list, pad: float = 0.0, merge_gap: float = 15.0) -> list:
    """Collapse spike clips into merged [start, end] windows (Phase 3 gating)."""
    if not spikes:
        return []
    iv = sorted(((max(0.0, s["start"] - pad), s["end"] + pad) for s in spikes))
    out = [list(iv[0])]
    for a, b in iv[1:]:
        if a <= out[-1][1] + merge_gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


# ---------------------------------------------------------------------------
# Twitch fetch (Phase 1)
# ---------------------------------------------------------------------------
_TWITCH_GQL = "https://gql.twitch.tv/gql"
_TWITCH_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"
_VIDEO_COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


def _json_or_unavailable(r, url: str):
    """Parse a response as JSON, or raise ChatUnavailable. A Cloudflare challenge
    or HTML error page returns HTTP 200 with non-JSON; treat that as a graceful
    fallback, not a crash."""
    try:
        return r.json()
    except ValueError as e:
        raise ChatUnavailable(f"invalid JSON from {url}: {e}")


def _post_json(url: str, headers: dict, body, timeout: int = 25):
    if _rq is None:
        raise ChatUnavailable("no HTTP client (install curl_cffi or requests)")
    r = _rq.post(url, headers=headers, json=body, timeout=timeout, **_IMPERSONATE)
    if r.status_code != 200:
        raise ChatUnavailable(f"{url} returned HTTP {r.status_code}")
    return _json_or_unavailable(r, url)


def _get_json(url: str, timeout: int = 25):
    if _rq is None:
        raise ChatUnavailable("no HTTP client (install curl_cffi or requests)")
    r = _rq.get(url, timeout=timeout, **_IMPERSONATE)
    if r.status_code != 200:
        raise ChatUnavailable(f"{url} returned HTTP {r.status_code}")
    return _json_or_unavailable(r, url)


def fetch_twitch_vod_chat(video_id, *, max_messages: int = 0,
                          max_pages: int = 20000, on_progress=None) -> list:
    """Page a Twitch VOD's full chat replay via the GraphQL comments API.

    Raises ChatUnavailable if the VOD is gone (data.video is null) — i.e. the
    streamer didn't leave it up. `max_messages`/`max_pages` cap very long VODs.
    """
    vid = str(video_id).lstrip("vV")
    headers = {"Client-ID": _TWITCH_CLIENT_ID, "Content-Type": "application/json"}
    messages: list = []
    cursor = None
    nulls = 0
    pages = 0
    while pages < max_pages:
        pages += 1
        variables = ({"videoID": vid, "contentOffsetSeconds": 0} if cursor is None
                     else {"videoID": vid, "cursor": cursor})
        body = [{"operationName": "VideoCommentsByOffsetOrCursor", "variables": variables,
                 "extensions": {"persistedQuery": {"version": 1, "sha256Hash": _VIDEO_COMMENTS_HASH}}}]
        data = _post_json(_TWITCH_GQL, headers, body)
        d0 = data[0] if isinstance(data, list) else data
        video = (d0.get("data") or {}).get("video")
        if video is None:
            raise ChatUnavailable("Twitch VOD unavailable (deleted/expired) — no chat replay")
        comments = video.get("comments") or {}
        edges = comments.get("edges") or []
        if not edges:
            nulls += 1
            if nulls > 10:
                break
        for e in edges:
            node = e.get("node") or {}
            off = node.get("contentOffsetSeconds")
            try:
                off = float(off)
            except (TypeError, ValueError):
                continue   # skip a bad row, don't sink the whole chat fetch
            frags = (node.get("message") or {}).get("fragments") or []
            text = "".join((f.get("text") or "") for f in frags)
            messages.append(ChatMessage(off, text, message_weight(text)))
        if on_progress and messages:
            on_progress(len(messages), messages[-1].offset)
        if max_messages and len(messages) >= max_messages:
            break
        if not (comments.get("pageInfo") or {}).get("hasNextPage"):
            break
        cursor = edges[-1].get("cursor") if edges else None
        if not cursor:
            break
    messages.sort(key=lambda m: m.offset)
    return messages


def twitch_video_id(twitch_vod_url: str):
    """Extract the numeric video id from a Twitch VOD/highlight URL or bare id.

    Handles `twitch.tv/videos/<id>`, highlight forms `twitch.tv/<chan>/v/<id>` and
    `/video/<id>`, and a bare `<id>` / `v<id>`. Clip slugs (clips.twitch.tv/<slug>)
    have no numeric id and return None."""
    if not twitch_vod_url:
        return None
    s = str(twitch_vod_url).strip()
    m = re.search(r"/(?:videos?|v)/(\d{6,})", s)   # URL forms (videos/, video/, v/)
    if m:
        return m.group(1)
    m = re.fullmatch(r"v?(\d{6,})", s)             # bare id or v<id>
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Kick fetch (Phase 4)
# ---------------------------------------------------------------------------

def _parse_iso(s: str) -> float:
    """Kick timestamps: 'YYYY-MM-DD HH:MM:SS' or ISO 'YYYY-MM-DDTHH:MM:SSZ'."""
    s = str(s).strip().replace(" ", "T").replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def kick_vod_info(channel_slug: str) -> dict:
    """Latest Kick VOD's {channel_id, start_epoch, duration_seconds}."""
    slug = str(channel_slug).lstrip("@")
    items = _get_json(f"https://kick.com/api/v2/channels/{slug}/videos")
    if not items:
        raise ChatUnavailable(f"No Kick VODs for '{channel_slug}'")
    v = items[0]
    start = v.get("start_time") or v.get("created_at")
    return {
        "channel_id": v.get("channel_id"),
        "start_epoch": _parse_iso(start) if start else 0.0,
        "duration_seconds": float(v.get("duration") or 0) / 1000.0,  # ms -> s
    }


def _kick_page(channel_id, start_time=None, cursor=None):
    url = f"https://kick.com/api/v2/channels/{channel_id}/messages"
    params = []
    if start_time:
        params.append(f"start_time={start_time}")
    if cursor:
        params.append(f"cursor={cursor}")
    if params:
        url += "?" + "&".join(params)
    data = _get_json(url).get("data") or {}
    return data.get("messages") or [], data.get("cursor")


def fetch_kick_vod_chat(channel_id, start_epoch: float, duration_seconds: float,
                        *, max_messages: int = 0, max_pages: int = 20000,
                        on_progress=None) -> list:
    """Page a Kick VOD's chat replay. Seeks to the VOD end, then walks backward by
    cursor to the start (Kick returns messages older than the cursor), mapping each
    `created_at` to an offset from the VOD start."""
    if not channel_id or duration_seconds <= 0:
        raise ChatUnavailable("Kick VOD missing channel_id/duration")
    end_epoch = start_epoch + duration_seconds
    messages: list = []
    seen = set()
    msgs, cursor = _kick_page(channel_id, start_time=_to_iso(end_epoch))
    pages = 0
    while pages < max_pages:
        pages += 1
        oldest = None
        for m in msgs:
            mid = m.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            ca = m.get("created_at")
            if not ca:
                continue
            try:
                ce = _parse_iso(ca)
            except (ValueError, TypeError):
                continue   # one malformed timestamp shouldn't abort the replay
            oldest = ce if oldest is None else min(oldest, ce)
            off = ce - start_epoch
            if -2.0 <= off <= duration_seconds + 5.0:
                text = m.get("content") or ""
                messages.append(ChatMessage(max(0.0, off), text, message_weight(text)))
        if on_progress and messages:
            on_progress(len(messages), messages[-1].offset)
        if max_messages and len(messages) >= max_messages:
            break
        if oldest is not None and oldest < start_epoch:
            break  # walked past the VOD start
        if not cursor:
            break
        msgs, cursor = _kick_page(channel_id, cursor=cursor)
    messages.sort(key=lambda m: m.offset)
    return messages


# ---------------------------------------------------------------------------
# Source dispatch
# ---------------------------------------------------------------------------

def fetch_chat_for_source(cfg: dict, *, on_progress=None) -> list:
    """Fetch chat replay for the configured source, or raise ChatUnavailable.

    Twitch: from the twitch.tv/videos/<id> URL. Kick: from the channel's latest
    VOD. vodvod/m3u8/local: unsupported (vodvod IDs aren't Twitch video IDs and
    those VODs are usually gone from Twitch)."""
    src = cfg.get("source_type")
    if src == "twitch":
        vid = twitch_video_id(cfg.get("twitch_vod_url", ""))
        if not vid:
            raise ChatUnavailable("could not parse a Twitch video id from the URL")
        return fetch_twitch_vod_chat(vid, max_messages=int(cfg.get("chat_max_messages", 0)),
                                     on_progress=on_progress)
    if src == "kick":
        info = kick_vod_info(cfg.get("kick_channel", ""))
        return fetch_kick_vod_chat(info["channel_id"], info["start_epoch"],
                                   info["duration_seconds"],
                                   max_messages=int(cfg.get("chat_max_messages", 0)),
                                   on_progress=on_progress)
    raise ChatUnavailable(f"chat replay not available for source '{src}'")


def _fmt(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
