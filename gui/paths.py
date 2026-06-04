"""Filesystem anchoring for the GUI.

The GUI is a thin front-end that *drives the existing repo scripts in place*. It
never reimplements the pipeline — it shells out to `.venv\\Scripts\\python.exe
pipeline.py` exactly like `run.ps1` does. So every path here is resolved relative
to the **repo root**, whether we're running from source (`python gui/app.py`) or
from a PyInstaller-frozen `TwitchHighlights.exe` sitting at the repo root.

Two different anchors, easy to confuse (see the Plan doc):

* **Repo/sibling files** (`.venv`, `pipeline.py`, `config.json`, `clips/`) live
  next to the exe -> anchor on the *exe directory* (`app_dir()`), NEVER `_MEIPASS`.
* **Bundled UI assets** (`web/index.html`) are packed *into* the exe -> anchor on
  `sys._MEIPASS` (`resource_path()`).
"""

from __future__ import annotations

import os
import sys


def app_dir() -> str:
    """The repo root: where `.venv`, `pipeline.py`, and `config.json` live.

    Frozen: the directory containing the exe (we ship it at the repo root).
    Source: the parent of this `gui/` package.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_path(*parts: str) -> str:
    """Absolute path to a bundled GUI asset (the `web/` dir, the icon, ...).

    Frozen: under PyInstaller's `_MEIPASS` temp-extract dir.
    Source: under this `gui/` package directory.
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def venv_python() -> str:
    """Path to the project's venv interpreter (the one the installer created)."""
    # Windows layout; the tool is Windows-first (README "Platform assumptions").
    return os.path.join(app_dir(), ".venv", "Scripts", "python.exe")


def pipeline_script() -> str:
    return os.path.join(app_dir(), "pipeline.py")


def config_path() -> str:
    return os.path.join(app_dir(), "config.json")


def config_example_path() -> str:
    return os.path.join(app_dir(), "config.example.json")


def clips_dir() -> str:
    """Default output root. Mirrors config `output_dir` default `./clips`."""
    return os.path.join(app_dir(), "clips")


def logs_dir() -> str:
    """Per-run progress sidecars + captured stderr live here (git-ignored)."""
    d = os.path.join(app_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def install_script() -> str:
    return os.path.join(app_dir(), "install.ps1")


def nightly_example_path() -> str:
    return os.path.join(app_dir(), "nightly.example.ps1")


def nightly_path() -> str:
    return os.path.join(app_dir(), "nightly.ps1")


def venv_ready() -> bool:
    """True once the installer has created the venv interpreter."""
    return os.path.isfile(venv_python())
