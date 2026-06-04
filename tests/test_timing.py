import json

import pytest

from modules import timing
from modules.progress import Progress


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Redirect the calibration store to a temp file."""
    path = tmp_path / "timing_calibration.json"
    monkeypatch.setattr(timing, "_store_path", lambda: str(path))
    return path


CFG = {"whisper_device": "cuda", "whisper_model": "medium",
       "llm_backend": "ollama", "ollama_model": "qwen2.5:14b"}


# ---------------------------------------------------------------------------
# factors_for / build_phase_plan
# ---------------------------------------------------------------------------

def test_factors_for_returns_defaults_without_calibration(store):
    f = timing.factors_for(CFG)
    assert f["transcribe"] == timing.DEFAULT_FACTORS["transcribe"]
    assert f["llm"] == timing.DEFAULT_FACTORS["llm"]


def test_build_phase_plan_includes_caption_only_when_burning():
    plan_keys = lambda cfg: [k for k, _ in timing.build_phase_plan(cfg, timing.DEFAULT_FACTORS)]
    assert "caption" in plan_keys({**CFG, "burn_subtitles": True})
    assert "caption" not in plan_keys({**CFG, "burn_subtitles": False})
    assert "avif" in plan_keys({**CFG, "avif_export": True})
    assert "avif" not in plan_keys({**CFG})


def test_source_has_zero_factor_in_plan():
    # The download is measured, never estimated ahead.
    plan = dict(timing.build_phase_plan(CFG, timing.DEFAULT_FACTORS))
    assert plan["source"] == 0.0


# ---------------------------------------------------------------------------
# record_run — learns per-phase factors (EMA), skips the download
# ---------------------------------------------------------------------------

def test_record_run_learns_observed_factors(store):
    # A 1000s video whose transcribe took 100s -> factor 0.1.
    timing.record_run(CFG, 1000.0, {"source": 300, "transcribe": 100, "llm": 200})
    data = json.loads(store.read_text())
    factors = data[timing._profile_key(CFG)]["factors"]
    assert factors["transcribe"] == pytest.approx(0.1)
    assert factors["llm"] == pytest.approx(0.2)
    assert "source" not in factors           # download is never learned
    # A later run nudges the value via EMA (alpha=0.4), not a full replace.
    timing.record_run(CFG, 1000.0, {"transcribe": 200})   # observed 0.2
    factors = json.loads(store.read_text())[timing._profile_key(CFG)]["factors"]
    assert 0.1 < factors["transcribe"] < 0.2


def test_factors_for_blends_learned_values(store):
    timing.record_run(CFG, 1000.0, {"llm": 500})   # factor 0.5
    assert timing.factors_for(CFG)["llm"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Progress.set_time_model — anchored, self-correcting estimate
# ---------------------------------------------------------------------------

def test_set_time_model_anchors_on_elapsed_plus_remaining(monkeypatch):
    p = Progress(verbose=False)
    # Pretend 100s already elapsed (the download) before the model is set.
    monkeypatch.setattr("modules.progress.time.monotonic", lambda: p._t0 + 100.0)
    # duration 1000s, one remaining phase at factor 0.2 -> remaining 200s.
    p.set_time_model(1000.0, [("source", 0.0), ("transcribe", 0.2)])
    # estimated_total = elapsed(100) + remaining(200) = 300
    assert p.estimated_total() == pytest.approx(300.0)


def test_estimate_reanchors_as_phases_complete(monkeypatch, capsys):
    p = Progress(verbose=False)
    clock = {"t": p._t0}
    monkeypatch.setattr("modules.progress.time.monotonic", lambda: clock["t"])
    p.set_time_model(1000.0, [("source", 0.0), ("transcribe", 0.1), ("llm", 0.2)])
    # Run the transcribe phase; advance the clock 50s inside it.
    clock["t"] = p._t0
    with p.phase("transcribe", "Transcribe"):
        clock["t"] = p._t0 + 50.0
    # After transcribe: elapsed 50 + remaining(llm 0.2*1000=200) = 250.
    assert p.estimated_total() == pytest.approx(250.0)
