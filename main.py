import os
import threading
import sys
import traceback
import tkinter as tk


def _ensure_homebrew_path():
    """Apps launched from the Dock/Finder don't inherit a Terminal's PATH,
    so Homebrew-installed tools (ffmpeg, deno) silently can't be found even
    though they're on the machine -- only running via Terminal happened to
    work. Add their usual install locations explicitly, before anything
    else (video_capture/yt-dlp) might shell out to them."""
    if sys.platform != "darwin":
        return
    extra = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin"]
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for p in extra:
        if p not in parts and os.path.isdir(p):
            parts.append(p)
    os.environ["PATH"] = os.pathsep.join(parts)


_ensure_homebrew_path()

from queue_manager import QueueManager
from server import create_server, DEFAULT_PORT
from gui import App, DEFAULT_DIR


def _handoff_to_running_instance() -> bool:
    """If VDR is already running, raise its window and report True.

    Without this a second launch still builds a whole second UI -- another
    window, another menu-bar icon -- while its server silently loses the race
    for the port, so the copy you're looking at is not the one the browser
    extension talks to. macOS only dedupes launches of the *same* bundle, which
    doesn't help when a copy is run from somewhere else (a build tree, a DMG).
    """
    import json
    import urllib.request

    base = f"http://127.0.0.1:{DEFAULT_PORT}"
    try:
        with urllib.request.urlopen(f"{base}/ping", timeout=1.5) as resp:
            if json.loads(resp.read().decode() or "{}").get("app") != "vdr":
                return False  # something else is on the port; let the normal path report it
    except Exception:
        return False

    try:
        urllib.request.urlopen(
            urllib.request.Request(f"{base}/show", method="POST"), timeout=1.5
        ).close()
    except Exception:
        pass  # already running is reason enough to bow out, even if /show fails
    return True


def main():
    if _handoff_to_running_instance():
        return

    os.makedirs(DEFAULT_DIR, exist_ok=True)
    qm = QueueManager(max_concurrent=3, global_speed_limit=None)

    root = tk.Tk()
    app = App(root, qm)
    # py2app's argv emulation passes links dropped on the Dock icon here.
    for arg in sys.argv[1:]:
        if arg.startswith(("http://", "https://")):
            root.after(0, lambda url=arg: app.add_url_from_drop(url))

    flask_app = create_server(
        qm, DEFAULT_DIR, video_queue_fn=app.queue_video, show_fn=app.show_window
    )

    def run_server():
        try:
            flask_app.run(host="127.0.0.1", port=DEFAULT_PORT, debug=False, use_reloader=False)
        except Exception as e:
            print(f"Local server failed to start: {e}")
            root.after(0, lambda: app.set_server_status(
                f"Local server failed to start ({e}) — browser extension won't work.", "red"))
            return

    threading.Thread(target=run_server, daemon=True).start()
    app.set_server_status(
        f"Local server running on http://127.0.0.1:{DEFAULT_PORT} — browser extension can connect.", "green")

    root.mainloop()


from crash_report import write_crash_log


def report_crash(text: str) -> None:
    path = write_crash_log(text)
    try:
        root = tk.Tk()
        root.withdraw()
        body = text[-2000:]
        if path:
            body += f"\n\nSaved to:\n{path}"
        from tkinter import messagebox
        messagebox.showerror("VDR failed to start", body)
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        report_crash(traceback.format_exc())
        sys.exit(1)
