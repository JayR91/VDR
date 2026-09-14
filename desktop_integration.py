"""Pick the right native integration for whatever OS we booted on.

gui.py used to import MacIntegration directly, which meant simply *starting*
VDR on Windows raised ImportError before any window appeared -- PyObjC is
macOS-only and is not installed there. Selecting behind one factory keeps the
GUI free of platform branches and makes "no native affordances available" a
supported state rather than a crash.

Every backend exposes the same surface:
    install_menu_bar() set_dock_badge(str) set_progress(str)
    notify_completion(title, body) play_completion_sound() .available
"""

import platform


class NullIntegration:
    """Used on Linux/BSD, and anywhere the optional deps are absent.

    Downloads, the queue and the local server all work without any of this;
    only the ornamental layer goes quiet.
    """

    def __init__(self, add_url_callback=None, show_callback=None, quit_callback=None):
        self.available = False
        self.status_item = None

    def install_menu_bar(self):
        return

    def set_dock_badge(self, label: str):
        return

    def set_progress(self, text: str):
        return

    def notify_completion(self, title: str, body: str):
        return

    def play_completion_sound(self):
        return


def bind_macos_reopen(root, callback, system=None):
    """Register Tk's Dock-reopen handler. Must not run off macOS.

    Cocoa Tk delivers a click on the Dock icon as the Tcl command
    ``::tk::mac::ReopenApplication``. That namespace does not exist on
    Windows (or Linux) Tk; ``createcommand`` then raises TclError and
    aborts ``App.__init__``. After a successful Setup.exe the window
    never appears, which looks like the installer did nothing.
    """
    if (system or platform.system()) != "Darwin":
        return False
    try:
        root.createcommand("::tk::mac::ReopenApplication", callback)
        return True
    except Exception:
        return False


def create_integration(add_url_callback=None, show_callback=None, quit_callback=None):
    system = platform.system()
    if system == "Darwin":
        from macos_integration import MacIntegration

        return MacIntegration(add_url_callback, show_callback, quit_callback)
    if system == "Windows":
        from windows_integration import WindowsIntegration

        return WindowsIntegration(add_url_callback, show_callback, quit_callback)
    return NullIntegration(add_url_callback, show_callback, quit_callback)
