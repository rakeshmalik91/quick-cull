"""Instant startup splash and launch orchestration for Quick Cull.

This module is imported *before* the application package, so it must stay
dependency free: standard library only, plain ``tkinter`` widgets, no
``customtkinter``, no Pillow, no NumPy, no Torch. Importing the real app
(:mod:`gui`) costs several seconds (OpenCV, rawpy, Ultralytics, ...), which is
why the splash used to appear only once everything was already loaded.

The flow implemented by :func:`launch_gui` is:

1. draw a frameless splash immediately (its own Tk root, no app window yet);
2. import the real application on a worker thread while the splash animates;
3. build the main window on the main thread, still handing status updates to
   the splash;
4. close the splash and run the main loop.

The splash owns a separate Tk interpreter from the main window. That is
deliberate: the main window class (``customtkinter.CTk``) cannot exist before
the heavy imports finish, so the splash needs a root of its own. Because Tk
only ever registers the *first* root as ``tkinter._default_root``, the main
window takes that slot over explicitly (see :func:`adopt_default_root`).
"""

from __future__ import annotations

import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import Any, Callable, Optional

APP_NAME = "Quick Cull"
APP_TITLE = "QUICK CULL"
APP_TAGLINE = "RAW + JPG Photo Culling Engine"
APP_VERSION = "1.0.0"

APP_DIR = Path(__file__).resolve().parent
ICON_PATH = APP_DIR / "media" / "quick_cull.ico"
ICON_PNG_PATH = APP_DIR / "media" / "quick_cull_icon.png"
SPLASH_ICON_PNG_PATH = APP_DIR / "media" / "quick_cull_splash_icon.png"

BG_COLOR = "#121212"
CARD_COLOR = "#1a1a1a"
BORDER_COLOR = "#1f538d"
ACCENT_COLOR = "#2f80ed"
TRACK_COLOR = "#243447"
TEXT_COLOR = "#ffffff"
MUTED_COLOR = "#8d99ae"
STATUS_COLOR = "#adb5bd"
DIM_COLOR = "#495057"

MIN_SPLASH_MS = 400


def ensure_tcl_env() -> None:
    """Point Tcl/Tk at the interpreter's own library folders when needed.

    Mirrors the guard in ``culler.gui`` but has to run *before* the first Tk
    root is created, which is why it cannot be imported from there.
    """
    if "TCL_LIBRARY" not in os.environ:
        tcl_dir = os.path.join(sys.prefix, "tcl", "tcl8.6")
        if not os.path.exists(tcl_dir):
            tcl_dir = os.path.join(sys.prefix, "Lib", "tcl8.6")
        if os.path.exists(tcl_dir):
            os.environ["TCL_LIBRARY"] = tcl_dir

    if "TK_LIBRARY" not in os.environ:
        tk_dir = os.path.join(sys.prefix, "tcl", "tk8.6")
        if not os.path.exists(tk_dir):
            tk_dir = os.path.join(sys.prefix, "Lib", "tk8.6")
        if os.path.exists(tk_dir):
            os.environ["TK_LIBRARY"] = tk_dir


def _font_family() -> str:
    if sys.platform == "win32":
        return "Segoe UI"
    if sys.platform == "darwin":
        return "Helvetica Neue"
    return "DejaVu Sans"


def adopt_default_root(window: Any) -> None:
    """Make ``window`` tkinter's default root.

    Tk only claims ``tkinter._default_root`` for the *first* root that is
    created and clears it again when that root is destroyed. While the splash
    lives it owns the slot, so without this the main window's fonts and other
    default-root lookups would be created on (or lost with) the splash's
    interpreter.

    Only live Tk windows are adopted: a stub or half-initialised window would
    break every later ``_get_default_root`` lookup.
    """
    try:
        if window.__dict__.get("tk") is None:
            return
        tk._default_root = window
    except Exception:
        pass


def apply_window_icon(window: Any, png_path: Optional[Path] = None,
                      ico_path: Optional[Path] = None) -> bool:
    """Apply the Quick Cull icon to a Tk window (title bar + taskbar).

    Returns ``True`` when the icon was applied. Missing assets or unsupported
    window managers are ignored so a missing icon never blocks startup.
    """
    png_path = Path(png_path) if png_path else ICON_PNG_PATH
    ico_path = Path(ico_path) if ico_path else ICON_PATH
    applied = False

    if png_path.exists():
        try:
            image = tk.PhotoImage(master=window, file=str(png_path))
            window._quick_cull_icon = image  # keep a reference alive
            window.iconphoto(True, image)
            applied = True
        except Exception:
            pass

    if sys.platform == "win32" and ico_path.exists():
        try:
            window.iconbitmap(default=str(ico_path))
            applied = True
        except Exception:
            pass

    return applied


