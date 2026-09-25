#!/bin/bash
# Builds "VDR.app" with PyInstaller and packages it into a
# double-clickable .dmg installer. Used both for local builds and by
# .github/workflows/release.yml on every version tag push.
set -euo pipefail

cd "$(dirname "$0")/.."

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
pyinstaller --noconfirm "$APP_NAME.spec"

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
  candidate="$(python3 -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())' 2>/dev/null || true)"
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
python3 scripts/build_extension.py

# GPL compliance: the bundled ffmpeg is a GPLv3 build, so the license text and
# the third-party notices (which carry the written offer for ffmpeg's source)
# have to travel with the binary, not just live in the repo.
echo "==> Bundling license + third-party notices"
cp LICENSE "dist/$APP_NAME.app/Contents/Resources/LICENSE"
cp THIRD-PARTY-NOTICES.md "dist/$APP_NAME.app/Contents/Resources/THIRD-PARTY-NOTICES.md"

echo "==> Creating $DMG_NAME"
# Plain `hdiutil create -srcfolder` sets no window size or icon layout at
# all, so Finder falls back to defaults that often stack the app and the
# Applications shortcut on top of each other -- it looks like nothing is
# draggable even though it technically is. dmgbuild constructs a proper
# .DS_Store with both icons laid out side by side (see dmg_settings.py),
# without needing Finder automation permissions to build it.
rm -f "$DMG_NAME" VDR-*-macOS-Installer.dmg "VDR Installer.dmg"
DMG_APP_NAME="$APP_NAME" DMG_APP_PATH="dist/$APP_NAME.app" \
  dmgbuild -s scripts/dmg_settings.py "$APP_NAME" "$DMG_NAME"

echo "==> Done: $DMG_NAME"
