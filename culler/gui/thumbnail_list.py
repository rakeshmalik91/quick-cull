import os
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


class ThumbnailList(ctk.CTkFrame):
    """
    Left sidebar displaying image thumbnails.
    Supports big 70x70 thumbnails for stacked photo variants with horizontal scrolling and zero scrollbar clipping.
    Supports multi-select checkboxes, Select All / Select None, and Ctrl/Shift mouse selection.
    """

    BATCH_SIZE = 20
    #: Wall-clock budget for one UI-thread row batch. Bounds the longest
    #: uninterrupted stretch of Tk work during a tab switch.
    ROW_BUILD_BUDGET_MS = 30

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
        super().__init__(master, width=340, corner_radius=5, **kwargs)
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

        self._btn_map: Dict[str, ctk.CTkButton] = {}
        self._row_frame_map: Dict[int, ctk.CTkFrame] = {}
        self._indicator_map: Dict[int, ctk.CTkFrame] = {}
        self._label_map: Dict[int, Union[ctk.CTkLabel, ctk.CTkButton]] = {}
        self._row_render_cache: Dict[int, Tuple[str, str]] = {}
        # Decoded thumbnails land here from the worker threads; one UI tick drains
        # the whole queue. Previously every thumbnail scheduled its own after(0),
        # so a folder load queued one Tk task per photo.
        self._thumb_result_queue: Deque[Tuple[str, Image.Image, int]] = deque()
        self._thumb_result_after_id: Optional[str] = None
        # path_str -> load_id of the request currently in flight. Without this, every
        # soft refresh (switching away and back mid-load) re-queued work already in
        # progress, so a folder load submitted each decode several times over.
        self._inflight_thumbs: Dict[str, int] = {}
        self._checkbox_map: Dict[int, ctk.CTkCheckBox] = {}
        self._ctk_img_cache: Dict[str, ctk.CTkImage] = {}

        self._batch_after_id: Optional[str] = None
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

        self.scroll_frame = ctk.CTkScrollableFrame(self, label_text="")
        self.scroll_frame.pack(side="top", fill="both", expand=True, padx=2, pady=2)

        self.progress_frame = ctk.CTkFrame(self, fg_color="transparent", height=36)
        self.progress_frame.pack(side="bottom", fill="x", padx=4, pady=(0, 4))
        self.progress_frame.pack_propagate(False)

        self.lbl_load_timing = ctk.CTkLabel(
            self.progress_frame,
            text="",
            height=16,
            font=ctk.CTkFont(size=9),
            text_color="#6f8ba6",
            anchor="w"
        )
        self.lbl_load_timing.pack(side="top", fill="x", padx=(2, 0))

        self.progress_row = ctk.CTkFrame(self.progress_frame, fg_color="transparent", height=20)
        self.progress_row.pack(side="top", fill="x")
        self.progress_row.pack_propagate(False)
        self.progress_row.grid_columnconfigure(1, weight=1)

        self.lbl_progress_text = ctk.CTkLabel(
            self.progress_row,
            text="",
            font=ctk.CTkFont(size=9),
            text_color="#888888",
            anchor="w"
        )
        self.lbl_progress_text.grid(row=0, column=0, sticky="w", padx=(2, 0))

        self.progress_bar = ctk.CTkProgressBar(
            self.progress_row,
            height=10,
            corner_radius=5,
            fg_color="#2b2b2b",
            progress_color="#3a86ff"
        )
        self.progress_bar.set(0.0)
        self.progress_bar.grid(row=0, column=1, sticky="ew", padx=(4, 2))

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

    def _update_load_timing(self):
        if self._folder_time_final is None and self._folder_time_start is None:
            self.lbl_load_timing.configure(text="")
            return
        parts = []
        folder_elapsed = self._elapsed_folder()
        if folder_elapsed is not None:
            parts.append(f"Folder: {self._format_elapsed(folder_elapsed)}")
        thumb_elapsed = self._elapsed_thumb()
        if thumb_elapsed is not None:
            parts.append(f"Thumbs: {self._format_elapsed(thumb_elapsed)}")
        self.lbl_load_timing.configure(text="   |   ".join(parts))

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
        self.lbl_load_timing.configure(text="")

    def _handle_select_all(self):
        if self.on_select_all:
            self.on_select_all()

    def _handle_select_none(self):
        if self.on_select_none:
            self.on_select_none()

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
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def set_selected_indices(self, selected_indices: Set[int], active_idx: int, active_path: Optional[Path] = None, auto_scroll: bool = True):
        total_items = len(self._row_frame_map)
        sel_count = len(selected_indices)
        self.lbl_selection_count.configure(text=f"({sel_count} Selected)")

        active_path_str = str(active_path) if active_path else None
        prev_sel = getattr(self, "_prev_selected_indices", set())
        prev_act = getattr(self, "_prev_active_idx", -1)
        prev_path = getattr(self, "_prev_active_path_str", None)

        # Compute exact set of row indices that changed state
        changed_indices = (selected_indices ^ prev_sel) | {active_idx, prev_act}

        for idx in changed_indices:
            if idx not in self._row_frame_map:
                continue
            frame = self._row_frame_map[idx]
            is_active = (idx == active_idx)
            is_selected = (idx in selected_indices)

            if is_active:
                frame.configure(border_color="#1f538d", border_width=2)
            elif is_selected:
                frame.configure(border_color="#ffb703", border_width=2)
            else:
                frame.configure(border_color="#3a3a3a", border_width=1)

            if idx in self._checkbox_map:
                chk = self._checkbox_map[idx]
                if is_selected:
                    chk.select()
                else:
                    chk.deselect()

        # Update button highlights only if active sub-path changed
        if active_path_str != prev_path:
            if prev_path and prev_path in self._btn_map:
                self._btn_map[prev_path].configure(fg_color="transparent")
            if active_path_str and active_path_str in self._btn_map:
                self._btn_map[active_path_str].configure(fg_color="#1f538d")

        self._prev_selected_indices = set(selected_indices)
        self._prev_active_idx = active_idx
        self._prev_active_path_str = active_path_str

        # Auto-scroll thumbnail list so active row is always visible and centered (ONLY during keyboard operations)
        if auto_scroll:
            try:
                if total_items > 1:
                    target_frac = (active_idx - 2) / float(max(1, total_items - 1))
                    target_frac = max(0.0, min(1.0, target_frac))
                    if hasattr(self.scroll_frame, "_parent_canvas"):
                        self.scroll_frame._parent_canvas.yview_moveto(target_frac)
            except Exception:
                pass

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
        flag_color = "#2b9348" if item.flag == FlagState.PICK else (
            "#d90429" if item.flag == FlagState.REJECT else "#4a4e69"
        )
        cached = self._row_render_cache.get(idx)
        if cached is None or cached[0] != flag_color:
            if idx in self._indicator_map:
                self._indicator_map[idx].configure(fg_color=flag_color)

        stars = "★" * item.rating if item.rating > 0 else ""
        text = None

        widget = self._label_map.get(idx)
        if widget is not None:
            if isinstance(widget, ctk.CTkLabel):
                primary_p = item.stacked_paths[0]
                base_stem = CullingSession.extract_base_stem(primary_p.stem)
                text = f"{base_stem.upper()} [{item.format_name}] {stars}"
            elif isinstance(widget, ctk.CTkButton):
                text = f"{item.filename}\n{stars}"
            if text is not None and (cached is None or cached[1] != text):
                widget.configure(text=text)

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

    def _count_thumb_requests(self, items: List[ImageItem]) -> int:
        count = 0
        for item in items:
            if item.is_stacked and len(item.stacked_paths) >= 2:
                count += len(item.stacked_paths)
            else:
                count += 1
        return count

    def _count_cached_thumb_requests(self, items: List[ImageItem]) -> int:
        count = 0
        for item in items:
            if item.is_stacked and len(item.stacked_paths) >= 2:
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
        if self._batch_after_id is not None:
            try:
                self.after_cancel(self._batch_after_id)
            except Exception:
                pass
            self._batch_after_id = None
        self._thumb_result_queue.clear()

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
            (str(item.path), tuple(str(p) for p in item.stacked_paths), item.filename)
            for item in items
        ]

    def update_items(self, items: List[ImageItem], selected_idx: int = 0, white_balance: str = "camera"):
        log_debug(f"ThumbnailList.update_items: updating {len(items)} items, selected_idx={selected_idx}")
        self.lbl_title.configure(text=f"Images ({len(items)})")

        self._load_id += 1
        current_load_id = self._load_id
        self._current_load_id = current_load_id

        new_signature = self._row_signature(items)
        if hasattr(self, "_current_item_signature") and self._current_item_signature == new_signature:
            self._cancel_batch_chain()
            self._pending_items = list(items)
            self._pending_selected_idx = selected_idx
            self._pending_white_balance = white_balance
            self._batch_raw_requests.clear()
            self._batch_other_requests.clear()
            self._batch_index = len(items)
            cached_count = self._count_cached_thumb_requests(items)
            self._loaded_thumbs = cached_count
            self.start_thumb_timing()

            prev_selected = getattr(self, "_prev_selected_indices", set())
            prev_act = getattr(self, "_prev_active_idx", -1)
            changed = ({selected_idx} ^ prev_selected) | {selected_idx, prev_act}
            for idx in changed:
                if idx not in self._row_frame_map:
                    continue
                frame = self._row_frame_map[idx]
                is_active = (idx == selected_idx)
                is_selected = (idx == selected_idx)
                if is_active:
                    frame.configure(border_color="#1f538d", border_width=2)
                elif is_selected:
                    frame.configure(border_color="#ffb703", border_width=2)
                else:
                    frame.configure(border_color="#3a3a3a", border_width=1)
            self._prev_selected_indices = {selected_idx}
            self._prev_active_idx = selected_idx

            for idx, item in enumerate(items):
                self.update_single_item_status(idx, item)
                if item.is_stacked and len(item.stacked_paths) >= 2:
                    for sub_p in item.stacked_paths:
                        ext = sub_p.suffix.lower()
                        if self.image_loader and str(sub_p) not in self._ctk_img_cache:
                            req = (sub_p, (90, 90), white_balance)
                            if self._is_thumb_pending(str(sub_p), current_load_id):
                                continue
                            if ext == ".arw":
                                self._batch_raw_requests.append(req)
                            else:
                                self._batch_other_requests.append(req)
                else:
                    ext = item.path.suffix.lower()
                    if self.image_loader and str(item.path) not in self._ctk_img_cache:
                        req = (item.path, (80, 80), white_balance)
                        if self._is_thumb_pending(str(item.path), current_load_id):
                            continue
                        if ext == ".arw":
                            self._batch_raw_requests.append(req)
                        else:
                            self._batch_other_requests.append(req)
            if self.image_loader:
                for path, size, wb in self._batch_raw_requests:
                    self._load_single_thumb_async(path, size, wb, current_load_id)
                for path, size, wb in self._batch_other_requests:
                    self._load_single_thumb_async(path, size, wb, current_load_id)

            self._total_thumbs = (cached_count
                                  + len(self._batch_raw_requests)
                                  + len(self._batch_other_requests))
            self._current_load_id = current_load_id
            self._update_progress_ui()
            return

        self._current_item_signature = new_signature

        self._cancel_batch_chain()

        self._btn_map.clear()
        self._row_frame_map.clear()
        self._indicator_map.clear()
        self._label_map.clear()
        self._checkbox_map.clear()
        self._invalidate_row_render_cache()
        self._ctk_img_cache.clear()
        self._inflight_thumbs.clear()
        self._prev_selected_indices = set()
        self._prev_active_idx = -1
        self._prev_active_path_str = None
        self._loaded_thumbs = 0

        for widget in self.scroll_frame.winfo_children():
            widget.destroy()

        self.progress_bar.set(0.0)
        self.lbl_progress_text.configure(text="")

        self.start_thumb_timing(reset=True)

        self._pending_items = list(items)
        self._pending_selected_idx = selected_idx
        self._pending_white_balance = white_balance

        if not items:
            self._total_thumbs = 0
            self.progress_bar.set(0.0)
            self.lbl_progress_text.configure(text="0 / 0")
            self.finish_load_timing()
            return

        self._total_thumbs = 0

        placeholder_pil_70 = self._create_placeholder_image((70, 70))
        placeholder_ctk_70 = ctk.CTkImage(light_image=placeholder_pil_70, dark_image=placeholder_pil_70, size=(70, 70))

        placeholder_pil_80 = self._create_placeholder_image((80, 80))
        placeholder_ctk_80 = ctk.CTkImage(light_image=placeholder_pil_80, dark_image=placeholder_pil_80, size=(80, 80))

        placeholder_pil_90 = self._create_placeholder_image((90, 90))
        placeholder_ctk_90 = ctk.CTkImage(light_image=placeholder_pil_90, dark_image=placeholder_pil_90, size=(90, 90))

        self._placeholder_ctk_70 = placeholder_ctk_70
        self._placeholder_ctk_80 = placeholder_ctk_80
        self._placeholder_ctk_90 = placeholder_ctk_90

        self._batch_index = 0
        self._batch_selected_idx = selected_idx
        self._batch_white_balance = white_balance
        # Rebuilt for every load cycle; also cleared by the soft-refresh path.
        self._batch_raw_requests = []
        self._batch_other_requests = []

        self._process_next_batch(current_load_id)

    def _process_next_batch(self, load_id: int):
        self._current_load_id = load_id
        items = self._pending_items
        start = self._batch_index
        end = min(start + self.BATCH_SIZE, len(items))
        selected_idx = self._batch_selected_idx
        white_balance = self._batch_white_balance

        # Row construction costs ~10 ms per row, so a fixed 20-row batch blocked the
        # UI for ~200 ms per tick and a full rebuild for seconds. Build until the time
        # budget is spent, then yield, so a tab switch never freezes the app.
        deadline = time.perf_counter() + (self.ROW_BUILD_BUDGET_MS / 1000.0)
        built = 0

        for idx in range(start, end):
            item = items[idx]
            is_selected = (idx == selected_idx)

            row_frame = ctk.CTkFrame(
                self.scroll_frame,
                height=190 if (item.is_stacked and len(item.stacked_paths) >= 2) else 96,
                corner_radius=6,
                border_width=2 if is_selected else 1,
                border_color="#1f538d" if is_selected else "#3a3a3a"
            )
            row_frame.pack(fill="x", padx=1, pady=2)
            row_frame.pack_propagate(False)
            self._row_frame_map[idx] = row_frame

            flag_color = "#2b9348" if item.flag == FlagState.PICK else (
                "#d90429" if item.flag == FlagState.REJECT else "#4a4e69"
            )
            indicator = ctk.CTkFrame(row_frame, width=5, fg_color=flag_color)
            indicator.pack(side="left", fill="y")
            self._indicator_map[idx] = indicator

            chk = ctk.CTkCheckBox(
                row_frame,
                text="",
                width=18,
                height=18,
                checkbox_width=18,
                checkbox_height=18,
                fg_color="#ffb703",
                hover_color="#fb8500",
                command=lambda i=idx: self._handle_chk_toggled(i)
            )
            chk.pack(side="left", padx=(4, 2))
            if is_selected:
                chk.select()
            else:
                chk.deselect()
            self._checkbox_map[idx] = chk

            stars = "★" * item.rating if item.rating > 0 else ""

            if item.is_stacked and len(item.stacked_paths) >= 2:
                info_box = ctk.CTkFrame(row_frame, fg_color="transparent")
                info_box.pack(side="top", fill="x", padx=4, pady=(2, 1))

                import re
                from ..culler_engine import CullingSession

                primary_p = item.stacked_paths[0]
                base_stem = CullingSession.extract_base_stem(primary_p.stem)

                lbl_name = ctk.CTkLabel(
                    info_box,
                    text=f"{base_stem.upper()} [{item.format_name}] {stars}",
                    font=ctk.CTkFont(size=11, weight="bold"),
                    anchor="w"
                )
                lbl_name.pack(side="left")
                self._label_map[idx] = lbl_name
                self._row_render_cache[idx] = (flag_color, f"{base_stem.upper()} [{item.format_name}] {stars}")

                thumb_scroll = ctk.CTkScrollableFrame(
                    row_frame,
                    orientation="horizontal",
                    height=160,
                    fg_color="transparent"
                )
                thumb_scroll.pack(side="top", fill="both", expand=True, padx=2, pady=1)

                for sub_p in item.stacked_paths:
                    badge_label = sub_p.name

                    btn_sub = ctk.CTkButton(
                        thumb_scroll,
                        text=badge_label,
                        image=self._ctk_img_cache.get(str(sub_p), self._placeholder_ctk_90),
                        compound="top",
                        font=ctk.CTkFont(size=10, weight="bold"),
                        width=95,
                        height=95,
                        fg_color="transparent",
                        hover_color="#333333",
                        command=lambda i=idx, p=sub_p: self._on_btn_clicked(i, p)
                    )
                    btn_sub.pack(side="left", padx=3)
                    self._btn_map[str(sub_p)] = btn_sub

                    if self.image_loader and str(sub_p) not in self._ctk_img_cache:
                        req = (sub_p, (90, 90), white_balance)
                        ext = sub_p.suffix.lower()
                        if ext == ".arw":
                            self._batch_raw_requests.append(req)
                        else:
                            self._batch_other_requests.append(req)

                raw_n = sum(1 for p in item.stacked_paths if p.suffix.lower() == ".arw")
                jpg_n = sum(1 for p in item.stacked_paths if p.suffix.lower() in (".jpg", ".jpeg"))
                parts = []
                if raw_n:
                    parts.append(f"{raw_n} ARW")
                if jpg_n:
                    parts.append(f"{jpg_n} JPG")
                comp = ", ".join(parts) if parts else f"{len(item.stacked_paths)} files"
                lbl_name.configure(text=f"{base_stem.upper()} [Stacked: {comp}] {stars}")
            else:
                txt = f"{item.filename} {stars}"

                btn = ctk.CTkButton(
                    row_frame,
                    text=txt,
                    image=self._ctk_img_cache.get(str(item.path), self._placeholder_ctk_80),
                    compound="left",
                    anchor="w",
                    font=ctk.CTkFont(size=11, weight="bold"),
                    height=88,
                    fg_color="transparent",
                    hover_color="#333333",
                    command=lambda i=idx, p=item.path: self._on_btn_clicked(i, p)
                )
                btn.pack(side="left", fill="both", expand=True, padx=2, pady=1)
                self._btn_map[str(item.path)] = btn
                self._label_map[idx] = btn
                self._row_render_cache[idx] = (flag_color, f"{item.filename}\n{stars}")

                if self.image_loader and str(item.path) not in self._ctk_img_cache:
                    req = (item.path, (80, 80), white_balance)
                    ext = item.path.suffix.lower()
                    if ext == ".arw":
                        self._batch_raw_requests.append(req)
                    else:
                        self._batch_other_requests.append(req)

        built += 1
        if built >= 1 and time.perf_counter() >= deadline:
            end = idx + 1
            self._batch_index = end
            self._update_progress_ui()
            self._batch_after_id = self.after(1, lambda: self._process_next_batch(load_id))
            return

        self._batch_index = end
        self._update_progress_ui()

        if end < len(items):
            self._batch_after_id = self.after(1, lambda: self._process_next_batch(load_id))
        else:
            self._total_thumbs = len(self._batch_raw_requests) + len(self._batch_other_requests)
            if self.image_loader:
                for path, size, wb in self._batch_raw_requests:
                    self._load_single_thumb_async(path, size, wb, load_id)
                for path, size, wb in self._batch_other_requests:
                    self._load_single_thumb_async(path, size, wb, load_id)
            self._batch_after_id = None

    def _is_thumb_load_complete(self) -> bool:
        """True when every row exists and no thumbnail request is outstanding.

        Completion must not be derived from the loaded/total counters: requests that
        were already in flight from an earlier load are painted but never counted, so
        the counters can never meet and the duration timer would run forever.
        """
        return (self._batch_index >= len(self._pending_items)
                and not self._inflight_thumbs
                and not self._thumb_result_queue)

    def _update_progress_ui(self):
        complete = self._is_thumb_load_complete()

        if self._total_thumbs <= 0:
            self.progress_bar.set(1.0 if complete else 0.0)
            self.lbl_progress_text.configure(text="" if not complete else "Done")
        else:
            pct = min(1.0, self._loaded_thumbs / self._total_thumbs)
            self.progress_bar.set(pct)
            self.lbl_progress_text.configure(
                text=f"{self._loaded_thumbs} / {self._total_thumbs}" if not complete else
                f"{self._total_thumbs} / {self._total_thumbs}")

        if complete:
            self.progress_bar.set(1.0)
            self.freeze_load_timing()

    def _is_thumb_pending(self, path_str: str, load_id: int = 0) -> bool:
        """True when this path is already queued or decoding.

        Deliberately independent of ``load_id``: a decoded thumbnail is keyed by path
        and reused by whichever load asks for it next, so switching away and back
        mid-load must not queue the same decode again.
        """
        return path_str in self._inflight_thumbs

    def _load_single_thumb_async(self, file_path: Path, max_size: Tuple[int, int], white_balance: str, load_id: int):
        path_str = str(file_path)
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
            except Exception as e:
                log_error(f"Error generating thumbnail for {file_path.name}", exc_info=True)
            finally:
                pass

        self._executor.submit(worker)

    def _queue_thumb_result(self, path_str: str, pil_thumb: Image.Image, load_id: int):
        """Hand a decoded thumbnail to the UI thread (coalesced)."""
        self._thumb_result_queue.append((path_str, pil_thumb, load_id))
        if self._thumb_result_after_id is None:
            self._thumb_result_after_id = self.after(1, self._drain_thumb_results)

    def _drain_thumb_results(self):
        """Apply every thumbnail that finished since the last tick, in one pass."""
        self._thumb_result_after_id = None
        if not self._thumb_result_queue:
            return

        current_load_id = getattr(self, "_current_load_id", None)
        applied = 0
        while self._thumb_result_queue:
            path_str, pil_thumb, load_id = self._thumb_result_queue.popleft()
            # Paint whenever a row for this path still exists: thumbnails are keyed by
            # path, so a decode started for the previous tab is still valid here.
            self._apply_thumb_image(path_str, pil_thumb)
            self._inflight_thumbs.pop(path_str, None)
            if current_load_id is not None and load_id != current_load_id:
                continue
            # Count only the current load, so progress still reaches 100%.
            applied += 1

        if applied:
            self._loaded_thumbs += applied
            self._update_progress_ui()

    def _apply_thumb_image(self, path_str: str, pil_thumb: Image.Image) -> bool:
        btn = self._btn_map.get(path_str)
        if btn is None:
            return False
        w, h = pil_thumb.size
        ctk_img = ctk.CTkImage(light_image=pil_thumb, dark_image=pil_thumb, size=(w, h))
        self._ctk_img_cache[path_str] = ctk_img
        btn.configure(image=ctk_img)
        return True

    def _update_btn_image(self, path_str: str, pil_thumb: Image.Image, load_id: int):
        """Apply one thumbnail immediately (kept for direct callers and tests)."""
        if load_id != getattr(self, "_current_load_id", load_id):
            return
        self._apply_thumb_image(path_str, pil_thumb)
        self._loaded_thumbs += 1
        self._update_progress_ui()

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
        if hasattr(self, "_executor"):
            self._executor.shutdown(wait=False, cancel_futures=True)
        self._reset_load_timing()
        super().destroy()
