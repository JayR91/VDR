#!/usr/bin/env python3
"""Produce per-browser builds of the VDR Connector extension.

`browser_extension/` is the shared source and its manifest.json is the
Chromium one, so it can be loaded there unpacked with no warnings.

Chromium and Firefox disagree on two MV3 details, and each complains about
the other's spelling:

  * background: Chromium wants `service_worker`; Firefox does not support
    service workers in MV3 and wants `scripts`. Chromium warns
    "'background.scripts' requires manifest version of 2 or lower".
  * extension id: Firefox needs `browser_specific_settings.gecko.id` for a
    stable id; Chromium warns "Unrecognized manifest key".

Rather than ship one manifest that warns in both, this writes a Firefox
flavour into dist/ with those two keys swapped.

The per-user install path is platform-specific: macOS uses
~/Library/Application Support/VDR, Windows uses %LOCALAPPDATA%\\VDR (the
same directory as crash.log). Windows Setup also copies and registers from
this tree via extension_install.py — running this script on Windows is what
developers from source get; the frozen installer does it for everyone else.
"""
import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import extension_install  # noqa: E402

SRC = ROOT / "browser_extension"
OUT = ROOT / "dist" / "extension-firefox"


IGNORE = shutil.ignore_patterns("__pycache__", ".DS_Store", "*.pem")


def _manifest_for(flavour: str) -> str:
    if flavour == "firefox":
        return extension_install.firefox_manifest_text(SRC / "manifest.json")
    manifest = json.loads((SRC / "manifest.json").read_text())
    if flavour == "safari":
        # Safari registers an MV3 `service_worker` but never actually runs it
        # here -- Develop > Web Extension Background Content stays empty, so
        # runtime.sendMessage from the content script gets no listener, the
        # fetch never happens, and the button reports "app not running".
        # A non-persistent background script does run.
        manifest["background"] = {"scripts": ["background.js"], "persistent": False}
        manifest.pop("key", None)
    return json.dumps(manifest, indent=2) + "\n"


def _sync(dest: pathlib.Path, flavour: str) -> pathlib.Path:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SRC, dest, ignore=IGNORE)
    if flavour != "chrome":
        (dest / "manifest.json").write_text(_manifest_for(flavour))
    return dest


def main() -> None:
    install = extension_install.install_root()
    _sync(OUT, "firefox")
    chrome_install = _sync(install / "extension-chrome", "chrome")
    firefox_install = _sync(install / "extension-firefox", "firefox")
    safari_src = _sync(install / "extension-safari", "safari")

    print("Load these paths in the browser (they survive moving/renaming the repo):")
    print(f"  Chromium : {chrome_install}")
    print(f"  Firefox  : {firefox_install / 'manifest.json'}")
    print(f"  Safari   : {safari_src}  (input for safari-web-extension-converter)")
    print(f"\nBuild-tree copy of the Firefox flavour: {OUT}")
    if sys.platform == "win32":
        print(
            "\nWindows Setup registers that Chromium folder automatically "
            "(VDR.exe --install-browser-extension). Restart Chrome once if "
            "the ⬇ VDR button is not on the player yet."
        )


if __name__ == "__main__":
    main()
