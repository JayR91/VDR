"""
Video/stream capture support, built on yt-dlp (the actively-maintained,
widely-used open-source extractor library — the same kind of engine real
download managers use for site-specific video capture).

Note: only use this against content you have the right to download
(your own uploads, permitted platforms, content licensed for it, etc.) —
respect the terms of service of whatever site you're pulling from.
"""
import os
import shutil
import sys
from typing import Callable, Optional

import yt_dlp
import yt_dlp.extractor as _ie_mod

import page_media
import vdr_log


def _bundled_ffmpeg_dir():
    """Directory holding the frozen build's ffmpeg, or None when running from
    source (in which case yt-dlp falls back to PATH).

    The binary is `ffmpeg` on macOS/Linux and `ffmpeg.exe` on Windows; probing
    for the bare name on Windows silently found nothing and left yt-dlp with
    no muxer, so merged video+audio downloads failed on exactly the machines
    least likely to have ffmpeg installed already.

    Where it lands differs by platform *and* by PyInstaller layout, so all the
    plausible spots get checked rather than just one:

      - `sys._MEIPASS` is the authoritative answer in a frozen build. For
        onedir it is the contents directory, for onefile the unpack temp dir.
      - Next to the executable is where the macOS .app puts it (Contents/MacOS)
        and where a hand-assembled build tree would.
      - `_internal/` beside the executable is PyInstaller 6's onedir contents
        directory. This is the one that actually bit us: VDR-windows.spec adds
        ffmpeg with dest ".", which PyInstaller 6 resolves *into* the contents
        directory, so the installed layout is `VDR/_internal/ffmpeg.exe` while
        this function only ever looked in `VDR/`. It found nothing, yt-dlp got
        no muxer, and every video needing a video+audio merge -- i.e. most
        YouTube above 360p -- died partway with "you have requested merging of
        multiple formats but ffmpeg is not installed", leaving a .part file.
    """
    if not getattr(sys, "frozen", False):
        return None
    exe_dir = os.path.dirname(sys.executable)
    candidates = [
        getattr(sys, "_MEIPASS", None),
        exe_dir,
        os.path.join(exe_dir, "_internal"),
    ]
    for directory in candidates:
        if not directory or not os.path.isdir(directory):
            continue
        for name in ("ffmpeg.exe", "ffmpeg"):
            if os.path.exists(os.path.join(directory, name)):
                return directory
    return None


def _ffmpeg_available() -> bool:
    """Whether a muxer exists at all -- bundled, or already on the user's PATH.

    Drives the progressive-stream fallback in download_video(): without ffmpeg
    a merged format selector cannot produce a file, and yt-dlp aborts rather
    than quietly picking something else.
    """
    if _bundled_ffmpeg_dir():
        return True
    return shutil.which("ffmpeg") is not None

_extractor_classes = None


def _get_extractor_classes():
    """Site-specific extractors only -- excludes yt-dlp's "Generic" extractor,
    which matches literally any http(s) URL as a last-resort fallback and
    would misclassify plain file downloads (zips, PDFs, ...) as video."""
    global _extractor_classes
    if _extractor_classes is None:
        _extractor_classes = [c for c in _ie_mod.gen_extractor_classes() if c.ie_key() != "Generic"]
    return _extractor_classes


_UNSUPPORTED_MARKERS = (
    "unsupported url",
    "no video formats found",
    "unable to extract",
)


def _looks_unsupported(err: Optional[Exception]) -> bool:
    return any(m in str(err or "").lower() for m in _UNSUPPORTED_MARKERS)


def resolve_page_media(url: str) -> Optional[str]:
    """A media URL embedded in [url], when yt-dlp does not know the site.

    yt-dlp recognises sites by name and, failing that, looks for a <video> tag
    or a bare playlist link. Course platforms built on React/Next.js satisfy
    neither: the player is handed its URL from inside a JSON blob, so yt-dlp
    answers "Unsupported URL" for a page whose HLS ladder is sitting in plain
    text in the markup. Reading it out and passing that along is enough --
    yt-dlp handles the playlist itself perfectly well.

    Returns None when the page has no media in it, which is also the honest
    answer for a page that builds its URLs in JavaScript.
    """
    try:
        return page_media.resolve(url)
    except Exception:
        return None


