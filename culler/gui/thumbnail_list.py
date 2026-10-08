import os
import tkinter as tk
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Set, Tuple, Union
import customtkinter as ctk
from PIL import Image, ImageDraw

from ..culler_engine import ImageItem, FlagState, CullingSession
from ..image_loader import ImageLoader
from ..logger import log_debug, log_error
from .row_pool import (
    INITIAL_BIND_ROWS,
    REBIND_ROWS_PER_TICK,
    ROW_HEIGHT,
    RowPool,
    row_height_for,
)

#: Cooldown (in ms) before submitting thumbnail batch loads during rapid scrolling or key navigation.
THUMB_LOAD_COOLDOWN_MS = 120


def get_consumed_memory_mb(image_loader: Optional[ImageLoader] = None) -> float:
    """Return process RSS memory consumption in megabytes, with fallback."""
    try:
        import psutil
        rss = psutil.Process().memory_info().rss / (1024.0 * 1024.0)
        if rss > 0:
            return rss
    except Exception:
        pass
    if image_loader is not None and hasattr(image_loader, "cache_stats"):
        try:
            stats = image_loader.cache_stats()
            total_bytes = stats.get("thumb_bytes", 0) + stats.get("full_bytes", 0)
            return total_bytes / (1024.0 * 1024.0)
        except Exception:
            pass
    return 0.0


class PoolRangeBar(tk.Canvas):
    """Horizontal range bar indicating the loaded window [start, end] out of total items."""

    @classmethod
    def _find_bg_color(cls, widget):
        curr = widget
        while curr is not None:
            if hasattr(curr, "_fg_color") and curr._fg_color not in ("transparent", None):
                color = curr._apply_appearance_mode(curr._fg_color)
                if color and color != "transparent":
                    return color
            curr = getattr(curr, "master", None)
        return "#242424"

    def __init__(self, master, height: int = 6, bg: str = "#343638", fill_color: str = "#1f538d", **kwargs):
        bg_color = self._find_bg_color(master)
        super().__init__(
            master,
            height=height,
            bg=bg_color,
            highlightthickness=0,
            bd=0,
            **kwargs
        )
        self._bar_height = height
        self._track_color = bg
        self._fill_color = fill_color
        self._start_frac = 0.0
        self._end_frac = 0.0
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event):
        self._redraw()

    def set(self, *args):
        """Set position: set(end_frac) or set(start_frac, end_frac)."""
        if len(args) == 1:
            self._start_frac = 0.0
            self._end_frac = max(0.0, min(1.0, float(args[0])))
        elif len(args) >= 2:
            self._start_frac = max(0.0, min(1.0, float(args[0])))
            self._end_frac = max(0.0, min(1.0, float(args[1])))
            if self._end_frac < self._start_frac:
                self._end_frac = self._start_frac
        self._redraw()

    def get(self) -> float:
        """Return end fraction for compatibility with progress bar checks."""
        return self._end_frac

    def get_range(self) -> Tuple[float, float]:
        """Return (start_fraction, end_fraction)."""
        return (self._start_frac, self._end_frac)

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self._bar_height
        if w <= 1:
            return

        # Draw track (background trough)
        self._draw_pill(0, 0, w, h, self._track_color)

        # Draw filled range if any
        if self._end_frac > self._start_frac:
            x0 = int(round(self._start_frac * w))
            x1 = int(round(self._end_frac * w))
            if x1 - x0 < h:
                x1 = min(w, x0 + h)
            self._draw_pill(x0, 0, x1, h, self._fill_color)

    def _draw_pill(self, x0: int, y0: int, x1: int, y1: int, color: str):
        w = x1 - x0
        h = y1 - y0
        if w <= 0 or h <= 0:
            return
        r = h // 2
        if w < 2 * r:
            self.create_oval(x0, y0, x1, y1, fill=color, outline="", width=0)
        else:
            self.create_oval(x0, y0, x0 + 2 * r, y0 + 2 * r, fill=color, outline="", width=0)
            self.create_oval(x1 - 2 * r, y0, x1, y0 + 2 * r, fill=color, outline="", width=0)
            self.create_rectangle(x0 + r, y0, x1 - r, y1, fill=color, outline="", width=0)



