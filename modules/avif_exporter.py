"""Export finished clips to Discord-friendly animated AVIFs.

Thin orchestrator over the AvifTools PowerShell module
(github.com/NexRushVr/optimized-discord-gifs-avif) — it builds a job, shells out
to `scripts/avif_export.ps1` (which imports AvifTools and runs `Convert-ToAvif`),
and collects the results. No encoding logic lives here.

Each clip yields two AVIFs named after the clip itself, so a highlight's mp4 and
its AVIFs share one `<streamer>-<random>` identity:
    <streamer>-<random>-not.avif   high quality (low CRF, 60fps — "not optimized")
    <streamer>-<random>-opt.avif   optimized   (higher CRF, 30fps — small for chat)

Usable two ways:
- as a library:  export_clips_to_avif(clips, out_dir, cfg, on_progress)
- as a script:   python modules/avif_exporter.py --manifest <path> --source captioned|raw
                 (the GUI's on-demand "make AVIFs" button calls this in the venv)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _driver_path() -> str:
    return os.path.join(_repo_root(), "scripts", "avif_export.ps1")


def _default_local_repo() -> str:
    """A sibling checkout of the AvifTools repo, if present (dev convenience)."""
    cand = os.path.join(os.path.dirname(_repo_root()),
                        "optimized-discord-gifs-avif", "module", "AvifTools")
    return cand if os.path.isdir(cand) else ""


def _avif_base(clip_path: str) -> str:
    """The shared name for a clip's AVIFs: the clip stem minus a `_captioned`
    suffix, so the captioned and raw cuts of one highlight map to one base."""
    stem = os.path.splitext(os.path.basename(clip_path))[0]
    if stem.endswith("_captioned"):
        stem = stem[: -len("_captioned")]
    return stem


def _size_suffix(mb) -> str:
    """Filename suffix for a target-size variant: 10 -> '10mb', 7.5 -> '7_5mb'."""
    return ("%g" % float(mb)).replace(".", "_") + "mb"


def _variants(cfg) -> list:
    """Quality mode (avif_target_mb == 0) -> the not/opt pair. Target mode -> one
    size-targeted variant (<base>-<N>mb.avif), aimed under N MB."""
    target = float(cfg.get("avif_target_mb") or 0)
    if target > 0:
        # fps acts as a cap (the HQ fps "request") in target mode.
        return [{"suffix": _size_suffix(target), "targetMb": target,
                 "fps": int(cfg.get("avif_hq_fps", 60))}]
    return [
        {"suffix": "not", "crf": int(cfg.get("avif_hq_crf", 18)),
         "fps": int(cfg.get("avif_hq_fps", 60))},
        {"suffix": "opt", "crf": int(cfg.get("avif_opt_crf", 30)),
         "fps": int(cfg.get("avif_opt_fps", 30))},
    ]


def export_clips_to_avif(clips, out_dir, cfg, on_progress=None):
    """Encode each clip in `clips` to an -opt and -not AVIF in `out_dir`.

    on_progress(done, total, label) is called per encode for progress display.
    Returns a list of {input, name, opt, not} dicts.
    """
    clips = [c for c in clips if c]
    if not clips:
        return []
    os.makedirs(out_dir, exist_ok=True)

    items = [{"input": os.path.abspath(c), "name": _avif_base(c)} for c in clips]
    variants = _variants(cfg)
    levers = [s.strip() for s in str(cfg.get("avif_levers", "Quality,Resolution,Fps")).split(",")
              if s.strip()]
    job = {
        "outDir": os.path.abspath(out_dir),
        "modulePath": cfg.get("avif_module_path", "") or "",
        "localRepo": _default_local_repo(),
        "maxWidth": int(cfg.get("avif_max_width", 854)),
        "preset": int(cfg.get("avif_preset", 6)),
        "needTargetSize": any(v.get("targetMb", 0) for v in variants),
        "levers": levers,
        "minWidth": int(cfg.get("avif_min_width", 480)),
        "minFps": int(cfg.get("avif_min_fps", 24)),
        "variants": variants,
        "items": items,
    }

    job_file = tempfile.mktemp(suffix=".avifjob.json")
    result_file = tempfile.mktemp(suffix=".avifres.json")
    with open(job_file, "w", encoding="utf-8") as f:
        json.dump(job, f)

    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", _driver_path(), "-JobFile", job_file, "-ResultFile", result_file]
    verbose = bool(cfg.get("verbose"))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=_CREATE_NO_WINDOW,
    )
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith("AVIFPROGRESS"):
                parts = line.split()
                done = total = 0
                if len(parts) >= 2 and "/" in parts[1]:
                    try:
                        done, total = (int(x) for x in parts[1].split("/", 1))
                    except ValueError:
                        pass
                label = parts[2] if len(parts) >= 3 else ""
                if on_progress:
                    on_progress(done, total, label)
            elif line and verbose:
                print(f"    {line}")
    finally:
        proc.wait()

    results = []
    try:
        # utf-8-sig: PowerShell 5.1's Set-Content -Encoding utf8 writes a BOM.
        with open(result_file, encoding="utf-8-sig") as f:
            data = json.load(f)
        results = data.get("results", []) if isinstance(data, dict) else []
    except (OSError, json.JSONDecodeError):
        pass
    for p in (job_file, result_file):
        try:
            os.remove(p)
        except OSError:
            pass

    by_input: dict[str, dict] = {}
    for r in results:
        by_input.setdefault(r["input"], {})[r["variant"]] = r["file"]
    out = []
    for it in items:
        files = by_input.get(it["input"], {})   # {suffix: path}
        out.append({"input": it["input"], "name": it["name"], "files": files,
                    "opt": files.get("opt"), "not": files.get("not")})
    return out


def clips_from_manifest(manifest: list, source: str) -> list:
    """Pick each entry's clip file. source='captioned' falls back to the raw cut
    when an entry has no captioned variant; 'raw' always uses the cut."""
    files = []
    for item in manifest if isinstance(manifest, list) else []:
        if not isinstance(item, dict):
            continue
        cap = item.get("captioned")
        raw = item.get("file")
        files.append(cap if (source == "captioned" and cap) else raw)
    return [f for f in files if f]


def _resolve_clip(p: str, run_dir: str) -> str:
    """Resolve a manifest clip path. The pipeline writes paths relative to the
    REPO ROOT (output_dir is `./clips/...`), not the run dir — so resolving
    against run_dir double-joins the path and the file is "missing". Try the
    likely bases and pick the one that exists."""
    if not p:
        return p
    if os.path.isabs(p):
        return os.path.normpath(p)
    for base in (_repo_root(), run_dir, os.getcwd()):
        cand = os.path.normpath(os.path.join(base, p))
        if os.path.isfile(cand):
            return cand
    return os.path.normpath(os.path.join(_repo_root(), p))


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Export run clips to AVIF via AvifTools.")
    ap.add_argument("--manifest", required=True, help="Path to clips_manifest.json")
    ap.add_argument("--source", choices=["captioned", "raw"], default="captioned")
    ap.add_argument("--target", type=float, default=None,
                    help="Target size in MB (one <base>-<N>mb.avif per clip). "
                         "Omit/0 for the quality not+opt pair.")
    ap.add_argument("--out", default=None,
                    help="Output dir (default <run>/avif, or <run>/avif-clean for raw)")
    args = ap.parse_args(argv)

    sys.path.insert(0, _repo_root())
    try:
        from config import load_config  # noqa: E402
        cfg = load_config(os.path.join(_repo_root(), "config.json"))
    except Exception:
        from config import DEFAULT_CONFIG  # noqa: E402
        cfg = dict(DEFAULT_CONFIG)

    if args.target is not None:
        cfg["avif_target_mb"] = args.target

    manifest_path = os.path.abspath(args.manifest)
    run_dir = os.path.dirname(manifest_path)
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    clips = [_resolve_clip(p, run_dir) for p in clips_from_manifest(manifest, args.source)]
    if not clips:
        print("No clips found in manifest.")
        return 1

    # Keep captioned- and raw-derived AVIFs in separate folders so they don't
    # collide (both reuse the same <streamer>-<random> base).
    out_dir = args.out or os.path.join(run_dir, "avif" if args.source == "captioned" else "avif-clean")

    results = export_clips_to_avif(
        clips, out_dir, cfg,
        on_progress=lambda d, t, lbl: print(f"AVIFPROGRESS {d}/{t} {lbl}", flush=True),
    )
    made = sum(len(r.get("files") or {}) for r in results)
    print(f"AVIFDONE {made} files -> {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
