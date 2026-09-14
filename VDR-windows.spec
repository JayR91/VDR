# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows build.

Kept separate from VDR.spec rather than branching inside it: the macOS spec
ends in a BUNDLE() call that only means anything on macOS, and the two
platforms disagree about almost every other knob (argv_emulation, icon
format, console). One file per target is easier to read than one file full
of `if sys.platform` -- and lets scripts/build_dmg.sh keep working untouched.
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ffmpeg is fetched next to the spec by scripts/build_windows.ps1 before this
# runs. Bundling it is what lets a downloaded VDR merge separate video and
# audio streams (most YouTube 1080p+) without the user installing anything.
# When it is absent the build still succeeds and falls back to PATH at
# runtime, so a developer can build without the download step.
_binaries = []
for _name in ("ffmpeg.exe", "ffprobe.exe"):
    if os.path.exists(_name):
        _binaries.append((_name, "."))

# yt-dlp loads extractors and locale data at runtime; a frozen build that
# only follows static imports raises inside YoutubeDL and surfaces as
# PyInstaller's "Unhandled exception in script" with no useful window.
_hidden = ["pystray._win32", "PIL._tkinter_finder", "extension_install"] + collect_submodules("yt_dlp")
_datas = collect_data_files("yt_dlp") + [
    # Unpacked Chromium extension. Setup copies this to %LOCALAPPDATA%\VDR
    # and VDR.exe --install-browser-extension registers it so the ⬇ VDR
    # latch appears on videos without a manual "Load unpacked".
    ("browser_extension", "browser_extension"),
]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=_binaries,
    datas=_datas,
    # pystray picks its backend at runtime via importlib, so PyInstaller's
    # static analysis never sees the Win32 one and silently ships a build
    # whose tray icon can't start.
    hiddenimports=_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VDR',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX mangles some Windows DLLs (notably Python extension modules) badly
    # enough that the app fails to start, and Defender treats UPX-packed
    # executables as more suspicious. Not worth the few MB here.
    upx=False,
    # A GUI app: without this Windows opens a console window behind the UI
    # every launch.
    console=False,
    disable_windowed_traceback=False,
    # argv_emulation is a macOS-only mechanism (it pumps Apple Events).
    # Setting it on Windows does nothing; links arrive via the local server
    # and the browser extension instead.
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='AppIcon.ico',
    # Written by scripts/build_windows.ps1 from the release version. Optional
    # so a plain `pyinstaller VDR-windows.spec` still works from a checkout.
    version='version_info.txt' if os.path.exists('version_info.txt') else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='VDR',
)
