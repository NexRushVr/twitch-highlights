"""Twitch Highlights — desktop GUI entry point.

A thin pywebview shell around the existing pipeline. Run from source with
`python gui\\app.py` (see gui.bat) or as the frozen `TwitchHighlights.exe`.

The whole app is: one native window hosting `web/index.html`, a `JsApi` bridge
(api.py) the page calls into, and a background subprocess (runner.py) that drives
the unchanged `pipeline.py` and streams progress back to the page.
"""

from __future__ import annotations

import os
import sys

# Make the flat gui modules importable both from source (`python gui/app.py`,
# where this dir is already sys.path[0]) and when frozen. Belt-and-suspenders.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import webview  # noqa: E402  (after sys.path setup)

import paths  # noqa: E402
from api import JsApi  # noqa: E402

WEBVIEW2_DOWNLOAD = "https://developer.microsoft.com/microsoft-edge/webview2/"


def _message_box(title: str, text: str) -> None:
    """Native Win32 message box — used before the webview exists."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)  # MB_ICONINFORMATION
    except Exception:
        # Headless / non-Windows fallback.
        print(f"{title}: {text}", file=sys.stderr)


def _webview2_present() -> bool:
    """True if the Evergreen WebView2 runtime is installed.

    Present by default on Windows 11; we still check so a missing runtime yields
    a friendly message instead of an opaque crash. Non-Windows -> assume present
    (dev convenience; the tool is Windows-first).
    """
    if sys.platform != "win32":
        return True
    try:
        import winreg
    except ImportError:
        return True

    # The Evergreen runtime registers this client GUID. 64-bit Windows puts the
    # machine-wide key under WOW6432Node; per-user installs land in HKCU.
    candidates = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
    ]
    for hive, subkey in candidates:
        try:
            with winreg.OpenKey(hive, subkey) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                if pv and pv != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def main() -> int:
    if not paths.venv_ready():
        _message_box(
            "Twitch Highlights — setup needed",
            "The project isn't installed yet (no .venv found).\n\n"
            "Double-click install.bat in the twitch-highlights folder first, "
            "then launch this app again.",
        )
        return 1

    if not _webview2_present():
        _message_box(
            "Twitch Highlights — WebView2 required",
            "This app needs the Microsoft Edge WebView2 runtime, which wasn't "
            "found on this PC.\n\nInstall the free 'Evergreen' runtime from:\n"
            f"{WEBVIEW2_DOWNLOAD}\n\nThen launch this app again.",
        )
        # Best-effort: open the download page.
        try:
            os.startfile(WEBVIEW2_DOWNLOAD)  # noqa: S606
        except OSError:
            pass
        return 1

    index = paths.resource_path("web", "index.html")
    api = JsApi()
    window = webview.create_window(
        "Twitch Highlights",
        url=index,
        js_api=api,
        width=1120,
        height=820,
        min_size=(900, 640),
        background_color="#11131a",
    )
    api.attach(window)

    # Once the window is gone, evaluate_js would throw — flip the alive flag so
    # any in-flight run's progress pushes become no-ops.
    def _on_closed() -> None:
        api.set_alive(False)

    try:
        window.events.closed += _on_closed
    except Exception:
        pass

    # gui=None lets pywebview pick the platform default (EdgeChromium on Windows).
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
