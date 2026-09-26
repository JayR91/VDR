import os
import subprocess
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


def _stdin_needs_pipe_for_macos_tk(platform=None, stdin_is_tty=None, environ=None):
    """Whether fd 0 should be replaced with a pipe before Tk starts (macOS).

    Tk's macOS init opens a hidden console window whenever stdin is nullish,
    and a /dev/null stdin is exactly how Finder/Dock launches arrive. That
    console path runs console.tcl, which builds a menubar and hands it to
    Tk's tkSetMainMenu before the default application menu exists; on
    2026-09-26 that aborted the process (NSMenuItem initWithTitle: assertion
    inside +[NSMenuItem(TKUtils) itemWithSubmenu:], see the crash report).
    A terminal launch never takes that path, because a tty stdin is not
    nullish -- which is also why running from source never showed this.

    VDR reads no stdin, so hand fd 0 one end of a pipe: non-nullish and
    non-tty skips the console entirely and every launch behaves like a
    terminal one. TK_CONSOLE is honoured as-is -- if it is set, the user
    asked for the console.
    """
    plat = sys.platform if platform is None else platform
    if plat != "darwin":
        return False
    env = os.environ if environ is None else environ
    if env.get("TK_CONSOLE"):
        return False
    if stdin_is_tty is None:
        try:
            stdin_is_tty = bool(sys.stdin and sys.stdin.isatty())
        except Exception:
            stdin_is_tty = False
    return not stdin_is_tty


def _pipe_over_stdin():
    """Point fd 0 at a pipe (see _stdin_needs_pipe_for_macos_tk)."""
    try:
        read_fd, write_fd = os.pipe()
        os.dup2(read_fd, 0)
        os.close(read_fd)
        # Keep the write end open for the life of the process, so a stray
        # read blocks like a terminal would instead of seeing EOF.
        globals().setdefault("_stdin_pipe_write_fd", write_fd)
    except OSError:
        pass


def main():
    if _stdin_needs_pipe_for_macos_tk():
        _pipe_over_stdin()
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
    """Show a startup failure without building a second Tk root.

    The old version always created a fresh tk.Tk() to host the messagebox.
    On macOS, creating a new Tk root while one already exists is a documented
    tkinter crash (python/cpython#123204): Tk Aqua keeps a reference to the
    first interpreter and aborts while rebuilding menus. That is the same
    abort-inside-NSMenuItem shape as the 2026-09-26 crash report, so the
    alert goes through osascript instead, which touches no Tk state at all.
    """
    path = write_crash_log(text)
    body = text[-2000:]
    if path:
        body += f"\n\nSaved to:\n{path}"
    if sys.platform == "darwin":
        try:
            subprocess.run([
                "osascript",
                "-e", "on run argv",
                "-e", "display alert (item 1 of argv) message (item 2 of argv) as critical",
                "-e", "end run",
                "VDR failed to start", body,
            ])
        except Exception:
            pass
        return
    try:
        root = tk.Tk()
        root.withdraw()
        from tkinter import messagebox
        messagebox.showerror("VDR failed to start", body)
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    # Windows Setup runs `VDR.exe --install-browser-extension` (and the
    # uninstaller the matching --uninstall flag). That must not open the Tk
    # window or write crash.log — it is a silent registry/file copy. On macOS
    # nothing runs it automatically; there it is the one-time setup helper a
    # user runs, so it also reveals the extension folder and opens
    # chrome://extensions (see extension_install.install_from_cli).
    if "--install-browser-extension" in sys.argv or "--uninstall-browser-extension" in sys.argv:
        try:
            from extension_install import install_from_cli
            ok = install_from_cli(uninstall="--uninstall-browser-extension" in sys.argv)
            sys.exit(0 if ok else 1)
        except Exception:
            traceback.print_exc()
            sys.exit(1)
    try:
        main()
    except Exception:
        report_crash(traceback.format_exc())
        sys.exit(1)