def looks_like_video_url(url: str) -> bool:
    """True if any of yt-dlp's 1800+ site-specific extractors recognizes this
    URL -- not just YouTube, but Vimeo, TikTok, Instagram, Reddit, Twitch,
    and effectively every major video site yt-dlp supports."""
    for ie_class in _get_extractor_classes():
        try:
            if ie_class.suitable(url):
                return True
        except Exception:
            continue
    return False


def cookie_browsers(platform: Optional[str] = None) -> tuple:
    """Browsers whose cookie stores are tried, in order, when a site demands
    a logged-in session.

    Reading these can prompt for Keychain access on macOS, which is why it's
    a last resort rather than the default path. On Windows, Safari is not a
    browser, and Chrome 127+ app-bound encryption usually fails to decrypt
    cookies — Firefox (then Edge) is the order that actually works.
    """
    plat = sys.platform if platform is None else platform
    if plat == "win32":
        return ("firefox", "edge", "chrome", "brave")
    if plat == "darwin":
        return ("safari", "chrome", "firefox")
    return ("firefox", "chrome", "chromium")


# Back-compat alias for anything that imported the old tuple.
_COOKIE_BROWSERS = cookie_browsers()

# Default ceiling on video height. See download_video() for why this exists.
MAX_HEIGHT = 1080


class DownloadPaused(Exception):
    """Raise from a progress_hook to intentionally abort an in-progress
    download (e.g. the user clicked Pause). yt-dlp's downloader resumes
    from partial fragments by default, so a later call with the same URL
    picks back up rather than starting over."""


def is_supported(url: str) -> bool:
    """Quick check whether yt-dlp recognizes this URL without downloading."""
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "simulate": True}) as ydl:
            ydl.extract_info(url, download=False, process=False)
        return True
    except Exception:
        return False


def get_info(url: str) -> dict:
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
        return ydl.extract_info(url, download=False)


