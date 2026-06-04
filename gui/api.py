"""The `JsApi` bridge — every Python method the web frontend can call.

In pywebview, an instance passed as `js_api=` exposes its public methods to JS as
`pywebview.api.<method>(...)`, each returning a Promise that resolves with the
method's JSON-serializable return value. We also push *unsolicited* progress
events the other way via `window.evaluate_js("window.onProgress(...)")`.

Everything here is a thin, read-mostly wrapper over the existing repo: it reads
`config.json`, shells `install.ps1 -Check`, opens files in the default player,
writes `nightly.ps1`. The heavy lifting stays in `pipeline.py`.
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess

import paths
from runner import VALID_MODES, VALID_SOURCES, PipelineRunner

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_TASK_NAME = "twitch-highlights-nightly"

# install.ps1 -Check line prefixes -> UI severity.
_CHECK_RE = re.compile(r"^\s*\[(OK|WARN|FAIL|WOULD)\]\s+(.*)$")
_INFO_RE = re.compile(r"^\s*\.\.\.\s+(.*)$")
_SECTION_RE = re.compile(r"^=+\s*(.*?)\s*=+$")

# Keys the Settings view is allowed to round-trip. Anything outside this set in
# config.json is preserved untouched (advanced knobs, api keys, etc.).
_SETTINGS_KEYS = (
    "whisper_model", "whisper_device", "llm_backend", "ollama_model",
    "openai_model", "max_clips", "clip_mode", "trigger_phrase",
    "burn_subtitles", "cleanup_source", "quality", "verbose",
)


class JsApi:
    def __init__(self):
        # NOTE: these MUST stay underscore-prefixed. pywebview builds the JS
        # bridge by recursively walking every *public* (non-`_`) attribute of
        # this object (webview/util.py get_functions). A public `window`
        # attribute pointing back at the pywebview Window is circular and makes
        # that walk never finish, so the page's `loaded` event never fires and
        # the whole UI hangs. Keeping state private makes pywebview expose only
        # the public *methods* below as JS functions.
        self._window = None         # set by app.py after create_window
        self._alive = True
        self._runner = PipelineRunner(emit=self._push)

    # -- window plumbing (called from app.py, not from JS) ------------------

    def attach(self, window) -> None:
        self._window = window

    def set_alive(self, alive: bool) -> None:
        self._alive = alive

    def _push(self, event: dict) -> None:
        """Forward a runner/progress event to the JS side. Thread-safe."""
        win = self._window
        if win is None or not self._alive:
            return
        try:
            # json.dumps(ensure_ascii=True) escapes U+2028/2029, so the result
            # is valid JS as well as valid JSON.
            win.evaluate_js("window.onProgress(%s)" % json.dumps(event))
        except Exception:
            # Window may be tearing down mid-push; never let it crash the run.
            pass

    # -- environment / status ----------------------------------------------

    def get_status(self) -> dict:
        """Cheap, instant status for first paint (no subprocess)."""
        return {
            "venv_ready": paths.venv_ready(),
            "has_config": os.path.isfile(paths.config_path()),
            "app_dir": paths.app_dir(),
            "running": self._runner.is_running(),
            "nightly_registered": self._nightly_registered(),
        }

    def preflight(self) -> dict:
        """Run `install.ps1 -Check` (read-only) and parse its OK/WARN/FAIL lines.

        Slow-ish (it imports torch in the venv to test CUDA) — the UI shows a
        spinner while this resolves.
        """
        script = paths.install_script()
        if not os.path.isfile(script):
            return {"ok": False, "error": "install.ps1 not found.", "items": []}
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", script, "-Check"],
                cwd=paths.app_dir(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_CREATE_NO_WINDOW,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Preflight timed out (180s).", "items": []}
        except OSError as e:
            return {"ok": False, "error": f"Could not run preflight: {e}", "items": []}

        raw = (proc.stdout or "") + (proc.stderr or "")
        items: list[dict] = []
        section = ""
        for line in raw.splitlines():
            m_sec = _SECTION_RE.match(line.strip())
            if m_sec and m_sec.group(1) and "===" not in m_sec.group(1):
                section = m_sec.group(1)
                continue
            m = _CHECK_RE.match(line)
            if m:
                level = m.group(1)
                items.append({
                    "level": {"OK": "ok", "WARN": "warn", "FAIL": "fail",
                              "WOULD": "warn"}[level],
                    "text": m.group(2).strip(),
                    "section": section,
                })
                continue
            m_info = _INFO_RE.match(line)
            if m_info:
                items.append({"level": "info", "text": m_info.group(1).strip(),
                              "section": section})
        ok = not any(i["level"] == "fail" for i in items)
        return {"ok": ok, "items": items, "raw": raw}

    # -- config -------------------------------------------------------------

    def _load_config_dict(self) -> dict:
        """Full config dict: config.json if present, else the example template."""
        for path in (paths.config_path(), paths.config_example_path()):
            if os.path.isfile(path):
                try:
                    with open(path, encoding="utf-8-sig") as f:
                        return json.load(f)
                except (OSError, json.JSONDecodeError):
                    continue
        return {}

    def get_config(self) -> dict:
        cfg = self._load_config_dict()
        return {
            "config": cfg,
            "has_config": os.path.isfile(paths.config_path()),
            "editable_keys": list(_SETTINGS_KEYS),
        }

    def save_config(self, updates: dict) -> dict:
        """Merge `updates` into config.json, preserving every other key.

        Written as UTF-8 *without* BOM to match what the installer writes (and
        what `config.py`'s `utf-8-sig` reader tolerates either way)."""
        if not isinstance(updates, dict):
            return {"status": "error", "message": "Invalid settings payload."}
        cfg = self._load_config_dict()
        for key, val in updates.items():
            if key not in _SETTINGS_KEYS:
                continue
            # Defensive coercion so a bad client value can't write a config the
            # pipeline mishandles (e.g. a cleared field -> max_clips:0 -> 0 clips).
            if key == "max_clips":
                try:
                    val = max(1, int(val))
                except (TypeError, ValueError):
                    val = 10
            cfg[key] = val
        try:
            with open(paths.config_path(), "w", encoding="utf-8", newline="\n") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except OSError as e:
            return {"status": "error", "message": f"Could not save config: {e}"}
        return {"status": "saved", "config": cfg}

    # -- run control --------------------------------------------------------

    def start_run(self, opts: dict) -> dict:
        return self._runner.start(opts or {})

    def cancel_run(self) -> dict:
        return self._runner.cancel()

    def run_meta(self) -> dict:
        """Static options for the run form (kept in sync with the pipeline)."""
        return {"sources": list(VALID_SOURCES), "modes": list(VALID_MODES)}

    # -- results ------------------------------------------------------------

    def list_runs(self) -> dict:
        """Every clips_manifest.json under ./clips, newest first."""
        root = paths.clips_dir()
        runs = []
        pattern = os.path.join(root, "**", "clips_manifest.json")
        for manifest in glob.glob(pattern, recursive=True):
            try:
                mtime = os.path.getmtime(manifest)
                with open(manifest, encoding="utf-8") as f:
                    entries = json.load(f)
                count = len(entries) if isinstance(entries, list) else 0
            except (OSError, json.JSONDecodeError):
                continue
            run_dir = os.path.dirname(manifest)
            label = os.path.relpath(run_dir, root).replace(os.sep, " / ")
            runs.append({
                "label": label,
                "manifest": manifest,
                "dir": run_dir,
                "count": count,
                "mtime": mtime,
            })
        runs.sort(key=lambda r: r["mtime"], reverse=True)
        return {"runs": runs}

    def load_results(self, manifest: str | None = None) -> dict:
        """Clip cards for one run (the given manifest, or the most recent)."""
        if not manifest:
            runs = self.list_runs()["runs"]
            if not runs:
                return {"entries": [], "dir": None, "manifest": None}
            manifest = runs[0]["manifest"]
        if not os.path.isfile(manifest):
            return {"entries": [], "dir": None, "manifest": manifest,
                    "error": "Manifest not found."}
        try:
            with open(manifest, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            return {"entries": [], "dir": None, "manifest": manifest,
                    "error": f"Could not read manifest: {e}"}

        run_dir = os.path.dirname(manifest)
        entries = []
        for item in raw if isinstance(raw, list) else []:
            meta = item.get("meta", {}) if isinstance(item, dict) else {}
            file_rel = item.get("file", "") if isinstance(item, dict) else ""
            cap_rel = item.get("captioned", "") if isinstance(item, dict) else ""
            abs_file = self._abs(file_rel)
            abs_cap = self._abs(cap_rel) if cap_rel else None
            start = meta.get("start")
            end = meta.get("end")
            duration = (end - start) if isinstance(start, (int, float)) and isinstance(end, (int, float)) else None
            entries.append({
                "file": abs_file,
                "captioned": abs_cap,
                "name": os.path.basename(abs_file or file_rel),
                "reason": meta.get("reason", ""),
                "score": meta.get("score"),
                "description": meta.get("description", ""),
                "start": start,
                "end": end,
                "duration": duration,
                "exists": bool(abs_file and os.path.isfile(abs_file)),
            })
        return {"entries": entries, "dir": run_dir, "manifest": manifest}

    @staticmethod
    def _abs(rel: str) -> str:
        if not rel:
            return ""
        if os.path.isabs(rel):
            return os.path.normpath(rel)
        return os.path.normpath(os.path.join(paths.app_dir(), rel))

    # -- shell-outs ---------------------------------------------------------

    def open_path(self, path: str) -> dict:
        """Open a file in its default app, or a folder in Explorer."""
        if not path or not os.path.exists(path):
            return {"status": "error", "message": "Path not found."}
        try:
            os.startfile(path)  # noqa: S606 - Windows-only, intended.
        except OSError as e:
            return {"status": "error", "message": str(e)}
        return {"status": "ok"}

    def open_containing(self, path: str) -> dict:
        if not path:
            return {"status": "error", "message": "No path."}
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        return self.open_path(folder)

    def pick_file(self) -> dict:
        """Native open-file dialog for the `local` source type."""
        if self._window is None:
            return {"path": None}
        try:
            import webview
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("Video files (*.mp4;*.ts;*.mkv;*.mov)", "All files (*.*)"),
            )
        except Exception as e:
            return {"path": None, "error": str(e)}
        if not result:
            return {"path": None}
        return {"path": result[0]}

    # -- nightly scheduling -------------------------------------------------

    def _nightly_registered(self) -> bool:
        try:
            proc = subprocess.run(
                ["schtasks", "/Query", "/TN", _TASK_NAME],
                capture_output=True, text=True,
                creationflags=_CREATE_NO_WINDOW, timeout=15,
            )
            return proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def schedule_nightly(self, cfg: dict) -> dict:
        """Write nightly.ps1 from the user's channel lists, then (optionally)
        register a daily Task Scheduler job. Confirm-before-register lives in the
        UI; `register=False` only writes the script."""
        cfg = cfg or {}
        # Strip a leading "@" — the resolvers canonicalize per site, so users
        # never have to remember which platform wants it.
        def _chans(key):
            return [n for n in
                    ((c or "").strip().lstrip("@") for c in (cfg.get(key) or []))
                    if n]
        vodvod = _chans("vodvod_channels")
        kick = _chans("kick_channels")
        if not vodvod and not kick:
            return {"status": "error", "message": "Add at least one channel."}

        time_str = (cfg.get("time") or "03:00").strip()
        if not re.match(r"^\d{1,2}:\d{2}$", time_str):
            return {"status": "error", "message": "Time must be HH:MM (24h)."}
        backend = cfg.get("llm_backend") or "ollama"
        model = cfg.get("ollama_model") or "gpt-oss:20b"
        max_vodvod = int(cfg.get("max_clips_vodvod") or 10)
        max_kick = int(cfg.get("max_clips_kick") or 15)

        # Reject anything that isn't a plausible channel handle / model / backend
        # before it reaches the generated PowerShell. These strings are otherwise
        # untrusted (stream/channel-derived); _render_nightly also single-quotes
        # them, so this is belt-and-suspenders against script injection.
        chan_re = re.compile(r"^@?[A-Za-z0-9_.-]{1,64}$")
        bad = [c for c in (vodvod + kick) if not chan_re.match(c)]
        if bad:
            return {"status": "error",
                    "message": "Invalid channel name(s): " + ", ".join(bad[:5])
                    + ". Use letters, numbers, _ . - (optional leading @)."}
        if backend not in ("ollama", "openai"):
            return {"status": "error", "message": "llm_backend must be ollama or openai."}
        if not re.match(r"^[A-Za-z0-9_.:-]{1,64}$", model):
            return {"status": "error", "message": "Invalid model name."}

        script = self._render_nightly(vodvod, kick, backend, model, max_vodvod, max_kick)
        try:
            with open(paths.nightly_path(), "w", encoding="utf-8", newline="\r\n") as f:
                f.write(script)
        except OSError as e:
            return {"status": "error", "message": f"Could not write nightly.ps1: {e}"}

        register_cmd = (
            f'schtasks /Create /SC DAILY /ST {time_str} /TN "{_TASK_NAME}" '
            f'/TR "powershell -NoProfile -ExecutionPolicy Bypass -File '
            f'{paths.nightly_path()}" /F'
        )
        if not cfg.get("register"):
            return {"status": "written", "path": paths.nightly_path(),
                    "command": register_cmd}

        try:
            proc = subprocess.run(
                ["schtasks", "/Create", "/SC", "DAILY", "/ST", time_str,
                 "/TN", _TASK_NAME, "/TR",
                 f'powershell -NoProfile -ExecutionPolicy Bypass -File "{paths.nightly_path()}"',
                 "/F"],
                capture_output=True, text=True,
                creationflags=_CREATE_NO_WINDOW, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"status": "written", "path": paths.nightly_path(),
                    "command": register_cmd,
                    "message": f"Wrote nightly.ps1 but could not register: {e}"}

        if proc.returncode == 0:
            return {"status": "registered", "path": paths.nightly_path(),
                    "time": time_str}
        # Most common failure is needing an elevated shell.
        return {
            "status": "needs_elevation",
            "path": paths.nightly_path(),
            "command": register_cmd,
            "message": (proc.stderr or proc.stdout or "").strip()
                       or "schtasks was denied — run the command below in an "
                          "Administrator PowerShell.",
        }

    def unschedule_nightly(self) -> dict:
        try:
            proc = subprocess.run(
                ["schtasks", "/Delete", "/TN", _TASK_NAME, "/F"],
                capture_output=True, text=True,
                creationflags=_CREATE_NO_WINDOW, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"status": "error", "message": str(e)}
        if proc.returncode == 0:
            return {"status": "removed"}
        return {"status": "error",
                "message": (proc.stderr or proc.stdout or "").strip()}

    @staticmethod
    def _render_nightly(vodvod, kick, backend, model, max_vodvod, max_kick) -> str:
        def ps_lit(s):
            # Single-quoted PowerShell literal: PS does NO $ / backtick / $(...)
            # subexpression expansion inside single quotes, so this is safe even
            # for hostile input. Double any embedded single quote to escape it.
            return "'%s'" % str(s).replace("'", "''")

        def ps_list(items):
            return ", ".join(ps_lit(i) for i in items)

        backend_lit = ps_lit(backend)
        model_lit = ps_lit(model)
        max_vodvod = int(max_vodvod)
        max_kick = int(max_kick)

        return f"""# Nightly highlight pipeline runner (generated by the GUI).
# Edit freely — re-generating from the app overwrites this file.
# Registered as Task Scheduler job "{_TASK_NAME}".

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logFile = Join-Path $logDir ("nightly_{{0}}.log" -f (Get-Date -Format "yyyy-MM-dd"))

$venvPy = Join-Path $PSScriptRoot ".venv\\Scripts\\python.exe"

$vodvodChannels = @({ps_list(vodvod)})
$kickChannels   = @({ps_list(kick)})

$llmBackend  = {backend_lit}
$ollamaModel = {model_lit}

"===== Run started: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" | Tee-Object -FilePath $logFile -Append | Out-Host

foreach ($ch in $vodvodChannels) {{
    "----- vodvod $ch -----" | Tee-Object -FilePath $logFile -Append | Out-Host
    & $venvPy pipeline.py `
        --source-type vodvod `
        --channel $ch `
        --clip-mode all `
        --max-clips {max_vodvod} `
        --llm-backend $llmBackend `
        --model $ollamaModel 2>&1 | Tee-Object -FilePath $logFile -Append | Out-Host
}}

foreach ($ch in $kickChannels) {{
    "----- kick $ch -----" | Tee-Object -FilePath $logFile -Append | Out-Host
    & $venvPy pipeline.py `
        --source-type kick `
        --channel $ch `
        --clip-mode all `
        --max-clips {max_kick} `
        --llm-backend $llmBackend `
        --model $ollamaModel 2>&1 | Tee-Object -FilePath $logFile -Append | Out-Host
}}

"===== Run finished: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" | Tee-Object -FilePath $logFile -Append | Out-Host
"""
