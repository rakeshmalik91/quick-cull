from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
import customtkinter as ctk
from ..culler_engine import ImageItem, FlagState
from ..logger import log_error
from .tooltip import ToolTip


#: Stable identifiers for the panel's sections, in their default order. The order the
#: user drags them into is persisted against these keys, not against widget instances.
BAG_KEYS = ("action", "reset", "move", "tags", "rating", "meta")

BAG_SETTINGS_KEY = "meta_panel_bag_order"
BAG_COLLAPSED_SETTINGS_KEY = "meta_panel_bag_collapsed"

#: The panel is the right-hand sidebar. Its bags live in one scrollable column so a short
#: window scrolls instead of overlapping them, and the width the tag buttons are laid out
#: from is derived from this rather than hard-coded.
PANEL_WIDTH = 290
PANEL_PADDING = 10
TAG_GRID_PAD = 2
#: Inner width available to a bag: the panel, less its own padding on both sides.
TAG_ROW_WIDTH = PANEL_WIDTH - PANEL_PADDING * 4


class MetadataPanel(ctk.CTkFrame):
    """
    Right sidebar containing Pick/Reject action buttons, Move Picked / Move Rejected actions with custom output folders,
    Unflag All, Tagging controls (Blur, Duplicate, Dark, Over-exposed, Custom), star rating controls, and EXIF card.

    The bags keep the arrangement they have always had. The only structural change is
    that they live in one scrollable column: packed straight into a fixed-height panel
    they overflowed, and Tk placed the remainder on top of each other. Each bag's title
    doubles as a drag handle, so the order is the user's to choose and is persisted.
    Each bag is collapsible to maximize viewport space.
    """

    def __init__(
        self,
        master,
        on_set_flag: Callable[[FlagState], None],
        on_set_rating: Callable[[int], None],
        on_toggle_tag: Optional[Callable[[str], None]] = None,
        on_unflag_all: Optional[Callable[[], None]] = None,
        on_untag_all: Optional[Callable[[], None]] = None,
        on_unrate_all: Optional[Callable[[], None]] = None,
        on_clear_all: Optional[Callable[[], None]] = None,
        on_crop: Optional[Callable[[], None]] = None,
        on_annotate: Optional[Callable[[], None]] = None,
        on_convert_jpg: Optional[Callable[[], None]] = None,
        on_move_picked: Optional[Callable[[], None]] = None,
        on_move_rejected: Optional[Callable[[], None]] = None,
        on_trash_rejected: Optional[Callable[[], None]] = None,
        on_config_output_folders: Optional[Callable[[], None]] = None,
        initial_picked_folder: str = "_SELECTED",
        initial_rejected_folder: str = "_REJECTED",
        bag_order: Optional[List[str]] = None,
        on_bag_order_changed: Optional[Callable[[List[str]], None]] = None,
        collapsed_states: Optional[Dict[str, bool]] = None,
        on_bag_collapse_changed: Optional[Callable[[Dict[str, bool]], None]] = None,
        **kwargs
    ):
        kwargs.setdefault("width", PANEL_WIDTH)
        kwargs.setdefault("corner_radius", 5)
        super().__init__(master, **kwargs)
        self.pack_propagate(False)

        self.on_set_flag = on_set_flag
        self.on_set_rating = on_set_rating
        self.on_toggle_tag = on_toggle_tag
        self.on_unflag_all = on_unflag_all
        self.on_untag_all = on_untag_all
        self.on_unrate_all = on_unrate_all
        self.on_clear_all = on_clear_all
        self.on_crop = on_crop
        self.on_annotate = on_annotate
        self.on_convert_jpg = on_convert_jpg
        self.on_move_picked = on_move_picked
        self.on_move_rejected = on_move_rejected
        self.on_trash_rejected = on_trash_rejected
        self.on_config_output_folders = on_config_output_folders
        self.on_bag_order_changed = on_bag_order_changed
        self.on_bag_collapse_changed = on_bag_collapse_changed

        self.picked_folder = initial_picked_folder
        self.rejected_folder = initial_rejected_folder

        self.current_item: Optional[ImageItem] = None
        self._tag_buttons: dict = {}
        self._bags: Dict[str, ctk.CTkFrame] = {}
        self._bag_titles: Dict[str, ctk.CTkLabel] = {}
        self._bag_pack: Dict[str, dict] = {}
        self._bag_title_colors: Dict[str, str] = {}
        self._bag_contents: Dict[str, ctk.CTkFrame] = {}
        self._bag_collapse_btns: Dict[str, ctk.CTkButton] = {}
        self._bag_collapsed: Dict[str, bool] = {k: False for k in BAG_KEYS}
        self._bag_order: List[str] = self._sanitize_order(bag_order)
        self._drag_key: Optional[str] = None
        self._drag_target: Optional[str] = None

        self._build_widgets()
        self._apply_bag_order()
        if collapsed_states:
            self.set_all_collapsed_states(collapsed_states)

    def _sanitize_order(self, order: Optional[List[str]]) -> List[str]:
        """A persisted order, filtered to known keys and completed with any new ones.

        A bag added in a later version must still appear, and a key that no longer
        exists must not leave a hole in the layout.
        """
        if not order:
            return list(BAG_KEYS)
        seen = [k for k in order if k in BAG_KEYS]
        for key in BAG_KEYS:
            if key not in seen:
                seen.append(key)
        return seen

    def _create_bag_header(
        self,
        parent: ctk.CTkFrame,
        key: str,
        title_text: str,
        font_size: int = 12,
        padx: int = 0,
        pady: tuple = (0, 3),
    ) -> Tuple[ctk.CTkLabel, ctk.CTkButton]:
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.pack(fill="x", padx=padx, pady=pady)
        lbl = ctk.CTkLabel(
            header,
            text=title_text,
            font=ctk.CTkFont(size=font_size, weight="bold")
        )
        lbl.pack(side="left", anchor="w")
        btn = ctk.CTkButton(
            header,
            text="▼",
            width=20,
            height=18,
            fg_color="transparent",
            hover_color="#333333",
            text_color="#888888",
            font=ctk.CTkFont(size=10),
            command=lambda: self.toggle_bag_collapse(key)
        )
        btn.pack(side="right")
        ToolTip(btn, f"Collapse / Expand {title_text}")
        return lbl, btn

    def _register_bag(
        self,
        key: str,
        frame: ctk.CTkFrame,
        title: ctk.CTkLabel,
        pack_options: dict,
        content_frame: Optional[ctk.CTkFrame] = None,
        collapse_btn: Optional[ctk.CTkButton] = None,
    ) -> None:
        """Remember a bag so it can be reordered, and make its title a drag handle.

        The title doubles as the handle rather than adding a separate grip widget, so
        the bags look exactly as they did before.
        """
        self._bags[key] = frame
        self._bag_titles[key] = title
        self._bag_pack[key] = pack_options
        if content_frame is not None:
            self._bag_contents[key] = content_frame
        if collapse_btn is not None:
            self._bag_collapse_btns[key] = collapse_btn
        # CustomTkinter has no "unset" for text_color, so the original is kept and
        # restored rather than cleared.
        self._bag_title_colors[key] = title.cget("text_color")
        title.configure(cursor="hand2")
        ToolTip(title, "Drag to reorder section | Double-click to collapse/expand")
        title.bind("<ButtonPress-1>", self._on_bag_drag_start)
        title.bind("<B1-Motion>", self._on_bag_drag_motion)
        title.bind("<ButtonRelease-1>", self._on_bag_drag_end)
        title.bind("<Double-Button-1>", lambda e, k=key: self.toggle_bag_collapse(k))

    # ------------------------------------------------------------- collapsible sections

    def toggle_bag_collapse(self, key: str) -> None:
        self.set_bag_collapsed(key, not self._bag_collapsed.get(key, False))

    def set_bag_collapsed(self, key: str, collapsed: bool) -> None:
        if key not in self._bags:
            return
        self._bag_collapsed[key] = bool(collapsed)
        content = self._bag_contents.get(key)
        btn = self._bag_collapse_btns.get(key)
        if content is not None:
            if collapsed:
                content.pack_forget()
                if btn is not None:
                    btn.configure(text="▶")
            else:
                if key == "meta":
                    content.pack(fill="both", expand=True, padx=10, pady=4)
                else:
                    content.pack(fill="x")
                if btn is not None:
                    btn.configure(text="▼")
        if self.on_bag_collapse_changed:
            try:
                self.on_bag_collapse_changed(self.bag_collapsed_state())
            except Exception:
                log_error("Failed to persist bag collapsed state", exc_info=True)

    def is_bag_collapsed(self, key: str) -> bool:
        return self._bag_collapsed.get(key, False)

    def bag_collapsed_state(self) -> Dict[str, bool]:
        return dict(self._bag_collapsed)

    def set_all_collapsed_states(self, states: Dict[str, bool]) -> None:
        if not states or not isinstance(states, dict):
            return
        for key, collapsed in states.items():
            if key in self._bags:
                self.set_bag_collapsed(key, bool(collapsed))

    # ------------------------------------------------------------- reordering

    def bag_order(self) -> List[str]:
        """The current section order, for persisting."""
        return list(self._bag_order)

    def set_bag_order(self, order: List[str]) -> None:
        self._bag_order = self._sanitize_order(order)
        self._apply_bag_order()

    def _apply_bag_order(self) -> None:
        """Repack the bags in the current order, with one consistent set of options."""
        for frame in self._bags.values():
            frame.pack_forget()
        for key in self._bag_order:
            frame = self._bags.get(key)
            if frame is not None:
                frame.pack(**self._bag_pack[key])

    def _on_bag_drag_start(self, event) -> None:
        key = self._bag_key_of(event.widget)
        if key is None:
            return
        self._drag_key = key
        self._drag_target = key
        self._bag_titles[key].configure(text_color="#3a86ff")

    def _on_bag_drag_motion(self, event) -> None:
        if self._drag_key is None:
            return
        target = self._bag_at_y(event.y_root)
        if target is None or target == self._drag_target:
            return
        self._drag_target = target
        order = list(self._bag_order)
        order.remove(self._drag_key)
        order.insert(order.index(target), self._drag_key)
        if order != self._bag_order:
            self._bag_order = order
            self._apply_bag_order()

    def _on_bag_drag_end(self, event) -> None:
        if self._drag_key is None:
            return
        self._bag_titles[self._drag_key].configure(
            text_color=self._bag_title_colors.get(self._drag_key, "#ffffff"))
        self._drag_key = None
        self._drag_target = None
        if self.on_bag_order_changed:
            try:
                self.on_bag_order_changed(self.bag_order())
            except Exception:
                log_error("Failed to persist the metadata panel section order", exc_info=True)

    def _bag_key_of(self, widget) -> Optional[str]:
        for key, title in self._bag_titles.items():
            if title is widget:
                return key
        return None

    def _bag_at_y(self, y_root: int) -> Optional[str]:
        """The bag whose vertical centre is nearest ``y_root``.

        Using the centre line keeps the result stable no matter how tall a bag's
        contents happen to be, which is what makes the drop position predictable.
        """
        best = None
        best_delta = None
        for key in self._bag_order:
            frame = self._bags.get(key)
            if frame is None:
                continue
            try:
                top = frame.winfo_rooty()
                height = max(1, frame.winfo_height())
            except Exception:
                continue
            delta = abs((top + height / 2.0) - y_root)
            if best_delta is None or delta < best_delta:
                best, best_delta = key, delta
        return best

    def _build_widgets(self):
        # One scrollable column for the bags. Packed straight into a fixed-height panel
        # they overflowed, and Tk placed whatever did not fit on top of the rest.
        self._bags_area = ctk.CTkScrollableFrame(self, label_text="")
        self._bags_area.pack(side="top", fill="both", expand=True)

        # Action Buttons Box
        self.action_box = ctk.CTkFrame(self._bags_area, fg_color="transparent")
        self.lbl_action, btn_c_action = self._create_bag_header(
            self.action_box, "action", "CULLING ACTIONS", 12, pady=(0, 4)
        )
        self.action_content = ctk.CTkFrame(self.action_box, fg_color="transparent")
        self.action_content.pack(fill="x")
        self._register_bag("action", self.action_box, self.lbl_action,
                           {"side": "top", "fill": "x", "padx": 10, "pady": 6},
                           self.action_content, btn_c_action)

        self.btn_pick = ctk.CTkButton(
            self.action_content,
            text="[P] PICK",
            fg_color="#1b4332",
            hover_color="#2b9348",
            font=ctk.CTkFont(weight="bold", size=14),
            command=lambda: self.on_set_flag(FlagState.PICK)
        )
        self.btn_pick.pack(fill="x", pady=2)
        ToolTip(self.btn_pick, "Shortcut: P (Pick) | Shift+P (UnPick)")

        self.btn_reject = ctk.CTkButton(
            self.action_content,
            text="[X] REJECT",
            fg_color="#5c0612",
            hover_color="#d90429",
            font=ctk.CTkFont(weight="bold", size=14),
            command=lambda: self.on_set_flag(FlagState.REJECT)
        )
        self.btn_reject.pack(fill="x", pady=2)
        ToolTip(self.btn_reject, "Shortcut: X (Reject) | Shift+X (UnReject)")

        self.btn_unflag = ctk.CTkButton(
            self.action_content,
            text="[U] UNFLAG",
            fg_color="#1f538d",
            hover_color="#14375e",
            command=lambda: self.on_set_flag(FlagState.UNFLAGGED)
        )
        self.btn_unflag.pack(fill="x", pady=2)
        ToolTip(self.btn_unflag, "Shortcut: U (Unflag active photo)")

        # Clear / Reset Metadata Row (Flags, Tags, Ratings, All side by side)
        self.reset_box = ctk.CTkFrame(self._bags_area, fg_color="transparent")
        self.lbl_reset, btn_c_reset = self._create_bag_header(
            self.reset_box, "reset", "CLEAR METADATA", 11, pady=(0, 3)
        )
        self.reset_content = ctk.CTkFrame(self.reset_box, fg_color="transparent")
        self.reset_content.pack(fill="x")
        self._register_bag("reset", self.reset_box, self.lbl_reset,
                           {"side": "top", "fill": "x", "padx": 10, "pady": 4},
                           self.reset_content, btn_c_reset)

        self.reset_btn_row = ctk.CTkFrame(self.reset_content, fg_color="transparent")
        self.reset_btn_row.pack(fill="x")

        if self.on_unflag_all:
            self.btn_unflag_all = ctk.CTkButton(
                self.reset_btn_row,
                text="🚩 Flags",
                width=62,
                height=26,
                fg_color="#1f538d",
                hover_color="#14375e",
                font=ctk.CTkFont(size=10, weight="bold"),
                command=self.on_unflag_all
            )
            self.btn_unflag_all.pack(side="left", padx=1)
            ToolTip(self.btn_unflag_all, "Clear flags across all photos")

        if self.on_untag_all:
            self.btn_untag_all = ctk.CTkButton(
                self.reset_btn_row,
                text="🏷️ Tags",
                width=62,
                height=26,
                fg_color="#1f538d",
                hover_color="#14375e",
                font=ctk.CTkFont(size=10, weight="bold"),
                command=self.on_untag_all
            )
            self.btn_untag_all.pack(side="left", padx=1)
            ToolTip(self.btn_untag_all, "Remove all tags from all photos")

        if self.on_unrate_all:
            self.btn_unrate_all = ctk.CTkButton(
                self.reset_btn_row,
                text="⭐ Stars",
                width=62,
                height=26,
                fg_color="#1f538d",
                hover_color="#14375e",
                font=ctk.CTkFont(size=10, weight="bold"),
                command=self.on_unrate_all
            )
            self.btn_unrate_all.pack(side="left", padx=1)
            ToolTip(self.btn_unrate_all, "Reset star ratings to 0")

        if self.on_clear_all:
            self.btn_clear_all = ctk.CTkButton(
                self.reset_btn_row,
                text="💥 All",
                width=62,
                height=26,
                fg_color="#1f538d",
                hover_color="#14375e",
                font=ctk.CTkFont(size=10, weight="bold"),
                command=self.on_clear_all
            )
            self.btn_clear_all.pack(side="left", padx=1)
            ToolTip(self.btn_clear_all, "Clear Flags, Tags, AND Ratings across all photos")

        # Move & Export Operations Box
        self.move_box = ctk.CTkFrame(self._bags_area, fg_color="transparent")
        self.lbl_move, btn_c_move = self._create_bag_header(
            self.move_box, "move", "MOVE & EXPORT", 12, pady=(0, 4)
        )
        self.move_content = ctk.CTkFrame(self.move_box, fg_color="transparent")
        self.move_content.pack(fill="x")
        self._register_bag("move", self.move_box, self.lbl_move,
                           {"side": "top", "fill": "x", "padx": 10, "pady": 4},
                           self.move_content, btn_c_move)

        p_name = Path(self.picked_folder).name or self.picked_folder
        r_name = Path(self.rejected_folder).name or self.rejected_folder

        f_pick_row = ctk.CTkFrame(self.move_content, fg_color="transparent")
        f_pick_row.pack(fill="x", pady=2)

        self.btn_move_picked = ctk.CTkButton(
            f_pick_row,
            text=f"📁 Move Picked -> [{p_name}]",
            fg_color="#1b4332",
            hover_color="#2b9348",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._handle_move_picked
        )
        self.btn_move_picked.pack(side="left", fill="x", expand=True)

        self.btn_open_picked = ctk.CTkButton(
            f_pick_row,
            text="📂",
            width=32,
            fg_color="#1b4332",
            hover_color="#2b9348",
            font=ctk.CTkFont(size=13),
            command=lambda: self._open_folder(self.picked_folder)
        )
        self.btn_open_picked.pack(side="left", padx=(2, 0))
        ToolTip(self.btn_open_picked, "Open picked folder in file explorer")

        f_reject_row = ctk.CTkFrame(self.move_content, fg_color="transparent")
        f_reject_row.pack(fill="x", pady=2)

        self.btn_move_rejected = ctk.CTkButton(
            f_reject_row,
            text=f"📁 Move Rejected -> [{r_name}]",
            fg_color="#5c0612",
            hover_color="#d90429",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._handle_move_rejected
        )
        self.btn_move_rejected.pack(side="left", fill="x", expand=True)

        self.btn_open_rejected = ctk.CTkButton(
            f_reject_row,
            text="📂",
            width=32,
            fg_color="#5c0612",
            hover_color="#d90429",
            font=ctk.CTkFont(size=13),
            command=lambda: self._open_folder(self.rejected_folder)
        )
        self.btn_open_rejected.pack(side="left", padx=(2, 0))
        ToolTip(self.btn_open_rejected, "Open rejected folder in file explorer")

        f_trash_reject_row = ctk.CTkFrame(self.move_content, fg_color="transparent")
        f_trash_reject_row.pack(fill="x", pady=2)

        self.btn_trash_rejected = ctk.CTkButton(
            f_trash_reject_row,
            text="🗑️ Delete (Move to trash) All Rejected",
            fg_color="#5c0612",
            hover_color="#d90429",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._handle_trash_rejected
        )
        self.btn_trash_rejected.pack(side="left", fill="x", expand=True)
        ToolTip(self.btn_trash_rejected, "Shortcut: Shift+D (Move all rejected photos to Recycle Bin / Trash)")

        if self.on_crop:
            self.btn_crop = ctk.CTkButton(
                self.move_content,
                text="✂️ Crop This Image",
                fg_color="#1f538d",
                hover_color="#14375e",
                text_color="#ffffff",
                font=ctk.CTkFont(size=11, weight="bold"),
                command=self.on_crop
            )
            self.btn_crop.pack(fill="x", pady=2)
            ToolTip(self.btn_crop, "Shortcut: C (Crop - Hold Shift for 1:1 Square)")

        if self.on_annotate:
            self.btn_annotate = ctk.CTkButton(
                self.move_content,
                text="🎯 Correct Bounding Box",
                fg_color="#a37a00",
                hover_color="#7a5c00",
                text_color="#ffffff",
                font=ctk.CTkFont(size=11, weight="bold"),
                command=self.on_annotate
            )
            self.btn_annotate.pack(fill="x", pady=2)
            ToolTip(self.btn_annotate, "Shortcut: B (Draw subject and eye boxes to correct AI)")

        if self.on_convert_jpg:
            self.btn_convert_jpg = ctk.CTkButton(
                self.move_content,
                text="🖼️ Convert Selected to JPG",
                fg_color="#1f538d",
                hover_color="#14375e",
                font=ctk.CTkFont(size=11, weight="bold"),
                command=self.on_convert_jpg
            )
            self.btn_convert_jpg.pack(fill="x", pady=2)
            ToolTip(self.btn_convert_jpg, "Convert selected photo(s) to JPG (Shortcut: Ctrl+S)")

        # Tags Box (Blur, Duplicate, Dark, Over-exposed + Custom from Settings)
        self.tags_box = ctk.CTkFrame(self._bags_area, fg_color="transparent")
        self.lbl_tags, btn_c_tags = self._create_bag_header(
            self.tags_box, "tags", "IMAGE TAGS", 12, pady=(0, 4)
        )
        self.tags_content = ctk.CTkFrame(self.tags_box, fg_color="transparent")
        self.tags_content.pack(fill="x")
        self._register_bag("tags", self.tags_box, self.lbl_tags,
                           {"side": "top", "fill": "x", "padx": 10, "pady": 4},
                           self.tags_content, btn_c_tags)

        self._tags_container = ctk.CTkFrame(self.tags_content, fg_color="transparent")
        self._tags_container.pack(fill="x")

        self._build_tag_buttons([])

        # Rating Stars Box
        self.rating_box = ctk.CTkFrame(self._bags_area, fg_color="transparent")
        self.lbl_stars, btn_c_stars = self._create_bag_header(
            self.rating_box, "rating", "STAR RATING", 12, pady=(0, 4)
        )
        self.rating_content = ctk.CTkFrame(self.rating_box, fg_color="transparent")
        self.rating_content.pack(fill="x")
        self._register_bag("rating", self.rating_box, self.lbl_stars,
                           {"side": "top", "fill": "x", "padx": 10, "pady": 4},
                           self.rating_content, btn_c_stars)

        self.star_btn_frame = ctk.CTkFrame(self.rating_content, fg_color="transparent")
        self.star_btn_frame.pack(fill="x")

        self.star_buttons = []
        for star in range(1, 6):
            btn = ctk.CTkButton(
                self.star_btn_frame,
                text=f"★{star}",
                width=46,
                fg_color="#3a86ff",
                hover_color="#0077b6",
                font=ctk.CTkFont(size=11, weight="bold"),
                command=lambda s=star: self._handle_set_rating(s)
            )
            btn.pack(side="left", padx=2)
            ToolTip(btn, f"Shortcut: {star} (Set rating to {star} Star{'s' if star > 1 else ''})")
            self.star_buttons.append(btn)

        # Metadata Card Box
        self.meta_card = ctk.CTkFrame(self._bags_area, corner_radius=6, fg_color="#242424")
        self.lbl_meta_title, btn_c_meta = self._create_bag_header(
            self.meta_card, "meta", "EXIF METADATA", 12, padx=10, pady=(6, 2)
        )
        self.meta_content = ctk.CTkFrame(self.meta_card, fg_color="transparent")
        self.meta_content.pack(fill="both", expand=True, padx=10, pady=4)
        self._register_bag("meta", self.meta_card, self.lbl_meta_title,
                           {"side": "top", "fill": "x", "padx": 10, "pady": 6},
                           self.meta_content, btn_c_meta)

        self.lbl_meta_details = ctk.CTkLabel(
            self.meta_content,
            text="No image selected.",
            justify="left",
            anchor="nw",
            font=ctk.CTkFont(family="Consolas", size=11)
        )
        self.lbl_meta_details.pack(fill="both", expand=True)

    def update_output_folders(self, picked_folder: str, rejected_folder: str):
        self.picked_folder = picked_folder
        self.rejected_folder = rejected_folder
        p_name = Path(picked_folder).name or picked_folder
        r_name = Path(rejected_folder).name or rejected_folder
        self.btn_move_picked.configure(text=f"📁 Move Picked -> [{p_name}]")
        self.btn_move_rejected.configure(text=f"📁 Move Rejected -> [{r_name}]")

    def _handle_set_rating(self, star: int):
        if self.on_set_rating:
            current_r = self.current_item.rating if self.current_item else 0
            new_r = 0 if current_r == star else star
            self.on_set_rating(new_r)

    def _handle_move_picked(self):
        if self.on_move_picked:
            self.on_move_picked()

    def _handle_move_rejected(self):
        if self.on_move_rejected:
            self.on_move_rejected()

    def _handle_trash_rejected(self):
        if self.on_trash_rejected:
            self.on_trash_rejected()

    def _handle_config_folders(self):
        if self.on_config_output_folders:
            self.on_config_output_folders()

    def _open_folder(self, folder_name: str):
        """Open the target output folder in the OS file explorer."""
        import os
        import subprocess
        try:
            # Resolve relative folder against the active session directory
            if os.path.isabs(folder_name):
                target = Path(folder_name)
            else:
                # Walk up to find the session directory from gui.py
                app = self.winfo_toplevel()
                session_dir = None
                if hasattr(app, "_get_active_tab"):
                    tab = app._get_active_tab()
                    if tab and tab.get("session") and tab["session"].directory:
                        session_dir = Path(tab["session"].directory)
                if session_dir:
                    target = session_dir / folder_name
                else:
                    target = Path(folder_name)

            if target.exists() and target.is_dir():
                if os.name == "nt":
                    os.startfile(str(target))
                else:
                    subprocess.Popen(["xdg-open", str(target)])
            else:
                from tkinter import messagebox as mb
                mb.showinfo("Open Folder", f"Folder does not exist yet:\n{target}")
        except Exception:
            pass

    def _toggle_tag(self, tag_name: str):
        if self.on_toggle_tag:
            self.on_toggle_tag(tag_name)

    def _build_tag_buttons(self, custom_tags: List[str]):
        """Build tag toggle buttons for standard + custom tags."""
        for widget in self._tags_container.winfo_children():
            widget.destroy()
        self._tag_buttons.clear()

        all_tags = ["Blur", "Duplicate", "Dark", "Over-exposed"] + list(custom_tags)

        # Two per row, each sized from the panel width. The old fixed 125 px buttons
        # needed ~258 px of inner width, which is wider than the panel, so the tags bag
        # grew past its neighbours and the bags below it overlapped them.
        button_width = max(90, (TAG_ROW_WIDTH - TAG_GRID_PAD * 6) // 2)

        # Layout in rows of 2
        row_frame = None
        for idx, tag in enumerate(all_tags):
            if idx % 2 == 0:
                row_frame = ctk.CTkFrame(self._tags_container, fg_color="transparent")
                row_frame.pack(fill="x", pady=1)
            btn = ctk.CTkButton(
                row_frame,
                text=f"🏷️ {tag}",
                width=button_width,
                height=26,
                fg_color="#3a3a3a",
                hover_color="#555555",
                font=ctk.CTkFont(size=10, weight="bold"),
                command=lambda t=tag: self._toggle_tag(t)
            )
            btn.pack(side="left", padx=TAG_GRID_PAD, pady=1, fill="x", expand=True)
            self._tag_buttons[tag] = btn

        # Re-highlight if there's a current item
        if self.current_item:
            for tag, btn in self._tag_buttons.items():
                if self.current_item.has_tag(tag):
                    btn.configure(fg_color="#7b2cbf")
                else:
                    btn.configure(fg_color="#3a3a3a")

    def refresh_tag_buttons(self, custom_tags: List[str]):
        """Refresh tag buttons with updated custom tags from settings."""
        self._build_tag_buttons(custom_tags)

    def update_item_metadata(self, item: Optional[ImageItem]):
        self.current_item = item
        if item is None:
            self.lbl_meta_details.configure(text="No image selected.")
            for btn in self.star_buttons:
                btn.configure(fg_color="#3a86ff", text_color="#ffffff")
            return

        m = item.metadata
        flag_str = item.flag.value
        stars_str = "★" * item.rating if item.rating > 0 else "None"
        stacked_str = f"\nStacked: {len(item.stacked_paths)} files" if item.is_stacked else ""
        tags_display = item.tags_str if item.tags_str else "None"

        # Update star button colors (highlight active rating in gold #ffb703)
        cur_rating = item.rating if item else 0
        for star_num, btn in enumerate(self.star_buttons, start=1):
            if star_num == cur_rating:
                btn.configure(fg_color="#ffb703", text_color="#000000")
            else:
                btn.configure(fg_color="#3a86ff", text_color="#ffffff")

        # Update tag button colors (highlight active tags in purple #7b2cbf)
        for tag, btn in self._tag_buttons.items():
            if item.has_tag(tag):
                btn.configure(fg_color="#7b2cbf")
            else:
                btn.configure(fg_color="#3a3a3a")

        txt = (
            f"File: {item.filename}\n"
            f"Format: {item.format_name}{stacked_str}\n"
            f"Size: {item.formatted_size}\n"
            f"Status: {flag_str}\n"
            f"Rating: {stars_str}\n"
            f"Sharpness: {item.sharpness_score}\n"
            f"Tags: {tags_display}\n"
            "------------------------\n"
            f"Camera: {m.get('model', 'N/A')}\n"
            f"Lens: {m.get('lens', 'N/A')}\n"
            f"ISO: {m.get('iso', 'N/A')}\n"
            f"Shutter: {m.get('shutter_speed', 'N/A')}\n"
            f"Aperture: {m.get('aperture', 'N/A')}\n"
            f"Focal Length: {m.get('focal_length', 'N/A')}\n"
            f"Date Taken: {m.get('date_taken', 'N/A')}\n"
        )
        self.lbl_meta_details.configure(text=txt)

    def update_metadata(self, item: Optional[ImageItem]):
        self.update_item_metadata(item)

    def clear(self):
        """Clear metadata panel details display when no image is selected."""
        self.update_item_metadata(None)
