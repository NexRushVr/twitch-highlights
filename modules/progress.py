"""Per-phase progress display for the pipeline.

Two render modes, toggled by `--verbose`:

- Compact (default): a labeled header per phase + elapsed/overall % when the
  phase ends. Iterating phases (LLM chunks, clip cuts, captions) get a tqdm
  progress bar via `progress.iter(...)`. Subprocess log spam is suppressed at
  the call sites (each module accepts a `quiet=True` kwarg and pipes stdout
  to DEVNULL; stderr stays captured so errors are still surfaced).

- Verbose: phase headers and overall % still print, but log spam from
  subprocess children, per-chunk LLM updates, and Whisper's tqdm bar passes
  through unchanged — same behavior as before this module existed.

Overall progress is reported two ways:

- Before the source is probed (we don't know the video duration yet): a
  weighted sum of completed phases (transcription dominates).

- After `set_estimated_total(seconds)` is called: a time-based ratio of
  `elapsed / estimated_total`. The estimate comes from `ffprobe`-ing the
  resolved source and multiplying by a hardware factor (~0.15 on CUDA, ~1.5
  on CPU). This is what the user actually wants to know — "we're 3 minutes
  into what should be a 60-minute run, so ~5%" — regardless of which phase
  is currently running.

Time-based progress is clamped to 99% until the pipeline finishes, so a
faster-than-expected run doesn't oscillate past 100% mid-phase.
"""

import itertools
import json
import os
import sys
import threading
import time
from contextlib import contextmanager

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


# Rough wall-clock weights for the seven pipeline phases. Used only as a
# fallback before `set_estimated_total` is called.
DEFAULT_PHASE_WEIGHTS: dict[str, float] = {
    "source":     0.20,
    "audio":      0.02,
    "transcribe": 0.55,
    "llm":        0.10,
    "peaks":      0.01,
    "clip":       0.06,
    "caption":    0.06,
}


