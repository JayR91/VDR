import os
import platform
import queue
import subprocess
import threading
import traceback
import datetime as dt
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from queue_manager import QueueManager
from engine import Status
import video_capture
from organizer import categorized_destination, organize_completed_file
from desktop_integration import create_integration
from focus_guard import FocusGuard, POLICY_HOLD

DEFAULT_DIR = os.path.expanduser("~/Downloads/VDR")


def _open_path(path):
    # subprocess.Popen launches and returns immediately; os.system() would
    # block the whole Tk event loop until the shell it spawns exits, which
    # freezes the UI and makes clicks feel like they didn't register.
    try:
        if os.name == "nt":
            os.startfile(path)
        elif os.uname().sysname == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def human_size(n):
    if n is None:
        return "?"
    n = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _enable_mac_clipboard_shortcuts(root):
    """Without an app-level Edit menu, macOS Tk doesn't always route
    Cmd+V/C/X/A into Entry/Text widgets, and plain Tk Entry/Text widgets have
    no right-click context menu at all on any platform. Wire up both,
    application-wide, including future dialogs."""

    def paste(event):
        event.widget.event_generate("<<Paste>>")
        return "break"

    def copy(event):
        event.widget.event_generate("<<Copy>>")
        return "break"

    def cut(event):
        event.widget.event_generate("<<Cut>>")
        return "break"

    def select_all(event):
        w = event.widget
        if isinstance(w, (tk.Entry, ttk.Entry)):
            w.selection_range(0, "end")
        elif isinstance(w, tk.Text):
            w.tag_add("sel", "1.0", "end")
        return "break"

    root.bind_all("<Command-v>", paste)
    root.bind_all("<Command-c>", copy)
    root.bind_all("<Command-x>", cut)
    root.bind_all("<Command-a>", select_all)

    def show_context_menu(event):
        w = event.widget
        if not isinstance(w, (tk.Entry, ttk.Entry, tk.Text)):
            return
        menu = tk.Menu(w, tearoff=0)
        menu.add_command(label="Cut", command=lambda: w.event_generate("<<Cut>>"))
        menu.add_command(label="Copy", command=lambda: w.event_generate("<<Copy>>"))
        menu.add_command(label="Paste", command=lambda: w.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="Select All", command=lambda: select_all(event))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # Right-click is <Button-2> under Tk's macOS bindings; <Button-3> and
    # Control-click cover trackpads/mice configured differently.
    for seq in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
        root.bind_all(seq, show_context_menu)


class VideoTask:
    def __init__(self, url, dest_dir):
        self.url = url
        self.dest_path = os.path.join(dest_dir, "(fetching title…)")
        self.total_size = None
        self.status = Status.QUEUED
        self.speed = 0.0
        self._downloaded = 0
        # yt-dlp has no live pause/resume API. Pausing raises DownloadPaused
        # from the progress_hook to abort the transfer; since yt-dlp resumes
        # from partial fragments by default, Resume just restarts the same
        # download and it picks up where it left off.
        self.stop_event = threading.Event()
        self.start_fn = None
        self._user_paused = False

    def bytes_downloaded(self):
        return self._downloaded

    def pause(self):
        if self.status in (Status.DOWNLOADING, Status.HELD):
            self._user_paused = True
            self.stop_event.set()
            self.status = Status.PAUSED
            self.speed = 0.0

    def hold_for_focus(self):
        if self._user_paused or self.status != Status.DOWNLOADING:
            return
        self.stop_event.set()
        self.status = Status.HELD
        self.speed = 0.0

    def release_from_focus(self):
        if self.status != Status.HELD or self._user_paused or not self.start_fn:
            return
        self.stop_event.clear()
        self.status = Status.QUEUED
        self.start_fn()

    def resume(self):
        if self.status == Status.PAUSED and self.start_fn:
            self._user_paused = False
            self.stop_event.clear()
            self.status = Status.QUEUED
            self.start_fn()
        elif self.status == Status.HELD:
            self.release_from_focus()

    def cancel(self):
        self.stop_event.set()
        self.status = Status.CANCELLED


