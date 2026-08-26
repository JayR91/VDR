"""Optional native Windows affordances.

The Windows counterpart to macos_integration.MacIntegration, exposing the
same surface so gui.py never has to care which platform it is on.

Where the two platforms differ, this aims for the *idiomatic Windows*
equivalent rather than a literal translation:

  - macOS puts a utility icon in the menu bar; Windows puts it in the
    notification area ("system tray"). Both are the thing that keeps the app
    reachable after its window is closed.
  - macOS shows download progress as a Dock badge. Windows has no Dock, and
    the taskbar's progress API (ITaskbarList3) is COM-only and needs the
    window handle, which Tk does not hand out portably. The tray tooltip is
    the honest equivalent: it is where Windows users already hover to see
    what a background app is doing.

Everything here degrades to a no-op when pystray/Pillow are missing, exactly
like the macOS side degrades without PyObjC -- the download engine never
depends on any of it.
"""

import platform
import threading


class WindowsIntegration:
    def __init__(self, add_url_callback=None, show_callback=None, quit_callback=None):
        self.available = False
        self.status_item = None
        self._add_url = add_url_callback
        self._show = show_callback
        self._quit = quit_callback
        self._progress_text = ""
        self._icon = None
        self._thread = None
        if platform.system() != "Windows":
            return
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401
            self.available = True
        except ImportError:
            return

    # --- tray icon -------------------------------------------------------

    def _make_image(self, size=64):
        """Draw the tray glyph in code rather than shipping a second asset.

        The installer already carries AppIcon.ico for the executable and Start
        menu; a tray icon has to be a PIL image regardless, so drawing the
        same downward arrow keeps the two from drifting apart.
        """
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        accent = (40, 120, 184, 255)
        # A downward arrow: shaft plus head, matching the "⇩" the macOS
        # menu-bar view draws.
        draw.rectangle([size * 0.42, size * 0.16, size * 0.58, size * 0.60], fill=accent)
        draw.polygon(
            [(size * 0.26, size * 0.55), (size * 0.74, size * 0.55), (size * 0.50, size * 0.84)],
            fill=accent,
        )
        return image

    def install_menu_bar(self):
        """Named after its macOS counterpart so gui.py can call it blindly."""
        if not self.available or self._icon:
            return
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("Show VDR", self._on_show, default=True),
            pystray.MenuItem("Quit VDR", self._on_quit),
        )
        self._icon = pystray.Icon("VDR", self._make_image(), "VDR", menu)
        self.status_item = self._icon

        # The tray pump gets a thread of its own that we start and own.
        #
        # pystray's own run_detached() is the obvious call here and it is the
        # wrong one: it does setup work on the calling thread before handing
        # off, and where there is no interactive desktop to attach a window to
        # -- a headless CI runner, a session-0 service, a locked-down kiosk --
        # that setup does not fail, it blocks. install_menu_bar() is invoked
        # from root.after(), i.e. on Tk's main thread, so blocking there
        # freezes the entire UI before the window has finished coming up. A
        # tray icon is a convenience; it must never be able to take the app
        # hostage.
        #
        # daemon=True matters just as much: a non-daemon tray thread would
        # keep the process alive after the window closes, leaving an
        # invisible VDR that only Task Manager can end.
        def pump():
            try:
                self._icon.run()
            except Exception:
                # No notification area (no Explorer shell, headless session).
                # Losing the tray icon must not take the app down with it.
                self.available = False

        self._thread = threading.Thread(target=pump, name="vdr-tray", daemon=True)
        self._thread.start()

    def _on_show(self, icon=None, item=None):
        if self._show:
            self._show()

    def _on_quit(self, icon=None, item=None):
        # stop() ends the pump thread's message loop; the thread is a daemon
        # either way, so a failure here still cannot wedge the shutdown.
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
        if self._quit:
            self._quit()

    # --- progress / badge ------------------------------------------------

    def set_dock_badge(self, label: str):
        """No Dock on Windows; progress rides the tray tooltip instead.

        Kept as a real method (not dropped) so gui.py's call sites stay
        identical across platforms.
        """
        return

    def set_progress(self, text: str):
        if not (self.available and self._icon):
            return
        if text == self._progress_text:
            return
        self._progress_text = text
        try:
            self._icon.title = f"VDR — {text}" if text else "VDR"
        except Exception:
            pass

    # --- notifications ---------------------------------------------------

    def notify_completion(self, title: str, body: str):
        if self.available and self._icon:
            try:
                # title/body can contain a downloaded video's title, i.e.
                # untrusted text. pystray passes them to the shell balloon as
                # data, never as a command line, so there is nothing to
                # escape here -- unlike the AppleScript path on macOS.
                self._icon.notify(body, title)
                return
            except Exception:
                pass

    def play_completion_sound(self):
        try:
            import winsound

            # Asynchronous: the default (synchronous) flag would block Tk's
            # event loop for the length of the sound.
            winsound.MessageBeep(winsound.MB_OK)
        except Exception:
            pass
