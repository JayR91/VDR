"""Startup crash dump for frozen Windows builds.

PyInstaller's windowed bootloader titles the dialog "Unhandled exception in
script" and is easy to dismiss without the traceback. Write it to a stable
path the user can paste.
"""

from __future__ import annotations

import os
import sys


def crash_log_path() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "VDR", "crash.log")
    return os.path.join(os.path.expanduser("~"), ".vdr", "crash.log")


def write_crash_log(text: str) -> str | None:
    path = crash_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path
    except Exception:
        return None
