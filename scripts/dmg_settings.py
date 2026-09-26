"""dmgbuild settings for the installer DMG (see scripts/build_dmg.sh).

A plain `hdiutil create -srcfolder ...` DMG has no window size or icon
layout set at all -- Finder falls back to defaults that often stack the
app and the Applications shortcut on top of each other or open a tiny
window, making it look like nothing is draggable. dmgbuild constructs a
proper .DS_Store directly, so the standard "drag app onto Applications"
layout actually renders as one, with no Finder automation/permissions
needed to build it.

When DMG_EXTRA_DIR is set, that folder (the per-browser extension copies
and the Setup Guide, staged by build_dmg.sh) is placed below the two
standard icons.
"""

import os

app_name = os.environ["DMG_APP_NAME"]
app_path = os.environ["DMG_APP_PATH"]
extra_dir = os.environ.get("DMG_EXTRA_DIR")

format = "UDZO"
files = [app_path]
symlinks = {"Applications": "/Applications"}
icon_locations = {
    f"{app_name}.app": (140, 120),
    "Applications": (360, 120),
}
window_rect = ((200, 200), (500, 300))
icon_size = 100
default_view = "icon-view"
show_icon_preview = True
include_icon_view_settings = True

if extra_dir:
    files.append(extra_dir)
    icon_locations[os.path.basename(extra_dir)] = (250, 265)
    # Taller window so the extra folder does not sit on top of the app icon.
    window_rect = ((200, 200), (500, 400))
