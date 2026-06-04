"""App version shown in the GUI so it's obvious which build is running.

`build_gui.ps1` bakes the git tag into `_BAKED` for the frozen exe; running from
source resolves it live via `git describe`.
"""

import os
import subprocess
import sys

_BAKED = "dev"   # build_gui.ps1 overwrites this line with the release tag


def get_version() -> str:
    if getattr(sys, "frozen", False):
        return _BAKED
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            ["git", "-C", root, "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return _BAKED
