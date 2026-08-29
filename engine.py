import os
import json
import time
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable, List

import requests


class Status(Enum):
    QUEUED = "queued"
    SCHEDULED = "scheduled"
    CONNECTING = "connecting"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    HELD = "focus hold"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


class TokenBucket:
    """Global bandwidth throttle shared across all active segments/tasks."""

    def __init__(self, rate_bytes_per_sec: Optional[int]):
        self.rate = rate_bytes_per_sec
        # Start with a small burst allowance (not a full second's worth) so a
        # freshly-applied limit is felt almost immediately rather than after
        # draining a full-second head start.
        self.tokens = min(rate_bytes_per_sec or 0, 65536)
        self.lock = threading.Lock()
        self.last = time.monotonic()

    def set_rate(self, rate_bytes_per_sec: Optional[int]):
        with self.lock:
            self.rate = rate_bytes_per_sec
            self.tokens = rate_bytes_per_sec or 0
            self.last = time.monotonic()

    def consume(self, amount: int, pause_event: Optional[threading.Event] = None,
                cancel_event: Optional[threading.Event] = None):
        if not self.rate:
            return  # unlimited
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return
            if pause_event is not None and not pause_event.is_set():
                # Paused mid-throttle-wait: block here (in small slices) rather than
                # sleeping through the pause and writing stale data once resumed.
                pause_event.wait(timeout=0.2)
                continue
            with self.lock:
                now = time.monotonic()
                elapsed = now - self.last
                self.last = now
                self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
                if self.tokens >= amount:
                    self.tokens -= amount
                    return
                needed = amount - self.tokens
                wait = needed / self.rate
            time.sleep(min(wait, 0.2))


@dataclass
class PageNotAFile(Exception):
    """The URL serves a web page, not a downloadable file.

    Saving it anyway is the worst outcome available: the download "succeeds",
    and what lands is ~130 KB of markup wearing the page's name. That is
    exactly what happened with course pages -- five copies of
    `agentic-ai-applications` in Downloads/VDR/Other, each one the HTML.

    Carries the URL so the caller can do the useful thing instead: look inside
    the page for the media it embeds.
    """

    def __init__(self, url: str):
        super().__init__("This link is a web page, not a file.")
        self.url = url


def _reject_web_page(content_type, url):
    """Raise if the server is offering markup rather than a file.

    Deliberately narrow. Only text/html and application/xhtml+xml count --
    application/xml and application/json are left alone, since those are
    legitimately downloadable and some servers mislabel media with them.
    """
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct in ("text/html", "application/xhtml+xml"):
        raise PageNotAFile(url)


class SegmentState:
    index: int
    start: int
    end: int  # -1 means unknown total length (single-stream fallback)
    downloaded: int = 0


