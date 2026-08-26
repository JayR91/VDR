"""
Video/stream capture support, built on yt-dlp (the actively-maintained,
widely-used open-source extractor library — the same kind of engine real
download managers use for site-specific video capture).

Note: only use this against content you have the right to download
(your own uploads, permitted platforms, content licensed for it, etc.) —
respect the terms of service of whatever site you're pulling from.
"""
import os
import sys
from typing import Callable, Optional

import yt_dlp
import yt_dlp.extractor as _ie_mod


def _bundled_ffmpeg_dir():
    """When frozen by PyInstaller, ffmpeg ships next to the executable so end
    users need nothing preinstalled -- Homebrew on macOS (see
    scripts/build_dmg.sh), a PATH entry on Windows (see
    scripts/build_windows.ps1). Returns None when running from source,
    falling back to PATH.

    The binary is `ffmpeg` on macOS/Linux and `ffmpeg.exe` on Windows; probing
    for the bare name on Windows silently found nothing and left yt-dlp with
    no muxer, so merged video+audio downloads failed on exactly the machines
    least likely to have ffmpeg installed already.
    """
    if not getattr(sys, "frozen", False):
        return None
    exe_dir = os.path.dirname(sys.executable)
    for name in ("ffmpeg.exe", "ffmpeg"):
        if os.path.exists(os.path.join(exe_dir, name)):
            return exe_dir
    return None

_extractor_classes = None


def _get_extractor_classes():
    """Site-specific extractors only -- excludes yt-dlp's "Generic" extractor,
    which matches literally any http(s) URL as a last-resort fallback and
    would misclassify plain file downloads (zips, PDFs, ...) as video."""
    global _extractor_classes
    if _extractor_classes is None:
        _extractor_classes = [c for c in _ie_mod.gen_extractor_classes() if c.ie_key() != "Generic"]
    return _extractor_classes


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


# Browsers whose cookie stores are tried, in order, when a site demands a
# logged-in session. Reading these can prompt for Keychain access on macOS,
# which is why it's a last resort rather than the default path.
_COOKIE_BROWSERS = ("safari", "chrome", "firefox")

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
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
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

    def _try(extra_opts) -> Optional[Exception]:
        """Run one attempt. Returns None on success, or the exception raised."""
        before = set(os.listdir(dest_dir))
        try:
            with yt_dlp.YoutubeDL({**base_opts, **extra_opts}) as ydl:
                ydl.download([url])
            return None
        except Exception as e:
            # A failed attempt shouldn't leave partial fragments behind for
            # the next attempt (or the user) to trip over.
            for name in set(os.listdir(dest_dir)) - before:
                try:
                    os.remove(os.path.join(dest_dir, name))
                except OSError:
                    pass
            return e

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
        for browser in _COOKIE_BROWSERS:
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
            # Keep the cookie attempt's own error. A browser that *is* signed
            # in gets past the login wall and then fails for some other reason
            # (no matching format, say); reporting the earlier login-required
            # error instead would send the user off signing in again to fix
            # something that has nothing to do with signing in.
            if not _looks_like_login_required(err):
                last_err = err

    raise _friendly_error(last_err)


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
        return LoginRequired(
            "This video requires being signed in. VDR already tried your "
            "Safari, Chrome and Firefox sessions without finding one that "
            "works — sign in to the site in one of those browsers, then try "
            "again."
        )
    return err
