"""Platform-dispatch tests for the Windows port.

These run on any OS. The point is not to emulate Windows -- it is to pin the
two things that actually broke when the app was macOS-only:

  1. gui.py calls a fixed set of methods on whatever integration it is given.
     If a backend is missing one, the app dies at the moment a download
     finishes rather than at import, which is the worst time to find out.
  2. The system probes used to be bare macOS shell-outs. On Windows the old
     code did not merely return a wrong answer, it raised (os.uname is
     Unix-only), so the fallbacks must be exercised, not assumed.
"""
import os
import sys
import platform
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import desktop_integration
import focus_guard
from desktop_integration import NullIntegration, create_integration

# The exact surface gui.py touches (see self.mac.* call sites there).
REQUIRED = [
    "install_menu_bar",
    "set_dock_badge",
    "set_progress",
    "notify_completion",
    "play_completion_sound",
]

failures = []


def check(label, condition):
    print(f"{label:<52} -> {'ok' if condition else 'FAIL'}")
    if not condition:
        failures.append(label)


# --- 1. every backend satisfies the interface -------------------------------
backends = {"NullIntegration": NullIntegration}
try:
    from windows_integration import WindowsIntegration
    backends["WindowsIntegration"] = WindowsIntegration
except ImportError:
    pass
if platform.system() == "Darwin":
    from macos_integration import MacIntegration
    backends["MacIntegration"] = MacIntegration

for name, cls in backends.items():
    inst = cls(lambda u: None, lambda: None, lambda: None)
    missing = [m for m in REQUIRED if not callable(getattr(inst, m, None))]
    check(f"{name} implements gui.py's surface", not missing)
    check(f"{name} exposes .available", isinstance(getattr(inst, "available", None), bool))

# --- 2. a non-native backend must be safe to actually call ------------------
# gui.py calls these unconditionally on every completed download.
null = NullIntegration()
try:
    null.install_menu_bar()
    null.set_dock_badge("3")
    null.set_progress("42%")
    null.notify_completion("Download complete", "clip.mp4")
    null.play_completion_sound()
    check("NullIntegration methods are callable no-ops", True)
except Exception as e:
    check(f"NullIntegration methods are callable no-ops ({e})", False)

# WindowsIntegration off-Windows must degrade, not explode.
if "WindowsIntegration" in backends:
    w = backends["WindowsIntegration"](lambda u: None, lambda: None, lambda: None)
    if platform.system() != "Windows":
        check("WindowsIntegration inert off-Windows", w.available is False)
    try:
        w.install_menu_bar()
        w.set_dock_badge("1")
        w.set_progress("7%")
        w.play_completion_sound()
        check("WindowsIntegration safe when unavailable", True)
    except Exception as e:
        check(f"WindowsIntegration safe when unavailable ({e})", False)

# --- 2b. installing the tray icon must never block the caller ---------------
# gui.py calls install_menu_bar() from root.after(), i.e. on Tk's main thread.
# pystray's run_detached() does setup on the calling thread, and where there is
# no interactive desktop (headless CI, session-0 service) that setup blocks
# rather than failing -- which froze the whole UI before the window finished
# appearing. The pump now runs on a daemon thread we own, so this returns
# immediately everywhere. Timing it turns a regression into a fast failure
# instead of a hung job.
for name, cls in backends.items():
    inst = cls(lambda u: None, lambda: None, lambda: None)
    start = time.monotonic()
    try:
        inst.install_menu_bar()
        elapsed = time.monotonic() - start
        check(f"{name}.install_menu_bar returns promptly ({elapsed:.2f}s)", elapsed < 5.0)
    except Exception as e:
        check(f"{name}.install_menu_bar returns promptly ({e})", False)

# --- 3. factory picks the backend matching the host -------------------------
expected = {"Darwin": "MacIntegration", "Windows": "WindowsIntegration"}.get(
    platform.system(), "NullIntegration"
)
check(f"create_integration picks {expected}", type(create_integration()).__name__ == expected)

# --- 4. system probes return sane values and never raise --------------------
idle = focus_guard.read_idle_seconds()
check("read_idle_seconds returns a non-negative float", isinstance(idle, float) and idle >= 0)

on_batt, low = focus_guard.read_power()
check("read_power returns two bools", isinstance(on_batt, bool) and isinstance(low, bool))

# Unknown platforms must report "idle, on mains" so Focus Guard never
# throttles a machine whose signals it cannot read.
real_system = platform.system
try:
    platform.system = lambda: "Haiku"
    check("unknown OS reports idle", focus_guard.read_idle_seconds() == 9999.0)
    check("unknown OS reports mains power", focus_guard.read_power() == (False, False))
    check("unknown OS gets NullIntegration",
          type(desktop_integration.create_integration()).__name__ == "NullIntegration")
finally:
    platform.system = real_system