class DownloadTask:
    def __init__(
        self,
        url: str,
        dest_path: str,
        num_segments: int = 8,
        headers: Optional[dict] = None,
        max_retries: int = 5,
        bucket: Optional[TokenBucket] = None,
        progress_cb: Optional[Callable] = None,
        status_cb: Optional[Callable] = None,
    ):
        self.url = url
        self.dest_path = dest_path
        self.state_path = dest_path + ".vdrstate.json"
        self.num_segments = num_segments
        self.headers = headers or {}
        self.max_retries = max_retries
        self.bucket = bucket
        self.progress_cb = progress_cb
        self.status_cb = status_cb

        self.total_size: Optional[int] = None
        self.accept_ranges = False
        self.segments: List[SegmentState] = []
        self.status = Status.QUEUED
        self.error_message = ""
        # Set when the server answered with markup instead of a file.
        self.is_web_page = False

        self.pause_event = threading.Event()
        self.pause_event.set()  # set = running, cleared = paused
        self.cancel_event = threading.Event()

        self.lock = threading.Lock()
        self.start_time = None
        self.downloaded_at_start = 0
        self.speed = 0.0
        self._user_paused = False

    # ---------- status / persistence ----------

    def _set_status(self, status: Status, error: str = ""):
        self.status = status
        self.error_message = error
        if self.status_cb:
            self.status_cb(self)

    def _probe(self):
        resp = requests.head(self.url, headers=self.headers, allow_redirects=True, timeout=15)
        resp.raise_for_status()
        _reject_web_page(resp.headers.get("Content-Type"), self.url)
        cl = resp.headers.get("Content-Length")
        self.total_size = int(cl) if cl else None
        self.accept_ranges = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
        if self.total_size is None:
            with requests.get(self.url, headers=self.headers, stream=True, timeout=15) as r:
                r.raise_for_status()
                _reject_web_page(r.headers.get("Content-Type"), self.url)
                cl = r.headers.get("Content-Length")
                if cl:
                    self.total_size = int(cl)
                if r.headers.get("Accept-Ranges", "").lower() == "bytes":
                    self.accept_ranges = True

    def _load_state(self) -> bool:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    data = json.load(f)
                self.total_size = data["total_size"]
                self.segments = [SegmentState(**s) for s in data["segments"]]
                return True
            except Exception:
                return False
        return False

    def _save_state(self):
        with self.lock:
            data = {
                "url": self.url,
                "total_size": self.total_size,
                "segments": [s.__dict__ for s in self.segments],
            }
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def _cleanup_state(self):
        try:
            if os.path.exists(self.state_path):
                os.remove(self.state_path)
        except Exception:
            pass

    def _init_segments(self):
        if not self.total_size or not self.accept_ranges:
            self.segments = [SegmentState(0, 0, -1)]
            self.num_segments = 1
            return
        seg_size = self.total_size // self.num_segments
        segments = []
        start = 0
        for i in range(self.num_segments):
            end = self.total_size - 1 if i == self.num_segments - 1 else start + seg_size - 1
            segments.append(SegmentState(i, start, end))
            start = end + 1
        self.segments = segments

    def bytes_downloaded(self) -> int:
        with self.lock:
            return sum(s.downloaded for s in self.segments)

    def _all_segments_complete(self) -> bool:
        return all((s.end != -1 and s.start + s.downloaded > s.end) for s in self.segments)

    # ---------- lifecycle ----------

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self.cancel_event.clear()
            self.pause_event.set()
            self._set_status(Status.CONNECTING)

            resumed = self._load_state()
            if not resumed:
                self._probe()
                self._init_segments()
                os.makedirs(os.path.dirname(self.dest_path) or ".", exist_ok=True)
                with open(self.dest_path, "wb") as f:
                    if self.total_size:
                        f.truncate(self.total_size)
                self._save_state()
            elif not os.path.exists(self.dest_path):
                with open(self.dest_path, "wb") as f:
                    if self.total_size:
                        f.truncate(self.total_size)

            self._set_status(Status.DOWNLOADING)
            self.start_time = time.monotonic()
            self.downloaded_at_start = self.bytes_downloaded()

            threads = []
            for seg in self.segments:
                if seg.end != -1 and seg.start + seg.downloaded > seg.end:
                    continue
                t = threading.Thread(target=self._download_segment, args=(seg,), daemon=True)
                threads.append(t)
                t.start()

            stop_monitor = threading.Event()

            def monitor():
                while not stop_monitor.is_set():
                    time.sleep(1)
                    elapsed = time.monotonic() - self.start_time
                    if elapsed > 0:
                        self.speed = (self.bytes_downloaded() - self.downloaded_at_start) / elapsed
                    if self.progress_cb:
                        self.progress_cb(self)
                    self._save_state()

            mon_thread = threading.Thread(target=monitor, daemon=True)
            mon_thread.start()

            for t in threads:
                t.join()
            stop_monitor.set()
            mon_thread.join()

            if self.cancel_event.is_set():
                self._set_status(Status.CANCELLED)
                self._cleanup_state()
                return

            if self.status in (Status.PAUSED, Status.HELD):
                self._save_state()
                return

            if self._all_segments_complete() or (self.total_size is None and self.status != Status.ERROR):
                self._set_status(Status.COMPLETED)
                self._cleanup_state()
            elif self.status != Status.ERROR:
                self._set_status(Status.ERROR, "Incomplete download")
        except PageNotAFile as e:
            # Flagged rather than only described, so the UI can do the useful
            # thing -- look inside the page for its media -- instead of making
            # the user read an error and retry by hand.
            self.is_web_page = True
            self._set_status(Status.ERROR, str(e))
        except Exception as e:
            self._set_status(Status.ERROR, str(e))

    def _download_segment(self, seg: SegmentState):
        attempt = 0
        while attempt <= self.max_retries:
            if self.cancel_event.is_set():
                return
            try:
                self.pause_event.wait()
                if self.cancel_event.is_set():
                    return
                range_start = seg.start + seg.downloaded
                headers = dict(self.headers)
                if seg.end != -1:
                    if range_start > seg.end:
                        return
                    headers["Range"] = f"bytes={range_start}-{seg.end}"
                elif range_start > 0:
                    headers["Range"] = f"bytes={range_start}-"

                with requests.get(self.url, headers=headers, stream=True, timeout=30) as r:
                    r.raise_for_status()
                    with open(self.dest_path, "r+b") as f:
                        f.seek(range_start)
                        for chunk in r.iter_content(chunk_size=65536):
                            if self.cancel_event.is_set():
                                return
                            self.pause_event.wait()
                            if not chunk:
                                continue
                            if self.bucket:
                                self.bucket.consume(len(chunk), self.pause_event, self.cancel_event)
                            # re-check: consume() may have blocked through a pause/cancel
                            if self.cancel_event.is_set():
                                return
                            self.pause_event.wait()
                            f.write(chunk)
                            with self.lock:
                                seg.downloaded += len(chunk)
                return
            except Exception as e:
                attempt += 1
                if attempt > self.max_retries:
                    self._set_status(Status.ERROR, f"Segment {seg.index} failed after {self.max_retries} retries: {e}")
                    return
                # wait() (not sleep()) so a cancel during backoff takes effect
                # immediately instead of leaving Cancel looking hung for up to 30s.
                if self.cancel_event.wait(timeout=min(2 ** attempt, 30)):
                    return

    def pause(self):
        if self.status in (Status.DOWNLOADING, Status.CONNECTING, Status.HELD):
            self._user_paused = True
            self.pause_event.clear()
            self._set_status(Status.PAUSED)
            self._save_state()

    def hold_for_focus(self):
        """Pause because Focus Guard says so — not a user pause."""
        if self._user_paused:
            return
        if self.status in (Status.DOWNLOADING, Status.CONNECTING):
            self.pause_event.clear()
            self._set_status(Status.HELD)
            self._save_state()

    def release_from_focus(self):
        if self.status != Status.HELD or self._user_paused:
            return
        self._continue_download()

    def resume(self):
        if self.status == Status.PAUSED:
            self._user_paused = False
            self._continue_download()
        elif self.status == Status.HELD:
            self.release_from_focus()
        elif self.status == Status.ERROR:
            self.start()

    def _continue_download(self):
        self.pause_event.set()
        self._set_status(Status.DOWNLOADING)
        self.start_time = time.monotonic()
        self.downloaded_at_start = self.bytes_downloaded()

        def monitor():
            while self.status == Status.DOWNLOADING:
                time.sleep(1)
                elapsed = time.monotonic() - self.start_time
                if elapsed > 0:
                    self.speed = (self.bytes_downloaded() - self.downloaded_at_start) / elapsed
                if self.progress_cb:
                    self.progress_cb(self)
                self._save_state()

        threading.Thread(target=monitor, daemon=True).start()

    def cancel(self):
        self.cancel_event.set()
        self.pause_event.set()
        self._set_status(Status.CANCELLED)
        self._cleanup_state()
