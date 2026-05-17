import re
import time

import pytest

from modules.progress import (
    DEFAULT_PHASE_WEIGHTS,
    Progress,
    fmt_seconds,
)


# ---------------------------------------------------------------------------
# fmt_seconds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("s,expected", [
    (0,        "0:00"),
    (5,        "0:05"),
    (65,       "1:05"),
    (3600,     "1:00:00"),
    (3725,     "1:02:05"),
    (-3,       "0:00"),       # negative input clamped to zero
])
def test_fmt_seconds(s, expected):
    assert fmt_seconds(s) == expected


# ---------------------------------------------------------------------------
# Progress.phase — prints headers, advances overall %
# ---------------------------------------------------------------------------

def test_progress_phase_prints_header_and_footer(capsys):
    p = Progress(verbose=False)
    with p.phase("source", "Resolving source"):
        pass
    out = capsys.readouterr().out
    assert "[1/7] Resolving source..." in out
    # Footer should print elapsed and overall %.
    assert "done in" in out
    assert re.search(r"overall \d+%", out)


def test_progress_phase_increments_step_counter(capsys):
    p = Progress(verbose=False)
    with p.phase("source", "Resolving source"):
        pass
    with p.phase("audio", "Extracting audio"):
        pass
    out = capsys.readouterr().out
    assert "[1/7] Resolving source..." in out
    assert "[2/7] Extracting audio..." in out


def test_progress_phase_advances_overall_weight(capsys):
    p = Progress(verbose=False)
    with p.phase("source", "S"):
        pass
    # After completing the source phase (weight 0.20) overall should be ~20%.
    with p.phase("audio", "A"):
        pass
    out = capsys.readouterr().out
    # Look for "(overall 20%" in the audio phase header — i.e., we accumulated
    # the source weight before starting audio.
    assert re.search(r"\[2/7\] A\.\.\..*overall 20%", out)


# ---------------------------------------------------------------------------
# Progress with a known total runtime — switches to elapsed/total ratio
# ---------------------------------------------------------------------------

def test_progress_set_estimated_total_clamps_to_99(capsys, monkeypatch):
    """If we've blown past the estimated total, overall stays at 99% until
    finish, so the meter doesn't oscillate past 100% mid-run."""
    p = Progress(verbose=False)
    # Pretend the whole run should take 10s.
    p.set_estimated_total(10.0)
    # Walk the clock 100s into the future before the first phase.
    monkeypatch.setattr(
        "modules.progress.time.monotonic",
        lambda: p._t0 + 100.0,
    )
    with p.phase("source", "S"):
        pass
    out = capsys.readouterr().out
    # Both header and footer must show 99% (clamped), not 1000%.
    assert "overall 99%" in out
    # And the ETA suffix should report "0:00 left of ~0:10".
    assert re.search(r"~0:00 left of ~0:10", out)


def test_progress_time_based_overall_reflects_elapsed_fraction(capsys, monkeypatch):
    """At elapsed=15s of an estimated 60s total, overall should report 25%."""
    p = Progress(verbose=False)
    p.set_estimated_total(60.0)
    monkeypatch.setattr(
        "modules.progress.time.monotonic",
        lambda: p._t0 + 15.0,
    )
    with p.phase("source", "S"):
        pass
    out = capsys.readouterr().out
    # The phase header is printed at start (elapsed=15s → 25%).
    assert re.search(r"\[1/7\] S\.\.\..*overall 25%", out)


def test_progress_eta_suffix_absent_when_no_estimate(capsys):
    """Without set_estimated_total the ETA suffix is empty."""
    p = Progress(verbose=False)
    with p.phase("source", "S"):
        pass
    out = capsys.readouterr().out
    assert "left of" not in out


# ---------------------------------------------------------------------------
# Progress.iter — verbose-mode passthrough vs tqdm wrapping
# ---------------------------------------------------------------------------

def test_progress_iter_verbose_returns_iterable_directly():
    p = Progress(verbose=True)
    src = [1, 2, 3]
    wrapped = p.iter(src, total=3, desc="x")
    assert wrapped is src   # passthrough — no tqdm wrapping


def test_progress_iter_quiet_yields_same_items():
    """Even when tqdm wraps, iteration must produce the same items in order."""
    p = Progress(verbose=False)
    src = [1, 2, 3, 4, 5]
    assert list(p.iter(src, total=5, desc="x")) == src


# ---------------------------------------------------------------------------
# Phase weights — must sum close to 1.0 so overall progresses to ~100%
# ---------------------------------------------------------------------------

def test_default_phase_weights_sum_to_one():
    total = sum(DEFAULT_PHASE_WEIGHTS.values())
    assert 0.99 <= total <= 1.01


# ---------------------------------------------------------------------------
# Heartbeat spinner gating
# ---------------------------------------------------------------------------

def test_spinner_disabled_in_verbose_mode():
    p = Progress(verbose=True)
    assert p._spinner_enabled() is False


def test_spinner_disabled_when_stdout_not_a_tty(capsys):
    # pytest's captured stdout is not a tty → spinner must stay off so logs
    # don't fill with carriage-return frames.
    p = Progress(verbose=False)
    assert p._spinner_enabled() is False


def test_spinner_enabled_when_tty(monkeypatch):
    p = Progress(verbose=False)

    class _FakeTTY:
        def isatty(self):
            return True

    monkeypatch.setattr("modules.progress.sys.stdout", _FakeTTY())
    assert p._spinner_enabled() is True


def test_phase_runs_without_spinner_and_still_prints(capsys, monkeypatch):
    """A phase with a forced TTY + spinner=False must skip the heartbeat
    thread entirely but still emit header/footer."""
    started = {"count": 0}

    class _Boom(Exception):
        pass

    def _no_start(self):
        started["count"] += 1

    monkeypatch.setattr("modules.progress._Heartbeat.start", _no_start)

    p = Progress(verbose=False)
    with p.phase("source", "Resolving source", spinner=False):
        pass
    out = capsys.readouterr().out
    assert "[1/7] Resolving source..." in out
    assert "done in" in out
    assert started["count"] == 0  # heartbeat never started
