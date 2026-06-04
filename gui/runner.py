"""Spawn and monitor a single `pipeline.py` run.

This is the bridge between the GUI and the *unmodified* pipeline. It:

1. Translates a form `opts` dict into the same CLI flags `run.ps1` would build.
2. Spawns `.venv\\Scripts\\python.exe pipeline.py <flags>` with `cwd` = repo root
   (so `./downloads` and `./clips` resolve exactly like the CLI), no console
   window, and `VOD_CLIP_PROGRESS_JSON` pointed at a fresh sidecar file.
3. Tails that JSONL sidecar on a background thread and forwards every phase event
   to the UI via the injected `emit` callback.
4. On exit, classifies the outcome (success / skip-guard / cancelled / error),
   locates the produced `clips_manifest.json`, and emits a terminal `run_end`.

Only ONE run at a time — the pipeline is GPU-bound and not meant to be parallel.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

from paths import (
    app_dir,
    config_path,
    logs_dir,
    pipeline_script,
    venv_python,
    venv_ready,
)

# No console flash when we spawn the child from a windowed (--noconsole) GUI.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

# Clip modes / source types the UI offers — kept in lockstep with run.ps1 and
# the README's documented surface.
VALID_SOURCES = ("kick", "twitch", "vodvod", "local")
VALID_MODES = ("all", "reaction", "dance", "hype", "music", "phrase")


def build_args(opts: dict) -> tuple[list[str], str | None]:
    """Translate a form dict into pipeline CLI flags.

    Returns `(args, error)`. `error` is a human string when the form is
    incomplete (missing channel/url/path), else None. Mirrors the flag->config
    mapping in pipeline.py (`--url` -> twitch_vod_url, `--channel` -> kick/vodvod
    by source) and run.ps1's question flow.
    """
    source = (opts.get("source_type") or "").strip()
    if source not in VALID_SOURCES:
        return [], f"Pick a source (got '{source or 'nothing'}')."

    args: list[str] = ["--source-type", source]

    if source in ("kick", "vodvod"):
        channel = (opts.get("channel") or "").strip()
        if not channel:
            label = "Kick channel" if source == "kick" else "vodvod.top channel"
            return [], f"Enter a {label} name."
        # Don't make the user think about the leading "@". Both resolvers already
        # canonicalize (vodvod adds "@", kick strips it), so we send a bare name
        # and let each site decide — typing "eevi" or "@eevi" both work.
        channel = channel.lstrip("@")
        args += ["--channel", channel]
    elif source == "twitch":
        url = (opts.get("url") or "").strip()
        if not url:
            return [], "Paste a Twitch VOD URL (twitch.tv/videos/...)."
        args += ["--url", url]
    elif source == "local":
        path = (opts.get("path") or "").strip()
        if not path:
            return [], "Choose a local video file."
        if not os.path.isfile(path):
            return [], f"File not found: {path}"
        args += ["--path", path]

    mode = (opts.get("clip_mode") or "all").strip()
    if mode not in VALID_MODES:
        mode = "all"
    args += ["--clip-mode", mode]

    if mode == "phrase":
        phrase = (opts.get("trigger_phrase") or "clip it").strip()
        args += ["--trigger-phrase", phrase]
    else:
        # phrase mode ignores --max-clips by design (it keeps every match), so
        # only pass it for the LLM-driven modes — same as run.ps1.
        max_clips = opts.get("max_clips")
        if max_clips not in (None, "", 0, "0"):
            args += ["--max-clips", str(max_clips)]

    for key, flag in (("start_time", "--start-time"), ("end_time", "--end-time")):
        val = (opts.get(key) or "").strip()
        if val:
            args += [flag, val]

    if opts.get("force"):
        args += ["--force"]
    if opts.get("verbose"):
        args += ["--verbose"]

    # Use the installer-tuned config when present (run.ps1 does the same).
    if os.path.isfile(config_path()):
        args += ["--config", "config.json"]

    return args, None


class PipelineRunner:
    """Owns the lifecycle of one pipeline subprocess and its progress feed."""

    def __init__(self, emit):
        # emit: callable(dict) -> pushes the event to the JS side. Must be
        # safe to call from a background thread (pywebview's evaluate_js is).
        self._emit = emit
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        self._starting = False
        self._seq = 0

    # -- public API ---------------------------------------------------------

    def is_running(self) -> bool:
        with self._lock:
            return self._starting or (self._proc is not None and self._proc.poll() is None)

    def _release_slot(self) -> None:
        with self._lock:
            self._starting = False

    def start(self, opts: dict) -> dict:
        """Validate, then launch on a background thread. Returns immediately."""
        # Claim the single run slot atomically. `_proc` is only set later on the
        # worker thread, so without this a fast double-click could pass the
        # is_running() check twice and spawn two concurrent pipelines.
        with self._lock:
            if self._starting or (self._proc is not None and self._proc.poll() is None):
                return {"status": "busy", "message": "A run is already in progress."}
            self._starting = True

        if not venv_ready():
            self._release_slot()
            return {
                "status": "no_venv",
                "message": "Setup hasn't been run yet. Double-click install.bat "
                           "first, then come back.",
            }

        args, err = build_args(opts)
        if err:
            self._release_slot()
            return {"status": "invalid", "message": err}

        thread = threading.Thread(target=self._run, args=(args,), daemon=True)
        thread.start()
        cmd = "python pipeline.py " + " ".join(args)
        return {"status": "started", "command": cmd}

    def cancel(self) -> dict:
        """Kill the whole process tree (pipeline spawns ffmpeg / Playwright /
        Ollama children that a bare terminate would orphan)."""
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return {"status": "idle"}
            self._cancelled = True
            pid = proc.pid
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                creationflags=_CREATE_NO_WINDOW,
                capture_output=True,
                check=False,
            )
        except OSError as e:
            return {"status": "error", "message": f"Could not cancel: {e}"}
        return {"status": "cancelling"}

    # -- internals ----------------------------------------------------------

    def _run(self, args: list[str]) -> None:
        self._seq += 1
        stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{self._seq}"
        progress_path = os.path.join(logs_dir(), f"progress_{stamp}.jsonl")
        log_path = os.path.join(logs_dir(), f"run_{stamp}.log")

        # Pre-create the sidecar so the tail thread has something to open even if
        # the child is slow to write its first event.
        open(progress_path, "w", encoding="utf-8").close()

        env = os.environ.copy()
        env["VOD_CLIP_PROGRESS_JSON"] = progress_path
        # Force UTF-8 + unbuffered child stdio so the combined log streams
        # cleanly regardless of the parent's code page.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        cmd = [venv_python(), "-u", pipeline_script(), *args]

        self._emit({
            "type": "run_started",
            "command": "python pipeline.py " + " ".join(args),
            "log": log_path,
        })

        stop = threading.Event()
        rc = -1
        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as logf:
                proc = subprocess.Popen(
                    cmd,
                    cwd=app_dir(),
                    env=env,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    creationflags=_CREATE_NO_WINDOW,
                )
                with self._lock:
                    self._proc = proc
                    self._cancelled = False
                    self._starting = False   # slot now held by a live _proc

                tail = threading.Thread(
                    target=self._tail_progress,
                    args=(progress_path, stop),
                    daemon=True,
                )
                tail.start()
                rc = proc.wait()
                stop.set()
                tail.join(timeout=3.0)
        except OSError as e:
            self._emit({
                "type": "run_end",
                "returncode": -1,
                "outcome": "error",
                "error": f"Failed to launch pipeline: {e}",
            })
            return
        finally:
            with self._lock:
                cancelled = self._cancelled
                self._proc = None
                self._starting = False   # release slot even if launch failed

        self._emit(self._classify(rc, cancelled, log_path))

    def _tail_progress(self, path: str, stop: threading.Event) -> None:
        """Forward each JSON line the child appends, until stopped + drained.

        Buffers an incomplete trailing line (no newline yet) across reads so a
        progress event caught mid-write isn't split and dropped."""
        pos = 0
        buf = ""
        # Poll ~5x/sec. After `stop`, do one final drain so the terminal
        # phase_end isn't lost to a race with proc exit.
        while True:
            stopped = stop.is_set()
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    data = f.read()
                    pos = f.tell()
            except OSError:
                data = ""
            if data:
                buf += data
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    self._forward_line(line)
            if stopped:
                self._forward_line(buf)   # last line may lack a trailing newline
                return
            stop.wait(0.2)

    def _forward_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            self._emit(json.loads(line))
        except json.JSONDecodeError:
            pass

    def _classify(self, rc: int, cancelled: bool, log_path: str) -> dict:
        """Turn an exit code + the captured log into a terminal run_end event."""
        log_text = ""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                log_text = f.read()
        except OSError:
            pass

        if cancelled:
            return {
                "type": "run_end",
                "returncode": rc,
                "outcome": "cancelled",
                "message": "Run cancelled. A partial download may remain in "
                           "downloads/ — delete that video file before re-running. "
                           "(Force re-cuts clips but still reuses a cached video.)",
            }

        manifest = self._find_manifest(log_text)

        # Skip-guard: pipeline.py prints "[skip] <manifest> already has N clips"
        # and returns exit 0 with no phase events.
        skip_line = next(
            (ln for ln in log_text.splitlines() if ln.lstrip().startswith("[skip]")),
            None,
        )
        if rc == 0 and skip_line:
            return {
                "type": "run_end",
                "returncode": 0,
                "outcome": "skipped",
                "manifest": manifest,
                "message": skip_line.strip(),
            }

        if rc == 0:
            return {
                "type": "run_end",
                "returncode": 0,
                "outcome": "success",
                "manifest": manifest,
            }

        # Failure: surface the tail of the captured stdout+stderr (the pipeline
        # writes tracebacks to stderr; there is no separate log file).
        tail = "\n".join(log_text.splitlines()[-40:]).strip()
        return {
            "type": "run_end",
            "returncode": rc,
            "outcome": "error",
            "error": tail or f"Pipeline exited with code {rc}.",
            "log": log_path,
        }

    @staticmethod
    def _find_manifest(log_text: str) -> str | None:
        """Pull the manifest path from the pipeline output. Handles paths that
        contain spaces (e.g. a custom output_dir under C:\\Users\\John Doe\\)."""
        def resolve(cand):
            cand = cand.strip().strip('"')
            return os.path.normpath(os.path.join(app_dir(), cand)) if cand else None

        # Success path: "   Manifest: <path>" — take the whole remainder.
        for line in log_text.splitlines():
            s = line.strip()
            if s.startswith("Manifest:"):
                m = resolve(s[len("Manifest:"):])
                if m:
                    return m
        # Skip-guard: "[skip] <path> already has N clips. ..." — the path may
        # contain spaces, so slice it out rather than tokenizing on whitespace.
        for line in log_text.splitlines():
            s = line.strip()
            if s.startswith("[skip]"):
                rest = s[len("[skip]"):].strip()
                idx = rest.find(" already has")
                m = resolve(rest[:idx] if idx != -1 else rest)
                if m:
                    return m
        # Generic fallback (paths without spaces): last token ending in the file.
        for line in log_text.splitlines():
            if "clips_manifest.json" in line:
                for tok in reversed(line.split()):
                    if tok.endswith("clips_manifest.json"):
                        return resolve(tok)
        return None
