"""Focus Guard — power- and attention-aware downloading.

A download manager that only knows about clock time and speed caps has no
idea whether the machine is on battery, in Low Power Mode / Battery Saver, or
whether you are actively using the keyboard and mouse.

Focus Guard watches those signals and:
  - pauses transfers on battery / Low Power Mode
  - crawls (slow cap) while you are using the machine, so browsing stays snappy
  - runs at full (or your own) speed once the machine has been idle

The signals are read natively per platform (macOS: ioreg/pmset; Windows:
GetLastInputInfo/GetSystemPowerStatus) behind one pair of functions.
"""

from __future__ import annotations

import platform
import re
import subprocess
import threading
import time
from typing import Callable, Optional

IDLE_SECONDS = 20
CRAWL_BYTES_PER_SEC = 256 * 1024  # 256 KB/s while you are at the keyboard

POLICY_OFF = "off"
POLICY_FULL = "full"
POLICY_CRAWL = "active"
POLICY_HOLD = "battery"


def decide_policy(enabled: bool, on_battery: bool, low_power: bool, idle_seconds: float) -> str:
    if not enabled:
        return POLICY_OFF
    if on_battery or low_power:
        return POLICY_HOLD
    if idle_seconds < IDLE_SECONDS:
        return POLICY_CRAWL
    return POLICY_FULL


def _read_idle_seconds_darwin() -> float:
    try:
        out = subprocess.check_output(
            ["ioreg", "-c", "IOHIDSystem", "-d", "4"],
            text=True, timeout=2,
        )
        match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        if match:
            return int(match.group(1)) / 1_000_000_000
    except Exception:
        pass
    return 9999.0


def _read_idle_seconds_windows() -> float:
    """Seconds since the last keyboard/mouse input, via GetLastInputInfo.

    There is no `ioreg` to shell out to on Windows, and no CLI that reports
    this at all -- it is a Win32 call or nothing. GetLastInputInfo returns the
    tick count of the last input event, which we subtract from the current
    tick count.

    Both values come back as 32-bit unsigned milliseconds and wrap roughly
    every 49.7 days. Masking the difference back into 32 bits makes the wrap
    harmless instead of producing a huge negative idle time (which would read
    as "idle forever" and let downloads run at full speed while the user is
    typing).
    """
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 9999.0
        now = ctypes.windll.kernel32.GetTickCount()
        return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return 9999.0


def read_idle_seconds() -> float:
    system = platform.system()
    if system == "Darwin":
        return _read_idle_seconds_darwin()
    if system == "Windows":
        return _read_idle_seconds_windows()
    # Unknown platform: report "idle" so Focus Guard never throttles a
    # machine it cannot actually measure.
    return 9999.0


def _read_power_darwin() -> tuple[bool, bool]:
    on_battery = False
    low_power = False
    try:
        batt = subprocess.check_output(["pmset", "-g", "batt"], text=True, timeout=2)
        on_battery = "Now drawing from 'Battery Power'" in batt
    except Exception:
        pass
    try:
        info = subprocess.check_output(["pmset", "-g"], text=True, timeout=2)
        match = re.search(r"lowpowermode\s+(\d+)", info)
        if match:
            low_power = match.group(1) != "0"
    except Exception:
        pass
    return on_battery, low_power


def _read_power_windows() -> tuple[bool, bool]:
    """Return (on_battery, low_power_mode) from GetSystemPowerStatus.

    ACLineStatus is 0 on battery, 1 on mains, 255 when the system cannot
    tell -- desktops without a battery usually report 255, and treating that
    as "on battery" would pause every download forever on a machine that is
    permanently plugged in. Only an explicit 0 counts.

    SystemStatusFlag is Windows' Battery Saver, the closest counterpart to
    macOS Low Power Mode.
    """
    try:
        import ctypes

        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_byte),
                ("BatteryFlag", ctypes.c_byte),
                ("BatteryLifePercent", ctypes.c_byte),
                ("SystemStatusFlag", ctypes.c_byte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong),
            ]

        status = SYSTEM_POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return False, False
        on_battery = status.ACLineStatus == 0
        low_power = status.SystemStatusFlag == 1
        return on_battery, low_power
    except Exception:
        return False, False


def read_power() -> tuple[bool, bool]:
    """Return (on_battery, low_power_mode)."""
    system = platform.system()
    if system == "Darwin":
        return _read_power_darwin()
    if system == "Windows":
        return _read_power_windows()
    return False, False


class FocusGuard:
    def __init__(self, apply_policy: Callable[[str], None],
                 on_change: Optional[Callable[[str, str], None]] = None):
        self._apply_policy = apply_policy
        self._on_change = on_change
        self.enabled = False
        self.policy = POLICY_OFF
        self.detail = "Off"
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def set_enabled(self, enabled: bool):
        self.enabled = bool(enabled)
        self._tick(force=True)

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(3):
            self._tick()

    def _tick(self, force: bool = False):
        on_battery, low_power = read_power()
        idle = read_idle_seconds()
        policy = decide_policy(self.enabled, on_battery, low_power, idle)
        if policy == POLICY_OFF:
            detail = "Off — downloads run at your speed limit"
        elif policy == POLICY_HOLD:
            reason = "Low Power Mode" if low_power and not on_battery else "battery"
            detail = f"Paused — Mac is on {reason}"
        elif policy == POLICY_CRAWL:
            detail = "Crawling at 256 KB/s while you use the Mac"
        else:
            detail = "Full speed — Mac is idle and plugged in"

        changed = policy != self.policy
        self.policy = policy
        self.detail = detail
        if changed or force:
            self._apply_policy(policy)
            if self._on_change:
                self._on_change(policy, detail)
