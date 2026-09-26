"""Optional native macOS affordances.

The download engine remains usable without PyObjC.  On macOS, installing the
optional dependency enables a Dock badge, native notifications/chime, a menu
bar utility, dropping URLs onto its menu-bar icon, and a one-time
"Setup Browser Extension…" item that refreshes the unpacked extension, shows
its folder and opens Chrome's extensions page.
"""

import platform
import subprocess


class MacIntegration:
    def __init__(self, add_url_callback=None, show_callback=None, quit_callback=None,
                 setup_callback=None):
        self.available = False
        self.status_item = None
        self._add_url = add_url_callback
        self._show = show_callback
        self._quit = quit_callback
        self._setup = setup_callback
        self._progress_text = ""
        self._view = None
        if platform.system() != "Darwin":
            return
        try:
            from AppKit import (
                NSApplication,
                NSApplicationActivationPolicyRegular,
                NSStatusBar,
                NSVariableStatusItemLength,
            )
            self.NSApplication = NSApplication
            self.NSStatusBar = NSStatusBar
            self.NSVariableStatusItemLength = NSVariableStatusItemLength
            self.available = True
            # VDR is a normal Dock-visible app: the Dock icon is how you get
            # the window back after closing it, and a visible Dock tile is
            # also what makes the download-percentage badge render at all.
            # Set the policy explicitly rather than relying on the default --
            # it keeps this correct regardless of what Tk's own Cocoa init
            # decides, and makes the intent obvious next to install_menu_bar().
            #
            # Closing the window does NOT quit: WM_DELETE_WINDOW just hides it
            # (see gui.py) so downloads and the local server keep running, and
            # clicking the Dock icon reopens it.
            app = NSApplication.sharedApplication()
            app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
            app.activateIgnoringOtherApps_(True)
        except ImportError:
            return

    def install_menu_bar(self):
        if not self.available or self.status_item:
            return
        import objc
        from AppKit import NSMenu, NSMenuItem, NSView, NSPasteboardTypeURL, NSPasteboardTypeFileURL
        from Foundation import NSObject
        integration = self

        class DropView(NSView):
            def initWithFrame_(self, frame):
                self = objc.super(DropView, self).initWithFrame_(frame)
                if self is not None:
                    self.registerForDraggedTypes_([NSPasteboardTypeURL, NSPasteboardTypeFileURL])
                return self

            def draggingEntered_(self, sender):
                # NSDragOperationCopy; using its numeric value avoids an SDK-version import.
                return 1

            def performDragOperation_(self, sender):
                board = sender.draggingPasteboard()
                value = board.stringForType_(NSPasteboardTypeURL) or board.stringForType_(NSPasteboardTypeFileURL)
                if value and integration._add_url:
                    integration._add_url(str(value))
                    return True
                return False

            def mouseDown_(self, event):
                if integration.status_item and integration._menu:
                    integration.status_item.popUpStatusItemMenu_(integration._menu)

            def drawRect_(self, rect):
                from AppKit import NSColor, NSFont, NSAttributedString
                NSColor.clearColor().set()
                # Keep the view recognisable even in a crowded menu bar.
                NSAttributedString.alloc().initWithString_attributes_("⇩", {"NSFont": NSFont.systemFontOfSize_(16)}).drawAtPoint_((4, 1))
                if integration._progress_text:
                    NSAttributedString.alloc().initWithString_attributes_(
                        integration._progress_text, {"NSFont": NSFont.systemFontOfSize_(12)}
                    ).drawAtPoint_((22, 4))

        self._view = DropView.alloc().initWithFrame_(((0, 0), (26, 22)))
        self.status_item = self.NSStatusBar.systemStatusBar().statusItemWithLength_(26)
        self.status_item.setView_(self._view)
        menu = NSMenu.alloc().init()
        open_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Show VDR", "showWindow:", "")
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit VDR", "quitApp:", "")

        class Actions(NSObject):
            def showWindow_(self, sender):
                if integration._show:
                    integration._show()
            def setupExtension_(self, sender):
                if integration._setup:
                    integration._setup()
            def quitApp_(self, sender):
                if integration._quit:
                    integration._quit()

        self._actions = Actions.alloc().init()
        open_item.setTarget_(self._actions)
        quit_item.setTarget_(self._actions)
        menu.addItem_(open_item)
        if self._setup:
            # The one-time "Load unpacked" click is unavoidable on current
            # Chromium; this item does the rest of the walkthrough --
            # refresh the files, reveal the folder, open the extensions page.
            setup_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Setup Browser Extension…", "setupExtension:", ""
            )
            setup_item.setTarget_(self._actions)
            menu.addItem_(setup_item)
        menu.addItem_(quit_item)
        self._menu = menu
        self.status_item.setMenu_(menu)

    def set_dock_badge(self, label: str):
        if self.available:
            self.NSApplication.sharedApplication().dockTile().setBadgeLabel_(label or None)

    def set_progress(self, text: str):
        """Show download progress next to the menu-bar icon.

        Complements set_dock_badge rather than replacing it: the Dock tile
        carries the percentage too, but the menu-bar copy stays visible when
        the Dock is hidden or auto-hiding.
        """
        if not (self.available and self.status_item and self._view):
            return
        if text == self._progress_text:
            return
        self._progress_text = text
        width = 26 if not text else 46
        frame = self._view.frame()
        self._view.setFrame_(((0, 0), (width, frame[1][1])))
        self.status_item.setLength_(width)
        self._view.setNeedsDisplay_(True)

    def notify_completion(self, title: str, body: str):
        if self.available:
            try:
                from Foundation import NSUserNotification, NSUserNotificationCenter
                note = NSUserNotification.alloc().init()
                note.setTitle_(title)
                note.setInformativeText_(body)
                NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(note)
                return
            except Exception:
                pass
        # Works on every reasonably current macOS release, including where the
        # old NSUserNotification API is unavailable. title/body can contain
        # a downloaded video's title -- untrusted, attacker-influenceable
        # text -- so they're passed as real argv items to the script's own
        # `argv`, never interpolated into the AppleScript source itself.
        # String-building the script text (even with quote-escaping) would
        # be an AppleScript-injection hole: quote-escaping alone doesn't
        # stop `&`, `do shell script`, or other AppleScript syntax in the
        # content from being parsed as code.
        subprocess.Popen([
            "osascript",
            "-e", "on run argv",
            "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
            "-e", "end run",
            title, body,
        ])

    def play_completion_sound(self):
        if self.available:
            try:
                from AppKit import NSSound
                sound = NSSound.soundNamed_("Glass")
                if sound:
                    sound.play()
                    return
            except Exception:
                pass
        subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
