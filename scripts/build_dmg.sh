#!/bin/bash
# Builds "VDR.app" with PyInstaller and packages it into a
# double-clickable .dmg installer. Used both for local builds and by
# .github/workflows/release.yml on every version tag push.
set -euo pipefail

cd "$(dirname "$0")/.."

# pyinstaller/dmgbuild live in the project's virtualenv, not on a fresh
# shell's PATH -- running this script from an unactivated shell died with
# "pyinstaller: command not found", and the bare `python3` it fell back to
# could not import dmgbuild. Resolve them here so CI and a bare terminal
# behave the same, while still honouring anything already on PATH.
if [ -x ".venv/bin/pyinstaller" ]; then PYINSTALLER=".venv/bin/pyinstaller"; else PYINSTALLER="pyinstaller"; fi
if [ -x ".venv/bin/dmgbuild" ]; then DMGBUILD=".venv/bin/dmgbuild"; else DMGBUILD="dmgbuild"; fi
if [ -x ".venv/bin/python" ]; then PYTHON=".venv/bin/python"; else PYTHON="python3"; fi

# Version is passed by .github/workflows/release.yml (the tag name); local
# builds get a placeholder, mirroring scripts/build_windows.ps1's -Version.
#
# Anything that is not a dotted number is rejected rather than interpolated:
# the workflow hands over github.ref_name, which is the branch name on a
# manual run, and taking that at face value produced an artifact called
# "VDR-main-macOS-Installer.dmg" while the same run's Windows build correctly
# fell back to 0.0.0. Both platforms now agree on what an absent version is.
VERSION="${1:-0.0.0}"
VERSION="${VERSION#v}"
if ! [[ "$VERSION" =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
  echo "==> '$VERSION' is not a version number; using 0.0.0"
  VERSION="0.0.0"
fi

APP_NAME="VDR"
# The filename says which OS it is for, rather than leaving that to the
# extension. Someone scanning a release page should not have to know that
# .dmg means macOS and .exe means Windows, and the two names sitting next to
# each other should be obviously a pair.
#
# No spaces: GitHub rewrites them to dots on upload, so "VDR Installer.dmg"
# arrived as "VDR.Installer.dmg" -- a name nothing in the repo actually used.
DMG_NAME="VDR-${VERSION}-macOS-Installer.dmg"

echo "==> Building '$APP_NAME.app' with PyInstaller"
rm -rf build dist
"$PYINSTALLER" --noconfirm "$APP_NAME.spec"

if [ ! -d "dist/$APP_NAME.app" ]; then
  echo "PyInstaller did not produce dist/$APP_NAME.app" >&2
  exit 1
fi

# Bundle ffmpeg so video downloads that need to merge separate video/audio
# streams work out of the box, without end users needing Homebrew or ffmpeg
# installed themselves. video_capture.py looks for it next to the frozen
# executable via yt-dlp's ffmpeg_location option.
#
# It has to be a *self-contained* binary. This used to copy `command -v
# ffmpeg`, which on any Mac with Homebrew is a 400 KB stub dynamically
# linked against /opt/homebrew/Cellar/ffmpeg/<ver>/lib/*.dylib -- it ran on
# the build machine and on no one else's, so every shipped DMG's merges
# failed exactly for the users the bundling was meant to help. The
# imageio-ffmpeg wheel (requirements.txt) carries a static, GPL ffmpeg for
# both Apple Silicon and Intel; that is the preferred source. Whatever is
# chosen is checked with otool, and a dynamically linked binary fails the
# build rather than shipping.
pick_ffmpeg() {
  local candidate
  candidate="$("$PYTHON" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())' 2>/dev/null || true)"
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    echo "$candidate"
    return
  fi
  if [ -n "${VDR_FFMPEG:-}" ] && [ -x "${VDR_FFMPEG}" ]; then
    echo "${VDR_FFMPEG}"
    return
  fi
  command -v ffmpeg || true
}

is_self_contained() {
  # Anything outside /usr/lib and /System is a library the user's Mac
  # cannot be assumed to have.
  ! otool -L "$1" | tail -n +2 | awk '{print $1}' | grep -Eq '^/(opt/homebrew|usr/local|Users|private)'
}

FFMPEG_BIN="$(pick_ffmpeg)"
if [ -n "$FFMPEG_BIN" ]; then
  if is_self_contained "$FFMPEG_BIN"; then
    echo "==> Bundling ffmpeg from $FFMPEG_BIN"
    cp "$FFMPEG_BIN" "dist/$APP_NAME.app/Contents/MacOS/ffmpeg"
    chmod 755 "dist/$APP_NAME.app/Contents/MacOS/ffmpeg"
  else
    echo "ERROR: $FFMPEG_BIN is dynamically linked against libraries only this" >&2
    echo "       machine has (see 'otool -L'). Shipping it would break video merging" >&2
    echo "       for every user without the same Homebrew install." >&2
    echo "       Fix: pip install imageio-ffmpeg   (static build, picked up automatically)" >&2
    echo "       or point VDR_FFMPEG at a static ffmpeg binary." >&2
    exit 1
  fi
else
  echo "==> WARNING: no ffmpeg found (pip install imageio-ffmpeg)." >&2
  echo "    Building without it -- video merging will fail for anyone" >&2
  echo "    who installs this app unless they separately install ffmpeg." >&2
fi

echo "==> Building per-browser extension packages"
"$PYTHON" scripts/build_extension.py

# GPL compliance: the bundled ffmpeg is a GPLv3 build, so the license text and
# the third-party notices (which carry the written offer for ffmpeg's source)
# have to travel with the binary, not just live in the repo.
echo "==> Bundling license + third-party notices"
cp LICENSE "dist/$APP_NAME.app/Contents/Resources/LICENSE"
cp THIRD-PARTY-NOTICES.md "dist/$APP_NAME.app/Contents/Resources/THIRD-PARTY-NOTICES.md"

echo "==> Staging the browser extension folder for the disk image"
# The app refreshes the *stable* copy in Application Support on launch (see
# extension_install.stage_unpacked), and that is the copy Chrome must load:
# Chromium records an unpacked extension's path on disk, so one loaded from a
# mounted disk image stops working the moment the image is ejected. This
# folder is what the Setup Guide points people at, plus a ready-to-inspect
# copy for anyone doing the load by hand.
EXTRA_DIR="dist/dmg-extra/Browser Extension"
rm -rf dist/dmg-extra
mkdir -p "$EXTRA_DIR"
cp -R "$HOME/Library/Application Support/VDR/extension-chrome" "$EXTRA_DIR/extension-chrome"
cp -R "$HOME/Library/Application Support/VDR/extension-firefox" "$EXTRA_DIR/extension-firefox"
cat > "$EXTRA_DIR/Setup Guide.txt" <<'GUIDE'
VDR - browser extension setup (one time)

The floating "VDR" button on video players comes from the "VDR Connector"
browser extension. Chrome, Edge, Brave, Opera and Vivaldi have to load it by
hand once, because it is not in the Chrome Web Store.

Do not load the extension from this disk image. Chromium remembers the
folder path of an unpacked extension, and a copy inside a disk image stops
working as soon as the image is ejected. Use the stable folder the app
maintains instead:

  1. Drag VDR to Applications and open it once. VDR writes the current
     extension to:
         ~/Library/Application Support/VDR/extension-chrome
  2. In VDR's menu bar icon menu, click "Setup Browser Extension...". It
     refreshes those files, reveals the folder in Finder and opens
     chrome://extensions.
  3. In the extensions page: turn on Developer mode (top right), click
     "Load unpacked" and choose the revealed extension-chrome folder.

Firefox: open about:debugging#/runtime/this-firefox, click "Load Temporary
Add-on..." and choose extension-firefox/manifest.json from the same
Application Support folder. Firefox drops temporary add-ons on restart.

Safari: needs a one-time conversion with Xcode's
safari-web-extension-converter; see the "Safari" section of README.md.

Keep VDR running (it can sit in the background): the extension talks to it
on 127.0.0.1:27182 and the button does nothing while VDR is closed.
GUIDE

echo "==> Creating $DMG_NAME"
# Plain `hdiutil create -srcfolder` sets no window size or icon layout at
# all, so Finder falls back to defaults that often stack the app and the
# Applications shortcut on top of each other -- it looks like nothing is
# draggable even though it technically is. dmgbuild constructs a proper
# .DS_Store with both icons laid out side by side (see dmg_settings.py),
# without needing Finder automation permissions to build it.
rm -f "$DMG_NAME" VDR-*-macOS-Installer.dmg "VDR Installer.dmg"
DMG_APP_NAME="$APP_NAME" DMG_APP_PATH="dist/$APP_NAME.app" \
  DMG_EXTRA_DIR="$EXTRA_DIR" \
  "$DMGBUILD" -s scripts/dmg_settings.py "$APP_NAME" "$DMG_NAME"

echo "==> Done: $DMG_NAME"
