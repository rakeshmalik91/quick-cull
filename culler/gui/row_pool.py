"""A recycled pool of grid rows bound to a window of a virtual item list.

The grid used to build one widget set per photo. That is fine for a couple of
hundred rows and unusable for a few thousand: Tk re-lays-out every packed child
whenever the container grows, so building n rows costs O(n^2). Measured on a
2771-ARW folder: 111 s to build the grid, of which 82 s was Tk's re-layout and
only 29 s was widget construction, and the whole chain drained inside one
event-loop pass, so the app was frozen for the duration.

This module keeps a fixed pool of row widgets - one screenful plus overscan -
and rebinds them to whichever item indices the scroll position exposes. The
per-frame cost becomes O(pool) instead of O(items).

The scrollable frame holds exactly two children: the pool and a spacer whose
height makes the scrollbar reflect the true content height, so a 2771-row
folder scrolls like a 25-row one.

Row heights vary (a stacked ARW+JPG row is twice as tall), so the scroll offset
is mapped to an item index through a prefix-sum of heights rather than a divide.
"""

import bisect
from typing import Callable, Dict, List, Optional, Tuple

import customtkinter as ctk

from ..logger import log_debug

#: Heights, including the pack padding, matching the previous per-item layout.
ROW_HEIGHT = 96
ROW_PADDING = 4
ROW_STRIDE = ROW_HEIGHT + ROW_PADDING

STACKED_ROW_HEIGHT = 190
STACKED_ROW_STRIDE = STACKED_ROW_HEIGHT + ROW_PADDING

#: Extra slots above and below the viewport, so a short scroll needs no rebind.
ROW_OVERSCAN = 45

#: Target pool size kept resident in memory.
POOL_TARGET_ROWS = 100
POOL_MIN_ROWS = 10

#: Differential sliding window settings:
SLIDE_CHUNK = 30
SLIDE_MARGIN = 20

#: Rows rebound per UI tick *while scrolling*.
REBIND_ROWS_PER_TICK = 10

#: Rows bound synchronously when a new item set arrives.
INITIAL_BIND_ROWS = 100

#: How often the canvas is polled for a scroll. Cheap (one Tk call), and it
#: catches the scrollbar drag as well as the wheel.
SCROLL_POLL_MS = 20


def row_height_for(item) -> int:
    """Height of the row that renders ``item``: stacked pairs are twice as tall, placeholders are default."""
    if getattr(item, "is_placeholder", False):
        return ROW_HEIGHT
    if getattr(item, "is_stacked", False) and len(item.stacked_paths) >= 2:
        return STACKED_ROW_HEIGHT
    return ROW_HEIGHT


