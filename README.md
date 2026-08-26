# VDR — Video Downloader

A fast, resumable desktop download manager for macOS and Windows, built in Python. VDR
does real segmented HTTP downloading, captures video from hundreds of streaming sites,
integrates with every major browser, and adapts its own behaviour to your machine's
battery and idle state.

Tested components: segmented downloads, pause/resume, resume-after-restart, retry/error
handling, and bandwidth throttling (see "What's been tested" below).

## Features

- **Multi-threaded / segmented downloads** — splits a file into up to 32 parallel segments
  when the server supports HTTP range requests, for faster downloads.
- **Pause / resume** — pauses instantly, mid-chunk, even while bandwidth-throttled.
- **Resume after app restart** — progress is checkpointed to a small `.vdrstate.json`
  sidecar file next to each download, so closing the app and relaunching resumes cleanly.
- **Automatic retry** — each segment retries independently with exponential backoff
  (default 5 retries) before the whole download is marked as failed.
- **Bandwidth throttling** — a global speed cap (KB/s) shared fairly across all active
  downloads and segments, adjustable live with a numeric field or slider.
- **Focus Guard** — VDR watches your machine's power and idle state: it pauses downloads
  on battery or Low Power Mode / Battery Saver, crawls at 256 KB/s while you're actively
  using the machine so browsing stays snappy, and returns to full speed once it's idle
  and plugged in. Toggle it in the toolbar. Reads the native signals on each platform
  (macOS `ioreg`/`pmset`, Windows `GetLastInputInfo`/`GetSystemPowerStatus`).
- **macOS integration** — a live Dock badge, menu-bar drop target, automatic light/dark
  appearance, native completion notifications, and the Glass system chime.
- **Windows integration** — a notification-area (tray) icon with Show/Quit and live
  progress in its tooltip, automatic light/dark appearance read from the registry,
  native toast notifications, and a completion chime. Closing the window keeps VDR
  running in the tray so downloads and the extension bridge continue.
- **Scheduling and organisation** — queue a URL for a future time (blank scheduling time
  means the next midnight); completed files are sorted into Videos, Documents, Zips,
  Audio, Images, or Other folders inside `~/Downloads/VDR`.
- **Video/stream capture** — powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp), the
  actively maintained open-source extractor used by many real download tools, for
  YouTube and hundreds of other sites.
- **Browser integration** — a Manifest V3 extension for Chrome/Edge/Brave/Opera/Vivaldi,
  Firefox, and Safari that can:
  - intercept the browser's native downloads and hand them to this app instead
    (so you get segmented/resumable downloading for regular browser downloads too)
  - send the current tab, or right-clicked links/videos, to the app via
    "Download with VDR"
  - inject a floating "⬇ VDR" button directly onto video players (YouTube, X/Twitter,
    and hundreds of other sites yt-dlp recognizes)

## Requirements

- Python 3.9+
- `pip install -r requirements.txt` (installs `requests`, `flask`, `yt-dlp`; the
  platform-specific extras are marked so only the ones for your OS are pulled in)
- On Linux, Tkinter needs the system package if it isn't already present:
  `sudo apt install python3-tk`
- For video downloads that need merging (video+audio), `ffmpeg` should be on your PATH.
  The packaged builds (DMG and Windows installer) bundle it, so installed copies need
  nothing preinstalled.

## Installing

