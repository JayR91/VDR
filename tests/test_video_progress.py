"""Progress arithmetic, format choice, pre-flight gating, cancel cleanup.

These pin the behaviours behind two user reports: "the size keeps
increasing" (an HLS estimate shown as a fact, and a two-stream download
whose counter reset between streams) and "it downloaded a video I didn't
ask for" (playlists, live streams, and the wrong media on a page).

No network, no Tk. yt-dlp hook dicts are written by hand from the shapes
yt-dlp actually emits.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import video_progress
from video_progress import VideoProgress, stable_total, format_eta
import video_capture
from video_capture import (
    prefers_progressive, DownloadPaused, DownloadCancelled, LiveStream,
    PlaylistDetected, MediaInfo, _sidecars,
)
import page_media

failures = []


def check(label, cond):
    print(f"{label:<64} -> {'ok' if cond else 'FAIL'}")
    if not cond:
        failures.append(label)


# --- stable totals ------------------------------------------------------------
merged = {"id": "v1", "duration": 100,
          "requested_formats": [{"filesize": 300}, {"filesize": 50}]}
check("merged download sums both streams' exact sizes", stable_total(merged) == (350, False))

approx = {"id": "v1", "requested_formats": [{"filesize": 300}, {"filesize_approx": 60}]}
check("one approximate stream makes the total an estimate", stable_total(approx) == (360, True))

derived = {"id": "v1", "duration": 10, "tbr": 800}  # 800 kbit/s * 10 s = 1_000_000 B
check("bitrate x duration fills in when no size is stated", stable_total(derived) == (1_000_000, True))

check("no size, no bitrate: honestly unknown", stable_total({"id": "v1", "tbr": 800}) == (None, False))
check("empty info is unknown", stable_total(None) == (None, False))

SIZE_PROBE_KEY = video_progress.SIZE_PROBE_KEY


def probe(info):
    """What yt-dlp emits for the before_dl probe: the full info dict."""
    return {"postprocessor": SIZE_PROBE_KEY, "status": "started", "info_dict": info}


def stream(info, **fields):
    """A per-stream hook dict: yt-dlp strips requested_formats from these."""
    per_stream = {k: v for k, v in info.items() if k != "requested_formats"}
    return {"info_dict": per_stream, **fields}


# --- an HLS download whose running estimate drifts ------------------------------
p = VideoProgress()
hls_info = {"id": "x1", "duration": 200, "_filename": "/d/a.mp4", "requested_formats": [
    {"filesize_approx": 66_000_000}, {"filesize_approx": 3_000_000}]}
p.update(probe(hls_info))
check("probe names the final file before any bytes", p.expected_path == "/d/a.mp4")
p.update(stream(hls_info, status="downloading", filename="a.fhls.mp4", downloaded_bytes=1_000_000,
                total_bytes_estimate=20_000_000, fragment_index=3, fragment_count=60, speed=1e6, eta=65))
first_total = p.total
p.update(stream(hls_info, status="downloading", filename="a.fhls.mp4", downloaded_bytes=30_000_000,
                total_bytes_estimate=58_000_000, fragment_index=30, fragment_count=60, speed=1e6, eta=30))
check("HLS total comes from the extractor and does not move with fragments",
      first_total == p.total == 69_000_000)
check("HLS total is flagged as an estimate", p.estimated is True)
check("fragment position is exposed", (p.fragment_index, p.fragment_count) == (30, 60))
check("fraction is 0..1 of the whole download", abs(p.fraction - 30 / 69) < 1e-6)

# --- two-stream merge: bytes accumulate instead of resetting ----------------------
p = VideoProgress()
info = {"id": "y1", "requested_formats": [{"filesize": 300}, {"filesize": 50}]}
p.update(probe(info))
p.update(stream(info, status="downloading", filename="v.f137.mp4", downloaded_bytes=200, total_bytes=300, speed=10.0))
p.update(stream(info, status="finished", filename="v.f137.mp4", downloaded_bytes=300, total_bytes=300))
p.update(stream(info, status="downloading", filename="v.f140.m4a", downloaded_bytes=10, total_bytes=50, speed=5.0))
check("audio stream continues the count, not from zero", p.downloaded == 310)
check("total stays the whole download while the second stream runs", p.total == 350)
check("exact sizes are not an estimate", p.estimated is False)
p.update(stream(info, status="finished", filename="v.f140.m4a", downloaded_bytes=50, total_bytes=50))
p.update({"postprocessor": "Merger", "status": "finished", "info_dict": {"filepath": "/tmp/v.mp4"}})
check("postprocessor reports the merged file", p.final_path == "/tmp/v.mp4")
check("both streams finished -> 100%", p.downloaded == 350 and p.fraction == 1.0)
p.finish(349)
check("finish() adopts the on-disk size as the only truth", (p.total, p.downloaded, p.estimated) == (349, 349, False))

# --- without a probe: two-stream totals still accumulate from the hooks --------------
p = VideoProgress()
p.update(stream(info, status="downloading", filename="v.f137.mp4", downloaded_bytes=200, total_bytes=300))
check("no probe: this stream's exact size is the total for now", (p.total, p.estimated) == (300, False))
p.update(stream(info, status="finished", filename="v.f137.mp4", total_bytes=300))
p.update(stream(info, status="downloading", filename="v.f140.m4a", downloaded_bytes=10, total_bytes=50))
check("no probe: finished stream + current stream, never resetting", (p.downloaded, p.total) == (310, 350))

# --- no extractor total: fall back to the hook, but never below what arrived ---------
p = VideoProgress()
p.update({"status": "downloading", "filename": "z", "downloaded_bytes": 500,
          "total_bytes_estimate": 400, "info_dict": {"id": "z"}})
check("running estimate below bytes arrived is raised to meet them", p.total == 500 and p.estimated)

# --- resume: already-finished stream is banked before the running one ---------------
p = VideoProgress()
p.update(probe(info))
p.update(stream(info, status="finished", filename="v.f137.mp4", total_bytes=300))
p.update(stream(info, status="downloading", filename="v.f140.m4a", downloaded_bytes=25, total_bytes=50))
check("resume: finished stream counted once, total not double-counted",
      (p.downloaded, p.total) == (325, 350))

# --- playlist: totals re-anchor per entry ----------------------------------------
p = VideoProgress()
e1 = {"id": "e1", "filesize": 100}
e2 = {"id": "e2", "filesize": 200}
p.update(probe(e1))
p.update(stream(e1, status="downloading", filename="e1.mp4", downloaded_bytes=100, total_bytes=100))
p.update(stream(e1, status="finished", filename="e1.mp4", total_bytes=100))
p.update(probe(e2))
p.update(stream(e2, status="downloading", filename="e2.mp4", downloaded_bytes=50, total_bytes=200))
check("playlist: second entry's total = banked bytes + its own size", (p.downloaded, p.total) == (150, 300))
p = VideoProgress()
p.update(probe(e1))
p.update(stream(e1, status="finished", filename="e1.mp4", total_bytes=100))
p.update(stream(e2, status="downloading", filename="e2.mp4", downloaded_bytes=50, total_bytes=200))
check("playlist: a new entry before its probe drops the stale anchor", p.total == 300)

# --- eta formatting -------------------------------------------------------------
check("eta formats", (format_eta(45), format_eta(130), format_eta(3780), format_eta(None)) == ("45s", "2m 10s", "1h 03m", ""))

# --- progressive vs merged choice -------------------------------------------------
x_formats = [  # what yt-dlp's twitter extractor returns for a typical post
    {"format_id": "hls-audio-128000", "vcodec": "none", "acodec": "mp4a.40.2", "protocol": "m3u8_native", "ext": "mp4"},
    {"format_id": "hls-632", "vcodec": "avc1.4d001f", "acodec": "mp4a.40.2", "protocol": "m3u8_native", "ext": "mp4", "height": 360},
    {"format_id": "hls-2176", "vcodec": "avc1.640020", "acodec": "mp4a.40.2", "protocol": "m3u8_native", "ext": "mp4", "height": 720},
    {"format_id": "http-632", "protocol": "https", "ext": "mp4", "height": 360, "tbr": 632},
    {"format_id": "http-2176", "protocol": "https", "ext": "mp4", "height": 720, "tbr": 2176},
]
check("X: progressive MP4 at the same height beats the HLS ladder", prefers_progressive(x_formats) is True)

yt_formats = [
    {"format_id": "18", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "protocol": "https", "ext": "mp4", "height": 360},
    {"format_id": "137", "vcodec": "avc1.640028", "acodec": "none", "protocol": "https", "ext": "mp4", "height": 1080},
    {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "protocol": "https", "ext": "m4a"},
]
check("YouTube: 360p progressive does not beat a 1080p merge", prefers_progressive(yt_formats) is False)

hi_prog = [{"format_id": "p", "vcodec": "avc1", "acodec": "mp4a", "protocol": "https", "ext": "mp4", "height": 2160}]
check("progressive above the cap is not a candidate", prefers_progressive(hi_prog) is False)

vp9_prog = [{"format_id": "p", "vcodec": "vp09.00.10.08", "acodec": "opus", "protocol": "https", "ext": "mp4", "height": 720},
            {"format_id": "v", "vcodec": "avc1", "acodec": "none", "protocol": "https", "ext": "mp4", "height": 720}]
check("a known non-H.264 progressive is not preferred", prefers_progressive(vp9_prog) is False)

check("no formats -> no preference", prefers_progressive([]) is False)

# The selector string itself, run through yt-dlp's real parser. X's
# progressive formats have *no* vcodec key; a filter written without `?`
# excludes them and the tier silently matches nothing.
import yt_dlp
selectable = [dict(f, url="u") for f in x_formats] + [
    {"format_id": "dash-hi", "protocol": "http_dash_segments", "ext": "mp4", "height": 1080, "vcodec": "avc1", "acodec": "mp4a", "url": "u"},
    {"format_id": "vp9-prog", "protocol": "https", "ext": "mp4", "height": 1080, "vcodec": "vp09.00", "acodec": "opus", "url": "u"},
]
with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
    picked = [f["format_id"] for f in ydl.build_format_selector(video_capture.PROGRESSIVE_FORMAT)(
        {"formats": selectable, "incomplete_formats": False})]
check("PROGRESSIVE_FORMAT picks X's codec-less 720p MP4 over HLS, DASH and VP9", picked == ["http-2176"])

# --- exception semantics -----------------------------------------------------------
check("cancel is a kind of deliberate stop", issubclass(DownloadCancelled, DownloadPaused))
check("pause is not a cancel", not isinstance(DownloadPaused(), DownloadCancelled))
pd = PlaylistDetected("u", 12, "Mix")
check("playlist message names the count", "12" in str(pd) and pd.count == 12)

# --- MediaInfo row labels -------------------------------------------------------
mi = MediaInfo(title="A" * 200, id="abc", ext="mp4")
check("suggested filename is capped and tagged", mi.suggested_filename() == "A" * 150 + " [abc].mp4")
pl = MediaInfo(title="Lectures", is_playlist=True, entry_count=7)
check("playlist label says how many", pl.suggested_filename() == "Lectures (7 videos)")

# --- sidecars name what yt-dlp writes ------------------------------------------------
check("sidecars cover .part and .ytdl", set(_sidecars("/d/f.mp4")) >= {"/d/f.mp4", "/d/f.mp4.part", "/d/f.mp4.ytdl"})

# --- download_video gates, with inspect() stubbed (no network) ---------------------------
_real_inspect = video_capture.inspect


def stub(info):
    video_capture.inspect = lambda url, extra_opts=None: info


tmp = tempfile.mkdtemp()
try:
    stub(MediaInfo(title="Live now", is_live=True))
    try:
        video_capture.download_video("https://x/live", tmp)
        check("live stream is refused before downloading", False)
    except LiveStream as e:
        check("live stream is refused before downloading", "still broadcasting" in str(e))

    stub(MediaInfo(title="Soon", is_upcoming=True))
    try:
        video_capture.download_video("https://x/soon", tmp)
        check("upcoming stream is refused", False)
    except LiveStream as e:
        check("upcoming stream is refused", "hasn't started" in str(e))

    stub(MediaInfo(title="Mix", is_playlist=True, entry_count=40))
    try:
        video_capture.download_video("https://x/list", tmp)
        check("playlist is refused unless allowed", False)
    except PlaylistDetected as e:
        check("playlist is refused unless allowed", e.count == 40)

    seen = []
    stub(MediaInfo(title="Clip", id="c1"))

    def stop_hook(d):
        raise DownloadCancelled()

    class FakeYDL:
        """Stands in for yt_dlp.YoutubeDL: writes a stream, reports it, then is cancelled."""
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def add_post_processor(self, pp, when=None):
            pass
        def download(self, urls):
            name = os.path.join(tmp, "Clip [c1].f1.mp4")
            with open(name + ".part", "wb") as fh:
                fh.write(b"x" * 10)
            for hook in self.opts["progress_hooks"]:
                hook({"status": "downloading", "filename": name, "tmpfilename": name + ".part",
                      "downloaded_bytes": 10})

    real_ydl = video_capture.yt_dlp.YoutubeDL
    video_capture.yt_dlp.YoutubeDL = FakeYDL
    try:
        try:
            video_capture.download_video("https://x/clip", tmp, progress_hook=stop_hook,
                                         info_cb=lambda i: seen.append(i.title))
            check("cancel propagates as DownloadCancelled", False)
        except DownloadCancelled as e:
            check("cancel propagates as DownloadCancelled", e.__traceback__ is not None)
        check("info_cb received the pre-flight title", seen == ["Clip"])
        check("cancel deleted the partial file yt-dlp reported",
              not os.path.exists(os.path.join(tmp, "Clip [c1].f1.mp4.part")))

        def pause_hook(d):
            raise DownloadPaused()

        try:
            video_capture.download_video("https://x/clip", tmp, progress_hook=pause_hook)
        except DownloadPaused:
            pass
        check("pause keeps the partial file for Resume",
              os.path.exists(os.path.join(tmp, "Clip [c1].f1.mp4.part")))

        # A second, unrelated file in the folder must survive another task's cancel.
        bystander = os.path.join(tmp, "Other [o9].f1.mp4.part")
        with open(bystander, "wb") as fh:
            fh.write(b"y")
        try:
            video_capture.download_video("https://x/clip", tmp, progress_hook=stop_hook)
        except DownloadCancelled:
            pass
        check("cancel leaves other downloads' files alone", os.path.exists(bystander))
    finally:
        video_capture.yt_dlp.YoutubeDL = real_ydl
finally:
    video_capture.inspect = _real_inspect

# --- page_media: declared media outranks the sweep -----------------------------------
html = """
<html><head>
<meta property="og:video" content="https://cdn.example.com/lesson/main_720p.mp4">
<script type="application/ld+json">{"@type":"VideoObject","contentUrl":"https://cdn.example.com/lesson/main.m3u8"}</script>
</head><body>
<video src="/lesson/main_720p.mp4"></video>
<div data-promo="https://cdn.example.com/promo/teaser_1080p.mp4"></div>
</body></html>
"""
declared = page_media.extract_declared_media(html, "https://site.example.com/lesson")
check("og:video, contentUrl and <video src> are all found",
      {u.split("?")[0] for u in declared} >= {"https://cdn.example.com/lesson/main_720p.mp4",
                                             "https://cdn.example.com/lesson/main.m3u8"})
check("relative <video src> is resolved against the page",
      "https://site.example.com/lesson/main_720p.mp4" in declared)
swept = page_media.extract_media_urls(html)
ranked = page_media.rank_media_urls(swept, declared)
check("the declared lesson beats the swept 1080p promo", "/lesson/main" in ranked[0] and "teaser" not in ranked[0])
check("among declared URLs the unlabelled ladder outranks an explicit 720p file",
      ranked[0].endswith("main.m3u8"))
check("the promo is still offered, last", ranked[-1].endswith("teaser_1080p.mp4"))
check("without declarations, resolution still leads",
      "teaser_1080p" in page_media.rank_media_urls(swept)[0])

meta_reversed = '<meta content="https://cdn.example.com/a.mp4" property="og:video:secure_url">'
check("meta with attributes reversed is read", page_media.extract_declared_media(meta_reversed) == ["https://cdn.example.com/a.mp4"])
check("og:image-style pictures are not media", page_media.extract_declared_media(
    '<meta property="og:video" content="https://e.com/poster.jpg">') == [])

# --- how the row reads --------------------------------------------------------------
import gui
from engine import Status
t = gui.VideoTask("https://x/clip", tmp)
t.dest_path = os.path.join(tmp, "Clip.mp4")
t.status = Status.DOWNLOADING
t.total_size, t.size_estimated, t._downloaded, t.speed, t.eta = 69_000_000, True, 30_000_000, 1_000_000, 39
name, size, pct, speed, status = gui.App._row_values(t)
check("estimated size is marked with ~", size == "~65.8MB")
check("progress shows the cumulative fraction against the marked total", pct.startswith("43.5%") and "/~65.8MB" in pct)
check("speed column carries the ETA while downloading", speed == "976.6KB/s, 39s")
t.size_estimated = False
check("measured size has no ~", gui.App._row_values(t)[1] == "65.8MB")
t.status, t.error_message = Status.ERROR, "This video is DRM-protected,\n so it can't be downloaded."
check("error row carries the reason", gui.App._row_values(t)[4].startswith("error — This video is DRM-protected, so"))
t.status, t._downloaded, t.total_size = Status.COMPLETED, 70_000_000, 69_000_000
check("percentage never exceeds 100", gui.App._row_values(t)[2].startswith("100.0%"))

t2 = gui.VideoTask("u", tmp)
t2.status = Status.DOWNLOADING
t2.cancel()
check("cancel marks CANCELLED before signalling the hook", t2.status == Status.CANCELLED and t2.stop_event.is_set())
t3 = gui.VideoTask("u", tmp)
t3.status = Status.ERROR
restarted = []
t3.start_fn = lambda: restarted.append(1)
t3.resume()
check("Resume on an errored video retries it", restarted == [1] and t3.status == Status.QUEUED)
t4 = gui.VideoTask("https://example.com/watch?v=x", tmp)
check("queued row shows the placeholder while the title is pending", gui.App._row_values(t4)[0] == gui.VideoTask.PLACEHOLDER)
t4.status, t4.error_message = Status.ERROR, "boom"
check("errored row without a title falls back to the URL", gui.App._row_values(t4)[0] == "https://example.com/watch?v=x")

print()
if failures:
    print(f"FAIL - {len(failures)} check(s):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS - video progress, format choice, gating, cancel cleanup, page ranking")
