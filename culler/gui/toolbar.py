from typing import Callable, Dict, Any, List, Optional
import customtkinter as ctk
from .tooltip import ToolTip


class MultiSelectDropdown(ctk.CTkFrame):
    """
    Custom multiselect dropdown widget.
    Shows a button that opens a popup with checkboxes.
    Button text dynamically reflects selected items.
    """
    _active_instance = None

    def __init__(
        self,
        master,
        values: List[str],
        all_label: str = "All",
        width: int = 95,
        on_change: Optional[Callable[[], None]] = None,
        button_fg_color: Optional[str] = None,
        button_hover_color: Optional[str] = None,
        button_text_color: Optional[str] = None,
        **kwargs
    ):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.all_label = all_label
        self.values = values
        self.on_change = on_change
        self._selected: set = set()  # Empty means "All"
        self._popup = None

        self.btn = ctk.CTkButton(
            self,
            text=all_label,
            width=width,
            height=28,
            fg_color=button_fg_color if button_fg_color is not None else "#3a86ff",
            hover_color=button_hover_color if button_hover_color is not None else "#2b6cb0",
            text_color=button_text_color if button_text_color is not None else "#ffffff",
            font=ctk.CTkFont(size=11),
            command=self._toggle_popup,
            anchor="w"
        )
        self.btn.pack(fill="x", expand=True)

        self.btn_clear = ctk.CTkButton(
            self.btn,
            text="✕",
            width=18,
            height=18,
            fg_color="transparent",
            hover_color="#d90429",
            text_color="#ffffff",
            font=ctk.CTkFont(size=9, weight="bold"),
            corner_radius=9,
            command=self.clear_selection
        )
        ToolTip(self.btn_clear, "Clear selection")

    def clear_selection(self):
        """Clear all selected items and reset filter to default 'All' state."""
        self._selected.clear()
        if hasattr(self, "_chk_vars"):
            for var in self._chk_vars.values():
                var.set(False)
        self._update_label()
        if self.on_change:
            self.on_change()

    def _toggle_popup(self):
        if self._popup and self._popup.winfo_exists():
            self.close_popup()
            return

        if MultiSelectDropdown._active_instance and MultiSelectDropdown._active_instance != self:
            MultiSelectDropdown._active_instance.close_popup()

        self._popup = ctk.CTkToplevel(self)
        self._popup.overrideredirect(True)
        self._popup.attributes("-topmost", True)
        MultiSelectDropdown._active_instance = self

        # Position below the button
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 2
        self._popup.geometry(f"+{x}+{y}")

        popup_frame = ctk.CTkFrame(self._popup, corner_radius=6, fg_color="#2a2a2a", border_width=1, border_color="#555555")
        popup_frame.pack(fill="both", expand=True, padx=0, pady=0)

        # Popup Header with Clear Button
        header_frame = ctk.CTkFrame(popup_frame, fg_color="transparent")
        header_frame.pack(fill="x", padx=6, pady=(4, 2))

        lbl_hdr = ctk.CTkLabel(
            header_frame,
            text="Select Options:",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color="#aaaaaa"
        )
        lbl_hdr.pack(side="left", padx=(2, 0))

        btn_popup_clear = ctk.CTkButton(
            header_frame,
            text="🧹 Clear",
            width=55,
            height=22,
            fg_color="#3a3a3a",
            hover_color="#d90429",
            text_color="#ffffff",
            font=ctk.CTkFont(size=10, weight="bold"),
            command=self.clear_selection
        )
        btn_popup_clear.pack(side="right")

        sep = ctk.CTkFrame(popup_frame, height=1, fg_color="#444444")
        sep.pack(fill="x", padx=4, pady=(2, 4))

        self._chk_vars: Dict[str, ctk.BooleanVar] = {}
        for val in self.values:
            var = ctk.BooleanVar(value=(val in self._selected))
            chk = ctk.CTkCheckBox(
                popup_frame,
                text=val,
                variable=var,
                font=ctk.CTkFont(size=11),
                checkbox_width=16,
                checkbox_height=16,
                command=lambda v=val, bv=var: self._on_check(v, bv)
            )
            chk.pack(anchor="w", padx=8, pady=2)
            self._chk_vars[val] = var

        self._popup.update_idletasks()

        # Bind click outside and Escape key globally on root
        root = self.winfo_toplevel()
        if not getattr(root, "_multiselect_bound", False):
            root.bind_all("<ButtonPress-1>", MultiSelectDropdown._global_on_click, add="+")
            root.bind_all("<Escape>", MultiSelectDropdown._global_on_escape, add="+")
            root._multiselect_bound = True

    @classmethod
    def _global_on_escape(cls, event):
        if cls._active_instance:
            cls._active_instance.close_popup()

    @classmethod
    def _global_on_click(cls, event):
        if not cls._active_instance or not cls._active_instance._popup:
            return
        inst = cls._active_instance
        if not inst._popup.winfo_exists():
            cls._active_instance = None
            return

        try:
            px = inst._popup.winfo_rootx()
            py = inst._popup.winfo_rooty()
            pw = inst._popup.winfo_width()
            ph = inst._popup.winfo_height()

            bx = inst.winfo_rootx()
            by = inst.winfo_rooty()
            bw = inst.winfo_width()
            bh = inst.winfo_height()

            rx = event.x_root
            ry = event.y_root

            inside_popup = (px <= rx <= px + pw) and (py <= ry <= py + ph)
            inside_btn = (bx <= rx <= bx + bw) and (by <= ry <= by + bh)

            if not inside_popup and not inside_btn:
                inst.close_popup()
        except Exception:
            pass

    def _on_check(self, value: str, var: ctk.BooleanVar):
        if var.get():
            self._selected.add(value)
        else:
            self._selected.discard(value)
        self._update_label()
        if self.on_change:
            self.on_change()

    def close_popup(self):
        if self._popup:
            try:
                if self._popup.winfo_exists():
                    self._popup.destroy()
            except Exception:
                pass
            self._popup = None
        if MultiSelectDropdown._active_instance == self:
            MultiSelectDropdown._active_instance = None

    def _update_label(self):
        if not self._selected:
            self.btn.configure(text=self.all_label)
            if hasattr(self, "btn_clear"):
                self.btn_clear.place_forget()
        else:
            if hasattr(self, "btn_clear"):
                self.btn_clear.place(relx=1.0, rely=0.5, anchor="e", x=-4)
            if len(self._selected) == 1:
                self.btn.configure(text=next(iter(self._selected)))
            elif len(self._selected) <= 2:
                self.btn.configure(text=", ".join(sorted(self._selected)))
            else:
                self.btn.configure(text=f"{len(self._selected)} selected")

    def get_selected(self) -> List[str]:
        """Return list of selected values, or empty list for 'All'."""
        return sorted(self._selected) if self._selected else []

    def set_values(self, values: List[str]):
        """Update available values, preserving current selection where possible."""
        self.values = values
        # Remove any selected items no longer in values
        self._selected = self._selected.intersection(set(values))
        self._update_label()

    def reset(self):
        """Reset to 'All' state."""
        self.clear_selection()


