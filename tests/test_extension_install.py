"""Windows extension install path, registry payload, and cookie-browser order.

These run on any OS. They pin the two things that left the ⬇ VDR latch off
a Windows 2.2.3 machine: build_extension.py only knew the macOS Application
Support path, and Setup never shipped or registered the unpacked extension.
"""
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extension_install
import video_capture

failures = []


def check(label, condition):
    print(f"{label:<64} -> {'ok' if condition else 'FAIL'}")
    if not condition:
        failures.append(label)


def zipfile_has_manifest(path: Path) -> bool:
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        return "manifest.json" in names and "content.js" in names


class FakeWinreg:
    HKEY_CURRENT_USER = object()
    REG_SZ = 1
    KEY_SET_VALUE = 2
    KEY_QUERY_VALUE = 1

    def __init__(self):
        self.store = {}

    def CreateKey(self, _root, path):
        self.store.setdefault(path, {})
        return path

    def OpenKey(self, _root, path, _reserved=0, _access=0):
        if path not in self.store:
            raise OSError("missing")
        return path

    def SetValueEx(self, handle, name, _reserved, _typ, value):
        self.store.setdefault(handle, {})[name] = value

    def QueryValueEx(self, handle, name):
        try:
            return self.store[handle][name], 1
        except KeyError:
            raise OSError("missing")

    def DeleteKey(self, _root, path):
        self.store.pop(path, None)

    def DeleteValue(self, handle, name):
        self.store.get(handle, {}).pop(name, None)

    def CloseKey(self, _handle):
        pass


# --- install_root dispatch ------------------------------------------------
root_win = extension_install.install_root(
    environ={"LOCALAPPDATA": r"C:\Users\test\AppData\Local"},
    platform="win32",
)
check(
    "Windows install root is LOCALAPPDATA\\VDR",
    root_win == Path(r"C:\Users\test\AppData\Local") / "VDR",
)
check(
    "Windows chrome dir sits next to crash.log",
    extension_install.chrome_extension_dir(root_win)
    == Path(r"C:\Users\test\AppData\Local") / "VDR" / "extension-chrome",
)
check(
    "macOS install root is Application Support/VDR",
    extension_install.install_root(platform="darwin")
    == Path.home() / "Library" / "Application Support" / "VDR",
)
check(
    "stable Chromium extension id is 32 a-p chars",
    len(extension_install.EXTENSION_ID) == 32
    and extension_install.EXTENSION_ID.isalpha()
    and extension_install.EXTENSION_ID.islower(),
)


# --- stage unpacked from the repo source ---------------------------------
src = Path(__file__).resolve().parent.parent / "browser_extension"
with tempfile.TemporaryDirectory() as tmp:
    staged = extension_install.stage_unpacked(src=src, root=Path(tmp))
    check("stages Chrome flavour with manifest", (staged["chrome"] / "manifest.json").is_file())
    check("stages content.js latch script", (staged["chrome"] / "content.js").is_file())
    chrome_manifest = json.loads((staged["chrome"] / "manifest.json").read_text())
    check("Chrome manifest keeps public key for stable id", "key" in chrome_manifest)
    import base64
    import hashlib
    der = base64.b64decode(chrome_manifest["key"])
    digest = hashlib.sha256(der).digest()[:16]
    eid = "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0xF)) for b in digest)
    check("manifest key hashes to EXTENSION_ID", eid == extension_install.EXTENSION_ID)
    ff_manifest = json.loads((staged["firefox"] / "manifest.json").read_text())
    check(
        "Firefox flavour uses background.scripts",
        ff_manifest.get("background", {}).get("scripts") == ["background.js"],
    )
    check(
        "Firefox flavour has gecko id",
        ff_manifest.get("browser_specific_settings", {}).get("gecko", {}).get("id")
        == extension_install.GECKO_ID,
    )
    check("Firefox flavour drops Chromium key", "key" not in ff_manifest)
    check("XPI contains manifest.json and content.js", zipfile_has_manifest(staged["xpi"]))

    payload = extension_install.registry_extension_payload(staged["chrome"])
    check("registry payload has unpacked path", os.path.isdir(payload["path"]))
    check("registry payload version matches manifest", payload["version"] == "1.0")
    settings = extension_install.firefox_extension_settings(staged["xpi"])
    gecko = settings[extension_install.GECKO_ID]
    check("Firefox policy uses normal_installed", gecko["installation_mode"] == "normal_installed")
    check("Firefox policy install_url is a file URI", gecko["install_url"].startswith("file:"))

    fake = FakeWinreg()
    written = extension_install.register_chromium_external(staged["chrome"], winreg_mod=fake)
    check("writes Chrome External Extensions key", any("Google\\Chrome\\Extensions" in k for k in written))
    check("writes Edge External Extensions key", any("Microsoft\\Edge\\Extensions" in k for k in written))
    chrome_key = f"Software\\Google\\Chrome\\Extensions\\{extension_install.EXTENSION_ID}"
    check(
        "Chrome registry path points at staged dir",
        fake.store[chrome_key]["path"] == str(staged["chrome"].resolve()),
    )
    check(
        "registers Firefox policy",
        extension_install.register_firefox_policy(staged["xpi"], winreg_mod=fake) is True,
    )
    ff = json.loads(fake.store[r"Software\Policies\Mozilla\Firefox"]["ExtensionSettings"])
    check("Firefox policy JSON names the gecko id", extension_install.GECKO_ID in ff)

    extension_install.unregister_chromium_external(winreg_mod=fake)
    check("unregister drops Chrome key", chrome_key not in fake.store)


# --- cookie browser order (YouTube signed-in Chrome on Windows) -------------
check(
    "Windows cookie order skips Safari, prefers Firefox",
    video_capture.cookie_browsers("win32") == ("firefox", "edge", "chrome", "brave"),
)
check(
    "macOS cookie order still tries Safari first",
    video_capture.cookie_browsers("darwin") == ("safari", "chrome", "firefox"),
)
check(
    "Safari cookie miss is a store failure",
    video_capture._looks_like_cookie_store_failure(
        Exception("Failed to load cookies from safari")
    ),
)
check(
    "YouTube bot check still looks like login",
    video_capture._looks_like_login_required(
        Exception("Sign in to confirm you're not a bot")
    ),
)

# --- overlay latches onto the YouTube player, not the inner container ------
content = (src / "content.js").read_text(encoding="utf-8")
check("overlay searches html5-video-player", ".html5-video-player" in content)
check("overlay re-latches on yt-navigate-finish", "yt-navigate-finish" in content)
check("overlay no longer one-shot data-vdr-attached", "data-vdr-attached" not in content)

print()
if failures:
    print(f"FAIL - {len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASS - Windows extension install + latch wiring")
