"""Ship and register the VDR Connector extension on Windows.

The floating ⬇ VDR latch is the Chrome/Edge/Brave content script, not a Tk
control. macOS already copies the unpacked extension to
~/Library/Application Support/VDR; Windows Setup used to ship none of that,
so a v2.2.3 user never saw the button.

On Windows this module:

  1. Copies the unpacked Chromium build to %LOCALAPPDATA%\\VDR\\extension-chrome
     (same tree crash.log lives in — a stable path Chrome can keep loaded).
  2. Writes each Chromium browser's HKCU External Extensions key (the
     documented Windows registry mechanism).
  3. Loads the unpacked folder into the user's default Chrome/Edge/Brave
     profile through the DevTools Protocol (Extensions.loadUnpacked — the
     same installer as chrome://extensions → Load unpacked). Consumer Chrome
     refuses local CRX force-install unless the machine is AD/AAD enrolled,
     so CDP is what actually enables the latch on a home PC.
  4. Writes a Firefox HKCU policy + XPI for ESR/Developer, where policy
     install is allowed without the Chrome Web Store.

Inno Setup runs ``VDR.exe --install-browser-extension``. The GUI also calls
``ensure_installed()`` on Windows so an upgrade refreshes the files.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable, Optional


EXTENSION_ID = "ojdhhngiphpdebmkolgghnliikgegbdi"
EXTENSION_VERSION = "1.0"
GECKO_ID = "vdr-connector@vdr-macos.app"
MARKER_NAME = ".vdr-registered"
CDP_PORT = 19222

# HKCU External Extensions parents. Official Chrome only honors update_url
# pointing at the Web Store; Chromium forks still read `path`. Writing both
# path + version is what Setup can do without elevation.
_CHROMIUM_EXTENSION_KEYS = (
    r"Software\Google\Chrome\Extensions",
    r"Software\Google\Chrome Beta\Extensions",
    r"Software\Google\Chrome SxS\Extensions",
    r"Software\Chromium\Extensions",
    r"Software\Microsoft\Edge\Extensions",
    r"Software\Microsoft\Edge Beta\Extensions",
    r"Software\BraveSoftware\Brave\Extensions",
    r"Software\Opera Software\Opera Stable\Extensions",
    r"Software\Vivaldi\Extensions",
)

_CHROMIUM_EXES = (
    (
        "chrome.exe",
        (
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        ),
    ),
    (
        "msedge.exe",
        (
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
            r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
        ),
    ),
    (
        "brave.exe",
        (
            r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
        ),
    ),
)


def install_root(environ=None, platform: Optional[str] = None) -> Path:
    """Stable per-user directory for the unpacked extension (and crash.log)."""
    env = os.environ if environ is None else environ
    plat = sys.platform if platform is None else platform
    if plat == "win32":
        base = env.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "VDR"
    if plat == "darwin":
        return Path.home() / "Library" / "Application Support" / "VDR"
    return Path.home() / ".local" / "share" / "VDR"


def chrome_extension_dir(root: Optional[Path] = None) -> Path:
    return (root or install_root()) / "extension-chrome"


def firefox_extension_dir(root: Optional[Path] = None) -> Path:
    return (root or install_root()) / "extension-firefox"


def bundled_extension_src() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidate = Path(meipass) / "browser_extension"
            if candidate.is_dir():
                return candidate
        next_to_exe = Path(sys.executable).resolve().parent / "browser_extension"
        if next_to_exe.is_dir():
            return next_to_exe
        internal = Path(sys.executable).resolve().parent / "_internal" / "browser_extension"
        if internal.is_dir():
            return internal
    return Path(__file__).resolve().parent / "browser_extension"


def _copy_tree(src: Path, dest: Path) -> None:
    """Refresh dest from src without dropping the CDP registration marker."""
    dest.mkdir(parents=True, exist_ok=True)
    skip = {"__pycache__", ".DS_Store", MARKER_NAME}
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if any(part in skip or str(part).endswith(".pem") for part in rel.parts):
            continue
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def firefox_manifest_text(src_manifest: Path) -> str:
    manifest = json.loads(src_manifest.read_text(encoding="utf-8"))
    manifest["background"] = {"scripts": ["background.js"]}
    manifest["browser_specific_settings"] = {
        "gecko": {"id": GECKO_ID, "strict_min_version": "109.0"}
    }
    manifest.pop("key", None)
    return json.dumps(manifest, indent=2) + "\n"


def stage_unpacked(src: Optional[Path] = None, root: Optional[Path] = None) -> dict:
    """Copy Chromium + Firefox flavours into the per-user data dir."""
    src = src or bundled_extension_src()
    root = root or install_root()
    chrome_dest = chrome_extension_dir(root)
    firefox_dest = firefox_extension_dir(root)
    _copy_tree(src, chrome_dest)
    _copy_tree(src, firefox_dest)
    (firefox_dest / "manifest.json").write_text(
        firefox_manifest_text(src / "manifest.json"), encoding="utf-8"
    )
    xpi = root / "extension-firefox.xpi"
    _zip_dir(firefox_dest, xpi)
    return {"chrome": chrome_dest, "firefox": firefox_dest, "xpi": xpi}


def _zip_dir(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(src).as_posix())


def file_url(path: Path) -> str:
    return path.resolve().as_uri()


def registry_extension_payload(unpacked_dir: Path) -> dict:
    """Values written under Software\\<browser>\\Extensions\\<id>."""
    return {
        "path": str(unpacked_dir.resolve()),
        "version": EXTENSION_VERSION,
    }


def firefox_extension_settings(xpi: Path) -> dict:
    return {
        GECKO_ID: {
            "installation_mode": "normal_installed",
            "install_url": file_url(xpi),
        }
    }


def _process_running(image: str) -> bool:
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    out = (completed.stdout or "").lower()
    return image.lower() in out and "no tasks" not in out


def _graceful_close(image: str, timeout_s: float = 20.0) -> bool:
    """Ask the process to quit. Never /F — that would drop the user's tabs."""
    if not _process_running(image):
        return True
    try:
        subprocess.run(
            ["taskkill", "/IM", image],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return not _process_running(image)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _process_running(image):
            return True
        time.sleep(0.4)
    return not _process_running(image)


def _first_existing(candidates: Iterable[str]) -> Optional[Path]:
    for raw in candidates:
        path = Path(os.path.expandvars(raw))
        if path.is_file():
            return path
    return None


def register_chromium_external(unpacked_dir: Path, winreg_mod=None) -> list:
    """Write HKCU External Extensions keys. Returns key paths written."""
    winreg = winreg_mod or __import__("winreg")
    payload = registry_extension_payload(unpacked_dir)
    written = []
    for parent in _CHROMIUM_EXTENSION_KEYS:
        key_path = f"{parent}\\{EXTENSION_ID}"
        try:
            handle = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
            try:
                winreg.SetValueEx(handle, "path", 0, winreg.REG_SZ, payload["path"])
                winreg.SetValueEx(handle, "version", 0, winreg.REG_SZ, payload["version"])
            finally:
                winreg.CloseKey(handle)
            written.append(key_path)
        except OSError:
            continue
    return written


def unregister_chromium_external(winreg_mod=None) -> None:
    winreg = winreg_mod or __import__("winreg")
    for parent in _CHROMIUM_EXTENSION_KEYS:
        key_path = f"{parent}\\{EXTENSION_ID}"
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        except OSError:
            continue


def register_firefox_policy(xpi: Path, winreg_mod=None) -> bool:
    winreg = winreg_mod or __import__("winreg")
    extra = firefox_extension_settings(xpi)
    try:
        handle = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, r"Software\Policies\Mozilla\Firefox"
        )
        try:
            data = {}
            try:
                raw, _ = winreg.QueryValueEx(handle, "ExtensionSettings")
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    data = parsed
            except OSError:
                pass
            except json.JSONDecodeError:
                data = {}
            data.update(extra)
            winreg.SetValueEx(
                handle, "ExtensionSettings", 0, winreg.REG_SZ, json.dumps(data)
            )
        finally:
            winreg.CloseKey(handle)
        return True
    except OSError:
        return False