class RowPool:
    """Fixed set of recycled rows over a virtual list.

    ``owner`` is the ThumbnailList the pool serves. The pool reads the owner's
    callback and cache attributes directly, because the widgets it builds are
    only meaningful in the context of that grid, but it never owns them.
    """

    def __init__(self, owner, container: ctk.CTkFrame):
        self.owner = owner
        self.container = container

        #: Row widgets live in their own frame so the scrollable frame keeps a constant
        #: child count and Tk's layout stays O(pool) instead of O(items).
        #: Must be opaque matching container to prevent un-erased bitblt ghosting during fast scrolling.
        fg_col = container.cget("fg_color") if hasattr(container, "cget") else ("#ebebeb", "#242424")
        self.pool_frame = ctk.CTkFrame(container, fg_color=fg_col, corner_radius=0)
        self.pool_frame.pack(side="top", fill="x")
        self.spacer: Optional[ctk.CTkFrame] = None

        self.items: List = []
        #: _offsets[i] is the pixel y at which item i starts; len == len(items)+1.
        self._offsets: List[int] = [0]

        self._slots: List[Dict] = []
        #: slot index -> item index it currently renders.
        self._slot_items: List[Optional[int]] = []
        #: item index -> slot index, the reverse of _slot_items.
        self._slot_by_item: Dict[int, int] = {}

        self._first_visible = 0
        self._last_visible = -1
        self._selected_idx = 0
        self._window_start: int = 0
        self._window_end: int = -1

        self._rebind_after_id: Optional[str] = None
        self._scroll_after_id: Optional[str] = None
        self._last_scroll_offset: int = -1
        self._forced_offset: Optional[int] = None
        self._applied_scrollregion: Optional[Tuple[int, int, int, int]] = None
        self._last_viewport: Tuple[int, int] = (0, 0)
        self._pool_dirty = True
        self.rebind_count = 0
        self._hook_scrollbar()

    def _hook_scrollbar(self) -> None:
        """Wrap the scrollbar command so scroll jumps and drags rebind immediately."""
        scrollbar = getattr(self.container, "_scrollbar", None)
        if scrollbar is None or getattr(scrollbar, "_row_pool_hooked", False):
            return
        orig_cmd = scrollbar.cget("command")

        def _on_scroll(*args):
            if orig_cmd:
                try:
                    orig_cmd(*args)
                except Exception:
                    pass
            self._on_scrollbar_movement()

        try:
            scrollbar.configure(command=_on_scroll)
            scrollbar._row_pool_hooked = True
        except Exception:
            pass

        canvas = getattr(scrollbar, "_canvas", None)
        if canvas is not None and not getattr(scrollbar, "_row_pool_release_hooked", False):
            try:
                canvas.bind(
                    "<ButtonRelease-1>",
                    lambda e: self._on_scrollbar_release(),
                    add="+",
                )
                self.owner.bind(
                    "<ButtonRelease-1>",
                    lambda e: self._on_scrollbar_release(),
                    add="+",
                )
                scrollbar._row_pool_release_hooked = True
            except Exception:
                pass

        p_canvas = self._canvas()
        if p_canvas is not None and not getattr(p_canvas, "_row_pool_wheel_hooked", False):
            try:
                p_canvas.bind(
                    "<MouseWheel>",
                    lambda e: self.owner.after_idle(self._on_scrollbar_movement),
                    add="+",
                )
                p_canvas._row_pool_wheel_hooked = True
            except Exception:
                pass

        if not getattr(self.container, "_row_pool_wheel_hooked", False):
            try:
                self.container.bind(
                    "<MouseWheel>",
                    lambda e: self.owner.after_idle(self._on_scrollbar_movement),
                    add="+",
                )
                self.container._row_pool_wheel_hooked = True
            except Exception:
                pass

    def _on_scrollbar_movement(self) -> None:
        """Called immediately whenever the scrollbar is clicked or dragged."""
        self._release_forced_offset()
        if not self.items:
            return
        offset = self._scroll_offset()
        total = len(self.items)

        if self._needs_rebind(offset):
            v_top = self.index_at_offset(offset)
            v_bottom = self.index_at_offset(offset + max(0, self._viewport_height()))
            v_top = max(0, min(v_top, total - 1))
            v_bottom = max(0, min(v_bottom, total - 1))

            if total <= POOL_TARGET_ROWS:
                target_start = 0
                target_end = total - 1
            else:
                target_start = max(0, min(v_top - 40, total - POOL_TARGET_ROWS))
                target_end = target_start + POOL_TARGET_ROWS - 1

            target_count = target_end - target_start + 1
            self._ensure_pool_size(target_count)
            self._update_window_position(self.offset_of(target_start))
            self._update_spacer()

            self._first_visible = v_top
            self._last_visible = v_bottom

            # Coalesce continuous drag motion non-blockingly so rapid drag events
            # never freeze the UI with redundant synchronous full-pool rebinds.
            self.request_sync(reason="scroll")
            if hasattr(self.owner, "_schedule_thumb_submit"):
                self.owner._schedule_thumb_submit(immediate=False)
        elif offset != self._last_scroll_offset:
            self._last_scroll_offset = offset
            self.owner._arm_thumb_drain()

    def _on_scrollbar_release(self) -> None:
        """Called when the user finishes dragging or clicking the scrollbar."""
        self._release_forced_offset()
        offset = self._scroll_offset()
        # Always sync on release: canvas may still be animating, so the window
        # might not match the actual scroll position even if _needs_rebind is False.
        self.sync()
        if offset != self._last_scroll_offset:
            self._last_scroll_offset = offset
            self.owner._arm_thumb_drain()
        if hasattr(self.owner, "_schedule_thumb_submit"):
            self.owner._schedule_thumb_submit(immediate=True)
        # Force immediate thumbnail load for the newly visible viewport rows
        if hasattr(self.owner, "_submit_thumbs_for_viewport"):
            self.owner._submit_thumbs_for_viewport()
        # Rebuild _btn_map for visible slots to fix stale entries after rebinds
        if hasattr(self.owner, "_rebuild_btn_map_for_visible"):
            self.owner._rebuild_btn_map_for_visible()

    # ------------------------------------------------------------------ items

    def set_items(self, items: List) -> None:
        self.items = list(items)
        offsets = [0]
        total = 0
        for item in self.items:
            total += row_height_for(item) + ROW_PADDING
            offsets.append(total)
        self._offsets = offsets
        self._window_start = 0
        self._window_end = -1
        self._hook_scrollbar()

    def clear_items(self) -> None:
        self.items = []
        self._offsets = [0]
        self._window_start = 0
        self._window_end = -1
        self._release_all_slots()
        self._update_window_position(0)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def total_content_height(self) -> int:
        return self._offsets[-1] if self._offsets else 0

    # ---------------------------------------------------------------- geometry

    def index_at_offset(self, offset_px: int) -> int:
        """Item index whose row contains ``offset_px``."""
        if not self.items:
            return 0
        offset_px = max(0, min(offset_px, self.total_content_height - 1))
        idx = bisect.bisect_right(self._offsets, offset_px) - 1
        return max(0, min(idx, len(self.items) - 1))

    def offset_of(self, index: int) -> int:
        if not self._offsets:
            return 0
        index = max(0, min(index, len(self._offsets) - 2))
        return self._offsets[index]

    def visible_indices(self) -> range:
        if self._window_end < self._window_start:
            return range(0, 0)
        return range(self._window_start, self._window_end + 1)

    def visible_item_indices(self) -> List[int]:
        if not self.items or self._window_end < self._window_start:
            return []
        return list(range(self._window_start, self._window_end + 1))

    def viewport_item_indices(self) -> List[int]:
        """Indices of items actually visible within the canvas viewport."""
        if not self.items:
            return []
        offset = self._scroll_offset()
        v_top = self.index_at_offset(offset)
        v_bottom = self.index_at_offset(offset + max(0, self._viewport_height()))
        v_top = max(0, min(v_top, len(self.items) - 1))
        v_bottom = max(0, min(v_bottom, len(self.items) - 1))
        return list(range(v_top, v_bottom + 1))

    def slot_for_index(self, index: int) -> Optional[Dict]:
        """The slot showing ``index``, in O(1).

        A linear scan here ran on every row status update, and status updates run for
        every bound row on every rebind.
        """
        slot_index = self._slot_by_item.get(index)
        if slot_index is None:
            return None
        return self._slots[slot_index]

    # --------------------------------------------------------------- scrolling

    def _canvas(self):
        return getattr(self.container, "_parent_canvas", None)

    def _viewport_height(self) -> int:
        canvas = self._canvas()
        if canvas is None:
            return 0
        return canvas.winfo_height()

    def _scroll_offset(self) -> int:
        """Current top of the viewport, in pixels of content.

        A scroll requested this turn is reported from ``_forced_offset`` rather than read
        back from the canvas: ``yview_moveto`` is asynchronous, and forcing Tk to flush
        idle work to read it back cost a full layout pass on every arrow-key press.
        """
        if self._forced_offset is not None:
            return self._forced_offset
        canvas = self._canvas()
        if canvas is None:
            return 0
        try:
            top = canvas.canvasy(0)
        except Exception:
            return 0
        return self._clamp_offset(top)

    def _clamp_offset(self, top: int) -> int:
        max_top = max(0, self.total_content_height - self._viewport_height())
        return max(0, min(int(top), max_top))

    def _release_forced_offset(self) -> None:
        """Drop the override once the canvas has caught up with what we asked for."""
        if self._forced_offset is None:
            return
        canvas = self._canvas()
        if canvas is None:
            self._forced_offset = None
            return
        try:
            if abs(canvas.canvasy(0) - self._forced_offset) <= 2:
                self._forced_offset = None
        except Exception:
            self._forced_offset = None

    def start_scroll_polling(self) -> None:
        """Watch the canvas so a wheel or scrollbar drag rebinds the pool."""
        if self._scroll_after_id is not None:
            return
        if self._canvas() is None:
            return
        self._scroll_after_id = self.owner.after(SCROLL_POLL_MS, self._poll_scroll)

    def stop_scroll_polling(self) -> None:
        if self._scroll_after_id is not None:
            try:
                self.owner.after_cancel(self._scroll_after_id)
            except Exception:
                pass
            self._scroll_after_id = None

    def _poll_scroll(self) -> None:
        self._scroll_after_id = None
        had_forced_offset = self._forced_offset is not None
        self._release_forced_offset()
        offset = self._scroll_offset()
        # If we just released a forced offset (canvas settled after scrollbar drag),
        # the actual position may differ from what we thought. Force an immediate
        # thumbnail submit for the new position.
        forced_released = had_forced_offset and self._forced_offset is None
        if offset != self._last_scroll_offset:
            self._last_scroll_offset = offset
            if self._needs_rebind(offset):
                self.request_sync(reason="scroll")
        if self.items:
            self.start_scroll_polling()
        # The poll doubles as the grid's heartbeat: it is the one thing that runs on the
        # UI thread every frame regardless of what the user is doing, so it is where a
        # waiting thumbnail result gets picked up.
        self.owner._arm_thumb_drain()
        if forced_released and hasattr(self.owner, "_schedule_thumb_submit"):
            self.owner._schedule_thumb_submit(immediate=True)

    def _needs_rebind(self, offset: int) -> bool:
        """True when the visible row range approaches or exceeds the loaded window boundary."""
        if not self.items or self._window_end < self._window_start:
            return True
        viewport = self._viewport_height()
        view_top = self.index_at_offset(offset)
        view_bottom = self.index_at_offset(offset + max(0, viewport))

        for idx in range(view_top, min(view_bottom + 1, len(self.items))):
            if idx not in self._slot_by_item:
                return True

        if view_bottom >= self._window_end - SLIDE_MARGIN and self._window_end < len(self.items) - 1:
            return True
        if view_top <= self._window_start + SLIDE_MARGIN and self._window_start > 0:
            return True
        if view_top < self._window_start or view_bottom > self._window_end:
            return True
        return False

    def scroll_to_index(self, index: int, align: str = "nearest") -> None:
        """Bring ``index`` into view."""
        if not self.items:
            return
        index = max(0, min(index, len(self.items) - 1))
        self.set_selected_index(index)

        canvas = self._canvas()
        if canvas is not None:
            viewport = self._viewport_height()
            height = row_height_for(self.items[index]) + ROW_PADDING
            top = self.offset_of(index)
            current = self._scroll_offset()

            if align == "top":
                target = top
            elif align == "center":
                target = top - max(0, (viewport - height) // 2)
            else:
                if top < current:
                    target = top
                elif top + height > current + viewport:
                    target = top - viewport + height
                else:
                    target = current

            if target != current:
                max_top = max(0, self.total_content_height - viewport)
                fraction = (max(0, min(target, max_top)) / self.total_content_height) if self.total_content_height else 0.0
                try:
                    canvas.yview_moveto(fraction)
                except Exception:
                    pass
                self._forced_offset = self._clamp_offset(target)
                self._last_scroll_offset = -1
                self.request_sync(reason="scroll_to_index")
            elif self._needs_rebind(current):
                self.request_sync(reason="scroll_to_index")
        else:
            self.request_sync(reason="scroll_to_index")

    # ------------------------------------------------------------------ syncing

    @property
    def window_start(self) -> int:
        return self._window_start

    @property
    def window_end(self) -> int:
        return self._window_end

    @property
    def loaded_pool_size(self) -> int:
        """Number of slots in the pool that are currently bound to an item."""
        if not self.items or self._window_end < self._window_start:
            return 0
        return self._window_end - self._window_start + 1

    @property
    def pool_size(self) -> int:
        """Total number of slots allocated in the pool."""
        return len(self._slots)

    def desired_pool_size(self) -> int:
        """Slots needed to cover the viewport plus overscan (capped at POOL_TARGET_ROWS)."""
        return min(POOL_TARGET_ROWS, max(1, len(self.items)))

    def request_sync(self, reason: str = "") -> None:
        """Schedule a pool reconciliation on the next idle tick."""
        self._pool_dirty = True
        if self._rebind_after_id is None:
            self._rebind_after_id = self.owner.after(1, self.sync)

    def sync(self, budget: int = INITIAL_BIND_ROWS, focus_idx: Optional[int] = None) -> None:
        """Reconcile the pool with the current scroll position using differential sliding."""
        self._rebind_after_id = None
        if not self.items:
            self._release_all_slots()
            self._window_start = 0
            self._window_end = -1
            self._update_spacer()
            self._update_window_position(0)
            if hasattr(self.owner, "_update_pool_stats"):
                self.owner._update_pool_stats()
            return

        if focus_idx is not None:
            v_top = focus_idx
            v_bottom = focus_idx
        else:
            offset = self._scroll_offset()
            v_top = self.index_at_offset(offset)
            v_bottom = self.index_at_offset(offset + max(0, self._viewport_height()))

        total = len(self.items)
        if total <= POOL_TARGET_ROWS:
            target_start = 0
            target_end = total - 1
            if self._window_start == 0 and self._window_end == total - 1:
                if all(i < len(self._slot_items) and self._slot_items[i] == i for i in range(total)):
                    self._first_visible = v_top
                    self._last_visible = v_bottom
                    self.start_scroll_polling()
                    return
        else:
            # Check if current window already covers the viewport with comfortable margin
            MARGIN = 20
            if (self._window_end >= self._window_start and
                v_top >= self._window_start + MARGIN and
                v_bottom <= self._window_end - MARGIN):
                all_bound = True
                for idx in range(v_top, min(v_bottom + 1, total)):
                    if idx not in self._slot_by_item:
                        all_bound = False
                        break
                if all_bound:
                    self._first_visible = v_top
                    self._last_visible = v_bottom
                    self.start_scroll_polling()
                    return

            # Viewport jumped or approached margin: calculate sliding window target
            if v_top > self._window_end or v_bottom < self._window_start:
                # Viewport jumped completely outside resident window; center on viewport
                target_start = max(0, min(v_top - 40, total - POOL_TARGET_ROWS))
            elif v_bottom >= self._window_end - SLIDE_MARGIN and self._window_end < total - 1:
                # Approaching bottom boundary; slide down by SLIDE_CHUNK
                target_start = min(total - POOL_TARGET_ROWS, self._window_start + SLIDE_CHUNK)
            elif v_top <= self._window_start + SLIDE_MARGIN and self._window_start > 0:
                # Approaching top boundary; slide up by SLIDE_CHUNK
                target_start = max(0, self._window_start - SLIDE_CHUNK)
            else:
                target_start = max(0, min(v_top - 40, total - POOL_TARGET_ROWS))
            target_end = target_start + POOL_TARGET_ROWS - 1

        self._first_visible = v_top
        self._last_visible = v_bottom

        target_count = target_end - target_start + 1
        self._ensure_pool_size(target_count)

        self._update_window_position(self.offset_of(target_start))
        self._update_spacer()

        visible_indices = [idx for idx in range(v_top, min(v_bottom + 1, total)) if target_start <= idx <= target_end]
        overscan_indices = [idx for idx in range(target_start, target_end + 1) if idx < v_top or idx > v_bottom]
        ordered_indices = visible_indices + overscan_indices

        bound_slots = []
        rebound_this_tick = 0
        for item_idx in ordered_indices:
            i = item_idx - target_start
            if i >= len(self._slot_items) or self._slot_items[i] != item_idx:
                if rebound_this_tick >= budget:
                    break
                self._rebind_slot(i, item_idx)
                bound_slots.append(i)
                rebound_this_tick += 1

        for i in range(target_count, len(self._slots)):
            if self._slot_items[i] is not None:
                self._release_slot(i)

        self._window_start = target_start
        self._window_end = target_end
        self.start_scroll_polling()

        # Re-sync _slot_by_item and owner maps for all currently bound slots to guarantee consistency
        self._slot_by_item = {item_idx: i for i, item_idx in enumerate(self._slot_items) if item_idx is not None}
        for i, item_idx in enumerate(self._slot_items):
            if item_idx is not None and item_idx < len(self.items):
                slot = self._slots[i]
                self._register_in_owner_maps(i, slot, item_idx)
                body = slot.get("body")
                if body and len(body) == 1:
                    it = self.items[item_idx]
                    self.owner._btn_map[str(it.path)] = body[0]
                    self.owner._label_map[item_idx] = body[0]

        if bound_slots:
            self.owner.on_rows_bound(self, bound_slots)
        if hasattr(self.owner, "_update_pool_stats"):
            self.owner._update_pool_stats()

        if rebound_this_tick >= budget:
            self._rebind_after_id = self.owner.after(1, lambda: self.sync(budget=budget, focus_idx=focus_idx))

    def _rebind_slot(self, slot_index: int, item_index: int) -> None:
        """Point an already-occupied slot at a different item, keeping its widgets."""
        slot = self._slots[slot_index]
        previous = self._slot_items[slot_index]
        if previous is not None:
            self._unregister_from_owner_maps(previous, slot)
            if self._slot_by_item.get(previous) == slot_index:
                self._slot_by_item.pop(previous, None)
        self._bind_slot(slot_index, item_index)

    def _apply_selection(self) -> None:
        for slot_index, item_index in enumerate(self._slot_items):
            if item_index is None:
                continue
            self._style_slot(self._slots[slot_index], item_index)

    # --------------------------------------------------------------- pool slots

    def _ensure_pool_size(self, size: int) -> None:
        while len(self._slots) < size:
            slot = self._create_slot(len(self._slots))
            self._slots.append(slot)
            self._slot_items.append(None)

    def _create_slot(self, slot_index: int) -> Dict:
        """An empty row: frame + flag bar + checkbox. The body is added on bind."""
        owner = self.owner
        frame = ctk.CTkFrame(
            self.pool_frame,
            height=ROW_HEIGHT,
            corner_radius=6,
            border_width=1,
            border_color="#3a3a3a"
        )
        frame.pack(side="top", fill="x", padx=1, pady=2)
        frame.pack_propagate(False)

        indicator = ctk.CTkFrame(frame, width=5, fg_color="#4a4e69")
        indicator.pack(side="left", fill="y")

        checkbox = ctk.CTkCheckBox(
            frame,
            text="",
            width=18,
            height=18,
            checkbox_width=18,
            checkbox_height=18,
            fg_color="#ffb703",
            hover_color="#fb8500",
            command=lambda i=slot_index: self._on_slot_checkbox(i)
        )
        checkbox.pack(side="left", padx=(4, 2))
        checkbox.deselect()

        log_debug(f"RowPool: created slot {slot_index}")
        return {
            "frame": frame,
            "indicator": indicator,
            "checkbox": checkbox,
            "body": None,
            "shape": None,
            "item_index": None,
            "paths": [],
            # Last value applied to each widget. Every CTk configure() redraws a canvas,
            # so a rebind that skips unchanged values costs a fraction of one that does
            # not - and a scroll rebinds a whole screenful at a time.
            "applied": {
                "height": ROW_HEIGHT,
                "border_color": "#3a3a3a",
                "border_width": 1,
            },
        }

    def _on_slot_checkbox(self, slot_index: int) -> None:
        item_index = self._slot_items[slot_index]
        if item_index is None:
            return
        self.owner._handle_chk_toggled(item_index)

    def _release_all_slots(self) -> None:
        for slot_index in range(len(self._slots)):
            self._release_slot(slot_index)
        if hasattr(self.owner, "_update_pool_stats"):
            self.owner._update_pool_stats()

    def _release_slot(self, slot_index: int) -> None:
        slot = self._slots[slot_index]
        item_index = self._slot_items[slot_index]
        if item_index is None and slot["body"] is None:
            # Slot is already clean/unbound; only ensure height if it was changed
            if slot["applied"].get("height") != ROW_HEIGHT:
                try:
                    slot["frame"].configure(height=ROW_HEIGHT)
                    slot["frame"].pack_propagate(False)
                    slot["applied"]["height"] = ROW_HEIGHT
                except Exception:
                    pass
            return

        if item_index is not None:
            self._unregister_from_owner_maps(item_index, slot)
            self._slot_by_item.pop(item_index, None)
        self._destroy_body(slot)
        self._slot_items[slot_index] = None
        slot["item_index"] = None

        applied = slot["applied"]
        cfg = {}
        if applied.get("border_color") != "#3a3a3a":
            cfg["border_color"] = "#3a3a3a"
        if applied.get("border_width") != 1:
            cfg["border_width"] = 1
        if applied.get("height") != ROW_HEIGHT:
            cfg["height"] = ROW_HEIGHT
        if cfg:
            try:
                slot["frame"].configure(**cfg)
                if "height" in cfg:
                    slot["frame"].pack_propagate(False)
            except Exception:
                pass
            applied.update(cfg)
        slot["checkbox"].deselect()

    def _unregister_from_owner_maps(self, item_index: int, slot: Dict) -> None:
        owner = self.owner
        if owner._row_frame_map.get(item_index) is slot.get("frame"):
            owner._row_frame_map.pop(item_index, None)
        if owner._indicator_map.get(item_index) is slot.get("indicator"):
            owner._indicator_map.pop(item_index, None)
        if owner._checkbox_map.get(item_index) is slot.get("checkbox"):
            owner._checkbox_map.pop(item_index, None)
        if owner._label_map.get(item_index) is not None:
            body = slot.get("body")
            if body and owner._label_map.get(item_index) in body:
                owner._label_map.pop(item_index, None)
            elif owner._label_map.get(item_index) is slot.get("label"):
                owner._label_map.pop(item_index, None)
        owner._row_render_cache.pop(item_index, None)

    def _destroy_body(self, slot: Dict) -> None:
        """Tear down a row's content and unregister it from the owner's maps."""
        owner = self.owner
        body = slot.get("body")
        for path_str in slot["paths"]:
            if body and owner._btn_map.get(path_str) in body:
                owner._btn_map.pop(path_str, None)
            elif not body:
                owner._btn_map.pop(path_str, None)
        slot["paths"] = []
        if slot["body"] is not None:
            for widget in slot["body"]:
                try:
                    widget.destroy()
                except Exception:
                    pass
            slot["body"] = None
        slot["shape"] = None
        slot["applied"].pop("text", None)
        slot["applied"].pop("image", None)
        slot["applied"].pop("btn_fg", None)
        slot["applied"].pop("btn_hover", None)
        if slot["applied"].get("height") != ROW_HEIGHT:
            try:
                slot["frame"].configure(height=ROW_HEIGHT)
                slot["frame"].pack_propagate(False)
                slot["applied"]["height"] = ROW_HEIGHT
            except Exception:
                pass

    def _bind_slot(self, slot_index: int, item_index: int) -> None:
        slot = self._slots[slot_index]
        owner = self.owner
        item = self.items[item_index]

        stacked = (
            not bool(getattr(item, "is_placeholder", False))
            and bool(getattr(item, "is_stacked", False))
            and len(item.stacked_paths) >= 2
        )
        shape = ("stacked", len(item.stacked_paths)) if stacked else ("plain", 1)

        # Unregister the paths this slot used to show *before* rebinding. When the shape
        # is unchanged the body is reused rather than destroyed, so nothing else clears
        # these: every rebind leaked a path -> button entry, and a stale entry let a
        # decoded thumbnail be painted onto a button showing a different photo.
        body = slot.get("body")
        for path_str in slot["paths"]:
            if body and self.owner._btn_map.get(path_str) in body:
                self.owner._btn_map.pop(path_str, None)
            elif not body:
                self.owner._btn_map.pop(path_str, None)
        slot["paths"] = []

        if slot["shape"] != shape:
            self._destroy_body(slot)
            slot["shape"] = shape

        height = row_height_for(item)
        if slot["applied"].get("height") != height:
            try:
                slot["frame"].configure(height=height)
                slot["frame"].pack_propagate(False)
                slot["applied"]["height"] = height
            except Exception:
                pass

        if stacked:
            self._bind_stacked_body(slot, item_index, item)
        else:
            self._bind_plain_body(slot, item_index, item)

        self._slot_items[slot_index] = item_index
        self._slot_by_item[item_index] = slot_index
        slot["item_index"] = item_index
        self._register_in_owner_maps(slot_index, slot, item_index)
        self._style_slot(slot, item_index)

    def _register_in_owner_maps(self, slot_index: int, slot: Dict, item_index: int) -> None:
        """Expose the bound row through the owner's index-keyed maps.

        Everything that reads a row by item index - status updates, selection,
        keyboard navigation - looks it up here. Under a recycled pool these maps hold
        only the visible window, which is exactly what those readers already guard
        against.
        """
        owner = self.owner
        owner._row_frame_map[item_index] = slot["frame"]
        owner._indicator_map[item_index] = slot["indicator"]
        owner._checkbox_map[item_index] = slot["checkbox"]
        # _label_map is deliberately left alone: the body binder has just set it for this
        # item, and popping it here meant no bound row ever had a label, so filenames,
        # ratings and stars silently stopped rendering.

    def _bind_plain_body(self, slot: Dict, item_index: int, item) -> None:
        owner = self.owner
        path_str = str(item.path)
        stars = "★" * item.rating if item.rating > 0 else ""
        expected_text = f"{item.filename}\n{stars}" if stars else item.filename

        is_active_path = (path_str == getattr(owner, "_current_active_path_str", None))
        expected_fg = "#1f538d" if is_active_path else "transparent"
        expected_hover = "#2b6cb0" if is_active_path else "#4a4a4a"

        if slot["body"] is None:
            button = ctk.CTkButton(
                slot["frame"],
                text=expected_text,
                image=owner.placeholder_image(80),
                compound="left",
                anchor="w",
                font=owner.row_font(),
                height=88,
                fg_color=expected_fg,
                hover_color=expected_hover,
                command=lambda i=item_index: self._on_slot_clicked(i)
            )
            button.pack(side="left", fill="both", expand=True, padx=2, pady=1)
            slot["body"] = [button]
            slot["applied"]["text"] = expected_text
            slot["applied"]["btn_fg"] = expected_fg
            slot["applied"]["btn_hover"] = expected_hover
        else:
            button = slot["body"][0]
            try:
                button.configure(command=lambda i=item_index: self._on_slot_clicked(i))
            except Exception:
                pass
            if slot["applied"].get("text") != expected_text:
                try:
                    button.configure(text=expected_text)
                    slot["applied"]["text"] = expected_text
                except Exception:
                    pass

            cfg = {}
            if slot["applied"].get("btn_fg") != expected_fg:
                cfg["fg_color"] = expected_fg
                slot["applied"]["btn_fg"] = expected_fg
            if slot["applied"].get("btn_hover") != expected_hover:
                cfg["hover_color"] = expected_hover
                slot["applied"]["btn_hover"] = expected_hover
            if cfg:
                try:
                    button.configure(**cfg)
                except Exception:
                    pass

        image = owner._ctk_img_cache.get(path_str, owner.placeholder_image(80))
        if slot["applied"].get("image") is not image:
            try:
                button.configure(image=image)
            except Exception:
                pass
            slot["applied"]["image"] = image
        slot["paths"] = [path_str]
        owner._btn_map[path_str] = button
        owner._label_map[item_index] = button
        owner._row_render_cache[item_index] = (owner.flag_color(item), expected_text)

    def _bind_stacked_body(self, slot: Dict, item_index: int, item) -> None:
        owner = self.owner
        base_stem = owner.base_stem_for(item)

        if slot["body"] is None:
            info_box = ctk.CTkFrame(slot["frame"], fg_color="transparent")
            info_box.pack(side="top", fill="x", padx=4, pady=(2, 1))
            label = ctk.CTkLabel(
                info_box, text="", font=owner.row_font(bold=True), anchor="w"
            )
            label.pack(side="left")
            strip = ctk.CTkScrollableFrame(
                slot["frame"],
                orientation="horizontal",
                height=160,
                fg_color="transparent"
            )
            strip.pack(side="top", fill="both", expand=True, padx=2, pady=1)
            slot["body"] = [info_box, strip]
            slot["label"] = label
            slot["strip"] = strip
        else:
            strip = slot["strip"]
            inner = getattr(strip, "_scrollable_frame", None) or getattr(strip, "_frame", None)
            if inner is not None:
                for widget in inner.winfo_children():
                    try:
                        widget.destroy()
                    except Exception:
                        pass
            else:
                for widget in strip.winfo_children():
                    try:
                        widget.destroy()
                    except Exception:
                        pass

        label = slot["label"]
        raw_n = sum(1 for p in item.stacked_paths if p.suffix.lower() == ".arw")
        jpg_n = sum(1 for p in item.stacked_paths if p.suffix.lower() in (".jpg", ".jpeg"))
        parts = []
        if raw_n:
            parts.append(f"{raw_n} ARW")
        if jpg_n:
            parts.append(f"{jpg_n} JPG")
        comp = ", ".join(parts) if parts else f"{len(item.stacked_paths)} files"
        stars = "★" * item.rating if item.rating > 0 else ""
        label.configure(text=f"{base_stem.upper()} [Stacked: {comp}] {stars}")

        paths = []
        for sub_p in item.stacked_paths:
            path_str = str(sub_p)
            paths.append(path_str)
            is_active_sub = (path_str == getattr(owner, "_current_active_path_str", None))
            sub_fg = "#1f538d" if is_active_sub else "transparent"
            sub_hover = "#2b6cb0" if is_active_sub else "#4a4a4a"
            button = ctk.CTkButton(
                strip,
                text=sub_p.name,
                image=owner._ctk_img_cache.get(path_str, owner.placeholder_image(90)),
                compound="top",
                font=owner.row_font(bold=True),
                width=95,
                height=95,
                fg_color=sub_fg,
                hover_color=sub_hover,
                command=lambda i=item_index, p=sub_p: self._on_slot_clicked(i, p)
            )
            button.pack(side="left", padx=3)
            owner._btn_map[path_str] = button

        slot["paths"] = paths
        owner._label_map[item_index] = label
        owner._row_render_cache[item_index] = (
            owner.flag_color(item),
            f"{base_stem.upper()} [Stacked: {comp}] {stars}",
        )

    def _on_slot_clicked(self, item_index: int, path=None) -> None:
        self.owner._on_btn_clicked(item_index, path)

    def _style_slot(self, slot: Dict, item_index: int) -> None:
        """Selection border, checkbox and flag colour for a bound row."""
        owner = self.owner
        item = self.items[item_index]
        is_active = (item_index == getattr(owner, "_current_active_idx", self._selected_idx))
        is_selected = (item_index in getattr(owner, "_current_selected_indices", {self._selected_idx}))
        applied = slot["applied"]

        border = ("#1f538d", 2) if is_active else (("#ffb703", 2) if is_selected else ("#3a3a3a", 1))
        if applied.get("border") != border:
            slot["frame"].configure(border_color=border[0], border_width=border[1])
            applied["border"] = border

        if applied.get("checked") != is_selected:
            if is_selected:
                slot["checkbox"].select()
            else:
                slot["checkbox"].deselect()
            applied["checked"] = is_selected

        flag_color = owner.flag_color(item)
        if applied.get("flag") != flag_color:
            slot["indicator"].configure(fg_color=flag_color)
            applied["flag"] = flag_color

    def set_selected_index(self, index: int) -> None:
        """Re-style the pool after the selected row changed."""
        previous = self._selected_idx
        self._selected_idx = index
        if previous == index:
            return
        old_slot = self.slot_for_index(previous)
        if old_slot is not None:
            self._style_slot(old_slot, previous)
        new_slot = self.slot_for_index(index)
        if new_slot is not None:
            self._style_slot(new_slot, index)

    def is_visible(self, index: int) -> bool:
        return index in self._slot_by_item

    # ------------------------------------------------------------------ spacer

    def _update_spacer(self) -> None:
        """Set the scroll region so the scrollbar reflects the true content height.

        Deliberately *no* filler widget. The obvious way to make the scrollbar honest -
        a transparent spacer frame as tall as the unwound content - costs 300-900 ms on
        every single scroll repaint, because Tk maps and redraws that enormous region.
        Measured on a 2771-item folder: 324 ms per repaint with the spacer, 4 ms without,
        for the same scroll range.

        Instead the scroll region is set analytically. CTkScrollableFrame resets it from
        ``bbox("all")`` on every ``<Configure>``, so :meth:`sync` and the owner's
        ``<Configure>`` handler both re-apply it afterwards.
        """
        canvas = self._canvas()
        if canvas is None:
            return
        try:
            width = max(1, canvas.winfo_width())
            height = max(1, self.total_content_height)
            wanted = (0, 0, width, height)
            current_sr = canvas.cget("scrollregion")
            expected_sr = f"0 0 {width} {height}"
            if self._applied_scrollregion != wanted or current_sr != expected_sr:
                canvas.configure(scrollregion=wanted)
                self._applied_scrollregion = wanted
        except Exception:
            pass

    def _update_window_position(self, y_pos: int) -> None:
        """Position the container frame in the canvas so bound rows match the viewport."""
        canvas = self._canvas()
        if canvas is not None and hasattr(self.container, "_create_window_id"):
            try:
                canvas.coords(self.container._create_window_id, 0, y_pos)
            except Exception:
                pass

    # ------------------------------------------------------------------ teardown

    def shutdown(self) -> None:
        self.stop_scroll_polling()
        if self._rebind_after_id is not None:
            try:
                self.owner.after_cancel(self._rebind_after_id)
            except Exception:
                pass
            self._rebind_after_id = None