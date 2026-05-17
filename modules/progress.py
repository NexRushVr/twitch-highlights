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

    def set_estimated_total(self, seconds: float) -> None:
        """Tell the display the expected wall-clock duration of the whole run.

        After this is called, overall % switches from "fraction of phase
        weights completed" to "elapsed / estimated_total" — what the user
        actually wants to know mid-run.
        """
        self._estimated_total = max(1.0, float(seconds))

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
        start = time.monotonic()
        overall_before = self._overall_pct()
        eta = self._eta_suffix()
        print(
            f"[{self._step_count}/{self._n_steps}] {label}...  "
            f"(overall {overall_before}%{eta})",
            flush=True,
        )
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