def unregister_firefox_policy(winreg_mod=None) -> None:
    winreg = winreg_mod or __import__("winreg")
    try:
        handle = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Policies\Mozilla\Firefox",
            0,
            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        )
    except OSError:
        return
    try:
        try:
            raw, _ = winreg.QueryValueEx(handle, "ExtensionSettings")
        except OSError:
            return
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return
        if isinstance(data, dict) and GECKO_ID in data:
            data.pop(GECKO_ID, None)
            if data:
                winreg.SetValueEx(
                    handle, "ExtensionSettings", 0, winreg.REG_SZ, json.dumps(data)
                )
            else:
                winreg.DeleteValue(handle, "ExtensionSettings")
    finally:
        winreg.CloseKey(handle)


def _write_runonce() -> None:
    if sys.platform != "win32":
        return
    try:
        import winreg
    except ImportError:
        return
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}" --install-browser-extension'
    else:
        main_py = Path(__file__).resolve().parent / "main.py"
        cmd = f'"{sys.executable}" "{main_py}" --install-browser-extension'
    try:
        handle = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\RunOnce",
        )
        try:
            winreg.SetValueEx(handle, "VDRBrowserExtension", 0, winreg.REG_SZ, cmd)
        finally:
            winreg.CloseKey(handle)
    except OSError:
        pass


