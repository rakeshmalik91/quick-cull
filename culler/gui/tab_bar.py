from typing import Callable, Optional, List
import tkinter as tk
import customtkinter as ctk

from .tooltip import ToolTip


ABOUT_ICON = "\u24d8"  # circled small i

ADD_TAB_ICON = "+"


class TabBar(ctk.CTkFrame):
    def __init__(
        self,
        master,
        on_tab_selected: Callable[[int], None] = None,
        on_tab_closed: Callable[[int], None] = None,
        on_tab_reordered: Callable[[int, int], None] = None,
        on_new_tab: Callable[[], None] = None,
        on_about: Callable[[], None] = None,
        **kwargs
    ):
        super().__init__(master, height=36, corner_radius=0, **kwargs)
        self.pack_propagate(False)

        self.on_tab_selected = on_tab_selected
        self.on_tab_closed = on_tab_closed
        self.on_tab_reordered = on_tab_reordered
        self.on_new_tab = on_new_tab
        self.on_about = on_about

        self._tab_buttons: List[ctk.CTkButton] = []
        self._close_buttons: List[ctk.CTkButton] = []
        self._tab_labels: List[str] = []
        self._active_index: int = 0
        self._tab_count: int = 0

        self._drag_source_idx: Optional[int] = None
        self._drag_over_idx: Optional[int] = None
        self._drag_start_x: int = 0
        self._drag_start_y: int = 0
        self._did_drag: bool = False

        self._scroll_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._scroll_frame.pack(side="left", fill="both", expand=True)

        self._canvas = tk.Canvas(self._scroll_frame, height=36, bg="#1a1a1a", highlightthickness=0)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

        self._inner_frame = ctk.CTkFrame(self._canvas, fg_color="transparent")
        self._canvas_window = self._canvas.create_window((0, 0), window=self._inner_frame, anchor="nw")

        self._btn_about = ctk.CTkButton(
            self,
            text=ABOUT_ICON,
            width=30,
            height=30,
            fg_color="#2b2b2b",
            hover_color="#3a3a3a",
            font=ctk.CTkFont(size=13),
            command=self._handle_about
        )
        self._btn_about.pack(side="right", padx=(2, 6), pady=3)
        ToolTip(self._btn_about, "About")

        # Lives inside the scrolling strip so it always sits right after the last tab.
        self._btn_add = ctk.CTkButton(
            self._inner_frame,
            text=ADD_TAB_ICON,
            width=30,
            height=30,
            fg_color="#2b2b2b",
            hover_color="#3a3a3a",
            font=ctk.CTkFont(size=16, weight="bold"),
            command=self._handle_new_tab
        )
        ToolTip(self._btn_add, "Open New Folder")
        self._btn_add.pack(side="left", padx=(6, 2), pady=3)

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._canvas_window, width=event.width)

    def _relayout(self):
        """Re-pack the strip so tab buttons keep their order and '+' stays last."""
        for btn in self._tab_buttons:
            btn.pack_forget()
        for close_btn in self._close_buttons:
            close_btn.pack_forget()
        self._btn_add.pack_forget()

        for index, btn in enumerate(self._tab_buttons):
            btn.pack(side="left", padx=(2, 0), pady=3)
            self._close_buttons[index].pack(side="left", padx=(0, 4), pady=3)

        self._btn_add.pack(side="left", padx=(6, 2), pady=3)
        self._update_scroll_region()

    def add_tab(self, label: str) -> int:
        idx = self._tab_count
        self._tab_count += 1
        self._tab_labels.append(label)

        btn = ctk.CTkButton(
            self._inner_frame,
            text=self._format_label(label),
            anchor="w",
            height=30,
            fg_color="#2a2a2a" if idx != self._active_index else "#3a86ff",
            hover_color="#3a3a3a" if idx != self._active_index else "#2b6cb0",
            text_color="#cccccc" if idx != self._active_index else "#ffffff",
            font=ctk.CTkFont(size=11),
        )
        btn.pack(side="left", padx=(2, 0), pady=3)

        btn.bind("<ButtonPress-1>", self._on_drag_start)
        btn.bind("<B1-Motion>", self._on_drag_motion)
        btn.bind("<ButtonRelease-1>", self._on_drag_release)
        btn.bind("<ButtonPress-2>", self._on_middle_click)

        close_btn = ctk.CTkButton(
            self._inner_frame,
            text="✕",
            width=20,
            height=20,
            fg_color="transparent",
            hover_color="#555555",
            text_color="#aaaaaa",
            font=ctk.CTkFont(size=9),
            command=lambda cb=None: None  # Set below with current reference
        )
        close_btn.configure(command=lambda cb=close_btn: self._handle_close_btn_click(cb))

        self._tab_buttons.append(btn)
        self._close_buttons.append(close_btn)
        self._relayout()
        return idx

    def remove_tab(self, index: int):
        if not (0 <= index < self._tab_count):
            return

        btn = self._tab_buttons.pop(index)
        btn.destroy()
        close_btn = self._close_buttons.pop(index)
        close_btn.destroy()
        self._tab_labels.pop(index)
        self._tab_count -= 1

        if self._tab_count == 0:
            self._active_index = 0
            self._relayout()
            return

        if self._active_index >= self._tab_count:
            self._active_index = self._tab_count - 1
        elif self._active_index == index:
            self._active_index = min(index, self._tab_count - 1)

        for i, b in enumerate(self._tab_buttons):
            b.configure(
                fg_color="#2a2a2a" if i != self._active_index else "#3a86ff",
                hover_color="#3a3a3a" if i != self._active_index else "#2b6cb0",
                text_color="#cccccc" if i != self._active_index else "#ffffff"
            )

        self._relayout()

    def set_active(self, index: int):
        if not (0 <= index < self._tab_count):
            return
        self._active_index = index
        for i, b in enumerate(self._tab_buttons):
            b.configure(
                fg_color="#2a2a2a" if i != self._active_index else "#3a86ff",
                hover_color="#3a3a3a" if i != self._active_index else "#2b6cb0",
                text_color="#cccccc" if i != self._active_index else "#ffffff"
            )

    def set_label(self, index: int, label: str):
        if 0 <= index < len(self._tab_labels):
            self._tab_labels[index] = label
            self._tab_buttons[index].configure(text=self._format_label(label))

    def get_active(self) -> int:
        return self._active_index

    def get_tab_count(self) -> int:
        return self._tab_count

    def get_labels(self) -> List[str]:
        return list(self._tab_labels)

    def reorder(self, from_idx: int, to_idx: int):
        if from_idx == to_idx or not (0 <= from_idx < self._tab_count) or not (0 <= to_idx < self._tab_count):
            return
        label = self._tab_labels.pop(from_idx)
        btn = self._tab_buttons.pop(from_idx)
        close_btn = self._close_buttons.pop(from_idx)
        self._tab_labels.insert(to_idx, label)
        self._tab_buttons.insert(to_idx, btn)
        self._close_buttons.insert(to_idx, close_btn)
        self._tab_count = len(self._tab_labels)

        old_active = self._active_index
        if old_active == from_idx:
            self._active_index = to_idx
        elif from_idx < to_idx and old_active > from_idx and old_active <= to_idx:
            self._active_index -= 1
        elif from_idx > to_idx and old_active >= to_idx and old_active < from_idx:
            self._active_index += 1

        for b in self._tab_buttons:
            b.pack_forget()
        for cb in self._close_buttons:
            cb.pack_forget()
        self._btn_add.pack_forget()
        for i, b in enumerate(self._tab_buttons):
            b.pack(side="left", padx=(2, 0), pady=3)
            self._close_buttons[i].pack(side="left", padx=(0, 4), pady=3)
            b.configure(
                fg_color="#2a2a2a" if i != self._active_index else "#3a86ff",
                hover_color="#3a3a3a" if i != self._active_index else "#2b6cb0",
                text_color="#cccccc" if i != self._active_index else "#ffffff"
            )
        self._btn_add.pack(side="left", padx=(6, 2), pady=3)
        self._update_scroll_region()

    def _format_label(self, label: str, max_len: int = 40) -> str:
        if len(label) <= max_len:
            return label
        return label[:max_len - 3] + "..."

    def _update_scroll_region(self):
        self._inner_frame.update_idletasks()
        bbox = self._inner_frame.bbox("all")
        if bbox:
            self._canvas.config(scrollregion=bbox)

    def _get_index_for_widget(self, widget) -> int:
        for idx, btn in enumerate(self._tab_buttons):
            if btn == widget or any(child == widget for child in btn.winfo_children()):
                return idx
        for idx, cb in enumerate(self._close_buttons):
            if cb == widget or any(child == widget for child in cb.winfo_children()):
                return idx
        return -1

    def _handle_close_btn_click(self, close_btn: ctk.CTkButton):
        if close_btn in self._close_buttons:
            idx = self._close_buttons.index(close_btn)
            self._handle_close(idx)

    def _on_middle_click(self, event):
        idx = self._get_index_for_widget(event.widget)
        if idx >= 0:
            self._handle_close(idx)

    def _handle_close(self, index: int):
        if self.on_tab_closed and 0 <= index < self._tab_count:
            self.on_tab_closed(index)

    def _handle_new_tab(self):
        if self.on_new_tab:
            self.on_new_tab()

    def _handle_about(self):
        if self.on_about:
            self.on_about()

    def _on_drag_start(self, event):
        idx = self._get_index_for_widget(event.widget)
        if idx < 0:
            return
        self._drag_source_idx = idx
        self._drag_over_idx = idx
        self._drag_start_x = event.x_root
        self._drag_start_y = event.y_root
        self._did_drag = False

    def _on_drag_motion(self, event):
        if self._drag_source_idx is None:
            return
        dx = event.x_root - self._drag_start_x
        dy = event.y_root - self._drag_start_y
        if abs(dx) > 4 or abs(dy) > 4:
            self._did_drag = True
        widget_under = event.widget.winfo_containing(event.x_root, event.y_root)
        if widget_under is None:
            return
        for i, btn in enumerate(self._tab_buttons):
            if btn == widget_under or any(child == widget_under for child in btn.winfo_children()):
                self._drag_over_idx = i
                return

    def _on_drag_release(self, event):
        if self._did_drag and self._drag_source_idx is not None and self._drag_over_idx is not None:
            if self._drag_source_idx != self._drag_over_idx and self.on_tab_reordered:
                self.on_tab_reordered(self._drag_source_idx, self._drag_over_idx)
        elif not self._did_drag:
            current_idx = self._get_index_for_widget(event.widget)
            if current_idx >= 0 and self.on_tab_selected:
                self.on_tab_selected(current_idx)
        self._drag_source_idx = None
        self._drag_over_idx = None
        self._did_drag = False