# --- 5. policy decisions are platform-independent ---------------------------
check("battery -> hold", focus_guard.decide_policy(True, True, False, 999) == focus_guard.POLICY_HOLD)
check("low power -> hold", focus_guard.decide_policy(True, False, True, 999) == focus_guard.POLICY_HOLD)
check("active user -> crawl", focus_guard.decide_policy(True, False, False, 1) == focus_guard.POLICY_CRAWL)
check("idle + mains -> full", focus_guard.decide_policy(True, False, False, 999) == focus_guard.POLICY_FULL)
check("disabled -> off", focus_guard.decide_policy(False, True, True, 0) == focus_guard.POLICY_OFF)

# --- 6. Dock-reopen Tcl command is macOS-only -------------------------------
# gui.py used to call root.createcommand("::tk::mac::ReopenApplication") on
# every platform. Windows Tk has no ::tk::mac namespace, so that raised and
# App.__init__ died: Setup.exe finished, then VDR.exe never showed a window.


class _BoomRoot:
    def createcommand(self, *args, **kwargs):
        raise AssertionError("createcommand must not run off Darwin")


check(
    "bind_macos_reopen skips Windows",
    desktop_integration.bind_macos_reopen(_BoomRoot(), lambda: None, system="Windows") is False,
)
check(
    "bind_macos_reopen skips Linux",
    desktop_integration.bind_macos_reopen(_BoomRoot(), lambda: None, system="Linux") is False,
)


class _OkRoot:
    def __init__(self):
        self.name = None

    def createcommand(self, name, callback):
        self.name = name


ok_root = _OkRoot()
check(
    "bind_macos_reopen registers on Darwin",
    desktop_integration.bind_macos_reopen(ok_root, lambda: None, system="Darwin") is True
    and ok_root.name == "::tk::mac::ReopenApplication",
)

import crash_report

# Host-independent: pin both branches of crash_log_path() by mocking
# sys.platform. The .vdr assertion used to run against the real host path
# first, so Windows CI failed even though LOCALAPPDATA dispatch was correct.
real_platform = sys.platform
saved_localappdata = os.environ.get("LOCALAPPDATA")
try:
    sys.platform = "darwin"
    posix_path = crash_report.crash_log_path()
    check(
        "crash log is under .vdr off-Windows",
        posix_path.endswith(os.path.join(".vdr", "crash.log")),
    )
    sys.platform = "win32"
    os.environ["LOCALAPPDATA"] = r"C:\Users\test\AppData\Local"
    win_path = crash_report.crash_log_path()
    check(
        "crash log is under LOCALAPPDATA on Windows",
        win_path == os.path.join(r"C:\Users\test\AppData\Local", "VDR", "crash.log"),
    )
finally:
    sys.platform = real_platform
    if saved_localappdata is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = saved_localappdata


# --- 7. the frozen build can find its own ffmpeg ----------------------------
# The regression this pins: VDR-windows.spec adds ffmpeg with dest ".", and
# PyInstaller 6 resolves that into the onedir *contents* directory, so the
# shipped layout is VDR\_internal\ffmpeg.exe. _bundled_ffmpeg_dir() only ever
# looked beside the executable, found nothing, and left yt-dlp with no muxer --
# so every download needing a video+audio merge (most of YouTube above 360p)
# stopped partway and left a .part file behind.
import tempfile

import video_capture

saved_frozen = getattr(sys, "frozen", None)
saved_meipass = getattr(sys, "_MEIPASS", None)
saved_executable = sys.executable
try:
    for layout, subdir in [("beside the exe", ""), ("in _internal", "_internal")]:
        with tempfile.TemporaryDirectory() as tmp:
            binary = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
            target_dir = os.path.join(tmp, subdir) if subdir else tmp
            os.makedirs(target_dir, exist_ok=True)
            open(os.path.join(target_dir, binary), "wb").close()

            sys.frozen = True
            sys.executable = os.path.join(tmp, "VDR.exe")
            # PyInstaller always sets _MEIPASS in a frozen build; point it at
            # the contents directory the way onedir actually does.
            sys._MEIPASS = os.path.join(tmp, "_internal")

            found = video_capture._bundled_ffmpeg_dir()
            check(
                f"frozen build finds ffmpeg {layout}",
                found is not None and os.path.samefile(found, target_dir),
            )

    # Running from source must still defer to PATH rather than inventing a dir.
    if saved_frozen is None:
        del sys.frozen
    else:
        sys.frozen = saved_frozen
    check(
        "unfrozen build defers to PATH",
        video_capture._bundled_ffmpeg_dir() is None,
    )
finally:
    if saved_frozen is None:
        if hasattr(sys, "frozen"):
            del sys.frozen
    else:
        sys.frozen = saved_frozen
    if saved_meipass is None:
        if hasattr(sys, "_MEIPASS"):
            del sys._MEIPASS
    else:
        sys._MEIPASS = saved_meipass
    sys.executable = saved_executable

print()
if failures:
    print(f"FAIL - {len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS - platform dispatch correct on {platform.system()}")