class SplashScreen:
    """Frameless dark splash shown while Quick Cull boots.

    Exposes the same ``set_status`` / ``close`` pair the main window drives, so
    it can be handed straight to ``ImageCullerApp(splash_screen=...)``.

    When ``master`` is an existing Tk window the splash becomes a ``Toplevel``
    of it; otherwise it creates its own root, which is what the launcher needs
    because no application window exists yet. ``window`` overrides both, which
    is mainly useful for tests.
    """

    def __init__(self, master: Any = None, width: int = 460, height: int = 332,
                 status: str = "Starting Quick Cull...", window: Any = None):
        self._closed = False
        self._cancelled = False
        self._running = False
        self._anim_job: Optional[str] = None
        self._icon: Optional[tk.PhotoImage] = None
        self._bar_x = 0
        self._bar_w = 110
        self._created_at = time.monotonic()

        if window is not None:
            self.window = window
        elif master is not None and getattr(master, "tk", None) is not None:
            self.window = tk.Toplevel(master)
        else:
            ensure_tcl_env()
            self.window = tk.Tk()

        self.window.withdraw()
        self.window.title(APP_NAME)
        self.window.configure(bg=BG_COLOR)
        self.window.resizable(False, False)
        self.window.geometry(f"{width}x{height}")

        try:
            self.window.overrideredirect(True)
        except Exception:
            pass
        try:
            self.window.attributes("-topmost", True)
        except Exception:
            pass

        previous_default = getattr(tk, "_default_root", None)
        try:
            # Widgets always use their master's interpreter, but fonts and
            # variables default to tkinter's default root, so point that at this
            # window while its widgets are being created.
            adopt_default_root(self.window)
            self._build(status)
            apply_window_icon(self.window)
        finally:
            if previous_default is not None:
                adopt_default_root(previous_default)

        self.window.bind("<Escape>", self._on_escape)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)

        self._center(width, height)
        self.window.deiconify()
        self.window.lift()
        self.window.update_idletasks()
        self.window.update()
        self._start_animation()

    # -- construction helpers ------------------------------------------------

    def _build(self, status: str) -> None:
        family = _font_family()

        self.card = tk.Frame(
            self.window,
            bg=CARD_COLOR,
            highlightbackground=BORDER_COLOR,
            highlightcolor=BORDER_COLOR,
            highlightthickness=2,
            bd=0,
        )
        self.card.pack(fill="both", expand=True, padx=2, pady=2)

        # grid keeps every row at its natural height: pack would let a large
        # image swallow the whole card and collapse the text rows to nothing.
        self.card.grid_columnconfigure(0, weight=1)

        row = 0
        if SPLASH_ICON_PNG_PATH.exists():
            try:
                self._icon = tk.PhotoImage(master=self.window, file=str(SPLASH_ICON_PNG_PATH))
                tk.Label(self.card, image=self._icon, bg=CARD_COLOR, bd=0).grid(
                    row=row, column=0, pady=(26, 0)
                )
                row += 1
            except Exception:
                self._icon = None

        tk.Label(
            self.card,
            text=APP_TITLE,
            bg=CARD_COLOR,
            fg=TEXT_COLOR,
            font=(family, 21, "bold"),
        ).grid(row=row, column=0, pady=(12, 0))
        row += 1

        tk.Label(
            self.card,
            text=APP_TAGLINE,
            bg=CARD_COLOR,
            fg=MUTED_COLOR,
            font=(family, 11),
        ).grid(row=row, column=0, pady=(6, 0))
        row += 1

        self.track = tk.Frame(self.card, bg=TRACK_COLOR, height=6, width=360)
        self.track.grid(row=row, column=0, pady=(22, 0), sticky="ew")
        self.track.grid_propagate(False)
        self.bar = tk.Frame(self.track, bg=ACCENT_COLOR, height=6, width=self._bar_w)
        self.bar.place(x=0, y=0, height=6)
        row += 1

        self.lbl_status = tk.Label(
            self.card,
            text=status,
            bg=CARD_COLOR,
            fg=STATUS_COLOR,
            font=(family, 11),
            width=40,
            anchor="center",
        )
        self.lbl_status.grid(row=row, column=0, pady=(12, 18))
        row += 1

        tk.Label(
            self.card,
            text=f"v{APP_VERSION}",
            bg=CARD_COLOR,
            fg=DIM_COLOR,
            font=(family, 9),
        ).grid(row=row, column=0, sticky="e", padx=16)

    def _center(self, width: int, height: int) -> None:
        try:
            screen_w = self.window.winfo_screenwidth()
            screen_h = self.window.winfo_screenheight()
            x = max(0, (screen_w - width) // 2)
            y = max(0, (screen_h - height) // 3)
            self.window.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            pass

    def _start_animation(self) -> None:
        if self._running:
            return
        self._running = True
        self._bar_x = 0
        self._animate()

    def _animate(self) -> None:
        if self._closed or not self._running:
            return
        try:
            track_w = self.track.winfo_width() or 360
            self.bar.place(x=self._bar_x, y=0, height=6)
            self._bar_x += 5
            if self._bar_x > track_w:
                self._bar_x = -self._bar_w
            self._anim_job = self.window.after(16, self._animate)
        except Exception:
            self._running = False

    def _on_escape(self, _event: Any = None) -> None:
        self.cancel()

    # -- public API ----------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        """True when the user dismissed the splash (Esc / close)."""
        return self._cancelled

    @property
    def elapsed_ms(self) -> float:
        """Milliseconds since the splash became visible."""
        return (time.monotonic() - self._created_at) * 1000.0

    def set_status(self, message: str) -> None:
        """Update the status line and repaint immediately."""
        if self._closed:
            return
        try:
            self.lbl_status.configure(text=message)
            self.pump()
        except Exception:
            pass

    def pump(self) -> None:
        """Process pending Tk events so the splash actually repaints."""
        if self._closed:
            return
        try:
            self.window.update_idletasks()
            self.window.update()
        except Exception:
            pass

    def spin_until(self, predicate: Callable[[], bool], poll_ms: int = 20) -> bool:
        """Run a nested main loop until ``predicate()`` is true or cancelled.

        Keeps the progress bar animating while the worker thread imports the
        application. Returns True when the predicate was satisfied.
        """
        if self._closed or self._cancelled:
            return predicate()
        self._running = True
        satisfied = False

        def _poll() -> None:
            nonlocal satisfied
            if self._cancelled or self._closed:
                return
            if predicate():
                satisfied = True
                self._running = False
                try:
                    self.window.quit()
                except Exception:
                    pass
                return
            self.window.after(poll_ms, _poll)

        self.window.after(poll_ms, _poll)
        try:
            self.window.mainloop()
        except Exception:
            pass
        return satisfied

    def cancel(self) -> None:
        """Mark the splash as dismissed by the user and stop its animations."""
        self._cancelled = True
        self._running = False
        try:
            self.window.quit()
        except Exception:
            pass

    def close(self) -> None:
        """Stop animations and destroy the splash window (idempotent)."""
        if self._closed:
            return
        self._closed = True
        self._running = False
        if self._anim_job is not None:
            try:
                self.window.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None
        try:
            self.window.destroy()
        except Exception:
            pass

    def __enter__(self) -> "SplashScreen":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def _import_app() -> Any:
    """Import the application module and return ``ImageCullerApp``."""
    import gui  # noqa: WPS433 (deliberately deferred: this is the slow import)

    return gui.ImageCullerApp


def launch_gui(initial_path: Optional[str] = None,
               workspace_path: Optional[str] = None,
               show_splash: bool = True,
               splash: Optional[SplashScreen] = None) -> Optional[Any]:
    """Start Quick Cull: splash first, then the main window.

    ``initial_path`` / ``workspace_path`` are forwarded to ``ImageCullerApp``.
    Returns the main window, or ``None`` when the user dismissed the splash.
    Any import or startup error is re-raised once the splash is out of the way.
    """
    owns_splash = splash is None
    if show_splash and splash is None:
        try:
            splash = SplashScreen(status="Starting Quick Cull...")
        except Exception:
            splash = None

    if splash is not None:
        splash.set_status("Loading libraries...")

    result: dict = {}

    def _worker() -> None:
        try:
            result["app_class"] = _import_app()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            result["error"] = exc
        finally:
            result["done"] = True

    worker = threading.Thread(target=_worker, name="quick-cull-import", daemon=True)
    worker.start()

    if splash is not None:
        loaded = splash.spin_until(lambda: result.get("done", False))
        if not loaded:
            # Either the user dismissed the splash or the window went away;
            # join first so the result dict is never read half-written.
            worker.join()
            if splash.cancelled:
                if owns_splash:
                    splash.close()
                return None
    else:
        worker.join()

    error = result.get("error")
    if error is not None:
        if owns_splash and splash is not None:
            splash.close()
        raise error

    # Avoid a one-frame flash when everything is already warm in the page cache.
    if splash is not None:
        elapsed_ms = splash.elapsed_ms
        if elapsed_ms < MIN_SPLASH_MS:
            time.sleep((MIN_SPLASH_MS - elapsed_ms) / 1000.0)

    app_class = result["app_class"]

    if splash is not None:
        splash.set_status("Initializing workspace...")

    try:
        app = app_class(
            initial_path=initial_path,
            workspace_path=workspace_path,
            show_splash=False,
            splash_screen=splash,
        )
    finally:
        if owns_splash and splash is not None:
            splash.close()

    app.mainloop()
    return app
