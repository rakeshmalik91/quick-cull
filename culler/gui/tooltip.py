import tkinter as tk
from typing import Optional


class ToolTip:
    """
    Sleek dark-mode hover tooltip for CustomTkinter & Tkinter widgets.
    Displays shortcut hints and helpful descriptions on mouse hover.
    """

    def __init__(self, widget, text: str, delay_ms: int = 350):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.tooltip_window: Optional[tk.Toplevel] = None
        self._timer_id: Optional[str] = None

        self.widget.bind("<Enter>", self._on_enter, add="+")
        self.widget.bind("<Leave>", self._on_leave, add="+")
        self.widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, event=None):
        self._schedule()

    def _on_leave(self, event=None):
        self._cancel()
        self._hide()

    def _schedule(self):
        self._cancel()
        if hasattr(self.widget, "after"):
            self._timer_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self):
        if self._timer_id:
            try:
                self.widget.after_cancel(self._timer_id)
            except Exception:
                pass
            self._timer_id = None

    def _show(self):
        if self.tooltip_window or not self.text:
            return

        try:
            tw = tk.Toplevel(self.widget)
            tw.wm_overrideredirect(True)
            tw.attributes("-topmost", True)
            tw.withdraw()

            label = tk.Label(
                tw,
                text=self.text,
                justify="center",
                background="#1e1e24",
                foreground="#ffd166",
                relief="solid",
                border=1,
                font=("Segoe UI", 9, "bold"),
                padx=8,
                pady=4
            )
            label.pack()

            tw.update_idletasks()
            tip_w = tw.winfo_reqwidth()
            tip_h = tw.winfo_reqheight()

            screen_w = tw.winfo_screenwidth()
            screen_h = tw.winfo_screenheight()

            widget_rx = self.widget.winfo_rootx()
            widget_ry = self.widget.winfo_rooty()
            widget_w = self.widget.winfo_width()
            widget_h = self.widget.winfo_height()

            # Desired position: centered below widget
            x = widget_rx + (widget_w // 2) - (tip_w // 2)
            y = widget_ry + widget_h + 6

            margin = 8

            # Prevent clipping on the right edge of screen
            if x + tip_w > screen_w - margin:
                x = screen_w - tip_w - margin

            # Prevent clipping on the left edge of screen
            if x < margin:
                x = margin

            # If overflowing the bottom of the screen, flip above the widget
            if y + tip_h > screen_h - margin:
                y = widget_ry - tip_h - 6
                if y < margin:
                    y = margin

            tw.wm_geometry(f"+{x}+{y}")
            tw.deiconify()
            self.tooltip_window = tw
        except Exception:
            if self.tooltip_window:
                try:
                    self.tooltip_window.destroy()
                except Exception:
                    pass
            self.tooltip_window = None

    def _hide(self):
        if self.tooltip_window:
            try:
                self.tooltip_window.destroy()
            except Exception:
                pass
            self.tooltip_window = None