class HeaderToolbar(ctk.CTkFrame):
    """
    Top Navigation and Action Toolbar containing:
    - Open Directory button
    - Filter controls (Segmented flag filter, multiselect rating filter, format filter, multiselect tag filter)
    - RAW scale preview selector (10%, 15%, 20%, 25%, 50%, 100%)
    - White balance mode selector (Camera vs Auto)
    - 100% full-resolution preview button
    - Automated Scan for Blur and Scan for Duplicates actions
    - Application preferences settings button
    """

    def __init__(
        self,
        master,
        on_open_explorer: Optional[Callable[[], None]] = None,
        on_refresh: Optional[Callable[[], None]] = None,
        on_filter_change: Callable[[str], None] = None,
        on_raw_settings_change: Callable[[], None] = None,
        on_load_100_percent: Callable[[], None] = None,
        on_scan_blur: Callable[[], None] = None,
        on_scan_duplicates: Callable[[], None] = None,
        on_open_settings: Optional[Callable[[], None]] = None,
        on_toggle_thumbs: Optional[Callable[[], None]] = None,
        on_toggle_tools: Optional[Callable[[], None]] = None,
        initial_raw_scale: float = 0.25,
        initial_wb: str = "camera",
        **kwargs
    ):
        super().__init__(master, height=50, corner_radius=0, **kwargs)

        self.on_open_explorer = on_open_explorer
        self.on_refresh = on_refresh
        self.on_filter_change = on_filter_change
        self.on_raw_settings_change = on_raw_settings_change
        self.on_load_100_percent = on_load_100_percent
        self.on_scan_blur = on_scan_blur
        self.on_scan_duplicates = on_scan_duplicates
        self.on_open_settings = on_open_settings
        self.on_toggle_thumbs = on_toggle_thumbs
        self.on_toggle_tools = on_toggle_tools

        self.initial_raw_scale = initial_raw_scale
        self.initial_wb = initial_wb

        self._build_widgets()

    def _build_widgets(self):
        # Toggle Thumbnail Panel Button (F8 / Ctrl+B)
        if self.on_toggle_thumbs:
            self.btn_toggle_thumbs = ctk.CTkButton(
                self,
                text="◀ 🎞️",
                width=46,
                height=28,
                fg_color="#1f538d",
                hover_color="#14375e",
                font=ctk.CTkFont(size=11, weight="bold"),
                command=self.on_toggle_thumbs
            )
            self.btn_toggle_thumbs.pack(side="left", padx=(4, 4))
            ToolTip(self.btn_toggle_thumbs, "Toggle Thumbnail Panel (F8 / Ctrl+B)")

        # Filter Segmented Control
        self.lbl_filter = ctk.CTkLabel(self, text="Filter:", font=ctk.CTkFont(weight="bold"))
        self.lbl_filter.pack(side="left", padx=(4, 2))

        self.seg_filter = ctk.CTkSegmentedButton(
            self,
            values=["All", "Pick", "Reject", "Unflagged"],
            command=lambda v: self.on_filter_change("filter")
        )
        self.seg_filter.set("All")
        self.seg_filter.pack(side="left", padx=3)

        # Tag Filter (MultiSelect)
        self.tag_filter = MultiSelectDropdown(
            self,
            values=["Blur", "Duplicate", "Dark", "Over-exposed"],
            all_label="All Tags",
            width=95,
            on_change=lambda: self.on_filter_change("filter")
        )
        self.tag_filter.pack(side="left", padx=3)
        ToolTip(self.tag_filter, "Filter by Tag (multi-select, OR logic)")

        # Rating Filter (MultiSelect)
        self.rating_filter = MultiSelectDropdown(
            self,
            values=["★ 1", "★ 2", "★ 3", "★ 4", "★ 5", "Unrated"],
            all_label="All Stars",
            width=90,
            on_change=lambda: self.on_filter_change("filter")
        )
        self.rating_filter.pack(side="left", padx=3)
        ToolTip(self.rating_filter, "Filter by Star Rating (multi-select, OR logic)")

        # Format Filter
        self.opt_format = ctk.CTkOptionMenu(
            self,
            values=["All Formats", ".ARW", ".JPG", ".PNG", ".HEIC"],
            command=lambda v: self.on_filter_change("filter"),
            width=95,
            fg_color="#1f538d",
            button_color="#14375e",
            button_hover_color="#0d233a",
            text_color="#ffffff"
        )
        self.opt_format.set("All Formats")
        self.opt_format.pack(side="left", padx=3)

        # 100% Full Resolution Button
        self.btn_100 = ctk.CTkButton(
            self,
            text="🔍",
            width=40,
            fg_color="#1f538d",
            hover_color="#14375e",
            font=ctk.CTkFont(weight="bold"),
            command=self.on_load_100_percent
        )
        self.btn_100.pack(side="left", padx=(8, 3))
        ToolTip(self.btn_100, "Load 100% Full Resolution for active photo")

        # RAW Load Scale Dropdown (Options: 10%, 15%, 20%, 25%, 50%, 100%)
        self.lbl_raw = ctk.CTkLabel(self, text="RAW:", font=ctk.CTkFont(weight="bold"))
        self.lbl_raw.pack(side="left", padx=(6, 2))

        scale_default_str = "25%"
        if self.initial_raw_scale == 0.10: scale_default_str = "10%"
        elif self.initial_raw_scale == 0.15: scale_default_str = "15%"
        elif self.initial_raw_scale == 0.20: scale_default_str = "20%"
        elif self.initial_raw_scale == 0.50: scale_default_str = "50%"
        elif self.initial_raw_scale == 1.00: scale_default_str = "100%"

        self.opt_raw_scale = ctk.CTkOptionMenu(
            self,
            values=["10%", "15%", "20%", "25%", "50%", "100%"],
            command=lambda v: self.on_raw_settings_change(),
            width=80,
            fg_color="#1f538d",
            button_color="#14375e",
            button_hover_color="#0d233a"
        )
        self.opt_raw_scale.set(scale_default_str)
        self.opt_raw_scale.pack(side="left", padx=3)

        # White Balance Dropdown
        self.lbl_wb = ctk.CTkLabel(self, text="WB:", font=ctk.CTkFont(weight="bold"))
        self.lbl_wb.pack(side="left", padx=(4, 2))

        wb_default_str = "Camera" if self.initial_wb == "camera" else "Auto"
        self.opt_wb = ctk.CTkOptionMenu(
            self,
            values=["Camera", "Auto"],
            command=lambda v: self.on_raw_settings_change(),
            width=85,
            fg_color="#1f538d",
            button_color="#14375e",
            button_hover_color="#0d233a"
        )
        self.opt_wb.set(wb_default_str)
        self.opt_wb.pack(side="left", padx=3)

        # Scan for Blur Button
        self.btn_scan_blur = ctk.CTkButton(
            self,
            text="🔍 Scan for Blur",
            width=110,
            fg_color="#1f538d",
            hover_color="#14375e",
            font=ctk.CTkFont(weight="bold"),
            command=self.on_scan_blur
        )
        self.btn_scan_blur.pack(side="left", padx=3)
        ToolTip(self.btn_scan_blur, "Scan photoshoot for blurry photos")

        # Scan for Duplicates Button
        self.btn_scan_dups = ctk.CTkButton(
            self,
            text="👯 Scan for Duplicates",
            width=135,
            fg_color="#1f538d",
            hover_color="#14375e",
            font=ctk.CTkFont(weight="bold"),
            command=self.on_scan_duplicates
        )
        self.btn_scan_dups.pack(side="left", padx=3)
        ToolTip(self.btn_scan_dups, "Detect duplicate burst shots & keep sharpest")

        # Toggle Tool Panel Button (F9 / Ctrl+J, far right edge)
        if self.on_toggle_tools:
            self.btn_toggle_tools = ctk.CTkButton(
                self,
                text="🛠️ ▶",
                width=46,
                height=28,
                fg_color="#1f538d",
                hover_color="#14375e",
                font=ctk.CTkFont(size=11, weight="bold"),
                command=self.on_toggle_tools
            )
            self.btn_toggle_tools.pack(side="right", padx=(2, 4))
            ToolTip(self.btn_toggle_tools, "Toggle Right Tool Panel (F9 / Ctrl+J)")

        # Settings Button
        if self.on_open_settings:
            self.btn_settings = ctk.CTkButton(
                self,
                text="⚙️ Settings",
                width=95,
                fg_color="#1f538d",
                hover_color="#14375e",
                font=ctk.CTkFont(weight="bold"),
                command=self.on_open_settings
            )
            self.btn_settings.pack(side="right", padx=4)
            ToolTip(self.btn_settings, "Open preferences & global settings")

        # Refresh button (right end)
        if self.on_refresh:
            self.btn_refresh = ctk.CTkButton(
                self,
                text="🔄",
                width=40,
                command=self.on_refresh,
                fg_color="#1f538d",
                hover_color="#14375e"
            )
            self.btn_refresh.pack(side="right", padx=4)
            ToolTip(self.btn_refresh, "Refresh current directory")

        # Open Folder in Explorer button (right end)
        if self.on_open_explorer:
            self.btn_explorer = ctk.CTkButton(
                self,
                text="📂",
                width=40,
                command=self.on_open_explorer,
                fg_color="#1f538d",
                hover_color="#14375e"
            )
            self.btn_explorer.pack(side="right", padx=4)
            ToolTip(self.btn_explorer, "Open current photo folder in OS File Explorer / Finder")

    def set_panel_visibility_state(self, thumbs_visible: bool, tools_visible: bool) -> None:
        if hasattr(self, "btn_toggle_thumbs"):
            if thumbs_visible:
                self.btn_toggle_thumbs.configure(text="◀ 🎞️", fg_color="#1f538d")
                ToolTip(self.btn_toggle_thumbs, "Hide Thumbnail Panel (F8 / Ctrl+B)")
            else:
                self.btn_toggle_thumbs.configure(text="▶ 🎞️", fg_color="#3a3a3a")
                ToolTip(self.btn_toggle_thumbs, "Show Thumbnail Panel (F8 / Ctrl+B)")
        if hasattr(self, "btn_toggle_tools"):
            if tools_visible:
                self.btn_toggle_tools.configure(text="🛠️ ▶", fg_color="#1f538d")
                ToolTip(self.btn_toggle_tools, "Hide Right Tool Panel (F9 / Ctrl+J)")
            else:
                self.btn_toggle_tools.configure(text="🛠️ ◀", fg_color="#3a3a3a")
                ToolTip(self.btn_toggle_tools, "Show Right Tool Panel (F9 / Ctrl+J)")

    def get_filter_values(self) -> Dict[str, Any]:
        return {
            "flag": self.seg_filter.get(),
            "rating": self.rating_filter.get_selected(),
            "format": self.opt_format.get(),
            "tag": self.tag_filter.get_selected()
        }

    def apply_filter_values(self, filter_vals: Dict[str, Any]):
        if "flag" in filter_vals:
            self.seg_filter.set(filter_vals["flag"])
        if "rating" in filter_vals:
            self.rating_filter.reset()
            for r in filter_vals.get("rating", []):
                self.rating_filter._selected.add(r)
            self.rating_filter._update_label()
        if "format" in filter_vals:
            self.opt_format.set(filter_vals["format"])
        if "tag" in filter_vals:
            self.tag_filter.reset()
            for t in filter_vals.get("tag", []):
                self.tag_filter._selected.add(t)
            self.tag_filter._update_label()

    def update_tag_options(self, available_tags: list):
        """Dynamically update the Tag Filter dropdown choices based on tags present in session."""
        base_tags = ["Blur", "Duplicate", "Dark", "Over-exposed"]
        for t in available_tags:
            if t and t not in base_tags:
                base_tags.append(t)
        self.tag_filter.set_values(base_tags)

    def get_format_filter(self) -> str:
        return self.opt_format.get()

    def get_raw_scale(self) -> float:
        val_str = self.opt_raw_scale.get()
        if "10%" in val_str: return 0.10
        if "15%" in val_str: return 0.15
        if "20%" in val_str: return 0.20
        if "50%" in val_str: return 0.50
        if "100%" in val_str: return 1.00
        return 0.25

    def get_white_balance(self) -> str:
        val_str = self.opt_wb.get()
        if "Auto" in val_str: return "auto"
        return "camera"
