# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

VDR ("Video Downloader") is a desktop download manager built in Python: a
Tkinter GUI, a segmented/resumable HTTP download engine, yt-dlp-based video capture, a local Flask
server for a companion Chrome extension, and macOS-specific integration (Dock badge, native
notifications, a menu-bar URL drop target plus a one-time "Setup Browser
Extension…" helper, and a "Focus Guard" that adapts download behavior to battery/idle state).

**Note:** `laser_wars/` is a separate, unrelated turn-based strategy game living in this same
directory — it is not part of the download manager and has its own `README.md`/`requirements.txt`.
Do not conflate the two when asked to work on "this project."

## Commands

```bash
# Setup
pip install -r requirements.txt      # requests, flask, yt-dlp (+ optional macOS: pyobjc, py2app, pyinstaller)

# Run
python main.py                       # opens the Tk window + starts the local server on :27182

# Build a double-clickable macOS .app (either flow works; the repo has configs for both)
PYINSTALLER_CONFIG_DIR=/private/tmp/vdr-pyinstaller-cache \
  pyinstaller --noconfirm "VDR.spec"
python setup.py py2app                # alternative: py2app packaging
```

```bash
# Tests: plain scripts, one per file, each prints "PASS - ..." and exits non-zero on failure.
for t in tests/test_*.py; do python "$t" | tail -1; done
```

The tests are deliberately *not* pytest modules — `tests/test_remove_row.py` creates a real Tk
root at import time, which crashes under pytest's collector. Run them as scripts. `tests/
test_video_progress.py` covers the video pipeline's pure logic (progress arithmetic, format
choice, live/playlist gating, cancel cleanup, page-media ranking, row rendering) with hand-written
yt-dlp hook dicts and a stubbed `YoutubeDL`; no network. There is no linter or formatter.

For a real end-to-end run without the GUI, a throwaway script that imports `video_capture` and
calls `download_video(url, tmpdir, progress_hook=...)` with a `VideoProgress` is the fastest
check (the 19-second "Me at the zoo" YouTube clip is a good subject). To exercise engine/queue
behavior (pause/resume of segmented downloads), drive `QueueManager` against a local Flask
server with `send_file(..., conditional=True)` (working `Accept-Ranges`) rather than a public
endpoint, which is flaky/rate-limited.

## Architecture

**Threading model is the load-bearing constraint.** `gui.py`'s `App` follows a strict rule:
background threads (download segment threads, the per-task monitor thread, video-download threads,
Focus Guard's polling thread) *never* touch Tk widgets directly. They only push `(kind, payload)`
tuples onto `App._events` (a `queue.Queue`); only `_drain_events()`/`_refresh()`, driven by
`root.after()` polling on the main thread, touch widgets. This was a deliberate fix for a real
cross-thread Tkinter crash — preserve it for any new async work.

**Two independent download pipelines share one `Status` enum and one Treeview:**

- **Regular file downloads** — `engine.DownloadTask` does real segmented HTTP: probes the URL for
  `Content-Length`/`Accept-Ranges`, splits into up to 32 segments, downloads each in its own thread
  with independent exponential-backoff retry, throttles through a shared `TokenBucket`, and
  checkpoints progress to a `<dest>.vdrstate.json` sidecar so a fresh `DownloadTask` pointed at the
  same path resumes correctly (used both for app-restart resume and for Focus-Guard-triggered
  pause/release). `pause()` is a true instant freeze (clears a `threading.Event` the segment loops
  block on, mid-chunk).
- **Video/stream downloads** — `gui.VideoTask` wraps `video_capture.download_video()` (yt-dlp) and
  duck-types the same interface (`status`, `bytes_downloaded()`, `pause/resume/cancel`) so it can
  sit in the same Treeview and go through the same `QueueManager`-adjacent code paths, but the
  mechanics are different: yt-dlp has no live pause API, so `pause()` raises `video_capture.
  DownloadPaused` from inside the `progress_hook`/`postprocessor_hook` to abort the transfer, and
  `resume()` just re-invokes `download_video()` on the same URL — yt-dlp's `continuedl` picks the
  partial fragments back up rather than restarting from zero. **Cancel is different from pause:**
  the hook raises `DownloadCancelled` (a `DownloadPaused` subclass) and `download_video()` deletes
  the files yt-dlp reported for *this* call (`.part`/`.ytdl` included) — never "everything new in
  the folder", because two videos can download at once (`VideoTask.MAX_CONCURRENT`, a semaphore;
  videos used to bypass the queue's concurrency cap entirely). `Resume` on an `ERROR` row retries.
  - `download_video()` **pre-flights** with `inspect()` (one `extract_info(download=False)`, flat
    playlists) before any bytes move. That is where live streams are refused (`LiveStream`),
    playlists are surfaced (`PlaylistDetected` → the GUI asks yes/no on the main thread and
    re-runs with `allow_playlist=True`), the row gets its title (`info_cb`), and
    `prefers_progressive()` decides whether to put `PROGRESSIVE_FORMAT` ahead of the merged
    `bv*+ba` tiers. A failed pre-flight returns `None` and the download proceeds as before — it
    can never make a download fail that would otherwise have worked.
  - **Progress is modelled in `video_progress.VideoProgress`**, not read raw from the hook.
    yt-dlp reports per *stream*: `downloaded_bytes` resets to zero when the audio stream starts
    after the video one, and for HLS/DASH `total_bytes_estimate` is recomputed after every
    fragment (the "size keeps increasing" report). `VideoProgress` accumulates bytes across
    streams and anchors the total on the whole download's expected size, flagging it
    `estimated` so `_row_values()` shows `~`. The whole-download size comes from
    `video_capture.VDRSizeProbePP`, a no-op postprocessor registered `before_dl`: its hook fires
    with the *full* info dict (`requested_formats`, `_filename`), which the per-stream progress
    hooks never carry (yt-dlp deletes `requested_formats` before handing each stream down).
  - `task.dest_path` progression: pre-flight `suggested_filename()` → probe's `_filename` (the
    real final name, before download) → `postprocessor_hook` `filepath` after merging.
    `progress_hooks` alone only ever see each fragment's temp filename (e.g. `...f251.webm`).
  - Format tiers, in order: (optional) `PROGRESSIVE_FORMAT` → H.264+AAC merged → same via
    alternate YouTube player clients (dodges per-client HTTP 403s) → yt-dlp's best → (no ffmpeg)
    progressive-only. Then, only on a login-wall error, every tier again with each browser's
    cookies; then, only on "unsupported URL", `page_media.resolve()` and one recursive call.

**`queue_manager.QueueManager`** owns the task list, concurrency limit, and the shared
`TokenBucket`. It also owns Focus Guard integration: `apply_focus_policy()` takes the effective
minimum of the user's speed limit and Focus Guard's crawl cap, and calls `hold_for_focus()` /
`release_from_focus()` on every task when the policy flips to/from `POLICY_HOLD`.

**`focus_guard.FocusGuard`** polls macOS battery/idle state every 3s via `pmset`/`ioreg` subprocess
calls and derives one of four policies (`off` / `full` / `active` (crawl at 256 KB/s) / `battery`
(hold)). This power/idle-adaptive behaviour is one of VDR's distinguishing features and is
called out in the README.

**`video_capture.download_video()`** prefers H.264+AAC (universally playable, unlike YouTube's
default AV1/VP9+Opus "best" streams which many players — including QuickTime — can't decode) and
caps at `MAX_HEIGHT` (1080). Why the merged `bv*[vcodec^=avc1]+ba` selector is not always right:
X, Reddit and most non-YouTube sites publish a *progressive* MP4 at the same top height as their
HLS ladder, but with no codec tag — so the avc1 selector skips it and picks HLS, which means
fragments, no known total, and an ffmpeg remux for the same picture. `prefers_progressive()`
compares the two heights from the pre-flight formats and inserts `PROGRESSIVE_FORMAT` first when
the plain file is as good. `looks_like_video_url()` here is the single source of truth for
video-site detection, imported by both `server.py` (browser extension traffic) and `gui.py` (the
"+ Add URL" dialog auto-routes video URLs to `queue_video()` instead of trying to
segment-download the webpage itself).

**`page_media.py`** is the fallback when yt-dlp answers "Unsupported URL": fetch the page, find
media URLs, hand the best to yt-dlp. Ranking is provenance first — URLs the page *declares* as
its video (`og:video`, JSON-LD `contentUrl`, `<video>/<source src>`; `extract_declared_media()`)
beat anything the regex sweep merely finds in the markup, however sharp — then resolution, then
plain file over manifest. Without the provenance rank a page with a 1080p promo and a 720p lesson
handed over the promo.

**`organizer.py`** provides post-download category routing (Videos/Documents/Zips/Audio/
Images/Other by extension) plus collision-safe dedupe/rename, shared by `server.py` (incoming
browser-extension URLs) and `gui.py` (completed regular + video files).

**`vdr_log.py`** is the runtime log (`~/.vdr/vdr.log` on macOS, `%LOCALAPPDATA%\VDR\vdr.log` on
Windows; rotating). yt-dlp's warnings/errors go there via `YtdlpLogger`. A frozen windowed build
has `sys.stderr is None`, so anything that prints instead of logging is silently lost — and
`traceback.print_exc()` there *raises*, which once killed the worker thread before it could report
the failure. Look in the log first when a download "just says error".

**`macos_integration.MacIntegration`** is entirely optional and self-disabling: if not on Darwin or
PyObjC isn't installed, `available` stays `False` and every method becomes a safe no-op (falling
back to `osascript`/`afplay` subprocess calls for notifications/sound where possible even without
PyObjC). Never assume it's present.

**`server.py`** is a Flask app on `127.0.0.1:27182` (localhost-only) for the Chrome extension.
`POST /add` auto-detects video vs. regular URLs and routes accordingly; `main.py` also forwards
`sys.argv` URLs (from py2app/PyInstaller argv-emulation when a link is dropped on the Dock icon)
into the same `add_url_from_drop` path.

**`browser_extension/`** (Manifest V3): `background.js` is the service worker — it intercepts
native Chrome downloads and adds the right-click "Download with VDR" menu. `content.js`
injects a floating "⬇ VDR" button onto YouTube's player (`.html5-video-player`), re-injecting on
`yt-navigate-finish` since YouTube is an SPA, and talks to the local server via
`chrome.runtime.sendMessage` to `background.js` rather than fetching directly — YouTube's page CSP
can block a content script's own `fetch()` to `127.0.0.1` but not the background worker's. On
feeds with many videos per page (X), `findPostUrl()` resolves the clicked `<video>` to its own
post via the `<a><time>` permalink inside the enclosing `<article>` — the *first* `/status/`
link in the article is often a reply-to or quoted post, which is how the wrong video got
downloaded. Edit `browser_extension/` then re-run `scripts/build_extension.py` (restarting the
app also refreshes the copies in place on every launch — see `extension_install.stage_unpacked`);
browsers load the stable copies under `~/Library/Application Support/VDR/extension-*`, not the
checkout. The manifest `key` pins the extension id, so refreshes and upgrades never re-id it,
and the app's "Setup Browser Extension…" menu item walks a user through the one-time "Load
unpacked" (Chromium >= 136 refuses a scripted install on the default profile).

## Non-obvious gotchas

- **`_open_path()`** (Open Folder / double-click-to-open in `gui.py`) must use `subprocess.Popen`,
  never `os.system()` — `os.system()` blocks the entire Tk event loop until the shell it spawns
  exits (~150ms+), which is enough to make every click feel unresponsive and get "double-clicked"
  by an impatient user.
- **Cmd+V / right-click paste don't work by default** in Tk Entry/Text widgets on macOS without an
  app-level Edit menu, even though the underlying `<<Paste>>` virtual event works fine. Both are
  wired manually in `_enable_mac_clipboard_shortcuts()`, bound via `bind_all` at the root so future
  dialogs (`simpledialog`, etc.) inherit them automatically.
- **ttk's native "aqua" theme on macOS ignores `style.map()` hover/pressed colors.** The app
  force-switches to the `"clam"` theme for real hover/press feedback, which means it no longer
  auto-follows system Dark Mode — `App._system_is_dark()` / `_apply_system_theme()` /
  `_sync_system_theme()` poll `defaults read -g AppleInterfaceStyle` every 1.5s and manually
  re-`style.configure(...)` every color to compensate. If you touch button/Treeview styling, update
  both the dark and light branches in `_apply_system_theme()`.
- **The installed `.app` at `~/Applications/VDR.app` is the PyInstaller bundle** from
  `scripts/build_dmg.sh` (`Contents/MacOS/VDR` + `Contents/MacOS/ffmpeg`), not the old bash
  launcher. GUI-launched apps don't inherit a Terminal's `PATH`, so `main._ensure_homebrew_path()`
  appends `/opt/homebrew/bin` etc. for `deno` (a JS runtime yt-dlp needs for YouTube's signature
  cipher). Check `Contents/Info.plist` / the binary's mtime against `git log` before debugging a
  user report: the installed copy can be weeks behind the checkout (it was, for every fix in
  September 2026).
- **The bundled ffmpeg must be self-contained.** Homebrew's `ffmpeg` is a ~400 KB stub linked
  against `/opt/homebrew/Cellar/ffmpeg/<ver>/lib/*.dylib`; copying it into the .app (which the
  build script used to do) produces an app whose merges work only on the build machine.
  `build_dmg.sh` now takes the static binary from the `imageio-ffmpeg` wheel (requirements.txt),
  checks it with `otool -L`, and fails the build on a dynamically linked one. Verify with
  `otool -L dist/VDR.app/Contents/MacOS/ffmpeg` — nothing outside `/usr/lib` and `/System`.
- **yt-dlp format filters drop formats whose field is *missing*** unless written none-inclusive:
  `[vcodec!^=vp]` excludes X's codec-less progressive MP4s; `[vcodec!^=?vp]` keeps them. A tier
  whose selector matches nothing is a silent no-op, so run new selectors through
  `ydl.build_format_selector(...)` against a hand-written format list (see
  `tests/test_video_progress.py`) rather than trusting them.
- **Raising out of a yt-dlp progress hook leaks the output file until the GC runs.** The
  exception's traceback pins the fragment downloader's frame, whose `ctx['dest_stream']` is the
  open `.part`; exception ↔ traceback ↔ frame locals is a cycle, so refcounting never frees it.
  `video_capture._detach()` strips the traceback from deliberate stops and `queue_video()`'s
  worker calls `gc.collect()` in `finally`. Symptom without this: `lsof` shows a deleted `.part`
  still open, tens of MB not returned to disk until quit.
- **Only `messagebox.askyesno` in `_ask_playlist()` is an intentional modal.** Everything else
  goes through `_flash_status()` / notifications, because a modal blocks `_refresh()` and every
  other download's progress with it.
