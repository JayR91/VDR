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

print()
if failures:
    print(f"FAIL - {len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS - platform dispatch correct on {platform.system()}")
