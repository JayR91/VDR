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
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import yt_dlp
import yt_dlp.extractor as _ie_mod

import page_media
import vdr_log
from video_progress import SIZE_PROBE_KEY


class VDRSizeProbePP(yt_dlp.postprocessor.PostProcessor):
    """Does nothing -- exists so its hook fires with the full info dict.

    Registered `before_dl`, after format selection. yt-dlp wraps every
    postprocessor's run() in "started"/"finished" hook calls that carry the
    complete info dict, `requested_formats` and `_filename` included. The
    per-stream progress hooks never see those (yt-dlp deletes
    requested_formats before handing each stream down), so this is how the
    UI learns the whole download's size and final name before the first byte.
    pp_key() strips the trailing "PP", giving video_progress.SIZE_PROBE_KEY.
    """

    def run(self, info):
        return [], info


assert VDRSizeProbePP.pp_key() == SIZE_PROBE_KEY


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

# One plain HTTP MP4 with both tracks, H.264 or codec-unknown, at or under
# the cap. Used ahead of the merged selectors when prefers_progressive() says
# the site's plain file is as good as its ladder (X, Reddit, most non-YouTube
# sites). See download_video() for the `?` (none-inclusive) filters.
PROGRESSIVE_FORMAT = (
    f"b[ext=mp4][height<={MAX_HEIGHT}][protocol^=http][protocol!*=dash]"
    f"[vcodec!^=?vp][vcodec!^=?av01]"
)


class DownloadPaused(Exception):
    """Raise from a progress_hook to intentionally abort an in-progress
    download (e.g. the user clicked Pause). yt-dlp's downloader resumes
    from partial fragments by default, so a later call with the same URL
    picks back up rather than starting over."""


class DownloadCancelled(DownloadPaused):
    """Raise from a progress_hook when the user cancelled outright.

    A subclass so every "deliberate stop, not a failure" check keeps
    working, but download_video() treats the two differently: a pause keeps
    its partial fragments for Resume, a cancel deletes them. Before this the
    two were one exception, and Cancel left `.part` files in the folder for
    good -- 60 MB of an X video the user explicitly said they did not want.
    """


class LiveStream(Exception):
    """The URL is a broadcast that is still going (or has not started).

    yt-dlp will happily record a live HLS stream, appending segments until
    the broadcaster stops -- hours later, or never. In a download manager
    that shows up as a file whose size climbs forever with no total, which
    is exactly what a user reported. Refusing up front, with a reason, is
    the honest answer until VDR has a real "record live stream" feature.
    """


class PlaylistDetected(Exception):
    """The URL is a whole playlist/channel, not one video.

    Raised before anything is downloaded so the UI can ask, rather than
    silently fetching dozens of videos the user may not have meant.
    """

    def __init__(self, url: str, count: int, title: str = ""):
        self.url = url
        self.count = count
        self.title = title or "This link"
        n = f"{count} videos" if count else "many videos"
        super().__init__(f"{self.title} is a playlist of {n}.")


@dataclass
class MediaInfo:
    """What a pre-flight look at the URL revealed, before any bytes move."""

    title: str = ""
    id: str = ""
    ext: str = "mp4"
    duration: Optional[float] = None
    is_live: bool = False
    is_upcoming: bool = False
    is_playlist: bool = False
    entry_count: int = 0
    extractor: str = ""
    formats: List[dict] = field(default_factory=list)

    def suggested_filename(self) -> str:
        """Mirrors download_video()'s outtmpl closely enough for a row label."""
        title = (self.title or "video")[:150]
        if self.is_playlist:
            n = f" ({self.entry_count} videos)" if self.entry_count else ""
            return f"{title}{n}"
        tag = f" [{self.id}]" if self.id else ""
        return f"{title}{tag}.{self.ext or 'mp4'}"