class App:
    def __init__(self, root, queue_manager: QueueManager):
        self.root = root
        self.qm = queue_manager
        # Background threads (download/monitor/video threads) never touch Tk
        # directly -- they only push onto this thread-safe queue. All real
        # widget updates happen on the main thread inside _refresh()/_drain_events().
        self._events = queue.Queue()
        self.qm.set_update_callback(lambda task: self._events.put(("task", task)))
        self.row_by_task = {}
        self._completed_handled = set()
        # Tasks the user removed. Removing one cancels it, and that status
        # change queues an update event -- without this, draining that event
        # would re-add the row that was just deleted (see remove_selected).
        self._removed_tasks = set()
        self._dark_mode = None

        root.title("VDR — Download Manager")
        root.geometry("1000x580")
        _enable_mac_clipboard_shortcuts(root)

        self.style = ttk.Style()
        # macOS's native "aqua" ttk theme renders buttons itself and mostly
        # ignores style.map() color changes, so hover/press feedback never
        # shows up. "clam" is a cross-platform theme that actually honors it
        # -- but unlike aqua it doesn't auto-adapt to system Dark Mode, so
        # every color below is set explicitly to match the app's dark look.
        self.style.theme_use("clam")
        self._apply_system_theme()

        def add_button(parent, text, command, **pack_opts):
            b = ttk.Button(parent, text=text, command=command, cursor="pointinghand")
            b.pack(side="left", **pack_opts)
            return b

        toolbar = ttk.Frame(root)
        toolbar.pack(fill="x", padx=10, pady=(10, 4))
        row1 = ttk.Frame(toolbar)
        row1.pack(fill="x", pady=(0, 6))
        row2 = ttk.Frame(toolbar)
        row2.pack(fill="x")

        add_button(row1, "+ Add URL", self.add_url_dialog)
        add_button(row1, "+ Add Video/Stream", self.add_video_dialog, padx=8)
        add_button(row1, "Schedule URL", self.schedule_url_dialog, padx=8)

        add_button(row2, "Pause", self.pause_selected)
        add_button(row2, "Resume", self.resume_selected, padx=8)
        add_button(row2, "Cancel", self.cancel_selected)
        add_button(row2, "Remove", self.remove_selected, padx=8)
        add_button(row2, "Open Folder", self.open_folder_selected)

        speed_frame = ttk.Frame(toolbar)
        speed_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(speed_frame, text="Speed limit (KB/s, 0=unlimited):").pack(side="left", padx=(0, 8))
        self.speed_var = tk.StringVar(value="0")
        ttk.Entry(speed_frame, textvariable=self.speed_var, width=8, font=("Helvetica", 12)).pack(side="left")
        add_button(speed_frame, "Apply", self.apply_speed_limit, padx=8)
        self.speed_slider = tk.Scale(speed_frame, from_=0, to=10240, orient="horizontal",
                                     showvalue=False, resolution=64, command=self._slider_speed_changed,
                                     highlightthickness=0, length=180)
        self.speed_slider.pack(side="left", padx=(4, 0))

        focus_frame = ttk.Frame(toolbar)
        focus_frame.pack(fill="x", pady=(8, 0))
        self.focus_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            focus_frame,
            text="Focus Guard",
            variable=self.focus_var,
            command=self._toggle_focus_guard,
        ).pack(side="left")
        self.focus_status = ttk.Label(
            focus_frame,
            text="Off — downloads ignore battery and whether you are using the Mac",
        )
        self.focus_status.pack(side="left", padx=10)
        self.focus_guard = FocusGuard(self._apply_focus_policy, self._on_focus_change)

        columns = ("file", "size", "progress", "speed", "status")
        self.tree = ttk.Treeview(root, columns=columns, show="headings", selectmode="browse")
        for col, label, width in [
            ("file", "File", 300), ("size", "Size", 90),
            ("progress", "Progress", 220), ("speed", "Speed", 100), ("status", "Status", 110),
        ]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=10)
        self.tree.bind("<Double-1>", self.open_selected)

        self.server_status = ttk.Label(root, text="Local server: starting…", foreground="gray")
        self.server_status.pack(anchor="w", padx=10, pady=(0, 2))
        # Transient activity line. Deliberately NOT a messagebox: those are
        # modal and block the Tk event loop, which stalls _refresh() -- so
        # progress, the menu-bar indicator, and post-download organisation all
        # freeze until someone clicks OK. In a background/menu-bar app the
        # dialog can sit unnoticed behind other windows, so the app just looks
        # broken. See _flash_status().
        self.activity_status = ttk.Label(root, text="", foreground="gray", justify="left")
        self.activity_status.pack(anchor="w", fill="x", padx=10, pady=(0, 10))
        # A message here can be a full sentence (e.g. the "sign in to the site"
        # hint), which would otherwise run straight off the right edge of the
        # window. Re-wrap to the label's actual width whenever it resizes.
        self.activity_status.bind(
            "<Configure>",
            lambda e: e.widget.configure(wraplength=max(200, e.width - 8)),
        )
        self._activity_clear_job = None

        self.mac = create_integration(self.add_url_from_drop, self.show_window, self.quit_app)
        self.root.after(700, self.mac.install_menu_bar)
        # Make sure the window is actually up front when the app is opened,
        # rather than buried behind whatever the user was already looking at.
        self.show_window()
        # Clicking the Dock icon while the app is already running (window
        # hidden) sends macOS's "reopen" event -- Tk's Cocoa port dispatches
        # that to this specific Tcl command name if it exists. Without this,
        # only the menu-bar "Show VDR" item could bring the window back.
        self.root.createcommand("::tk::mac::ReopenApplication", self.show_window)
        # Closing the window hides it rather than quitting -- downloads and
        # the local server (for the browser extension) keep running in the
        # background, exactly like closing Slack/Mail's window doesn't quit
        # them. Click the Dock icon to bring it back; Cmd+Q, the app menu, or
        # "Quit VDR" in the menu-bar icon are the real exits.
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_window)

        self.root.after(200, self._drain_events)
        self.root.after(500, self._refresh)
        self.root.after(1500, self._sync_system_theme)

    def _system_is_dark(self):
        # os.uname() is Unix-only -- calling it on Windows raises
        # AttributeError rather than returning something falsy, so the check
        # has to go through platform.system().
        system = platform.system()
        if system == "Darwin":
            try:
                result = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                        capture_output=True, text=True, timeout=2)
                return result.returncode == 0 and "Dark" in result.stdout
            except Exception:
                return False
        if system == "Windows":
            # Windows has no CLI equivalent of `defaults read`; the theme
            # lives in the per-user registry. AppsUseLightTheme is 0 in dark
            # mode (there is no "AppsUseDarkTheme" key).
            try:
                import winreg

                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                )
                try:
                    value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                    return value == 0
                finally:
                    key.Close()
            except Exception:
                return False
        return False

    def _apply_system_theme(self):
        dark = self._system_is_dark()
        self._dark_mode = dark
        bg, field, button, fg, heading = (("#2b2b2b", "#1e1e1e", "#4a4a4a", "white", "#3a3a3a") if dark
                                          else ("#f3f3f3", "#ffffff", "#e2e2e2", "#1f1f1f", "#d5d5d5"))
        accent = "#2878b8"
        self.root.configure(background=bg)
        self.style.configure("TFrame", background=bg)
        self.style.configure("TButton", font=("Helvetica", 13), padding=(14, 10), background=button, foreground=fg, borderwidth=0)
        self.style.map("TButton", background=[("pressed", "#2f6fa8"), ("active", accent)], foreground=[("active", "white")])
        self.style.configure("TLabel", font=("Helvetica", 12), background=bg, foreground=fg)
        self.style.configure("TCheckbutton", font=("Helvetica", 12), background=bg, foreground=fg)
        self.style.map("TCheckbutton", background=[("active", bg)], foreground=[("active", fg)])
        self.style.configure("TEntry", font=("Helvetica", 12), fieldbackground=field, foreground=fg)
        self.style.configure("Treeview", font=("Helvetica", 12), rowheight=32, background=field, fieldbackground=field, foreground=fg)
        self.style.map("Treeview", background=[("selected", accent)], foreground=[("selected", "white")])
        self.style.configure("Treeview.Heading", font=("Helvetica", 12, "bold"), background=heading, foreground=fg)

    def _sync_system_theme(self):
        if self._system_is_dark() != self._dark_mode:
            self._apply_system_theme()
        self.root.after(1500, self._sync_system_theme)

    # ---------- server status ----------

    def set_server_status(self, text, color="gray"):
        self.server_status.config(text=text, foreground=color)

    def _flash_status(self, text, error=False, hold_ms=8000):
        """Show a transient one-line message without blocking the event loop.

        Messages are collapsed to a single line so a long URL or traceback
        can't resize the window.
        """
        line = " ".join(str(text).split())
        self.activity_status.config(text=line, foreground="#d05a5a" if error else "gray")
        if self._activity_clear_job is not None:
            try:
                self.root.after_cancel(self._activity_clear_job)
            except Exception:
                pass
        self._activity_clear_job = self.root.after(
            hold_ms, lambda: self.activity_status.config(text="")
        )

    # ---------- actions ----------

    def add_url_dialog(self):
        url = simpledialog.askstring("Add URL", "Enter download URL:", parent=self.root)
        if not url:
            return
        if video_capture.looks_like_video_url(url):
            self.queue_video(url)
            return
        filename = url.split("/")[-1].split("?")[0] or "download"
        path = filedialog.asksaveasfilename(initialdir=os.path.join(DEFAULT_DIR, "Other"), initialfile=filename)
        if not path:
            return
        segs = simpledialog.askinteger("Segments", "Number of parallel segments:",
                                        initialvalue=8, minvalue=1, maxvalue=32, parent=self.root)
        task = self.qm.add(url, path, num_segments=segs or 8)
        self._add_row(task)

    def add_url_from_drop(self, url):
        """Used by the menu-bar drop target and macOS open-URL events."""
        if not url or not url.startswith(("http://", "https://")):
            return
        if video_capture.looks_like_video_url(url):
            self.queue_video(url)
            return
        filename = url.split("/")[-1].split("?")[0] or "download"
        task = self.qm.add(url, categorized_destination(DEFAULT_DIR, filename))
        self._events.put(("task", task))

    def schedule_url_dialog(self):
        url = simpledialog.askstring("Schedule download", "Enter download URL:", parent=self.root)
        if not url:
            return
        when = simpledialog.askstring("Schedule download", "Start time (YYYY-MM-DD HH:MM; blank = next midnight):", parent=self.root)
        try:
            start = (dt.datetime.combine(dt.date.today() + dt.timedelta(days=1), dt.time.min) if not when
                     else dt.datetime.strptime(when, "%Y-%m-%d %H:%M"))
            if start <= dt.datetime.now():
                raise ValueError
        except ValueError:
            messagebox.showerror("Schedule download", "Enter a future time as YYYY-MM-DD HH:MM.")
            return
        filename = url.split("/")[-1].split("?")[0] or "download"
        task = self.qm.schedule(url, categorized_destination(DEFAULT_DIR, filename), start.timestamp())
        self._add_row(task)

    def add_video_dialog(self):
        url = simpledialog.askstring("Add Video/Stream", "Enter video/stream page URL:", parent=self.root)
        if not url:
            return
        self.queue_video(url)

    def queue_video(self, url):
        os.makedirs(DEFAULT_DIR, exist_ok=True)
        task = VideoTask(url, DEFAULT_DIR)
        self._events.put(("task", task))
        self._events.put(("info", "Video/stream download started — it now shows up in the list below."))

        def hook(d):
            if task.stop_event.is_set():
                raise video_capture.DownloadPaused()
            if "postprocessor" in d:
                # Fires after merging/converting -- this is the only place
                # that reports the real final filepath (progress_hooks only
                # ever see each fragment's temp filename, which gets deleted
                # once merged).
                if d.get("status") == "finished":
                    info = d.get("info_dict") or {}
                    final_path = info.get("filepath") or info.get("_filename")
                    if final_path:
                        task.dest_path = final_path
                return
            filename = d.get("filename")
            if filename:
                task.dest_path = filename
            if d.get("status") == "downloading":
                task.status = Status.DOWNLOADING
                task.total_size = d.get("total_bytes") or d.get("total_bytes_estimate")
                task._downloaded = d.get("downloaded_bytes", 0)
                task.speed = d.get("speed") or 0.0

        def run():
            try:
                video_capture.download_video(url, DEFAULT_DIR, progress_hook=hook)
                task.status = Status.COMPLETED
                task.speed = 0.0
                self._events.put(("info", f"Video download complete:\n{url}"))
            except video_capture.DownloadPaused:
                pass  # status is already PAUSED; not an error
            except Exception as e:
                if task.status != Status.CANCELLED:
                    task.status = Status.ERROR
                    traceback.print_exc()
                    self._events.put(("error", f"Video download failed:\n{e}"))

        task.start_fn = lambda: threading.Thread(target=run, daemon=True).start()
        task.start_fn()

    def _add_row(self, task):
        iid = self.tree.insert("", "end", values=(
            os.path.basename(task.dest_path), "?", "0%", "-", task.status.value))
        self.row_by_task[task] = iid

    def _drain_events(self):
        """Runs on the main thread only. Safe place to touch Tk widgets."""
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "task":
                    if payload in self._removed_tasks:
                        continue  # deliberately gone; don't resurrect it
                    if payload not in self.row_by_task:
                        self._add_row(payload)
                elif kind == "info":
                    self._flash_status(payload)
                elif kind == "error":
                    # Errors get the same non-blocking treatment plus a native
                    # notification, which persists in Notification Centre --
                    # more discoverable than a modal in a background app, and
                    # it doesn't stall every other running download.
                    self._flash_status(payload, error=True)
                    self.mac.notify_completion("VDR — download failed", payload)
                elif kind == "focus":
                    self.focus_status.config(text=payload)
        except queue.Empty:
            pass
        self.root.after(200, self._drain_events)

    def _refresh(self):
        active = []
        for task, iid in list(self.row_by_task.items()):
            size = human_size(task.total_size)
            downloaded = task.bytes_downloaded()
            if task.total_size:
                pct = f"{(downloaded / task.total_size * 100):.1f}%  ({human_size(downloaded)}/{size})"
            else:
                pct = human_size(downloaded)
            speed = human_size(task.speed) + "/s" if task.speed else "-"
            try:
                self.tree.item(iid, values=(os.path.basename(task.dest_path), size, pct, speed, task.status.value))
            except tk.TclError:
                pass
            if task.status in (Status.CONNECTING, Status.DOWNLOADING):
                active.append(task)
            if task.status == Status.COMPLETED and task not in self._completed_handled:
                self._completed_handled.add(task)
                self._handle_completion(task)
        # A percentage is more useful for one download; otherwise match Mail's count.
        if len(active) == 1 and active[0].total_size:
            badge = str(round(active[0].bytes_downloaded() / active[0].total_size * 100))
            progress = f"{badge}%"
        else:
            badge = str(len(active)) if active else ""
            progress = badge
        self.mac.set_dock_badge(badge)
        # Dock badges don't render without a visible Dock tile, which this
        # app intentionally doesn't have -- this is the background-app
        # equivalent, shown next to the menu-bar icon instead.
        self.mac.set_progress(progress)
        self.root.after(1000, self._refresh)

    def _handle_completion(self, task):
        try:
            task.dest_path = organize_completed_file(task.dest_path, DEFAULT_DIR)
        except Exception:
            pass
        name = os.path.basename(task.dest_path)
        self.mac.play_completion_sound()
        self.mac.notify_completion("Download complete", name)

    def _on_close_window(self):
        """Hide rather than quit -- but only while something can bring it back.

        On macOS the Dock icon always can. On Windows that job belongs to the
        tray icon, and the tray is optional (pystray missing, or no Explorer
        shell). Hiding with no tray icon would leave VDR running with no way
        to reach it and no way to quit it short of Task Manager, so in that
        case closing the window means what it appears to mean.
        """
        if getattr(self.mac, "available", False):
            self.root.withdraw()
        else:
            self.quit_app()

    def show_window(self):
        self.root.after(0, lambda: (self.root.deiconify(), self.root.lift(), self.root.focus_force()))

    def quit_app(self):
        """Real exit, safe to call from an AppKit callback.

        The menu-bar item's action fires from Cocoa, outside Tk's event loop.
        Calling root.destroy() straight from there tears the interpreter down
        while Tcl still has timers queued, and the next one to fire lands in
        PyEval_RestoreThread with no thread state -- a hard abort
        (Py_FatalError: TstateNULL) rather than a clean quit. Bouncing through
        after() runs the teardown inside the event loop, where it's safe.
        """
        self.root.after(0, self.root.destroy)

    def _selected_task(self):
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        for task, tid in self.row_by_task.items():
            if tid == iid:
                return task
        return None

    def pause_selected(self):
        t = self._selected_task()
        if t:
            self.qm.pause(t)

    def resume_selected(self):
        t = self._selected_task()
        if t:
            self.qm.resume(t)

    def cancel_selected(self):
        t = self._selected_task()
        if t:
            self.qm.cancel(t)

    def remove_selected(self):
        t = self._selected_task()
        if t:
            # Mark it removed *before* qm.remove(), which cancels the task and
            # so fires a status update. That update is queued, not immediate,
            # so it would otherwise arrive after the row is gone and re-add it.
            self._removed_tasks.add(t)
            iid = self.row_by_task.pop(t)
            self.tree.delete(iid)
            self._completed_handled.discard(t)
            self.qm.remove(t)

    def open_folder_selected(self):
        t = self._selected_task()
        if not t:
            return
        _open_path(os.path.dirname(t.dest_path))

    def open_selected(self, event=None):
        t = self._selected_task()
        if not t:
            return
        # Double-click: open the downloaded file itself if it's there yet,
        # otherwise fall back to the folder (e.g. still downloading, or the
        # video's real filename isn't known until yt-dlp resolves it).
        if os.path.exists(t.dest_path):
            _open_path(t.dest_path)
        else:
            _open_path(os.path.dirname(t.dest_path))

    def apply_speed_limit(self):
        try:
            kb = float(self.speed_var.get())
            self.qm.set_speed_limit(int(kb * 1024) if kb > 0 else None)
            self._flash_status(f"Speed limit set to {'unlimited' if kb <= 0 else f'{kb:.0f} KB/s'}", hold_ms=4000)
        except ValueError:
            messagebox.showerror("VDR", "Enter a valid number")

    def _slider_speed_changed(self, value):
        kb = int(float(value))
        self.speed_var.set(str(kb))
        self.qm.set_speed_limit(kb * 1024 if kb else None)

    def _toggle_focus_guard(self):
        self.focus_guard.set_enabled(self.focus_var.get())

    def _apply_focus_policy(self, policy: str):
        self.qm.apply_focus_policy(policy)
        for task in list(self.row_by_task):
            if task in getattr(self.qm, "tasks", []):
                continue
            if policy == POLICY_HOLD:
                if hasattr(task, "hold_for_focus"):
                    task.hold_for_focus()
            elif hasattr(task, "release_from_focus"):
                task.release_from_focus()

    def _on_focus_change(self, policy: str, detail: str):
        self._events.put(("focus", detail))