Prebuilt installers for both platforms are attached to every
[release](https://github.com/JayR91/VDR/releases):

- **macOS** — `VDR-<version>-macOS-Installer.dmg`. Open it and drag VDR to
  Applications.
- **Windows** — `VDR-<version>-Windows-Setup.exe`. It installs per-user, so it
  needs no administrator rights and raises no UAC prompt.

Each filename names its own platform, so there is nothing to work out from the
extension.

Neither build is code-signed, so both operating systems will warn on first launch.
On macOS use right-click → Open. On Windows, SmartScreen shows "Windows protected your
PC" — choose **More info → Run anyway**.

## Running the app

```bash
pip install -r requirements.txt
python main.py
```

This opens the desktop window and starts a local server on
`http://127.0.0.1:27182` for the browser extension to talk to (only listens on
localhost — nothing external can reach it).

### Using the app

- **+ Add URL** — paste a direct file link, pick a save location and number of
  segments (default 8).
- **+ Add Video/Stream** — paste a video page URL (YouTube, etc.); this uses yt-dlp
  in the background and saves into `~/Downloads/VDR`.
- Select a row to **Pause / Resume / Cancel / Remove / Open Folder**.
- Set a global **speed limit** in KB/s (0 = unlimited) and click Apply.
- Turn on **Focus Guard** to pause on battery and slow down while you are at the keyboard.
- **Schedule URL** opens a future-time queue timer. Use a blank time for midnight.
- On macOS, drag an `http`/`https` link onto the `⇩` menu-bar icon to queue it without
  opening the window. The Dock badge shows a single download's percentage or the active
  download count; it clears on completion.
- On Windows, VDR lives in the notification area while its window is closed; hover the
  tray icon for progress, or use its Show/Quit menu. Windows has no drag-onto-icon
  equivalent, so use the browser extension or **+ Add URL** to queue links.

### Building the macOS app

Install the dependencies, then run:

```bash
PYINSTALLER_CONFIG_DIR=/private/tmp/vdr-pyinstaller-cache \
  pyinstaller --noconfirm "VDR.spec"
```

The resulting `dist/VDR.app` supports Dock URL delivery via macOS argv emulation;
links can be dropped onto its Dock icon after the bundle is launched. The menu-bar drop
target works while running from source as well. `setup.py` remains available for py2app
builds if you prefer that packaging flow.

### Building the Windows app

PyInstaller cannot cross-compile, so this has to run on Windows. From a checkout,
with [Inno Setup](https://jrsoftware.org/isdl.php) installed
(`winget install JRSoftware.InnoSetup`):

```powershell
pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -Version v2.2.0
```

That downloads ffmpeg, freezes `VDR-windows.spec` into `dist\VDR\`, and compiles
`installer.iss` into `dist_installer\VDR-<version>-Windows-Setup.exe`. Pass `-SkipFfmpeg` to
build without bundling it (the app then falls back to whatever is on PATH).

Both platforms are built and published automatically by
`.github/workflows/release.yml` when a `v*` tag is pushed.

## Installing the browser extension

`browser_extension/` is the shared source. Safari needs a one-time conversion into a
native app wrapper (Apple requires this — there's no "load unpacked" for Safari).
In all cases, make sure the desktop app (`main.py`, or the installed `.app`) is running
first — the extension only works while it's listening on `127.0.0.1:27182`.

First, generate the per-browser packages (`scripts/build_dmg.sh` does this too):

```bash
python3 scripts/build_extension.py
```

That writes ready-to-load copies to a stable location:

- Chromium: `~/Library/Application Support/VDR/extension-chrome`
- Firefox: `~/Library/Application Support/VDR/extension-firefox`

**Load from those paths, not from `browser_extension/` in the checkout.** Chromium
records the on-disk path of an unpacked extension and silently disables it if that path
ever moves — so loading it out of a source tree means renaming or relocating the repo
breaks the extension, and the floating button just stops appearing. Re-run the script
after changing extension code.

### Chrome, Edge, Brave, Opera, Vivaldi (Chromium)

1. Open `chrome://extensions` (`edge://extensions`, `brave://extensions`, etc).
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and select
   `~/Library/Application Support/VDR/extension-chrome`.

### Firefox

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on…** and select
   `~/Library/Application Support/VDR/extension-firefox/manifest.json`
   directly (not the folder).
3. This load is temporary — Firefox drops it on restart. For a permanent install,
   the extension needs to be signed by Mozilla (`web-ext sign`) or Firefox needs to be
   on the Developer/Nightly channel with `xpinstall.signatures.required` disabled.

Chromium and Firefox disagree on two MV3 details — Chromium wants
`background.service_worker` while Firefox wants `background.scripts`, and Firefox needs
a `browser_specific_settings.gecko` id that Chromium calls an unrecognized key. Each
warns about the other's spelling, which is why there are two packages rather than one
shared folder; loading either shows no manifest warnings.

### Safari

Safari extensions must be a signed Xcode app extension, not a loose folder of JS —
convert it once with Apple's own tool:

```bash
xcrun safari-web-extension-converter browser_extension/ --project-location /path/to/output
```

Open the generated Xcode project and build/run it (Cmd+R), then **copy the built
`.app` out of `DerivedData` into `~/Applications`** and launch it from there — Safari
does not reliably list an extension whose container app lives in `DerivedData`.
Then in Safari: **Settings → Developer** → check **"Allow Unsigned Extensions"**
(macOS asks for your account password) → **Settings → Extensions** → enable
**VDR Connector** → and on the first video page, click the extension's toolbar icon and
grant it permission for that site (Safari gates host access per-site, unlike Chrome's
`<all_urls>`; until you grant it, the content script never runs and no "⬇ VDR" button
appears).

Two Safari-specific gotchas worth knowing:
- **"Allow Unsigned Extensions" resets every time Safari restarts.** That's Apple's
  behavior for unsigned builds, not a bug here — a properly signed/notarized build
  doesn't need the setting at all.
- The per-site permission prompt is required before the injected button shows up.

### Using it

Click the extension icon (or, on the video button, click "⬇ VDR" directly on the player) to:
- toggle **"Intercept browser downloads"** — when on, downloads you'd normally see in
  the browser's download bar get sent to VDR instead
- **"Send this page/video to VDR"** — manually send the current tab
- or right-click any link/video/audio element → **"Download with VDR"**

## Project structure

```
vdr/
  engine.py            # core download engine (segments, resume, retry, throttling)
  queue_manager.py      # manages concurrent downloads + global speed limit
  focus_guard.py        # battery / idle-aware Focus Guard
  video_capture.py      # yt-dlp wrapper for video/stream downloads
  organizer.py           # post-download category routing + filename sanitization
  desktop_integration.py # picks the native integration for the running OS
  macos_integration.py   # optional Dock/menu-bar/notification integration (PyObjC)
  windows_integration.py # optional tray-icon/toast integration (pystray)
  server.py             # local Flask server for the browser extension
  gui.py                # Tkinter desktop UI
  main.py               # entry point — wires everything together
  requirements.txt
  VDR.spec               # PyInstaller spec, macOS (.app bundle)
  VDR-windows.spec       # PyInstaller spec, Windows (.exe tree)
  installer.iss          # Inno Setup script for the Windows installer
  scripts/
    build_dmg.sh         # macOS: freeze + package the DMG
    build_windows.ps1    # Windows: fetch ffmpeg, freeze, compile installer
  tests/
    test_remove_row.py       # real Tk/App integration test for row removal
    test_cross_platform.py   # platform dispatch + integration-surface parity
  browser_extension/
    manifest.json        # MV3; declares both Chromium's service_worker and
                          # Firefox's scripts background forms + gecko id
    background.js        # intercepts downloads, adds right-click menu, proxies
                          # fetches for content.js (page CSP can block those directly)
    content.js            # injects the floating "⬇ VDR" button onto video players
    popup.html / popup.js
```

## What's been tested

I built and ran an actual test suite against a local HTTP server (not just written
the code) before handing this over:

- **Segmented download integrity** — downloaded a 5MB file across 6 parallel
  segments and verified the SHA-256 checksum matched the source exactly.
- **Pause/resume correctness** — paused mid-download (including mid-throttle-sleep,
  which exposed and fixed a real bug), confirmed byte count froze while paused,
  then resumed and verified the final checksum.
- **Resume after simulated app restart** — paused a download, then created a
  *brand new* task object pointed at the same file (simulating closing and
  reopening the app), and confirmed it picked up from the saved state and
  finished with a correct checksum.
- **Bandwidth throttling** — capped a download at 1MB/s and confirmed it took
  the expected minimum time rather than finishing instantly.
- **Retry/error handling** — requested a nonexistent file and confirmed it
  correctly surfaces as an error after retries rather than hanging or crashing.
- **GUI integration** — ran the actual Tkinter app headlessly, queued a real
  download through it, and confirmed the download completed and the on-screen
  row updated correctly. This also caught and fixed a cross-thread Tkinter
  crash (background threads now only push events onto a thread-safe queue;
  the Tk main loop is the only thing that touches widgets).
- **Local server for the extension** — verified `/ping`, `/add` for a regular
  file, and `/add` for a recognized video URL all respond correctly.
- **Cross-browser extension, end to end, in real browsers** — loaded the unmodified
  extension in both Chrome and Firefox, clicked the injected "⬇ VDR" button on a real
  YouTube video in each, and confirmed a complete, correctly h264/aac-encoded file
  landed on disk in both cases. Also verified the local server's origin allowlist
  correctly accepts `chrome-extension://` and `moz-extension://` requests and rejects
  everything else (including a plain `https://` origin, simulating an arbitrary
  website trying to reach the local server).
- **Safari, end to end** — converted via `xcrun safari-web-extension-converter`, installed
  from `~/Applications`, enabled, granted site permission, and confirmed the injected
  "⬇ VDR" button downloads a real video to a complete h264/aac file, matching the
  Chrome and Firefox results byte for byte. Also confirmed the collision-safe rename
  kicked in correctly (a second download of the same video became `... (1).mp4` rather
  than overwriting the first).

**Not tested here**:
- Edge/Brave/Opera/Vivaldi specifically — these are Chromium and use the identical
  `chrome-extension://` origin and `chrome.*` APIs already verified under Chrome, but
  weren't individually installed and clicked through.

## Notes & limitations

- Segmented downloading requires the server to support HTTP range requests
  (`Accept-Ranges: bytes`); if it doesn't, the engine falls back to a single
  connection automatically.
- Only use the video-capture feature on content you have the right to
  download — respect the terms of service of whatever site you're pulling from.
- The app is not code-signed or notarized by Apple, so the first launch needs a
  right-click → Open (or an allow in System Settings → Privacy & Security).
- Ideas for where this goes next: a settings dialog for default segment count and
  retries, a proper icon set for the browser extension, and a signed/notarized build
  so the first-launch warning goes away.

## Support this project

If VDR is useful to you, sponsoring it is the most direct way to keep it maintained —
see the **Sponsor** button at the top of this repository.

## License

VDR is free software, licensed under the **GNU General Public License v3.0** — see
[LICENSE](LICENSE).

The distributed macOS app bundles an FFmpeg binary built with `--enable-gpl`, which is
what makes GPLv3 the license for the release as a whole. Full attribution for every
bundled component, along with the written offer for FFmpeg's corresponding source code,
is in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
