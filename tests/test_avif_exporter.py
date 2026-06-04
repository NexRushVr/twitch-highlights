import os

from modules import avif_exporter as ax


def test_clips_from_manifest_picks_captioned_then_falls_back():
    m = [{"file": "a.mp4", "captioned": "a_captioned.mp4"}, {"file": "b.mp4"}]
    assert ax.clips_from_manifest(m, "captioned") == ["a_captioned.mp4", "b.mp4"]
    assert ax.clips_from_manifest(m, "raw") == ["a.mp4", "b.mp4"]


def test_avif_base_strips_captioned_suffix():
    assert ax._avif_base("moonbuvr-c4cf42_captioned.mp4") == "moonbuvr-c4cf42"
    assert ax._avif_base("/x/y/moonbuvr-c4cf42.mp4") == "moonbuvr-c4cf42"


def test_size_suffix():
    assert ax._size_suffix(10) == "10mb"
    assert ax._size_suffix(7.5) == "7_5mb"


# ---------------------------------------------------------------------------
# Regression: manifests store REPO-ROOT-relative clip paths
# (e.g. "./clips/streamer/date/clip.mp4"). Resolving them against the run dir
# double-joined the path -> "missing" clips -> the on-demand export at a new
# size silently produced nothing. _resolve_clip must find the real file.
# ---------------------------------------------------------------------------

def test_resolve_clip_handles_repo_root_relative_path(tmp_path, monkeypatch):
    monkeypatch.setattr(ax, "_repo_root", lambda: str(tmp_path))
    run_dir = tmp_path / "clips" / "moonbuvr" / "2026-06-03"
    run_dir.mkdir(parents=True)
    clip = run_dir / "moonbuvr-c4cf42_captioned.mp4"
    clip.write_bytes(b"x")

    rel = os.path.join(".", "clips", "moonbuvr", "2026-06-03", clip.name)
    resolved = ax._resolve_clip(rel, str(run_dir))

    assert os.path.isfile(resolved)
    assert os.path.normpath(resolved) == os.path.normpath(str(clip))


def test_resolve_clip_handles_bare_filename(tmp_path, monkeypatch):
    # A run-dir-relative bare filename must still resolve.
    monkeypatch.setattr(ax, "_repo_root", lambda: str(tmp_path / "nope"))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    clip = run_dir / "x.mp4"
    clip.write_bytes(b"x")
    assert os.path.isfile(ax._resolve_clip("x.mp4", str(run_dir)))


def test_resolve_clip_passes_absolute_through(tmp_path):
    p = str(tmp_path / "abs.mp4")
    assert ax._resolve_clip(p, str(tmp_path / "run")) == os.path.normpath(p)
