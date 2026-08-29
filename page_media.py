"""Find the media a web page embeds, when no yt-dlp extractor covers the site.

yt-dlp knows ~1800 sites by name, and its Generic extractor catches many of
the rest by spotting a <video> tag or a bare playlist link. Neither helps on a
page that hands its player a URL from inside a JSON blob -- which is how most
React/Next.js course sites work now. deeplearning.ai is the example that
prompted this: the lesson page carries both an HLS ladder and a 360p MP4, both
plainly visible in the HTML, and yt-dlp still answers "Unsupported URL".

So: fetch the page, take the media URLs out of it, and hand the best one back
to yt-dlp, which is perfectly happy once it is pointed at a playlist.

This is deliberately narrow. It reads one page over HTTP and pattern-matches
the result. It runs no JavaScript, follows no player APIs, and does nothing to
get past a login or any form of protection -- a page whose media URL only
exists after scripts run stays out of reach, and that is fine.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from typing import List, Optional

# A browser-shaped User-Agent. Several CDNs answer bare urllib with a 403 HTML
# error page, which would otherwise look like "a page with no media in it".
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Cap the read. A page is text; anything past this is not markup we need, and
# an unbounded read on a hostile or misconfigured URL is an easy way to hang.
MAX_BYTES = 3 * 1024 * 1024

MEDIA_EXTS = ("m3u8", "mpd", "mp4", "webm", "mkv", "mov", "m4v")

# Absolute media URLs, optional query string. The trailing boundary stops
# ".webm" matching inside ".webmanifest".
_ABSOLUTE = re.compile(
    r"https?://[^\s\"'<>\\]+?\.(?:" + "|".join(MEDIA_EXTS) + r")(?=[^\w]|$)(?:\?[^\s\"'<>\\]*)?",
    re.IGNORECASE,
)

# `…_360p.mp4`, `…-1080P.m3u8`
_HEIGHT_LABEL = re.compile(r"(?:^|[^0-9])(\d{3,4})p(?:[^0-9]|$)", re.IGNORECASE)
# `…_1280x720.mp4`
_HEIGHT_DIMS = re.compile(r"(?:^|[^0-9])\d{3,4}x(\d{3,4})(?:[^0-9]|$)", re.IGNORECASE)

# What an unlabelled file, or a manifest, is credited with. A manifest carries
# a whole ladder whose top rung is usually the best the site publishes, so it
# is treated the same as an unlabelled file rather than ranked by extension.
# That keeps the comparison to the only question a filename can answer: is
# this one *explicitly* worse than the alternatives?
ASSUMED_HEIGHT = 1080

_NOISE = ("thumb", "poster", "preview", "sprite", "trailer")


def _filename(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    return path.rsplit("/", 1)[-1].lower()


def advertised_height(url: str) -> int:
    """The resolution a URL's filename claims, or [ASSUMED_HEIGHT] if silent.

    Only the filename is read, so this is a hint rather than a measurement --
    but a file calling itself 360p is telling the truth far more often than
    not, and that is the case worth acting on.
    """
    name = _filename(url)
    m = _HEIGHT_LABEL.search(name) or None
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    m = _HEIGHT_DIMS.search(name)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return ASSUMED_HEIGHT


def is_manifest(url: str) -> bool:
    name = _filename(url)
    return name.endswith(".m3u8") or name.endswith(".mpd")


def _rank(url: str):
    """Sort key, best first.

    Resolution leads. Ordering by container instead would put a progressive
    .mp4 above an .m3u8 unconditionally, so a page offering a 360p MP4 beside
    a 1080p ladder would hand over the 360p -- a download that succeeds and is
    simply the worst copy available.

    On a genuine tie the plain file wins: one request and no remux beats
    fetching and stitching segments for the same picture.
    """
    name = _filename(url)
    noisy = any(n in name or n in url.lower() for n in _NOISE)
    return (
        1 if noisy else 0,
        -advertised_height(url),
        1 if is_manifest(url) else 0,
        len(url),
    )


def fetch(page_url: str, timeout: float = 25.0) -> str:
    req = urllib.request.Request(page_url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text" not in ctype and "html" not in ctype and "json" not in ctype:
            # Already a media/binary URL; there is no page here to read.
            return ""
        raw = resp.read(MAX_BYTES)
    charset = "utf-8"
    if "charset=" in ctype:
        charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    return raw.decode(charset, "replace")


def unescape(markup: str) -> str:
    """Undo the escaping these URLs arrive wrapped in.

    This has to happen *before* matching, not after. A URL embedded in a JSON
    blob reads `https:\\u002F\\u002Fcdn...`, which contains no literal `://`
    at all -- so unescaping the regex's output is too late, because the regex
    never matched it in the first place. That is precisely the form these
    course platforms use.
    """
    return (
        markup.replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\/", "/")
        .replace("&amp;", "&")
        .replace("&#x2F;", "/")
    )


def extract_media_urls(markup: str) -> List[str]:
    """Every distinct media URL in [markup], unordered."""
    text = unescape(markup)
    out, seen = [], set()
    for raw in _ABSOLUTE.findall(text):
        key = raw.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(raw)
    return out


def rank_media_urls(urls: List[str]) -> List[str]:
    """Best first."""
    return sorted(urls, key=_rank)


def find_media_urls(page_url: str, timeout: float = 25.0) -> List[str]:
    """Media URLs embedded in [page_url], best first. Empty if none."""
    try:
        html = fetch(page_url, timeout=timeout)
    except Exception:
        return []
    if not html:
        return []

    return rank_media_urls(extract_media_urls(html))


def resolve(page_url: str, timeout: float = 25.0) -> Optional[str]:
    """The single best media URL on [page_url], or None."""
    urls = find_media_urls(page_url, timeout=timeout)
    return urls[0] if urls else None