def fmt_seconds(seconds: float) -> str:
    """Human-friendly elapsed-time string: `1:23` or `1:02:34`."""
    s = int(round(max(0.0, seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{sec:02d}"
    return f"{m:d}:{sec:02d}"


class _Heartbeat:
    """Background ticker that rewrites a single line every second so a long,
    output-silent phase (a multi-minute download, ffmpeg audio extract, etc.)
    visibly counts up instead of looking frozen.

    Only used on a real TTY — in pipes / CI / pytest it's a no-op so logs
    don't fill with carriage-return spam and tests stay deterministic.
    """

    _FRAMES = "|/-\\"
    _WIDTH = 78

    def __init__(self, label: str, progress: "Progress"):
        self._label = label
        self._progress = progress
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        # Wipe the spinner line so the phase footer prints cleanly.
        sys.stdout.write("\r" + " " * self._WIDTH + "\r")
        sys.stdout.flush()

    def _run(self) -> None:
        start = time.monotonic()
        for frame in itertools.cycle(self._FRAMES):
            # wait() returns True the instant stop() is called, so the phase
            # never blocks waiting on a full tick to finish.
            if self._stop.wait(1.0):
                return
            elapsed = time.monotonic() - start
            line = (
                f"   {frame} {self._label} — {fmt_seconds(elapsed)} elapsed"
                f"{self._progress._eta_suffix()}"
            )
            sys.stdout.write("\r" + line[: self._WIDTH].ljust(self._WIDTH))
            sys.stdout.flush()


class Progress:
    """Tiny per-phase progress + overall-% display."""

    def __init__(self, verbose: bool = False, weights: dict | None = None):
        self.verbose = verbose
        self.weights = dict(weights or DEFAULT_PHASE_WEIGHTS)
        self._n_steps = len(self.weights)
        self._step_count = 0
        self._completed_weight = 0.0
        self._t0 = time.monotonic()
        self._estimated_total: float | None = None
        self._cur_key: str | None = None
        # Opt-in machine-readable progress feed for GUI front-ends. When the
        # env var `VOD_CLIP_PROGRESS_JSON` points at a file we *also* append one
        # JSON line per phase event; the human-readable stdout is byte-for-byte
        # unchanged either way. When the var is unset (`_sink is None`) every
        # `_emit` below is a complete no-op, so default behavior never changes.
        self._sink = self._open_sink()

    @staticmethod
    def _open_sink():
        path = os.environ.get("VOD_CLIP_PROGRESS_JSON")
        if not path:
            return None
        try:
            # Line-buffered text append: each JSON line is flushed promptly so a
            # tailing GUI sees events live. Failure to open is non-fatal — the
            # pipeline must run with or without a listening front-end.
            return open(path, "a", encoding="utf-8", buffering=1)
        except OSError:
            return None

    def _emit(self, event: dict) -> None:
        """Append one JSON event line to the progress feed, if one is attached.

        Best-effort and never raises: a broken/closed sink must not take down a
        long pipeline run."""
        if self._sink is None:
            return
        try:
            self._sink.write(json.dumps(event) + "\n")
            self._sink.flush()
        except (OSError, ValueError):
            pass

    def _remaining_seconds(self) -> float | None:
        if self._estimated_total is None:
            return None
        return max(0.0, self._estimated_total - (time.monotonic() - self._t0))

    def sub(self, message: str, fraction: float | None = None,
            detail: str | None = None) -> None:
        """Emit a fine-grained sub-step inside the current phase — e.g. "Found
        VOD" or live download progress. Goes to the JSON feed only (no stdout),
        so the compact CLI display is untouched. No-op without a feed attached.
        """
        if self._sink is None:
            return
        self._emit({
            "type": "sub_progress",
            "phase_key": self._cur_key,
            "message": message,
            "fraction": fraction,
            "detail": detail,
            "elapsed": time.monotonic() - self._t0,
        })

    @contextmanager
    def download_monitor(self, path: str, label: str = "Downloading"):
        """While active, poll a growing output file (or directory) once a second
        and emit sub-progress with downloaded size + speed, so a GUI can show a
        live "Downloading 234 MB · 15.2 MB/s" during the otherwise-opaque source
        download. No feed attached -> no thread, no-op.
        """
        if self._sink is None:
            yield
            return

        def _size(p: str) -> int:
            try:
                if os.path.isdir(p):
                    return sum(
                        os.path.getsize(os.path.join(p, f))
                        for f in os.listdir(p)
                        if os.path.isfile(os.path.join(p, f))
                    )
                return os.path.getsize(p)
            except OSError:
                return 0

        stop = threading.Event()

        def _watch() -> None:
            last_size = 0
            last_t = time.monotonic()
            while not stop.wait(1.0):
                size = _size(path)
                now = time.monotonic()
                dt = now - last_t
                speed = (size - last_size) / dt if dt > 0 else 0.0
                last_size, last_t = size, now
                detail = f"{size / 1e6:.0f} MB · {max(0.0, speed) / 1e6:.1f} MB/s"
                self.sub(label, detail=detail)

        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()
        try:
            yield
        finally:
            stop.set()
            watcher.join(timeout=2.0)

    def set_estimated_total(self, seconds: float) -> None:
        """Tell the display the expected wall-clock duration of the whole run.

        After this is called, overall % switches from "fraction of phase
        weights completed" to "elapsed / estimated_total" — what the user
        actually wants to know mid-run.
        """
        self._estimated_total = max(1.0, float(seconds))
        self._emit({
            "type": "set_total",
            "estimated_total": self._estimated_total,
            "elapsed": time.monotonic() - self._t0,
        })

    def _overall_pct(self, *, finished: bool = False) -> int:
        elapsed = time.monotonic() - self._t0
        if self._estimated_total is not None:
            pct = elapsed / self._estimated_total
        else:
            pct = self._completed_weight
        if not finished:
            pct = min(pct, 0.99)
        return int(round(pct * 100))

    def _eta_suffix(self) -> str:
        if self._estimated_total is None:
            return ""
        elapsed = time.monotonic() - self._t0
        remaining = max(0.0, self._estimated_total - elapsed)
        return f", ~{fmt_seconds(remaining)} left of ~{fmt_seconds(self._estimated_total)}"

    def _spinner_enabled(self) -> bool:
        """Heartbeat only on an interactive TTY in compact mode — never in
        verbose (real logs already scroll) or non-TTY (pipes/CI/pytest)."""
        if self.verbose:
            return False
        try:
            return bool(sys.stdout.isatty())
        except (AttributeError, ValueError):
            return False

    @contextmanager
    def phase(self, key: str, label: str, spinner: bool = True):
        """Begin a named phase. Prints a start header and a done/elapsed line.

        `spinner=True` runs a live elapsed-time ticker for the duration of the
        phase (compact mode + TTY only). Pass `spinner=False` for phases that
        already render their own live output (a tqdm bar via `iter`, or
        Whisper's built-in progress bar) so the two don't fight for the line.
        """
        self._step_count += 1
        self._cur_key = key
        start = time.monotonic()
        overall_before = self._overall_pct()
        eta = self._eta_suffix()
        print(
            f"[{self._step_count}/{self._n_steps}] {label}...  "
            f"(overall {overall_before}%{eta})",
            flush=True,
        )
        self._emit({
            "type": "phase_start",
            "index": self._step_count,
            "total": self._n_steps,
            "key": key,
            "label": label,
            "overall": overall_before / 100.0,
            "estimated_total": self._estimated_total,
            "eta_seconds": self._remaining_seconds(),
            "elapsed": time.monotonic() - self._t0,
        })
        hb = None
        if spinner and self._spinner_enabled():
            hb = _Heartbeat(label, self)
            hb.start()
        try:
            yield self
        finally:
            if hb is not None:
                hb.stop()
            elapsed = time.monotonic() - start
            self._completed_weight = min(
                1.0, self._completed_weight + self.weights.get(key, 0.0)
            )
            overall_after = self._overall_pct()
            eta_after = self._eta_suffix()
            print(
                f"       v done in {fmt_seconds(elapsed)}  "
                f"(overall {overall_after}%{eta_after})",
                flush=True,
            )
            self._emit({
                "type": "phase_end",
                "index": self._step_count,
                "total": self._n_steps,
                "key": key,
                "label": label,
                "phase_elapsed": elapsed,
                "overall": overall_after / 100.0,
                "estimated_total": self._estimated_total,
                "eta_seconds": self._remaining_seconds(),
                "elapsed": time.monotonic() - self._t0,
            })

    def iter(self, iterable, total: int | None = None, desc: str | None = None):
        """Wrap `iterable` in a tqdm bar when not verbose; passthrough otherwise.

        In verbose mode the caller's per-item prints would interleave with a
        tqdm bar and break it; we let them through and skip the bar instead.
        """
        if self.verbose or tqdm is None:
            return iterable
        return tqdm(iterable, total=total, desc=desc, leave=False, dynamic_ncols=True)

    def total_elapsed(self) -> float:
        return time.monotonic() - self._t0
