"""Runtime log file.

VDR had no runtime log at all. A frozen GUI build (console=False) has no
stdout or stderr -- `sys.stderr` is None -- so every diagnostic the code
already wrote went nowhere:

  * gui.py's worker thread called traceback.print_exc() when a download
    failed. In the windowed build that call raises AttributeError on the
    None stream, which killed the worker thread *before* it could put the
    error on the event queue -- so the row went red with no message, and
    nothing was recorded anywhere. That is precisely the "it just stops and
    says error" a user sees and cannot act on.
  * yt-dlp's own errors were discarded entirely (quiet=True, no logger).

crash_report.py covers a failure to *start*. This covers everything after,
and writes next to it so there is one place to look.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from crash_report import crash_log_path

_LOGGER_NAME = "vdr"
_configured = False


def log_path() -> str:
    """Beside crash.log, so the two live in one directory the user can find."""
    return os.path.join(os.path.dirname(crash_log_path()), "vdr.log")


def get_logger() -> logging.Logger:
    """The app's logger, configured on first use.

    Never raises: a read-only or missing log directory must not stop VDR from
    downloading, so a failure to open the file degrades to logging nowhere.
    """
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        path = log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Rotating, because a long-lived download manager logging every
        # failure would otherwise grow without bound.
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
        )
        logger.addHandler(handler)
    except Exception:
        logger.addHandler(logging.NullHandler())

    # When there *is* a console (running from source, or the console build),
    # keep printing there too -- that is where a developer is already looking.
    if getattr(sys, "stderr", None) is not None and not getattr(sys, "frozen", False):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        logger.addHandler(stream)

    _configured = True
    return logger


class YtdlpLogger:
    """Adapter for yt-dlp's `logger` option.

    yt-dlp writes progress and status through this interface. Without one it
    writes to stdout/stderr, which in a windowed build are None -- so as well
    as losing the messages, its own writes can raise. Routing them here keeps
    the UI quiet while making the reason for a failure recoverable.
    """

    def __init__(self, logger: logging.Logger):
        self._log = logger

    def debug(self, msg):
        # yt-dlp sends both real debug output and ordinary "[download] ..."
        # status lines here; neither is worth a line in a user-facing log.
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self._log.warning("yt-dlp: %s", msg)

    def error(self, msg):
        self._log.error("yt-dlp: %s", msg)
