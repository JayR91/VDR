"""One stable progress view over yt-dlp's progress hooks.

yt-dlp reports progress per *stream*, not per download, and only knows a
stream's exact size when the server sends a Content-Length. Fed straight into
a Size column that produced two things users read as bugs:

  * For HLS/DASH the hook carries `total_bytes_estimate`, recomputed after
    every fragment from the average so far. The "Size" of a 20-minute X
    video therefore climbed for twenty minutes. The download was fine; the
    number was a running guess presented as a fact.
  * A merged video+audio download is two streams. `downloaded_bytes` drops
    back to zero when the audio starts, so the row jumped from 300 MB to
    0 B, and the total shown was whichever stream was current -- the
    small audio one, at the end.

This class turns the hook dicts into a single cumulative view: bytes done
across all streams, one total for the whole download, and an honest flag
saying whether that total is measured or estimated. The total prefers what
the extractor already knew before downloading (each requested format's
`filesize`, or `filesize_approx`, or bitrate x duration), because those do
not move while the bytes arrive.

Pure Python, no Tk, no yt-dlp import -- so the arithmetic can be tested with
hand-written hook dicts.
"""

from __future__ import annotations

from typing import Optional, Tuple

# The postprocessor key video_capture registers as a `before_dl` probe. Its
# hook dicts carry the *whole* info dict -- with `requested_formats` -- which
# the per-stream progress hooks never do (yt-dlp deletes that key before
# handing each stream to its downloader). It is the one moment the full
# download's size can be read before the bytes move.
SIZE_PROBE_KEY = "VDRSizeProbe"


def stable_total(info: Optional[dict]) -> Tuple[Optional[int], bool]:
    """Best pre-download estimate for the whole download.

    Returns (bytes, is_estimate). `bytes` is None when no format states a
    size *and* no bitrate/duration pair exists to derive one. A merged
    download lists its streams under `requested_formats`; a single-stream
    download is described by the info dict itself.
    """
    if not info:
        return None, False
    formats = info.get("requested_formats") or [info]
    duration = info.get("duration")
    total = 0
    estimate = False
    for f in formats:
        n = f.get("filesize")
        if not n:
            n = f.get("filesize_approx")
            estimate = True
        if not n:
            tbr = f.get("tbr")
            if tbr and duration:
                n = tbr * 1000 / 8 * duration
                estimate = True
        if not n:
            return None, False
        total += n
    return int(total), estimate


class VideoProgress:
    """Accumulates hook dicts into download-wide numbers.

    Feed every progress_hook and postprocessor_hook dict to update(); read
    the attributes afterwards. Attributes are plain values so a UI thread
    can copy them without locking anything.
    """

    def __init__(self):
        self.downloaded = 0            # bytes across all streams so far
        self.total: Optional[int] = None
        self.estimated = False         # True when `total` is a guess
        self.speed = 0.0               # bytes/s, current stream
        self.eta: Optional[float] = None  # seconds, current stream
        self.fragment_index: Optional[int] = None
        self.fragment_count: Optional[int] = None
        self.current_filename: Optional[str] = None
        self.expected_path: Optional[str] = None  # the final name, known before download
        self.final_path: Optional[str] = None  # set once merging/conversion is done
        self.streams_done = 0
        self._done_bytes = 0           # sum of finished streams
        self._stable: Optional[int] = None
        self._stable_estimate = False
        self._stable_id = None

    # ---- hooks -----------------------------------------------------------

    def update(self, d: dict) -> None:
        if "postprocessor" in d:
            self._postprocessor(d)
            return
        status = d.get("status")
        filename = d.get("filename")
        if filename:
            self.current_filename = filename
        info = d.get("info_dict")
        if info and self._stable_id is not None and info.get("id") != self._stable_id:
            # A different video started (playlist) and the probe for it has
            # not spoken yet: the old anchor describes the wrong video.
            self._stable_id = None
            self._stable = None
            self._stable_estimate = False

        if status == "downloading":
            current = d.get("downloaded_bytes") or 0
            self.downloaded = self._done_bytes + current
            self.speed = d.get("speed") or 0.0
            self.eta = d.get("eta")
            self.fragment_index = d.get("fragment_index")
            self.fragment_count = d.get("fragment_count")
            self._recompute_total(d)
        elif status == "finished":
            done = d.get("total_bytes") or d.get("downloaded_bytes") or 0
            self._done_bytes += done
            self.streams_done += 1
            self.downloaded = self._done_bytes
            self.speed = 0.0
            self.eta = None
            self._recompute_total(d)

    def _postprocessor(self, d: dict) -> None:
        info = d.get("info_dict") or {}
        if d.get("postprocessor") == SIZE_PROBE_KEY:
            if d.get("status") == "started":
                self.expect(info)
            return
        if d.get("status") != "finished":
            return
        path = info.get("filepath") or info.get("_filename")
        if path:
            self.final_path = path

    def expect(self, info: dict) -> None:
        """Anchor the total on the full, post-selection info dict.

        Called from the size probe with the top-level info (all requested
        formats present). Overrides whatever a per-stream hook may have
        guessed, and is re-anchored per video so playlists stay right.
        """
        entry_id = info.get("id")
        est, is_estimate = stable_total(info)
        self._stable_id = entry_id
        self._stable = (self._done_bytes + est) if est else None
        self._stable_estimate = is_estimate
        if self._stable:
            self.total = max(self._stable, self.downloaded)
            self.estimated = is_estimate
        path = info.get("_filename") or info.get("filepath")
        if path:
            self.expected_path = path

    # ---- totals ----------------------------------------------------------

    def _recompute_total(self, d: dict) -> None:
        """Pick the steadiest total available.

        Order of trust: a pre-download total from the extractor (does not
        move), then finished streams plus this stream's exact Content-Length,
        then finished streams plus yt-dlp's running estimate. Whatever wins
        is never allowed to sit below what has already arrived.
        """
        if self._stable:
            total, estimate = self._stable, self._stable_estimate
        else:
            exact = d.get("total_bytes")
            guess = d.get("total_bytes_estimate")
            if exact:
                total, estimate = self._done_bytes + exact, False
            elif guess:
                total, estimate = self._done_bytes + int(guess), True
            else:
                total, estimate = None, False
        if total is not None and total < self.downloaded:
            total = self.downloaded
            estimate = True
        self.total = total
        self.estimated = estimate

    # ---- derived ---------------------------------------------------------

    @property
    def fraction(self) -> Optional[float]:
        """0..1 of the whole download, or None when the total is unknown."""
        if not self.total:
            return None
        return min(1.0, self.downloaded / self.total)

    def finish(self, final_size: Optional[int]) -> None:
        """The file exists now; its size on disk is the only honest total."""
        if final_size is None:
            return
        self.total = final_size
        self.downloaded = final_size
        self.estimated = False
        self.speed = 0.0
        self.eta = None


def format_eta(seconds: Optional[float]) -> str:
    """'2m 10s' / '45s' / '1h 03m'; empty when unknown."""
    if seconds is None or seconds < 0:
        return ""
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s}s"
