"""Ordering and extraction for page_media.

The failure being pinned is a quiet one: picking the wrong candidate does not
error, it downloads the worst copy on the page and reports success.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import page_media

failures = []


def check(label, cond):
    print(f"{label:<56} -> {'ok' if cond else 'FAIL'}")
    if not cond:
        failures.append(label)


B = "https://video.deeplearning.ai/dlai/agentic-ai/W1/L5/lc-agentic"

# --- the real deeplearning.ai shape -----------------------------------------
urls = [f"{B}-master_360p.mp4?v=1759535129", f"{B}-master.m3u8?v=1759535129"]
best = sorted(urls, key=page_media._rank)[0]
check("1080p ladder beats an explicit 360p mp4", best.endswith(".m3u8?v=1759535129"))

# --- resolution parsing -----------------------------------------------------
check("reads _360p", page_media.advertised_height("https://e.com/a_360p.mp4") == 360)
check("reads 1920x1080", page_media.advertised_height("https://e.com/a_1920x1080.mp4") == 1080)
check("unlabelled assumed good",
      page_media.advertised_height("https://e.com/a.mp4") == page_media.ASSUMED_HEIGHT)
check("a version query is not a resolution",
      page_media.advertised_height("https://e.com/a.mp4?v=1759535129") == page_media.ASSUMED_HEIGHT)

# --- tie-breaks -------------------------------------------------------------
tie = sorted(["https://e.com/v/index.m3u8", "https://e.com/v/movie.mp4"], key=page_media._rank)[0]
check("on a tie the plain file wins (no remux)", tie == "https://e.com/v/movie.mp4")

ladder = sorted(["https://e.com/v/clip_360p.mp4", "https://e.com/v/clip_1080p.mp4",
                 "https://e.com/v/clip_720p.mp4"], key=page_media._rank)[0]
check("highest resolution wins among plain files", ladder == "https://e.com/v/clip_1080p.mp4")

noisy = sorted(["https://e.com/v/poster.mp4", "https://e.com/v/movie.mp4"], key=page_media._rank)[0]
check("posters and thumbnails sort last", noisy == "https://e.com/v/movie.mp4")

check("manifest detection", page_media.is_manifest("https://e.com/a.m3u8?x=1")
      and page_media.is_manifest("https://e.com/a.mpd")
      and not page_media.is_manifest("https://e.com/a.mp4"))

# --- extraction from markup, including escaped JSON -------------------------
html = (
    '<html><script>{"hls":"https:\\u002F\\u002Fcdn.example\\u002Fv\\u002Fshow-master.m3u8",'
    '"mp4":"https://cdn.example/v/show_360p.mp4"}</script>'
    '<img src="https://cdn.example/v/thumb.jpg"></html>'
)
found = page_media.rank_media_urls(page_media.extract_media_urls(html))
check("unescapes \\u002F in JSON blobs", any(x.endswith("show-master.m3u8") for x in found))
check("ladder ranked above the 360p", found and found[0].endswith("show-master.m3u8"))
check("a .jpg is not treated as media", not any(x.endswith(".jpg") for x in found))

# --- .webm must not match inside .webmanifest -------------------------------
check("boundary: .webmanifest is not media",
      not page_media._ABSOLUTE.findall('<link href="https://e.com/site.webmanifest">'))

# --- the engine must never save a web page as a file ------------------------
import engine

def _ct(value):
    """Would the engine accept this Content-Type as a downloadable file?"""
    try:
        engine._reject_web_page(value, "https://e.com/x")
        return True
    except engine.PageNotAFile:
        return False

print()
check("text/html is refused", not _ct("text/html"))
check("text/html with charset is refused", not _ct("text/html; charset=utf-8"))
check("application/xhtml+xml is refused", not _ct("application/xhtml+xml"))
check("video/mp4 is allowed", _ct("video/mp4"))
check("application/x-mpegURL is allowed", _ct("application/x-mpegURL"))
check("application/zip is allowed", _ct("application/zip"))
check("application/pdf is allowed", _ct("application/pdf"))
# Left alone deliberately: legitimately downloadable, and some servers
# mislabel media with them.
check("application/json is allowed", _ct("application/json"))
check("application/xml is allowed", _ct("application/xml"))
check("a missing Content-Type is allowed", _ct(None))

if failures:
    print(f"\nFAIL - {len(failures)} check(s): " + ", ".join(failures))
    sys.exit(1)
print("\nPASS - page media selection + web-page guard")