def inspect(url: str, extra_opts: Optional[dict] = None) -> Optional[MediaInfo]:
    """Resolve title / live-ness / playlist-ness / formats without downloading.

    Best effort: returns None when yt-dlp cannot extract (unknown site, login
    wall, transient error). Callers then proceed exactly as before, so a
    failing pre-flight can never make a download fail that would otherwise
    have worked -- download_video()'s own fallback chain handles those.

    Playlists are read "flat" (one request for the listing, none per entry)
    so a 500-video channel costs one round trip to identify as a playlist.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 20,
        "logger": vdr_log.YtdlpLogger(vdr_log.get_logger()),
        **(extra_opts or {}),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        vdr_log.get_logger().info("pre-flight could not read %s: %s: %s", url, type(e).__name__, e)
        return None
    if not info:
        return None
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        try:
            entries = list(entries)
        except Exception:
            entries = []
        return MediaInfo(
            title=info.get("title") or "",
            id=str(info.get("id") or ""),
            is_playlist=True,
            entry_count=len(entries) or int(info.get("playlist_count") or 0),
            extractor=info.get("extractor_key") or "",
        )
    live_status = info.get("live_status")
    return MediaInfo(
        title=info.get("title") or "",
        id=str(info.get("id") or ""),
        ext=info.get("ext") or "mp4",
        duration=info.get("duration"),
        is_live=bool(info.get("is_live")) or live_status == "is_live",
        is_upcoming=live_status == "is_upcoming",
        extractor=info.get("extractor_key") or "",
        formats=list(info.get("formats") or []),
    )


def _is_progressive(f: dict) -> bool:
    """A single file carrying both video and audio over plain HTTP."""
    if f.get("vcodec") == "none" or f.get("acodec") == "none":
        return False
    proto = str(f.get("protocol") or "")
    if proto.startswith("m3u8") or "dash" in proto or proto in ("ism", "rtmp", "rtsp", "mms"):
        return False
    if (f.get("ext") or "") != "mp4":
        return False
    vcodec = f.get("vcodec")
    # Unknown (None) is fine -- sites like X report no codec for their
    # progressive MP4s, and they are H.264 in practice. A *known* non-H.264
    # codec is not: QuickTime can't play it, which is the whole point.
    return vcodec is None or str(vcodec).startswith(("avc1", "h264"))


def prefers_progressive(formats: List[dict], max_height: int = MAX_HEIGHT) -> bool:
    """Should this video be fetched as one plain MP4 rather than merged streams?

    The default selectors ask for `bv*[vcodec^=avc1]+ba` -- the best H.264
    video stream plus the best audio, merged by ffmpeg. That is right for
    YouTube, whose progressive files stop at 360p/720p. It is wrong for X,
    Reddit and most non-YouTube sites, which publish progressive MP4s at the
    *same* top resolution as their HLS ladder -- there the selector picks the
    HLS variant purely because it happens to carry a codec tag, and the user
    gets a segment-by-segment download with no known total size, an ffmpeg
    remux at the end, and a Size column that drifts upward for the whole
    download. The plain file has an exact Content-Length, needs no merge,
    and is the same picture.

    So: prefer progressive when the best progressive height is at least the
    best height any *other* H.264 stream offers (both capped at max_height).
    """
    if not formats:
        return False

    def height(f):
        return f.get("height") or 0

    prog = [f for f in formats if _is_progressive(f) and height(f) <= max_height]
    if not prog:
        return False
    separate = [
        f for f in formats
        if not _is_progressive(f)
        and f.get("vcodec") not in (None, "none")
        and str(f.get("vcodec")).startswith(("avc1", "h264"))
        and height(f) <= max_height
    ]
    best_prog = max(height(f) for f in prog)
    best_sep = max((height(f) for f in separate), default=0)
    if best_prog == 0 and best_sep == 0:
        # Neither side states a height. A plain file is still the safer
        # download (exact size, no remux), so take it.
        return True
    return best_prog >= best_sep


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


def _detach(err: Exception) -> Exception:
    """Drop an exception's traceback before handing it up.

    A traceback pins every frame it passed through, and for a stop raised
    from inside a progress hook those frames include yt-dlp's fragment
    downloader with its open output file. The exception, its traceback and
    the frames' locals form a reference cycle, so nothing is freed until the
    cyclic collector happens to run -- in an idle GUI process that can be
    minutes. The visible symptom: a `.part` file deleted from the folder but
    still held open by VDR, its 60 MB not returned to the disk until quit.
    A deliberate pause/cancel has no traceback worth keeping.
    """
    err.__traceback__ = None
    return err


def _sidecars(path: str) -> List[str]:
    """The temp files yt-dlp creates alongside a stream it is writing."""
    return [path, path + ".part", path + ".ytdl", path + ".part-Frag0"]


def download_video(
    url: str,
    dest_dir: str,
    quality: str = "best",
    progress_hook: Optional[Callable] = None,
    audio_only: bool = False,
    _resolved: bool = False,
    allow_playlist: bool = False,
    allow_live: bool = False,
    info_cb: Optional[Callable[[MediaInfo], None]] = None,
):
    """Download [url] into [dest_dir], choosing formats and recovering from
    the usual site-specific failures.

    Raises PlaylistDetected / LiveStream before any bytes move when the URL
    is not a single finished video and the caller has not opted in. Raises
    DownloadPaused / DownloadCancelled when the progress hook did (a
    deliberate stop). Anything else that escapes is a real failure, already
    made presentable by _friendly_error().
    """
    os.makedirs(dest_dir, exist_ok=True)

    # Look before downloading. Cheap relative to the download, and it is the
    # only way to answer "is this live?" / "is this 400 videos?" / "what is
    # it called?" before committing. See inspect() for why a failure here is
    # not a failure of the download.
    info = inspect(url)
    if info is not None:
        if info.is_playlist and not allow_playlist:
            raise PlaylistDetected(url, info.entry_count, info.title)
        if info.is_upcoming and not allow_live:
            raise LiveStream(
                f"“{info.title or url}” is a scheduled live stream that hasn't started. "
                "There is nothing to download yet."
            )
        if info.is_live and not allow_live:
            raise LiveStream(
                f"“{info.title or url}” is a live stream that is still broadcasting. "
                "VDR downloads finished videos, so its size would keep growing "
                "until the stream ends. Try again once the broadcast is over."
            )
        if info_cb:
            try:
                info_cb(info)
            except Exception:
                pass

    # Every file this call touches, as reported by yt-dlp itself. This is
    # what a cancel deletes -- not "everything new in the folder", which
    # would also catch a *different* download that finished in between.
    touched: set = set()

    def _hook(d):
        for key in ("filename", "tmpfilename"):
            name = d.get(key)
            if name:
                touched.add(name)
        pp_info = d.get("info_dict") if "postprocessor" in d else None
        if pp_info:
            for key in ("filepath", "_filename"):
                name = pp_info.get(key)
                if name:
                    touched.add(name)
        if progress_hook:
            progress_hook(d)

    ffmpeg_dir = _bundled_ffmpeg_dir()
    base_opts = {
        "outtmpl": os.path.join(dest_dir, "%(title).150B [%(id)s].%(ext)s"),
        "merge_output_format": "mp4",
        **({"ffmpeg_location": ffmpeg_dir} if ffmpeg_dir else {}),
        "progress_hooks": [_hook],
        # progress_hooks only report each fragment's own temp filename (e.g.
        # "...f137.mp4"), which yt-dlp deletes once it's merged. postprocessor_hooks
        # additionally fire with the real final filepath once merging/conversion
        # finishes -- callers need that to know what file actually exists at the end.
        "postprocessor_hooks": [_hook],
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
        # When the site offers a plain MP4 at the same resolution the merged
        # selectors would reach, take the plain file first -- see
        # prefers_progressive() for why. `protocol` filters keep HLS and DASH
        # variants out even when they also carry an mp4 extension. The `?`
        # in the codec filters makes them none-inclusive: X reports no
        # vcodec at all for its progressive files, and without the `?`
        # yt-dlp drops a format whose field is missing, matching nothing.
        if info is not None and prefers_progressive(info.formats):
            attempts.insert(0, {"format": PROGRESSIVE_FORMAT, "postprocessors": []})

    def discard_partials():
        """Delete what this call was writing. Used by cancel and give_up.

        Pause must never come here: its partial fragments are what Resume
        continues from. The list comes from yt-dlp's own hook reports, plus
        the `.part` / `.ytdl` companions it writes beside each stream. It
        used to be "everything new in the folder since we started", which
        with two videos downloading at once meant one failing could delete
        the other's half-finished streams.
        """
        for name in list(touched):
            for path in _sidecars(name):
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass

    def _try(extra_opts) -> Optional[Exception]:
        """Run one attempt. Returns None on success, or the exception raised."""
        try:
            with yt_dlp.YoutubeDL({**base_opts, **extra_opts}) as ydl:
                ydl.add_post_processor(VDRSizeProbePP(ydl), when="before_dl")
                ydl.download([url])
            return None
        except DownloadCancelled as e:
            discard_partials()
            return _detach(e)
        except DownloadPaused as e:
            return _detach(e)
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
        discard_partials()
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
            vdr_log.get_logger().info(
                "login wall on %s -- retrying with %s cookies", url, browser
            )
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
                allow_playlist=allow_playlist,
                allow_live=allow_live,
                info_cb=info_cb,
            )

    give_up(last_err)


_LOGIN_MARKERS = (
    "only works when logged-in",
    "requires authentication",
    "private video",
    "sign in to confirm",
    "members-only",
    "this video is available to this channel's members",
    # yt-dlp's raise_login_required() appends this exact suffix, and each
    # site's own wording varies around it -- Hotstar says "This video is only
    # available for registered users". Before these two, that wall went
    # unrecognised for Hotstar: the browser-cookie retry below never ran at
    # all, and the user got yt-dlp's raw flag-laden error instead.
    "--cookies-from-browser",
    "registered users",
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
    # macOS refuses one app access to another app's data unless the privacy
    # prompt was accepted: Safari's cookie store needs Full Disk Access, and
    # a denied Chrome keychain read surfaces here too. Classifying these as
    # store failures keeps the original, actionable login error on screen
    # instead of "[Errno 1] Operation not permitted: .../Cookies.binarycookies".
    "operation not permitted",
    "permission denied",
    "user interaction is not allowed",
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
            "again. If macOS asks for keychain access (or Safari's cookies "
            "need VDR to have Full Disk Access), allow it and retry."
        )
    return err