def download_video(
    url: str,
    dest_dir: str,
    quality: str = "best",
    progress_hook: Optional[Callable] = None,
    audio_only: bool = False,
    _resolved: bool = False,
):
    os.makedirs(dest_dir, exist_ok=True)
    ffmpeg_dir = _bundled_ffmpeg_dir()
    base_opts = {
        "outtmpl": os.path.join(dest_dir, "%(title).150B [%(id)s].%(ext)s"),
        "merge_output_format": "mp4",
        **({"ffmpeg_location": ffmpeg_dir} if ffmpeg_dir else {}),
        "progress_hooks": [progress_hook] if progress_hook else [],
        # progress_hooks only report each fragment's own temp filename (e.g.
        # "...f137.mp4"), which yt-dlp deletes once it's merged. postprocessor_hooks
        # additionally fire with the real final filepath once merging/conversion
        # finishes -- callers need that to know what file actually exists at the end.
        "postprocessor_hooks": [progress_hook] if progress_hook else [],
        # "Download only the video, if the URL refers to a video AND a
        # playlist." This was False, which is how clicking the ⬇ VDR button on
        # a video downloaded a different one: YouTube puts almost every music
        # video inside an auto-generated Mix, so the page URL the extension
        # reads carries &list=RD..., and yt-dlp obligingly queued the whole
        # 484-entry mix and started at its first track. The clicked video was
        # never what arrived.
        #
        # True does not cost playlist support: a /playlist?list=... URL has no
        # single video to prefer, so it still resolves to all its entries.
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # yt-dlp's defaults give up on a stalled read quickly, which on a
        # home connection shows up as a download that stops partway and is
        # reported as an error with a .part file left behind. Retrying the
        # whole download, retrying individual fragments, and allowing a
        # longer read before declaring the socket dead are what turn a brief
        # network hiccup back into a download that finishes.
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
        # Pick up where a previous attempt left off instead of restarting.
        "continuedl": True,
        # A frozen GUI build has no console: yt-dlp's progress writer would be
        # writing to a stdout that does not exist. Progress reaches the UI
        # through progress_hooks regardless.
        "noprogress": True,
        # Without this yt-dlp's warnings and errors are simply discarded, so a
        # download that fails leaves nothing to explain why.
        "logger": vdr_log.YtdlpLogger(vdr_log.get_logger()),
    }

    if audio_only:
        attempts = [{"format": "bestaudio/best",
                     "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]}]
    elif quality != "best":
        attempts = [{"format": quality, "postprocessors": []}]
    else:
        # Prefer H.264 video + AAC audio (universally playable, incl. QuickTime)
        # over YouTube's AV1/VP9 + Opus "best" streams, which many players can't
        # decode. Some videos' preferred-format URLs come back HTTP 403 depending
        # on which YouTube "player client" served them, so retry with alternate
        # clients before falling back to whatever format is actually reachable.
        #
        # Capped at MAX_HEIGHT rather than taking the literal best: newer
        # yt-dlp surfaces 4K avc1 for clips that previously topped out far
        # lower, which turned a ~28MB download into ~270MB of the same short
        # film. 1080p keeps files (and wait times) sane by default; pass an
        # explicit `quality` to override.
        # Each "/" step relaxes one requirement. Insisting on mp4a audio is
        # right for YouTube but matches nothing on sites whose audio tracks
        # report no codec at all -- Vimeo's HLS audio comes back acodec=None,
        # so an mp4a-only selector fails with "Requested format is not
        # available" even though the video really is H.264. Falling back to
        # any audio track, and then to a progressive stream, keeps those
        # working while still preferring the QuickTime-friendly pairing.
        h264_fmt = (
            f"bv*[vcodec^=avc1][height<={MAX_HEIGHT}]+ba[acodec^=mp4a]"
            f"/bv*[vcodec^=avc1][height<={MAX_HEIGHT}]+ba"
            f"/b[vcodec^=avc1][height<={MAX_HEIGHT}]"
            f"/bv*[vcodec^=avc1]+ba[acodec^=mp4a]"
            f"/bv*[vcodec^=avc1]+ba"
            f"/b[vcodec^=avc1]"
        )
        attempts = [
            {"format": h264_fmt, "postprocessors": []},
            {"format": h264_fmt, "postprocessors": [],
             "extractor_args": {"youtube": {"player_client": ["android", "web"]}}},
            {"format": f"bestvideo[height<={MAX_HEIGHT}]+bestaudio/best[height<={MAX_HEIGHT}]/best",
             "postprocessors": []},
        ]
        # Every tier above leads with a `video+audio` selector, which yt-dlp
        # can only satisfy by muxing. With no ffmpeg anywhere it does not fall
        # through to the progressive alternative after the "/" -- the merged
        # selector *matched*, so it commits to it and then aborts with
        # "you have requested merging of multiple formats but ffmpeg is not
        # installed". A progressive-only tier last means the worst case is a
        # complete file at whatever single-stream quality the site offers,
        # instead of no file and an error.
        if not _ffmpeg_available():
            attempts.append({"format": f"b[ext=mp4][height<={MAX_HEIGHT}]/b[ext=mp4]/b",
                             "postprocessors": []})

    # Snapshotted once, before any attempt, so give_up() can tell what this
    # call created from what was already sitting in the folder.
    before_any = set(os.listdir(dest_dir))

    def _try(extra_opts) -> Optional[Exception]:
        """Run one attempt. Returns None on success, or the exception raised."""
        try:
            with yt_dlp.YoutubeDL({**base_opts, **extra_opts}) as ydl:
                ydl.download([url])
            return None
        except Exception as e:
            # Deliberately leaves partial fragments in place for the next
            # attempt. This used to delete everything the attempt produced,
            # which was actively harmful: a merged download fetches video and
            # audio as separate streams, so a hiccup on the audio stream --
            # a 403 on its URL, a dropped connection -- threw away a video
            # stream that had already finished downloading. The next attempt
            # then re-fetched hundreds of MB it already had, took long enough
            # to invite the same failure again, and the user watched three
            # full downloads end in an error and no file.
            #
            # Fragments are named per format ("...f299.mp4", "...f140.m4a"),
            # so a different format tier cannot collide with a previous one's
            # leftovers, and `continuedl` means an identical tier resumes from
            # them instead of restarting. Cleanup happens once, in give_up().
            vdr_log.get_logger().warning(
                "attempt failed (format=%r): %s: %s",
                extra_opts.get("format"), type(e).__name__, e,
            )
            return e

    def give_up(err: Exception):
        """Remove what this call produced, then raise a presentable error.

        Only reached once every attempt has failed, so there is nothing left
        worth resuming -- and leaving a half-downloaded stream behind would
        show up in the user's folder as a file that looks real but is not.
        """
        vdr_log.get_logger().error("giving up on %s: %s: %s", url, type(err).__name__, err)
        for name in set(os.listdir(dest_dir)) - before_any:
            try:
                os.remove(os.path.join(dest_dir, name))
            except OSError:
                pass
        raise _friendly_error(err)

    last_err = None
    for extra_opts in attempts:
        err = _try(extra_opts)
        if err is None:
            return
        if isinstance(err, DownloadPaused):
            # Intentional stop, not a real failure -- don't fall back to a
            # different format tier, and keep the partial fragments so the
            # next attempt (on Resume) can continue from them.
            raise err
        last_err = err

    # Only now, and only when the site actually said "log in", retry with the
    # user's browser cookies. Gating on the error keeps the cookie stores (and
    # the macOS Keychain prompt that reading Chrome's can trigger) completely
    # out of the picture for ordinary failures like a 404 or a dropped network.
    if _looks_like_login_required(last_err):
        for browser in cookie_browsers():
            for extra_opts in attempts:
                # Every format tier, not just the first: signing in only gets
                # past the login wall, and the site may still have no stream
                # matching the preferred H.264 pairing -- the later, looser
                # tiers are exactly what covers that.
                err = _try({**extra_opts, "cookiesfrombrowser": (browser,)})
                if err is None:
                    return
                if isinstance(err, DownloadPaused):
                    raise err
            # Cookie-store failures (Chrome's app-bound encryption, a missing
            # Safari on Windows, a locked DB) must not replace the original
            # YouTube/login error — that used to surface as "failed to load
            # cookies from safari" on a machine that has never had Safari.
            if _looks_like_cookie_store_failure(err):
                continue
            # Keep the cookie attempt's own error. A browser that *is* signed
            # in gets past the login wall and then fails for some other reason
            # (no matching format, say); reporting the earlier login-required
            # error instead would send the user off signing in again to fix
            # something that has nothing to do with signing in.
            if not _looks_like_login_required(err):
                last_err = err

    # Last resort before giving up: yt-dlp may simply not know this site.
    # Read the page, and if it embeds media, point yt-dlp at that instead.
    # Guarded on _resolved so a page whose media URL is itself unplayable
    # cannot bounce back in here and recurse.
    if not _resolved and _looks_unsupported(last_err):
        embedded = resolve_page_media(url)
        if embedded and embedded != url:
            return download_video(
                embedded,
                dest_dir,
                quality=quality,
                progress_hook=progress_hook,
                audio_only=audio_only,
                _resolved=True,
            )

    give_up(last_err)


_LOGIN_MARKERS = (
    "only works when logged-in",
    "requires authentication",
    "private video",
    "sign in to confirm",
    "members-only",
    "this video is available to this channel's members",
)


class LoginRequired(Exception):
    """The site refused anonymous access and no usable browser session was found."""


class DRMProtected(Exception):
    """The only streams on offer are DRM-encrypted, so there is nothing to fetch."""


_COOKIE_STORE_MARKERS = (
    "failed to load cookies",
    "failed to decrypt",
    "could not copy",
    "could not find",
    "no such browser",
    "unsupported cookie",
    "could not find chrome cookies",
)


def _looks_like_cookie_store_failure(err: Optional[Exception]) -> bool:
    text = str(err or "").lower()
    return any(m in text for m in _COOKIE_STORE_MARKERS)


def _looks_like_login_required(err: Optional[Exception]) -> bool:
    return any(m in str(err or "").lower() for m in _LOGIN_MARKERS)


def _friendly_error(err: Exception) -> Exception:
    """Turn yt-dlp's raw, flag-laden error text into something a GUI can show.

    yt-dlp's login errors read like CLI help ("Use --cookies, --netrc-cmd,
    ..."), which is noise to someone clicking a button in a download manager.
    """
    text = str(err or "")
    if "drm" in text.lower():
        # Not a fault to retry around: the streams are encrypted, and VDR has
        # no business trying to decrypt them. Say so plainly instead of
        # showing yt-dlp's "try another format" hint, which implies otherwise.
        return DRMProtected(
            "This video is DRM-protected, so it can't be downloaded. "
            "Watch it on the site instead."
        )
    if any(m in text.lower() for m in _LOGIN_MARKERS):
        if sys.platform == "win32":
            return LoginRequired(
                "This video requires being signed in, or YouTube asked VDR to "
                "confirm it isn't a bot. On Windows, Chrome's cookies usually "
                "cannot be read (Chrome encrypts them for Chrome only). Sign "
                "in to the site in Firefox, then try again."
            )
        return LoginRequired(
            "This video requires being signed in. VDR already tried your "
            "Safari, Chrome and Firefox sessions without finding one that "
            "works — sign in to the site in one of those browsers, then try "
            "again."
        )
    return err