def _clear_runonce() -> None:
    try:
        import winreg
    except ImportError:
        return
    try:
        handle = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\RunOnce",
            0,
            winreg.KEY_SET_VALUE,
        )
    except OSError:
        return
    try:
        try:
            winreg.DeleteValue(handle, "VDRBrowserExtension")
        except OSError:
            pass
    finally:
        winreg.CloseKey(handle)


# --- Chrome DevTools Protocol (load unpacked into the default profile) ------


def _ws_handshake(host: str, port: int, resource: str) -> socket.socket:
    import base64
    import hashlib

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = (
        f"GET {resource} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")
    sock = socket.create_connection((host, port), timeout=8)
    sock.sendall(req)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            raise OSError("CDP websocket handshake closed")
        buf += chunk
    header, _, _rest = buf.partition(b"\r\n\r\n")
    if b"101" not in header.split(b"\r\n", 1)[0]:
        sock.close()
        raise OSError(f"CDP websocket rejected: {header[:120]!r}")
    expected = base64.b64encode(
        hashlib.sha1(key.encode("ascii") + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
    )
    if expected not in header:
        # Some Chrome builds omit echoing in a way we still accept 101.
        pass
    sock.settimeout(12)
    return sock


def _ws_send(sock: socket.socket, payload: bytes) -> None:
    mask = os.urandom(4)
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack(">H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack(">Q", n))
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + masked)


def _ws_recv(sock: socket.socket) -> bytes:
    def read_exact(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("CDP websocket closed")
            buf += chunk
        return buf

    hdr = read_exact(2)
    opcode = hdr[0] & 0x0F
    length = hdr[1] & 0x7F
    masked = bool(hdr[1] & 0x80)
    if length == 126:
        length = struct.unpack(">H", read_exact(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", read_exact(8))[0]
    mask = read_exact(4) if masked else b""
    data = read_exact(length)
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    if opcode == 0x8:  # close
        return b""
    if opcode == 0x9:  # ping
        # pong
        sock.sendall(bytes([0x8A, 0x80, *os.urandom(4)]))
        return _ws_recv(sock)
    return data


def _cdp_load_unpacked(ws_url: str, unpacked_dir: Path) -> None:
    from urllib.parse import urlparse

    parsed = urlparse(ws_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or CDP_PORT
    resource = parsed.path or "/"
    if parsed.query:
        resource += "?" + parsed.query
    sock = _ws_handshake(host, port, resource)
    try:
        req = json.dumps(
            {
                "id": 1,
                "method": "Extensions.loadUnpacked",
                "params": {"path": str(unpacked_dir.resolve())},
            }
        ).encode("utf-8")
        _ws_send(sock, req)
        deadline = time.time() + 12
        while time.time() < deadline:
            raw = _ws_recv(sock)
            if not raw:
                break
            try:
                msg = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if msg.get("id") != 1:
                continue
            if "error" in msg:
                raise OSError(msg["error"].get("message") or str(msg["error"]))
            break
        try:
            _ws_send(sock, json.dumps({"id": 2, "method": "Browser.close"}).encode("utf-8"))
            time.sleep(0.3)
        except OSError:
            pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _wait_cdp(port: int, timeout_s: float = 12.0) -> str:
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            ws = data.get("webSocketDebuggerUrl")
            if ws:
                return ws
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
        time.sleep(0.25)
    raise OSError(f"Chrome DevTools never came up on port {port}: {last}")


def load_unpacked_via_cdp(
    unpacked_dir: Path,
    *,
    close_browser_if_needed: bool = False,
    launch_if_needed: bool = True,
) -> str:
    """Load the unpacked extension into the first available Chromium browser.

    Returns the exe name that accepted it. Raises OSError on failure.
    """
    last_err: Optional[Exception] = None
    installed = []
    for image, candidates in _CHROMIUM_EXES:
        exe = _first_existing(candidates)
        if exe is not None:
            installed.append((image, exe))
    # Prefer Google Chrome when it is installed. Falling through to Edge while
    # Chrome is running would sideload into a browser the user is not using.
    chrome_first = [pair for pair in installed if pair[0] == "chrome.exe"]
    targets = chrome_first or installed
    for image, exe in targets:
        running = _process_running(image)
        if running and close_browser_if_needed:
            _graceful_close(image)
            running = _process_running(image)
        if running:
            last_err = OSError(f"{image} is running; cannot attach DevTools flags")
            continue
        if not launch_if_needed:
            last_err = OSError(f"{image} is installed but not launched")
            continue
        proc = subprocess.Popen(
            [
                str(exe),
                f"--remote-debugging-port={CDP_PORT}",
                "--remote-debugging-address=127.0.0.1",
                "--enable-unsafe-extension-debugging",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            ws = _wait_cdp(CDP_PORT)
            _cdp_load_unpacked(ws, unpacked_dir)
            return image
        except Exception as exc:
            last_err = exc
            try:
                proc.terminate()
            except OSError:
                pass
        finally:
            try:
                if proc.poll() is None:
                    proc.wait(timeout=4)
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass
    raise last_err or OSError("no Chromium browser found to load the extension")


def _marker_path(unpacked_dir: Path) -> Path:
    return unpacked_dir / MARKER_NAME


def mark_registered(unpacked_dir: Path, how: str) -> None:
    _marker_path(unpacked_dir).write_text(how + "\n", encoding="utf-8")


def is_marked_registered(unpacked_dir: Optional[Path] = None) -> bool:
    return _marker_path(unpacked_dir or chrome_extension_dir()).is_file()


def ensure_installed(
    *,
    try_cdp: bool = True,
    close_browser_if_needed: bool = False,
    launch_if_needed: bool = True,
    winreg_mod=None,
) -> dict:
    """Copy files, write registry, and (on Windows) load into Chromium.

    Returns a result dict: chrome dir, keys written, cdp browser, and
    needs_browser_restart when the latch is staged but Chrome was busy.
    """
    staged = stage_unpacked()
    chrome_dir = staged["chrome"]
    result = {
        "chrome_dir": str(chrome_dir),
        "firefox_xpi": str(staged["xpi"]),
        "registry_keys": [],
        "cdp": None,
        "needs_browser_restart": False,
        "ok": True,
    }
    if sys.platform != "win32" and winreg_mod is None:
        return result

    result["registry_keys"] = register_chromium_external(chrome_dir, winreg_mod=winreg_mod)
    register_firefox_policy(staged["xpi"], winreg_mod=winreg_mod)

    if not try_cdp or sys.platform != "win32":
        return result

    try:
        image = load_unpacked_via_cdp(
            chrome_dir,
            close_browser_if_needed=close_browser_if_needed,
            launch_if_needed=launch_if_needed,
        )
        mark_registered(chrome_dir, f"cdp:{image}")
        result["cdp"] = image
        _clear_runonce()
    except Exception:
        result["needs_browser_restart"] = not is_marked_registered(chrome_dir)
        if result["needs_browser_restart"]:
            _write_runonce()
    return result


def uninstall_windows_extension(winreg_mod=None) -> bool:
    _clear_runonce()
    if sys.platform == "win32" or winreg_mod is not None:
        unregister_chromium_external(winreg_mod=winreg_mod)
        unregister_firefox_policy(winreg_mod=winreg_mod)
    root = install_root()
    for path in (
        chrome_extension_dir(root),
        firefox_extension_dir(root),
        root / "extension-firefox.xpi",
    ):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.is_file():
                path.unlink()
        except OSError:
            pass
    return True


def install_from_cli(*, uninstall: bool = False) -> bool:
    if uninstall:
        return uninstall_windows_extension()
    result = ensure_installed(
        try_cdp=True,
        close_browser_if_needed=True,
        launch_if_needed=True,
    )
    return bool(result.get("ok"))