class ThumbnailList(ctk.CTkFrame):
    """
    Left sidebar displaying image thumbnails.
    Supports big 70x70 thumbnails for stacked photo variants with horizontal scrolling and zero scrollbar clipping.
    Supports multi-select checkboxes, Select All / Select None, and Ctrl/Shift mouse selection.
    """

    def __init__(
        self,
        master,
        on_select_image: Callable[..., None],
        on_select_all: Optional[Callable[[], None]] = None,
        on_select_none: Optional[Callable[[], None]] = None,
        image_loader: Optional[ImageLoader] = None,
        on_load_stats_changed: Optional[Callable[[Dict[str, Optional[float]]], None]] = None,
        **kwargs
    ):
        kwargs.setdefault("width", 340)
        kwargs.setdefault("corner_radius", 5)
        super().__init__(master, **kwargs)
        self.pack_propagate(False)

        self.on_select_image = on_select_image
        self.on_select_all = on_select_all
        self.on_select_none = on_select_none
        self.on_load_stats_changed = on_load_stats_changed
        self.image_loader = image_loader
        self._executor = ThreadPoolExecutor(
            max_workers=min(8, max(2, (os.cpu_count() or 4) // 2)),
            thread_name_prefix="thumb",
        )

        # Shared fonts and placeholders. Built once, not per row: a 2771-row folder
        # would otherwise create 2771 of each.
        self._font_regular = ctk.CTkFont(size=11, weight="bold")
        self._font_bold = ctk.CTkFont(size=11, weight="bold")
        self._font_title = ctk.CTkFont(size=13, weight="bold")
        self._font_small = ctk.CTkFont(size=9)
        self._font_small_bold = ctk.CTkFont(size=10, weight="bold")
        self._placeholder_ctk_70 = self._make_placeholder(70)
        self._placeholder_ctk_80 = self._make_placeholder(80)
        self._placeholder_ctk_90 = self._make_placeholder(90)

        self._btn_map: Dict[str, ctk.CTkButton] = {}
        self._row_frame_map: Dict[int, ctk.CTkFrame] = {}
        self._indicator_map: Dict[int, ctk.CTkFrame] = {}
        self._label_map: Dict[int, Union[ctk.CTkLabel, ctk.CTkButton]] = {}
        self._row_render_cache: Dict[int, Tuple[str, Optional[str]]] = {}
        # Retained for compatibility with code that reads the row maps; with a
        # recycled pool only the visible window is present in them at any moment.
        # Decoded thumbnails land here from the worker threads; one UI tick drains
        # the whole queue. Previously every thumbnail scheduled its own after(0),
        # so a folder load queued one Tk task per photo.
        self._thumb_result_queue: Deque[Tuple[str, Image.Image, int]] = deque()
        self._thumb_result_after_id: Optional[str] = None
        # Guards the queue and the "is a drain already scheduled" flag: both are touched
        # by up to eight decode workers as well as the UI thread.
        self._inflight_lock = threading.Lock()
        # path_str -> load_id of the request currently in flight. Without this, every
        # soft refresh (switching away and back mid-load) re-queued work already in
        # progress, so a folder load submitted each decode several times over.
        self._inflight_thumbs: Dict[str, int] = {}
        # Paths whose decode produced nothing (unreadable file, filtered out, decode
        # error). Kept apart from _inflight_thumbs so a refresh does not queue them
        # again, while still letting a load finish: an entry stuck in _inflight_thumbs
        # forever means _is_thumb_load_complete() is never true, so the progress bar
        # never completes and the duration timer runs until the app closes.
        self._failed_thumbs: Set[str] = set()
        self._checkbox_map: Dict[int, ctk.CTkCheckBox] = {}
        self._ctk_img_cache: Dict[str, ctk.CTkImage] = {}

        self._batch_after_id: Optional[str] = None
        self._thumb_submit_after_id: Optional[str] = None
        self._current_item_signature: Optional[List] = None
        self._pending_items: List[ImageItem] = []
        self._pending_selected_idx: int = 0
        self._pending_white_balance: str = "camera"
        self._total_thumbs: int = 0
        self._loaded_thumbs: int = 0
        self._load_id: int = 0
        self._batch_index: int = 0
        self._batch_selected_idx: int = 0
        self._batch_white_balance: str = "camera"
        self._batch_raw_requests: List[Tuple[Path, Tuple[int, int], str]] = []
        self._batch_other_requests: List[Tuple[Path, Tuple[int, int], str]] = []

        self._folder_time_start: Optional[float] = None
        self._folder_time_final: Optional[float] = None
        self._thumb_time_start: Optional[float] = None
        self._thumb_time_final: Optional[float] = None
        self._folder_scan_active: bool = False
        self._load_cycle_active: bool = False
        self._timing_after_id: Optional[str] = None

        # Top Header Box
        self.hdr_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.hdr_frame.pack(side="top", fill="x", padx=4, pady=(4, 2))

        self.lbl_title = ctk.CTkLabel(
            self.hdr_frame, text="Images (0)", font=ctk.CTkFont(size=13, weight="bold"), anchor="w"
        )
        self.lbl_title.pack(side="left", padx=2)

        # Multi-select Action Buttons
        self.btn_select_none = ctk.CTkButton(
            self.hdr_frame,
            text="None",
            width=45,
            height=22,
            fg_color="#333333",
            hover_color="#555555",
            font=ctk.CTkFont(size=10, weight="bold"),
            command=self._handle_select_none
        )
        self.btn_select_none.pack(side="right", padx=2)

        self.btn_select_all = ctk.CTkButton(
            self.hdr_frame,
            text="Select All",
            width=65,
            height=22,
            fg_color="#1f538d",
            hover_color="#14375e",
            font=ctk.CTkFont(size=10, weight="bold"),
            command=self._handle_select_all
        )
        self.btn_select_all.pack(side="right", padx=2)

        self.lbl_selection_count = ctk.CTkLabel(
            self.hdr_frame,
            text="(1 Selected)",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#ffb703"
        )
        self.lbl_selection_count.pack(side="right", padx=4)

        # Search Panel (Top of Thumbnail List)
        self.search_panel = ctk.CTkFrame(self, fg_color="transparent")
        self.search_panel.pack(side="top", fill="x", padx=4, pady=(2, 2))

        self.search_frame = ctk.CTkFrame(self.search_panel, fg_color="transparent")
        self.search_frame.pack(side="top", fill="x")

        self.search_var = tk.StringVar(value="")
        self.search_entry = ctk.CTkEntry(
            self.search_frame,
            textvariable=self.search_var,
            placeholder_text="🔍 Search filename... (Ctrl+F)",
            height=28,
            font=ctk.CTkFont(size=11),
        )
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 2))

        self.btn_clear_search = ctk.CTkButton(
            self.search_frame,
            text="✕",
            width=24,
            height=24,
            fg_color="transparent",
            hover_color="#444444",
            text_color="#999999",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.clear_search,
        )
        self.btn_clear_search.pack(side="right")

        # Suggestions Container (continuous suggestions as typed)
        self.suggestion_container = ctk.CTkFrame(
            self.search_panel,
            fg_color="#202225",
            border_color="#3d4043",
            border_width=1,
            corner_radius=4,
        )
        self._suggestion_items: List[Tuple[int, ImageItem]] = []
        self._suggestion_buttons: List[ctk.CTkButton] = []
        self._highlighted_suggestion_idx: int = -1

        self.search_var.trace_add("write", self._on_search_text_changed)
        self.search_entry.bind("<Down>", self._on_search_down)
        self.search_entry.bind("<Up>", self._on_search_up)
        self.search_entry.bind("<Return>", self._on_search_return)
        self.search_entry.bind("<KP_Enter>", self._on_search_return)
        self.search_entry.bind("<Escape>", self._on_search_escape)

        self.scroll_frame = ctk.CTkScrollableFrame(self, label_text="")
        self.scroll_frame.pack(side="top", fill="both", expand=True, padx=2, pady=2)
        self.scroll_frame.bind("<Button-1>", lambda e: self._hide_suggestions(), add="+")

        # Recycled rows bound to the visible window, rather than one widget set per
        # photo. See culler/gui/row_pool.py for why the old layout was quadratic.
        self.row_pool = RowPool(self, self.scroll_frame)
        self._grid_size: Tuple[int, int] = (0, 0)
        self.scroll_frame.bind("<Configure>", self._on_grid_resize)

        # Only the duration readout lives here now. There used to be a progress bar and
        # an "N / M" counter under it, which reported how many *visible* rows had been
        # decoded - a batch count that said nothing useful once the grid was virtualised,
        # and one that flickered on every scroll.
        self.progress_frame = ctk.CTkFrame(self, fg_color="transparent", height=28)
        self.progress_frame.pack(side="bottom", fill="x", padx=4, pady=(0, 2))
        self.progress_frame.pack_propagate(False)

        self.pool_progress_bar = PoolRangeBar(
            self.progress_frame,
            height=6,
            bg="#343638",
            fill_color="#1f538d",
        )
        self.pool_progress_bar.pack(side="top", fill="x", padx=2, pady=(2, 2))
        self.pool_progress_bar.set(0.0, 0.0)

        self.lbl_pool_stats = ctk.CTkLabel(
            self.progress_frame,
            text="",
            height=14,
            font=ctk.CTkFont(size=9),
            text_color="#6f8ba6",
            anchor="w"
        )
        self.lbl_pool_stats.pack(side="top", fill="x", padx=(2, 0))
        self.lbl_load_timing = self.lbl_pool_stats

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes, secs = divmod(int(seconds), 60)
        if minutes < 60:
            return f"{minutes}:{secs:02d}"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}"

    def start_load_timing(self, folder_started_at: Optional[float] = None):
        """Begin the folder-scan timer (and reset any previous timing state)."""
        self._folder_time_start = folder_started_at if folder_started_at is not None else time.monotonic()
        self._folder_time_final = None
        self._thumb_time_start = None
        self._thumb_time_final = None
        self._folder_scan_active = True
        self._load_cycle_active = True
        self._ensure_timing_tick()
        self._update_load_timing()
        self._emit_load_stats()

    def finish_folder_timing(self):
        """Freeze the folder-scan timer at its total duration."""
        if not self._folder_scan_active or self._folder_time_start is None:
            return
        self._folder_time_final = time.monotonic() - self._folder_time_start
        self._folder_scan_active = False
        self._update_load_timing()
        self._emit_load_stats()

    def start_thumb_timing(self, reset: bool = False):
        """Start (or restart, when ``reset``) the thumbnail-load timer.

        Only active during a folder load cycle, so filter changes and tab switches
        keep showing the totals of the load that produced the current items.
        """
        if not self._load_cycle_active:
            return
        if self._thumb_time_start is not None and not reset:
            return
        self._thumb_time_start = time.monotonic()
        self._thumb_time_final = None
        self._ensure_timing_tick()
        self._update_load_timing()

    def finish_thumb_timing(self):
        """Freeze the thumbnail-load timer at its total duration."""
        if self._thumb_time_start is None or self._thumb_time_final is not None:
            return
        self._thumb_time_final = time.monotonic() - self._thumb_time_start
        self._update_load_timing()

    def freeze_load_timing(self):
        """Stop refreshing the thumbnail timer and keep its frozen total on screen.

        The folder timer is left running, because the directory scan can still be
        in flight when the thumbnails finish; :meth:`finish_folder_timing` corrects
        its value once the scan completes.
        """
        self.finish_thumb_timing()
        self._stop_timing_tick()
        self._load_cycle_active = False
        self._emit_load_stats()

    def finish_load_timing(self):
        """Terminal state for a load that ended before all thumbnails arrived."""
        self.finish_folder_timing()
        self.freeze_load_timing()

    def begin_thumb_timing(self):
        """Time the thumbnail render of the current item set (used when restoring a tab)."""
        self._load_cycle_active = True
        self.start_thumb_timing(reset=True)

    def show_load_stats(self, folder_seconds: Optional[float] = None, thumb_seconds: Optional[float] = None):
        """Show the saved totals of an earlier load, e.g. when switching tabs.

        No timers are restarted, so a re-render of cached thumbnails cannot
        overwrite the stats belonging to the tab being restored.
        """
        self._stop_timing_tick()
        self._folder_scan_active = False
        self._load_cycle_active = False
        if folder_seconds is None and thumb_seconds is None:
            self._folder_time_start = None
            self._folder_time_final = None
            self._thumb_time_start = None
            self._thumb_time_final = None
        else:
            self._folder_time_start = None
            self._folder_time_final = folder_seconds
            self._thumb_time_start = None
            self._thumb_time_final = thumb_seconds
        self._update_load_timing()

    def _emit_load_stats(self):
        if self.on_load_stats_changed is None:
            return
        try:
            self.on_load_stats_changed({
                "folder": self._elapsed_folder(),
                "thumb": self._elapsed_thumb(),
            })
        except Exception:
            log_error("Failed to report folder load stats", exc_info=True)

    def _elapsed_folder(self) -> Optional[float]:
        if self._folder_time_final is not None:
            return self._folder_time_final
        if self._folder_time_start is not None:
            return time.monotonic() - self._folder_time_start
        return None

    def _elapsed_thumb(self) -> Optional[float]:
        if self._thumb_time_final is not None:
            return self._thumb_time_final
        if self._thumb_time_start is not None:
            return time.monotonic() - self._thumb_time_start
        return None

    @property
    def items(self) -> List:
        return self.row_pool.items if hasattr(self, "row_pool") else []

    def _update_pool_stats(self):
        """Update footer label and progress bar with loaded window and consumed memory."""
        items = self.items
        if not items and self._folder_time_final is None and self._folder_time_start is None:
            self.lbl_pool_stats.configure(text="")
            if hasattr(self, "pool_progress_bar"):
                self.pool_progress_bar.set(0.0)
            return
        total = len(items)
        loaded = self.row_pool.loaded_pool_size if hasattr(self, "row_pool") else 0
        w_start = self.row_pool.window_start if hasattr(self, "row_pool") else 0
        w_end = self.row_pool.window_end if hasattr(self, "row_pool") else -1
        mem_mb = get_consumed_memory_mb(self.image_loader)
        mem_str = f"{int(round(mem_mb))} MB"

        if total > 0 and loaded > 0 and w_end >= w_start:
            display_start = w_start + 1
            display_end = min(total, w_end + 1)
            text = f"Image {display_start}-{display_end} of {total} loaded   |   Memory: {mem_str}"
            start_frac = max(0.0, min(1.0, w_start / total))
            end_frac = max(0.0, min(1.0, display_end / total))
            if hasattr(self, "pool_progress_bar"):
                self.pool_progress_bar.set(start_frac, end_frac)
        elif total > 0:
            text = f"0 of {total} loaded   |   Memory: {mem_str}"
            if hasattr(self, "pool_progress_bar"):
                self.pool_progress_bar.set(0.0, 0.0)
        else:
            text = f"0 of 0 loaded   |   Memory: {mem_str}"
            if hasattr(self, "pool_progress_bar"):
                self.pool_progress_bar.set(0.0, 0.0)

        self.lbl_pool_stats.configure(text=text)

    def _update_load_timing(self):
        self._update_pool_stats()

    def _ensure_timing_tick(self):
        if self._timing_after_id is not None:
            return
        self._timing_after_id = self.after(100, self._tick_load_timing)

    def _tick_load_timing(self):
        self._timing_after_id = None
        self._update_load_timing()
        if self._folder_time_start is None and self._thumb_time_start is None:
            return
        self._ensure_timing_tick()

    def _stop_timing_tick(self):
        if self._timing_after_id is not None:
            try:
                self.after_cancel(self._timing_after_id)
            except Exception:
                pass
            self._timing_after_id = None

    def _reset_load_timing(self):
        self._stop_timing_tick()
        self._folder_time_start = None
        self._folder_time_final = None
        self._thumb_time_start = None
        self._thumb_time_final = None
        self._folder_scan_active = False
        self._load_cycle_active = False
        self._update_pool_stats()

    def _handle_select_all(self):
        if self.on_select_all:
            self.on_select_all()

    def _handle_select_none(self):
        if self.on_select_none:
            self.on_select_none()

    # -------------------------------------------------------------------------
    # Filename Search & Continuous Suggestions
    # -------------------------------------------------------------------------

    def _on_search_text_changed(self, *args):
        query = self.search_var.get().strip()
        if not query:
            self._hide_suggestions()
            return
        self._update_suggestions(query)

    def _update_suggestions(self, query: str):
        q = query.lower()
        items = self._pending_items if self._pending_items else self.row_pool.items
        if not items:
            self._hide_suggestions()
            return

        matches: List[Tuple[int, ImageItem]] = []
        for idx, item in enumerate(items):
            fn = item.filename.lower()
            if q in fn:
                matches.append((idx, item))
            elif getattr(item, "is_stacked", False) and any(q in p.name.lower() for p in item.stacked_paths):
                matches.append((idx, item))

        if not matches:
            self._render_no_matches(query)
            return

        def rank_key(pair: Tuple[int, ImageItem]):
            idx, item = pair
            fn = item.filename.lower()
            stem = item.path.stem.lower()
            if fn.startswith(q) or stem.startswith(q):
                return (0, idx)
            return (1, idx)

        matches.sort(key=rank_key)
        self._suggestion_items = matches[:8]
        self._highlighted_suggestion_idx = 0
        self._render_suggestions()

    def _render_no_matches(self, query: str):
        self._suggestion_items = []
        self._highlighted_suggestion_idx = -1
        for w in self.suggestion_container.winfo_children():
            w.destroy()
        self._suggestion_buttons.clear()

        lbl = ctk.CTkLabel(
            self.suggestion_container,
            text=f"No matches for '{query}'",
            font=ctk.CTkFont(size=10, slant="italic"),
            text_color="#888888",
            height=24,
        )
        lbl.pack(fill="x", padx=6, pady=3)

        if not self.suggestion_container.winfo_ismapped():
            self.suggestion_container.pack(side="top", fill="x", pady=(2, 0))

    def _render_suggestions(self):
        for w in self.suggestion_container.winfo_children():
            w.destroy()
        self._suggestion_buttons.clear()

        for s_idx, (item_idx, item) in enumerate(self._suggestion_items):
            flag_icon = ""
            if item.flag == FlagState.PICK:
                flag_icon = " ✓"
            elif item.flag == FlagState.REJECT:
                flag_icon = " ✗"
            stars = f" {'★' * item.rating}" if item.rating > 0 else ""
            label_text = f"#{item_idx + 1}  {item.filename}{flag_icon}{stars}"

            btn = ctk.CTkButton(
                self.suggestion_container,
                text=label_text,
                anchor="w",
                height=24,
                fg_color="#1f538d" if s_idx == self._highlighted_suggestion_idx else "transparent",
                hover_color="#2b3b4c",
                text_color="#ffffff",
                font=ctk.CTkFont(size=11),
                command=lambda i=item_idx: self.jump_to_index(i),
            )
            btn.pack(fill="x", padx=2, pady=1)
            btn.bind("<Enter>", lambda e, s=s_idx: self._on_suggestion_hover(s))
            self._suggestion_buttons.append(btn)

        if not self.suggestion_container.winfo_ismapped():
            self.suggestion_container.pack(side="top", fill="x", pady=(2, 0))

    def _on_suggestion_hover(self, s_idx: int):
        self._highlighted_suggestion_idx = s_idx
        self._update_suggestion_highlight()

    def _update_suggestion_highlight(self):
        for s_idx, btn in enumerate(self._suggestion_buttons):
            if s_idx == self._highlighted_suggestion_idx:
                btn.configure(fg_color="#1f538d")
            else:
                btn.configure(fg_color="transparent")

    def _on_search_down(self, event=None):
        if not self._suggestion_items:
            return "break"
        self._highlighted_suggestion_idx = (self._highlighted_suggestion_idx + 1) % len(self._suggestion_items)
        self._update_suggestion_highlight()
        return "break"

    def _on_search_up(self, event=None):
        if not self._suggestion_items:
            return "break"
        self._highlighted_suggestion_idx = (self._highlighted_suggestion_idx - 1) % len(self._suggestion_items)
        self._update_suggestion_highlight()
        return "break"

    def _on_search_return(self, event=None):
        if self._suggestion_items:
            target_s_idx = (
                self._highlighted_suggestion_idx
                if 0 <= self._highlighted_suggestion_idx < len(self._suggestion_items)
                else 0
            )
            item_idx, _ = self._suggestion_items[target_s_idx]
            self.jump_to_index(item_idx)
        return "break"

    def _on_search_escape(self, event=None):
        self._hide_suggestions()
        try:
            self.scroll_frame.focus_set()
        except Exception:
            pass
        return "break"

    def _hide_suggestions(self):
        self._suggestion_items = []
        self._highlighted_suggestion_idx = -1
        for w in self.suggestion_container.winfo_children():
            try:
                w.destroy()
            except Exception:
                pass
        self._suggestion_buttons.clear()
        if self.suggestion_container.winfo_ismapped():
            self.suggestion_container.pack_forget()

    def jump_to_index(self, idx: int):
        """Select and jump to the image item at index idx."""
        items = self._pending_items if self._pending_items else self.row_pool.items
        if not (0 <= idx < len(items)):
            return
        item = items[idx]
        self._hide_suggestions()
        if self.on_select_image:
            self.on_select_image(idx, item.path, is_continuous=False, is_ctrl=False, is_shift=False, from_click=False)
        else:
            self.set_selected_index(idx, item.path)

    def focus_search(self):
        """Focus the search input and select existing text."""
        try:
            self.search_entry.focus_set()
            self.search_entry.select_range(0, "end")
            self.search_entry.icursor("end")
        except Exception:
            pass

    def clear_search(self, keep_focus: bool = True):
        """Clear search query and hide suggestions."""
        self.search_var.set("")
        self._hide_suggestions()
        if keep_focus:
            try:
                self.search_entry.focus_set()
            except Exception:
                pass

    def _handle_chk_toggled(self, idx: int):
        self._on_btn_clicked(idx, None)

    def _on_btn_clicked(self, idx: int, path: Optional[Path], event=None):
        is_ctrl = False
        is_shift = False

        try:
            import ctypes
            VK_SHIFT = 0x10
            VK_CONTROL = 0x11
            is_shift = bool(ctypes.windll.user32.GetKeyState(VK_SHIFT) & 0x8000)
            is_ctrl = bool(ctypes.windll.user32.GetKeyState(VK_CONTROL) & 0x8000)
        except Exception:
            pass

        if event is not None:
            state = getattr(event, "state", 0)
            if not is_shift:
                is_shift = bool(state & 0x0001)
            if not is_ctrl:
                is_ctrl = bool(state & 0x0004 or state & 0x20000)

        self.on_select_image(idx, path, is_continuous=False, is_ctrl=is_ctrl, is_shift=is_shift, from_click=True)

    def set_image_loader(self, image_loader: ImageLoader):
        self.image_loader = image_loader

    def shutdown(self):
        """
        Cleanly shutdown background thread pool executor on window exit.
        """
        if self._thumb_submit_after_id is not None:
            try:
                self.after_cancel(self._thumb_submit_after_id)
            except Exception:
                pass
            self._thumb_submit_after_id = None
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def set_selected_indices(self, selected_indices: Set[int], active_idx: int, active_path: Optional[Path] = None, auto_scroll: bool = True):
        # The pool only holds the visible window, so the item count comes from the
        # item list, not from the number of widgets.
        total_items = len(self._pending_items)
        sel_count = len(selected_indices)
        self.lbl_selection_count.configure(text=f"({sel_count} Selected)")

        active_path_str = str(active_path) if active_path else None
        prev_sel = getattr(self, "_prev_selected_indices", set())
        prev_act = getattr(self, "_prev_active_idx", -1)
        prev_path = getattr(self, "_prev_active_path_str", None)

        self._current_selected_indices = set(selected_indices)
        self._current_active_idx = active_idx
        self._current_active_path_str = active_path_str

        # Compute exact set of row indices that changed state
        changed_indices = (selected_indices ^ prev_sel) | {active_idx}
        if prev_act != -1:
            changed_indices.add(prev_act)

        # Bring the active row into view *before* styling, so its slot exists.
        if auto_scroll and total_items > 1:
            self.row_pool.scroll_to_index(active_idx)
            self._drain_pool_now()

        for idx in changed_indices:
            slot = self.row_pool.slot_for_index(idx)
            if slot is not None:
                self.row_pool._style_slot(slot, idx)

        # Ensure previously active slot loses active border if it wasn't caught in changed_indices
        curr_active_slot = self.row_pool.slot_for_index(active_idx)
        prev_active_slot = getattr(self, "_active_slot_ref", None)
        if prev_active_slot and prev_active_slot is not curr_active_slot:
            old_item = prev_active_slot.get("item_index")
            if old_item is not None:
                self.row_pool._style_slot(prev_active_slot, old_item)
            else:
                prev_active_slot["frame"].configure(border_color="#3a3a3a", border_width=1)
                prev_active_slot["applied"]["border"] = ("#3a3a3a", 1)
        self._active_slot_ref = curr_active_slot

        # If the active slot exists but its button isn't in _btn_map (e.g., mid-scroll incremental
        # rebind), force a pool drain so the button gets registered before we try to highlight it.
        if curr_active_slot is not None and active_path_str:
            if active_path_str not in self._btn_map:
                self._drain_pool_now()
                curr_active_slot = self.row_pool.slot_for_index(active_idx)

        self.row_pool.set_selected_index(active_idx)

        # Ensure only the current active button is highlighted in blue
        curr_btn = self._btn_map.get(active_path_str) if active_path_str else None
        # Fallback: if button not in _btn_map but slot exists, grab it directly from the slot body
        if curr_btn is None:
            active_slot = curr_active_slot or self.row_pool.slot_for_index(active_idx)
            if active_slot is not None:
                body = active_slot.get("body")
            if body and isinstance(body, list) and body:
                # Plain row: body[0] is the button
                # Stacked row: body[1] is the strip, buttons are in strip's internal frame
                if len(body) == 1:
                    curr_btn = body[0]
                elif len(body) >= 2:
                    strip = body[1]
                    inner = getattr(strip, "_scrollable_frame", None) or getattr(strip, "_frame", None)
                    if inner is not None:
                        children = inner.winfo_children()
                        if children:
                            curr_btn = children[0]
                    else:
                        children = strip.winfo_children()
                        if children:
                            curr_btn = children[0]

        prev_btn = getattr(self, "_active_btn_ref", None)
        if prev_btn and prev_btn is not curr_btn:
            try:
                prev_btn.configure(fg_color="transparent", hover_color="#4a4a4a")
            except Exception:
                pass
            self._active_btn_ref = None

        if prev_path and prev_path in self._btn_map and prev_path != active_path_str:
            try:
                self._btn_map[prev_path].configure(fg_color="transparent", hover_color="#4a4a4a")
            except Exception:
                pass

        if curr_btn is not None:
            curr_btn.configure(fg_color="#1f538d", hover_color="#2b6cb0")
            self._active_btn_ref = curr_btn
            # Also register in _btn_map if it wasn't there (fallback case)
            if active_path_str and active_path_str not in self._btn_map:
                self._btn_map[active_path_str] = curr_btn

        self._prev_selected_indices = set(selected_indices)
        self._prev_active_idx = active_idx
        self._prev_active_path_str = active_path_str

    def _drain_pool_now(self) -> None:
        """Make the selected row's slot exist, without rebinding the whole screen.

        Selection has to be visible in the same event-loop turn that requested it.
        But a full rebind here made every arrow-key press re-bind all ~21 slots at
        ~5 ms each - about 100 ms per press, which is what turned navigation into a
        freeze. With the recycled pool the max resident rows is ~100, so a full
        window rebind is fast. Use INITIAL_BIND_ROWS so slides complete in one tick.
        """
        pool = self.row_pool
        pool.sync(budget=INITIAL_BIND_ROWS)
        self._arm_thumb_drain()
        self._schedule_thumb_submit(immediate=False)

    def set_selected_index(self, selected_idx: int, active_path: Optional[Path] = None):
        self.set_selected_indices({selected_idx}, selected_idx, active_path)

    def update_single_item_status(self, idx: int, item: ImageItem):
        """
        Updates ONLY the flag indicator bar and star rating text for a single row
        without destroying or re-creating widgets (0ms, zero flickering!).

        Widget updates are skipped when the rendered values are unchanged: a soft
        refresh walks every row, and re-configuring 200 identical labels was the
        single largest cost of switching tabs.
        """
        flag_color = self.flag_color(item)
        cached = self._row_render_cache.get(idx)
        # Only the visible window has widgets; an off-screen row is updated when the
        # pool binds it, so this stays O(1) per call instead of O(rows).
        slot = self.row_pool.slot_for_index(idx)
        if slot is None:
            return

        if cached is None or cached[0] != flag_color:
            try:
                slot["indicator"].configure(fg_color=flag_color)
            except Exception:
                pass

        stars = "★" * item.rating if item.rating > 0 else ""
        text = None

        widget = self._label_map.get(idx)
        if widget is not None:
            if isinstance(widget, ctk.CTkLabel):
                text = f"{self.base_stem_for(item).upper()} [{item.format_name}] {stars}"
            elif isinstance(widget, ctk.CTkButton):
                text = f"{item.filename}\n{stars}" if stars else item.filename
            if text is not None and (cached is None or cached[1] != text):
                try:
                    widget.configure(text=text)
                except Exception:
                    pass

        self._row_render_cache[idx] = (flag_color, text)

    def _invalidate_row_render_cache(self, idx: Optional[int] = None):
        """Forget cached render state so the next status update re-applies it."""
        if idx is None:
            self._row_render_cache.clear()
        else:
            self._row_render_cache.pop(idx, None)

    def _create_placeholder_image(self, size=(70, 70)) -> Image.Image:
        img = Image.new("RGB", size, color="#222222")
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, size[0]-1, size[1]-1], outline="#383838")
        return img

    def _make_placeholder(self, size: int) -> ctk.CTkImage:
        pil = self._create_placeholder_image((size, size))
        return ctk.CTkImage(light_image=pil, dark_image=pil, size=(size, size))

    def placeholder_image(self, size: int) -> ctk.CTkImage:
        return getattr(self, f"_placeholder_ctk_{size}")

    def row_font(self, bold: bool = False) -> ctk.CTkFont:
        return self._font_bold if bold else self._font_regular

    @staticmethod
    def flag_color(item: ImageItem) -> str:
        if item.flag == FlagState.PICK:
            return "#2b9348"
        if item.flag == FlagState.REJECT:
            return "#d90429"
        return "#4a4e69"

    @staticmethod
    def base_stem_for(item: ImageItem) -> str:
        return CullingSession.extract_base_stem(item.stacked_paths[0].stem)

    def _on_grid_resize(self, event=None) -> None:
        """A wider or shorter grid needs a different number of pooled rows.

        Bound after CTkScrollableFrame's own ``<Configure>`` handler, so the scroll
        region this grid relies on is re-applied on top of the one it resets. Guarded on
        an actual size change: the handler also fires when the scroll region is written
        back, and syncing on that would leave a rebind pending forever.
        """
        size = (event.width, event.height) if event is not None else (0, 0)
        self.row_pool._update_spacer()
        if size == self._grid_size:
            return
        self._grid_size = size
        self.row_pool.request_sync(reason="resize")

    def on_rows_bound(self, pool: "RowPool", bound_slots: List[int]) -> None:
        """The pool rebound some rows: refresh their status and debounce thumbnail load."""
        for slot_index in bound_slots:
            item_index = pool._slot_items[slot_index]
            if item_index is None:
                continue
            self.update_single_item_status(item_index, pool.items[item_index])
        if bound_slots:
            self._schedule_thumb_submit(immediate=False)
        self._update_progress_ui()

    def _schedule_thumb_submit(self, immediate: bool = False) -> None:
        """Cooldown / debounce for thumbnail loading batches.

        Avoids flooding ImageLoader with dozens of intermediate batches while
        the user drags the scrollbar or holds the Down key, while ensuring the
        final resting position is always loaded.
        """
        if not self.image_loader:
            self._batch_raw_requests = []
            self._batch_other_requests = []
            return

        if self._thumb_submit_after_id is not None:
            try:
                self.after_cancel(self._thumb_submit_after_id)
            except Exception:
                pass
            self._thumb_submit_after_id = None

        if immediate:
            self._drain_debounced_thumb_requests()
        else:
            self._thumb_submit_after_id = self.after(
                THUMB_LOAD_COOLDOWN_MS,
                self._drain_debounced_thumb_requests,
            )

    def _drain_debounced_thumb_requests(self) -> None:
        """Submit pending thumbnail requests for the visible viewport rows."""
        self._thumb_submit_after_id = None
        if not self.image_loader or not self.row_pool.items:
            self._batch_raw_requests = []
            self._batch_other_requests = []
            return

        pool = self.row_pool
        # Prioritize rows currently on screen in the viewport first
        vis_indices = pool.viewport_item_indices()
        all_window = pool.visible_item_indices()
        seen = set(vis_indices)
        target_indices = vis_indices + [i for i in all_window if i not in seen]

        self._batch_raw_requests = []
        self._batch_other_requests = []
        for idx in target_indices:
            if idx < len(pool.items):
                self._queue_thumbs_for_item(idx, pool.items[idx])

        if self._batch_raw_requests or self._batch_other_requests:
            self._submit_pending_thumb_requests(getattr(self, "_current_load_id", self._load_id))
        self._arm_thumb_drain()

    def _submit_thumbs_for_viewport(self) -> None:
        """Force immediate thumbnail load for current viewport (bypasses debounce)."""
        if not self.image_loader or not self.row_pool.items:
            return
        pool = self.row_pool
        vis_indices = pool.viewport_item_indices()
        all_window = pool.visible_item_indices()
        seen = set(vis_indices)
        target_indices = vis_indices + [i for i in all_window if i not in seen]

        self._batch_raw_requests = []
        self._batch_other_requests = []
        for idx in target_indices:
            if idx < len(pool.items):
                self._queue_thumbs_for_item(idx, pool.items[idx])

        if self._batch_raw_requests or self._batch_other_requests:
            self._submit_pending_thumb_requests(getattr(self, "_current_load_id", self._load_id))
        self._arm_thumb_drain()

    def _rebuild_btn_map_for_visible(self) -> None:
        """Rebuild _btn_map for all currently visible slots (fixes stale map after rebinds)."""
        pool = self.row_pool
        for idx in pool.visible_item_indices():
            slot = pool.slot_for_index(idx)
            if slot is None:
                continue
            item = pool.items[idx] if idx < len(pool.items) else None
            if item is None:
                continue
            body = slot.get("body")
            if not body or not isinstance(body, list):
                continue
            # Plain row
            if len(body) == 1:
                btn = body[0]
                path_str = str(item.path)
                if path_str and btn:
                    self._btn_map[path_str] = btn
            # Stacked row
            elif len(body) >= 2:
                strip = body[1]
                inner = getattr(strip, "_scrollable_frame", None) or getattr(strip, "_frame", None)
                if inner is not None:
                    for widget in inner.winfo_children():
                        if hasattr(widget, "cget"):
                            try:
                                text = widget.cget("text")
                                for p in item.stacked_paths:
                                    if p.name == text:
                                        self._btn_map[str(p)] = widget
                                        break
                            except Exception:
                                pass
                else:
                    for widget in strip.winfo_children():
                        if hasattr(widget, "cget"):
                            try:
                                text = widget.cget("text")
                                for p in item.stacked_paths:
                                    if p.name == text:
                                        self._btn_map[str(p)] = widget
                                        break
                            except Exception:
                                pass

    def _queue_thumbs_for_item(self, item_index: int, item: ImageItem) -> None:
        if not self.image_loader:
            return
        white_balance = self._pending_white_balance
        load_id = getattr(self, "_current_load_id", self._load_id)
        if not getattr(item, "is_placeholder", False) and item.is_stacked and len(item.stacked_paths) >= 2:
            targets = [(p, (90, 90)) for p in item.stacked_paths]
        else:
            targets = [(item.path, (80, 80))]
        for path, size in targets:
            path_str = str(path)
            if path_str in self._ctk_img_cache or self._is_thumb_pending(path_str, load_id):
                continue
            request = (path, size, white_balance)
            if path.suffix.lower() == ".arw":
                self._batch_raw_requests.append(request)
            else:
                self._batch_other_requests.append(request)

    def _count_thumb_requests(self, items: List[ImageItem]) -> int:
        count = 0
        for item in items:
            if not getattr(item, "is_placeholder", False) and item.is_stacked and len(item.stacked_paths) >= 2:
                count += len(item.stacked_paths)
            else:
                count += 1
        return count

    def _count_cached_thumb_requests(self, items: List[ImageItem]) -> int:
        count = 0
        for item in items:
            if not getattr(item, "is_placeholder", False) and item.is_stacked and len(item.stacked_paths) >= 2:
                for sub_p in item.stacked_paths:
                    if str(sub_p) in self._ctk_img_cache:
                        count += 1
            else:
                if str(item.path) in self._ctk_img_cache:
                    count += 1
        return count

    def _cancel_batch_chain(self):
        """Stop any in-flight row batch and drop results queued for an older load.

        Without this, switching tabs repeatedly left several batch chains running;
        each one built rows for an item set that was no longer displayed.
        """
        self._cancel_row_chain()
        self._thumb_result_queue.clear()

    def _cancel_row_chain(self) -> None:
        """Cancel only the pending row-build tick.

        Decoded thumbnails already queued for painting are left alone: they are keyed
        by path and are still valid for whatever tab asks for them next, and dropping
        them would leave rows blank until the next decode.
        """
        if self._batch_after_id is not None:
            try:
                self.after_cancel(self._batch_after_id)
            except Exception:
                pass
            self._batch_after_id = None

    @staticmethod
    def _row_signature(items: List[ImageItem]):
        """Identity of what each grid row renders, for the soft-refresh check.

        Comparing only the primary path is not enough: a rescan can keep the same
        primary path while the stack composition changes (the JPG of an ARW+JPG
        pair is deleted, or a RAW appears next to a lone JPG). The row height,
        the filename label and the extra stacked thumbnails all depend on the
        stack, so a changed signature must rebuild the rows instead of reusing
        the existing buttons.
        """
        return [
            (
                str(item.path),
                tuple(str(p) for p in item.stacked_paths),
                item.filename,
                bool(getattr(item, "is_placeholder", False)),
            )
            for item in items
        ]

    def update_items(self, items: List[ImageItem], selected_idx: int = 0, white_balance: str = "camera"):
        log_debug(f"ThumbnailList.update_items: updating {len(items)} items, selected_idx={selected_idx}")
        self.lbl_title.configure(text=f"Images ({len(items)})")

        self._load_id += 1
        current_load_id = self._load_id
        self._current_load_id = current_load_id

        self._pending_items = list(items)
        self._pending_selected_idx = selected_idx
        self._pending_white_balance = white_balance
        self._batch_selected_idx = selected_idx
        self._batch_white_balance = white_balance
        self._batch_raw_requests = []
        self._batch_other_requests = []

        new_signature = self._row_signature(items)
        soft = self._current_item_signature == new_signature

        if not soft:
            self.clear_search(keep_focus=False)
            self._reset_rows()

        self._current_item_signature = new_signature
        self._prev_selected_indices = {selected_idx}
        self._prev_active_idx = selected_idx

        if not items:
            self.row_pool.set_items([])
            self.row_pool.sync()
            self._total_thumbs = 0
            self.finish_load_timing()
            return

        self.row_pool.set_items(items)
        self.row_pool.set_selected_index(selected_idx)
        self._total_thumbs = 0
        self._loaded_thumbs = 0

        # Bind the first screen before returning: the grid is empty at this point, and
        # the caller (a tab switch) paints the selection in the same turn.
        self.row_pool.sync(budget=INITIAL_BIND_ROWS)
        self._arm_thumb_drain()
        self._schedule_thumb_submit(immediate=True)

        if soft:
            # Same rows as last time. Only the visible ones have widgets, so this is a
            # bounded walk, and the statuses of the rest are re-applied lazily as the
            # pool rebinds them.
            cached_count = self._count_cached_thumb_requests(items)
            self._total_thumbs = cached_count
            self._loaded_thumbs = cached_count
            self.start_thumb_timing()
            self._update_progress_ui()
            return

        self.start_thumb_timing(reset=True)
        self._update_progress_ui()

    def _reset_rows(self) -> None:
        """Drop every row and cached render, but keep the pooled widgets.

        Destroying the pool itself would be a rebuild, which is exactly the cost the
        pool exists to avoid; only the contents change between item sets.
        """
        if self._thumb_submit_after_id is not None:
            try:
                self.after_cancel(self._thumb_submit_after_id)
            except Exception:
                pass
            self._thumb_submit_after_id = None
        self._batch_raw_requests = []
        self._batch_other_requests = []
        self._cancel_row_chain()
        self.row_pool.shutdown()
        self._btn_map.clear()
        self._row_frame_map.clear()
        self._indicator_map.clear()
        self._label_map.clear()
        self._checkbox_map.clear()
        self._invalidate_row_render_cache()
        self._ctk_img_cache.clear()
        self._inflight_thumbs.clear()
        self._failed_thumbs.clear()
        self._prev_selected_indices = set()
        self._prev_active_idx = -1
        self._prev_active_path_str = None
        self._loaded_thumbs = 0

        pool = self.row_pool
        pool._release_all_slots()
        pool._first_visible = 0
        pool._last_visible = -1
        pool._last_scroll_offset = -1
        pool._forced_offset = None
        pool._update_window_position(0)

    def _submit_pending_thumb_requests(self, load_id: int) -> None:
        """Hand every queued thumbnail request to the worker pool, then clear.

        The queue only ever holds requests for rows the pool just bound, so this is
        bounded by the viewport rather than by the size of the folder.
        """
        if not self.image_loader:
            self._batch_raw_requests = []
            self._batch_other_requests = []
            return
        self._total_thumbs += len(self._batch_raw_requests) + len(self._batch_other_requests)
        raw, other = self._batch_raw_requests, self._batch_other_requests
        self._batch_raw_requests = []
        self._batch_other_requests = []
        for path, size, wb in raw:
            self._load_single_thumb_async(path, size, wb, load_id)
        for path, size, wb in other:
            self._load_single_thumb_async(path, size, wb, load_id)


    def forget_paths(self, paths) -> int:
        """Drop cached thumbnail renders and in-flight bookkeeping for these files.

        A closed tab's photos must not leave a ``CTkImage`` (a full PIL copy plus Tk's
        own copy) alive in the grid cache for the life of the process.
        """
        keys = {str(p) for p in paths}
        if not keys:
            return 0
        removed = 0
        for key in list(self._ctk_img_cache):
            if key in keys:
                self._ctk_img_cache.pop(key, None)
                removed += 1
        for key in list(self._inflight_thumbs):
            if key in keys:
                self._inflight_thumbs.pop(key, None)
        self._failed_thumbs -= keys
        # Queued results for a closed tab have nowhere to go; painting one would put a
        # thumbnail on a button that belongs to a different folder.
        self._thumb_result_queue = type(self._thumb_result_queue)(
            item for item in self._thumb_result_queue if item[0] not in keys
        )
        return removed

    def _is_thumb_load_complete(self) -> bool:
        """True when the pool is settled and no thumbnail request is outstanding.

        Completion must not be derived from the loaded/total counters: requests that
        were already in flight from an earlier load are painted but never counted, so
        the counters can never meet and the duration timer would run forever. Nor can
        it require every item to have a row: the pool only ever binds what is
        visible, so "all rows built" would never become true for a large folder.
        """
        pool = self.row_pool
        return (pool._rebind_after_id is None
                and self._thumb_submit_after_id is None
                and not self._inflight_thumbs
                and not self._thumb_result_queue)

    def _update_progress_ui(self):
        """Settle the load cycle once there is nothing left to wait for.

        There is no progress bar or "N / M" counter any more. The count it reported was
        the number of *bound* rows that had been decoded, which after virtualisation is a
        window that changes as you scroll - a batch count, not a progress measure. What
        remains of this is the part that actually mattered: stopping the duration timer
        when the load settles.
        """
        if self._is_thumb_load_complete():
            self.freeze_load_timing()

    def _is_thumb_pending(self, path_str: str, load_id: int = 0) -> bool:
        """True when this path is already queued, decoding, or known not to yield a thumb.

        Deliberately independent of ``load_id``: a decoded thumbnail is keyed by path
        and reused by whichever load asks for it next, so switching away and back
        mid-load must not queue the same decode again.
        """
        return path_str in self._inflight_thumbs or path_str in self._failed_thumbs

    def _load_single_thumb_async(self, file_path: Path, max_size: Tuple[int, int], white_balance: str, load_id: int):
        path_str = str(file_path)
        self._failed_thumbs.discard(path_str)
        self._inflight_thumbs[path_str] = load_id

        def worker():
            try:
                pil_thumb = self.image_loader.get_thumbnail(
                    file_path,
                    max_size=max_size,
                    raw_scale=0.10,
                    white_balance=white_balance
                )
                if pil_thumb:
                    self._queue_thumb_result(path_str, pil_thumb, load_id)
                else:
                    # Nothing to paint: stop looking in flight, but remember that this
                    # path is not worth retrying on every refresh.
                    with self._inflight_lock:
                        self._inflight_thumbs.pop(path_str, None)
                        self._failed_thumbs.add(path_str)
            except Exception as e:
                log_error(f"Error generating thumbnail for {file_path.name}", exc_info=True)
                with self._inflight_lock:
                    self._inflight_thumbs.pop(path_str, None)
                    self._failed_thumbs.add(path_str)

        self._executor.submit(worker)

    def _queue_thumb_result(self, path_str: str, pil_thumb: Image.Image, load_id: int):
        """Hand a decoded thumbnail to the UI thread.

        Called from decode workers, so only the queue is touched here: registering a Tk
        timer from a worker is not thread-safe, and when it did fail nothing ever
        retried it. The result sat in the queue forever, so the row kept its placeholder
        and the path stayed "in flight" - a blank grid that never finished loading. The
        UI thread schedules the drain instead, from _arm_thumb_drain.
        """
        with self._inflight_lock:
            self._thumb_result_queue.append((path_str, pil_thumb, load_id))

    def _arm_thumb_drain(self) -> None:
        """Schedule the drain if results are waiting. UI thread only.

        Called from the places the UI thread already visits - the pool's scroll poll,
        update_items and selection changes - so a result is always delivered within one
        poll interval even if the worker could not schedule the drain itself.
        """
        if self._thumb_result_after_id is not None:
            return
        if not self._thumb_result_queue:
            return
        try:
            self._thumb_result_after_id = self.after(1, self._drain_thumb_results)
        except Exception:
            self._thumb_result_after_id = None

    def _drain_thumb_results(self):
        """Apply every thumbnail that finished since the last tick, in one pass."""
        self._thumb_result_after_id = None
        if not self._thumb_result_queue:
            return

        current_load_id = getattr(self, "_current_load_id", None)
        applied = 0
        while self._thumb_result_queue:
            path_str, pil_thumb, load_id = self._thumb_result_queue.popleft()
            with self._inflight_lock:
                self._inflight_thumbs.pop(path_str, None)
            if current_load_id is not None and load_id != current_load_id:
                continue
            self._apply_thumb_image(path_str, pil_thumb)
            applied += 1

        if applied:
            self._loaded_thumbs += applied
            self._update_progress_ui()
            self._update_pool_stats()

    def _apply_thumb_image(self, path_str: str, pil_thumb: Image.Image) -> bool:
        w, h = pil_thumb.size
        ctk_img = ctk.CTkImage(light_image=pil_thumb, dark_image=pil_thumb, size=(w, h))
        self._ctk_img_cache[path_str] = ctk_img
        btn = self._btn_map.get(path_str)
        if btn is None:
            return False
        btn.configure(image=ctk_img)
        return True

    def _update_btn_image(self, path_str: str, pil_thumb: Image.Image, load_id: int):
        """Apply one thumbnail immediately (kept for direct callers and tests)."""
        if load_id != getattr(self, "_current_load_id", load_id):
            return
        self._apply_thumb_image(path_str, pil_thumb)
        self._loaded_thumbs += 1
        self._update_progress_ui()
        self._update_pool_stats()

    def destroy(self):
        if hasattr(self, "_batch_after_id") and self._batch_after_id is not None:
            try:
                self.after_cancel(self._batch_after_id)
            except Exception:
                pass
            self._batch_after_id = None
        if getattr(self, "_thumb_result_after_id", None) is not None:
            try:
                self.after_cancel(self._thumb_result_after_id)
            except Exception:
                pass
            self._thumb_result_after_id = None
        if getattr(self, "_thumb_submit_after_id", None) is not None:
            try:
                self.after_cancel(self._thumb_submit_after_id)
            except Exception:
                pass
            self._thumb_submit_after_id = None
        if hasattr(self, "_executor"):
            self._executor.shutdown(wait=False, cancel_futures=True)
        self._reset_load_timing()
        super().destroy()
