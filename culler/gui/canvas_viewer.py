from typing import Optional, Tuple
import tkinter as tk
import customtkinter as ctk
from PIL import Image, ImageTk

from .view_transform import FitResult, RenderPyramid, contain_fit


class ImageCanvasViewer(ctk.CTkFrame):
    """
    Center viewport wrapping a Tkinter Canvas with interactive zoom (mouse wheel), pan (drag),
    and a prominent centered progress bar overlay for directory scanning.
    """

    def __init__(self, master, **kwargs):
        super().__init__(master, corner_radius=5, **kwargs)

        self.zoom_level: float = 1.0
        self.pan_x: float = 0.0
        self.pan_y: float = 0.0
        self.drag_start_x: float = 0.0
        self.drag_start_y: float = 0.0

        self.current_pil_img: Optional[Image.Image] = None
        self.current_tk_img: Optional[ImageTk.PhotoImage] = None
        self.canvas_img_id: Optional[int] = None
        self._last_rendered_state: Optional[Tuple[float, int, int]] = None
        self._zoom_timer: Optional[str] = None
        self._configure_after_id: Optional[str] = None
        # Resized renders of the current image, reused across redraws and zoom levels.
        self._pyramid = RenderPyramid()

        # Crop Mode Variables
        self.is_cropping: bool = False
        self.crop_start_x: float = 0.0
        self.crop_start_y: float = 0.0
        self.crop_box: Optional[Tuple[float, float, float, float]] = None
        self.crop_rect_id: Optional[int] = None
        self.on_confirm_crop_cb = None

        # Annotation Mode Variables
        self.is_annotating: bool = False
        self.anno_class: str = "Subject"
        self.anno_subject_box_px: Optional[Tuple[float, float, float, float]] = None
        self.anno_eye_box_px: Optional[Tuple[float, float, float, float]] = None
        self.anno_drag_start_x: float = 0.0
        self.anno_drag_start_y: float = 0.0
        self.anno_drag_rect_id: Optional[int] = None
        self.anno_subject_rect_id: Optional[int] = None
        self.anno_eye_rect_id: Optional[int] = None
        self.on_save_anno_cb = None
        self.current_image_path = None

        # Subject Detection Box
        self._detection_box: Optional[Tuple[float, float, float, float]] = None
        self._detection_rect_id: Optional[int] = None

        self.canvas = tk.Canvas(self, bg="#1a1a1a", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_release)
        self.canvas.bind("<ButtonPress-3>", self._on_right_drag_start)
        self.canvas.bind("<B3-Motion>", self._on_right_drag_motion)
        self.canvas.bind("<ButtonRelease-3>", self._on_right_drag_release)
        self.canvas.bind("<MouseWheel>", self._on_zoom)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Configure>", lambda e: self._on_geometry_change())

        # Centered Progress Overlay
        self.overlay_frame: Optional[ctk.CTkFrame] = None
        self._create_loading_overlay()

        # Top Crop Toolbar Overlay
        self.crop_toolbar: Optional[ctk.CTkFrame] = None
        self._create_crop_toolbar()

        # Top Annotation Toolbar Overlay
        self.anno_toolbar: Optional[ctk.CTkFrame] = None
        self._create_anno_toolbar()

    def _on_geometry_change(self):
        """Re-fit on a viewport resize.

        Coalesced: a window drag emits a long burst of ``<Configure>`` events, and
        resizing plus repainting on each one is what made the viewer stutter while the
        window was being sized. The final geometry always gets rendered.
        """
        if getattr(self, "_configure_after_id", None) is None:
            self._configure_after_id = self.after(16, self._flush_geometry_change)

    def _flush_geometry_change(self):
        self._configure_after_id = None
        self.redraw(force_resize=True)

    def current_fit(self) -> Optional[FitResult]:
        """The geometry currently on screen, or ``None`` before the first layout."""
        if self.current_pil_img is None:
            return None
        img_w, img_h = self.current_pil_img.size
        return contain_fit(
            img_w,
            img_h,
            self.canvas.winfo_width(),
            self.canvas.winfo_height(),
            zoom=self.zoom_level,
            pan_x=self.pan_x,
            pan_y=self.pan_y,
        )

    def _create_loading_overlay(self):
        self.overlay_frame = ctk.CTkFrame(
            self,
            fg_color="#1e1e24",
            corner_radius=12,
            border_width=2,
            border_color="#1f538d"
        )

        self.lbl_loading_title = ctk.CTkLabel(
            self.overlay_frame,
            text="📂 Loading Directory & EXIF Metadata...",
            font=ctk.CTkFont(size=15, weight="bold"),
            height=32
        )
        self.lbl_loading_title.pack(padx=30, pady=(20, 8))

        self.loading_progress = ctk.CTkProgressBar(
            self.overlay_frame,
            width=440,
            height=20,
            progress_color="#1f538d"
        )
        self.loading_progress.pack(padx=30, pady=10)
        self.loading_progress.set(0)

        self.lbl_loading_status = ctk.CTkLabel(
            self.overlay_frame,
            text="Initializing scan...",
            font=ctk.CTkFont(size=13),
            text_color="#a0a0a0",
            height=35
        )
        self.lbl_loading_status.pack(padx=30, pady=(5, 20))

        # Initially hidden until loading starts
        self.overlay_frame.place_forget()

    def show_loading(self, title: str = "📂 Loading Directory & EXIF Metadata..."):
        self.lbl_loading_title.configure(text=title)
        self.loading_progress.set(0.0)
        self.lbl_loading_status.configure(text="Reading files...")
        self.overlay_frame.place(relx=0.5, rely=0.5, anchor="center")
        self.overlay_frame.lift()

    def update_loading_progress(self, current: int, total: int, fraction: float):
        self.loading_progress.set(fraction)
        pct = int(fraction * 100)
        self.lbl_loading_status.configure(text=f"Reading EXIF metadata: {current} / {total} ({pct}%)")

    def hide_loading(self):
        self.overlay_frame.place_forget()

    def set_image(self, pil_img: Optional[Image.Image], preserve_zoom: bool = True):
        self.hide_loading()
        self.current_pil_img = pil_img
        self._pyramid.set_source(pil_img)
        if not preserve_zoom:
            self.zoom_level = 1.0
            self.pan_x = 0.0
            self.pan_y = 0.0
        self.redraw(force_resize=True)

    def clear(self):
        """Clear canvas viewer image (e.g. when 0 items match filter)."""
        self.clear_detection_box()
        self.set_image(None, preserve_zoom=False)

    def set_detection_box(
        self,
        ai_box: Optional[Tuple[float, float, float, float]],
        ai_eye_box: Optional[Tuple[float, float, float, float]] = None,
        manual_box: Optional[Tuple[float, float, float, float]] = None,
        manual_eye_box: Optional[Tuple[float, float, float, float]] = None
    ):
        self._ai_box = ai_box
        self._ai_eye_box = ai_eye_box
        self._manual_box = manual_box
        self._manual_eye_box = manual_eye_box
        self._draw_detection_rect()

    def clear_detection_box(self):
        self._ai_box = None
        self._ai_eye_box = None
        self._manual_box = None
        self._manual_eye_box = None
        
        # Clear AI rects
        if getattr(self, "_ai_rect_id", None) is not None:
            self.canvas.delete(self._ai_rect_id)
            self._ai_rect_id = None
        if getattr(self, "_ai_eye_rect_id", None) is not None:
            self.canvas.delete(self._ai_eye_rect_id)
            self._ai_eye_rect_id = None
            
        # Clear Manual rects
        if getattr(self, "_manual_rect_id", None) is not None:
            self.canvas.delete(self._manual_rect_id)
            self._manual_rect_id = None
        if getattr(self, "_manual_eye_rect_id", None) is not None:
            self.canvas.delete(self._manual_eye_rect_id)
            self._manual_eye_rect_id = None

    def _draw_detection_rect(self):
        # Clear existing AI rects
        if getattr(self, "_ai_rect_id", None) is not None:
            self.canvas.delete(self._ai_rect_id)
            self._ai_rect_id = None
        if getattr(self, "_ai_eye_rect_id", None) is not None:
            self.canvas.delete(self._ai_eye_rect_id)
            self._ai_eye_rect_id = None
            
        # Clear existing Manual rects
        if getattr(self, "_manual_rect_id", None) is not None:
            self.canvas.delete(self._manual_rect_id)
            self._manual_rect_id = None
        if getattr(self, "_manual_eye_rect_id", None) is not None:
            self.canvas.delete(self._manual_eye_rect_id)
            self._manual_eye_rect_id = None

        fit = self.current_fit()
        if fit is None:
            return

        def _draw_rect(box, outline, width, dash=None):
            nx1, ny1, nx2, ny2 = box
            x1, y1 = fit.canvas_from_normalized(nx1, ny1)
            x2, y2 = fit.canvas_from_normalized(nx2, ny2)
            return self.canvas.create_rectangle(
                x1, y1, x2, y2,
                outline=outline, width=width, dash=dash
            )

        # Draw AI Boxes (Solid lines)
        if getattr(self, "_ai_box", None):
            self._ai_rect_id = _draw_rect(self._ai_box, "#00ff00", 3)
            
        if getattr(self, "_ai_eye_box", None):
            self._ai_eye_rect_id = _draw_rect(self._ai_eye_box, "#ffb703", 2)
            
        # Draw Manual Boxes (Dashed lines)
        if getattr(self, "_manual_box", None):
            self._manual_rect_id = _draw_rect(self._manual_box, "#00ff00", 3, (4, 4))
            
        if getattr(self, "_manual_eye_box", None):
            self._manual_eye_rect_id = _draw_rect(self._manual_eye_box, "#ffb703", 2, (4, 4))

    def redraw(self, force_resize: bool = False, fast_mode: bool = False):
        if self.current_pil_img is None:
            self.canvas.delete("all")
            self.canvas_img_id = None
            self._last_rendered_state = None
            return

        img_w, img_h = self.current_pil_img.size
        fit = contain_fit(
            img_w,
            img_h,
            self.canvas.winfo_width(),
            self.canvas.winfo_height(),
            zoom=self.zoom_level,
            pan_x=self.pan_x,
            pan_y=self.pan_y,
        )
        if fit is None:
            return

        state_key = (round(self.zoom_level, 3), fit.width, fit.height)

        # FAST PATH: If the render size hasn't changed (e.g. simple mouse drag panning),
        # move the existing canvas item with the compositor instead of rebuilding a
        # PhotoImage from scratch.
        if not force_resize and self._last_rendered_state == state_key and self.canvas_img_id is not None:
            self.canvas.coords(self.canvas_img_id, fit.center_x, fit.center_y)
            self._draw_detection_rect()
            return

        resample = Image.Resampling.NEAREST if fast_mode else Image.Resampling.BILINEAR
        # Capped, and reused across redraws: the pyramid serves a size it has already
        # rendered and otherwise derives the new size from the smallest cached render
        # rather than from the full-resolution source.
        resized = self._pyramid.render((fit.width, fit.height), resample)
        self.current_tk_img = ImageTk.PhotoImage(resized)
        self._last_rendered_state = state_key

        if self.canvas_img_id is not None and self.canvas.type(self.canvas_img_id):
            self.canvas.itemconfig(self.canvas_img_id, image=self.current_tk_img)
            self.canvas.coords(self.canvas_img_id, fit.center_x, fit.center_y)
        else:
            self.canvas.delete("all")
            self.canvas_img_id = self.canvas.create_image(
                fit.center_x, fit.center_y, anchor="center", image=self.current_tk_img
            )

        self._draw_detection_rect()

    def _on_drag_start(self, event):
        self.drag_start_x = event.x
        self.drag_start_y = event.y

    def _on_drag_motion(self, event):
        dx = event.x - self.drag_start_x
        dy = event.y - self.drag_start_y
        self.pan_x += dx
        self.pan_y += dy
        self.drag_start_x = event.x
        self.drag_start_y = event.y

        # Hardware Coords Movement - 60+ FPS Silky Smooth Dragging
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        center_x = (canvas_w / 2) + self.pan_x
        center_y = (canvas_h / 2) + self.pan_y
        if self.canvas_img_id is not None:
            self.canvas.coords(self.canvas_img_id, center_x, center_y)
            self._draw_detection_rect()
        else:
            self.redraw(force_resize=False)

    def _create_crop_toolbar(self):
        self.crop_toolbar = ctk.CTkFrame(
            self,
            fg_color="#1a1a24",
            corner_radius=8,
            border_width=2,
            border_color="#ffb703",
            height=44,
            width=650
        )
        self.crop_toolbar.pack_propagate(False)

        lbl_title = ctk.CTkLabel(
            self.crop_toolbar,
            text="✂️ MANUAL CROP MODE",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#ffb703"
        )
        lbl_title.pack(side="left", padx=(12, 10))

        lbl_aspect = ctk.CTkLabel(
            self.crop_toolbar,
            text="Aspect Ratio:",
            font=ctk.CTkFont(size=11)
        )
        lbl_aspect.pack(side="left", padx=(5, 2))

        self.opt_aspect = ctk.CTkOptionMenu(
            self.crop_toolbar,
            values=["Free", "1:1 Square", "16:9 Widescreen", "4:3 Standard", "3:2 DSLR", "9:16 Story/Reel", "4:5 Portrait"],
            width=130,
            command=self._on_aspect_changed
        )
        self.opt_aspect.pack(side="left", padx=5)

        self.btn_confirm_crop = ctk.CTkButton(
            self.crop_toolbar,
            text="✔️ Save Crop (Enter / Ctrl+S)",
            fg_color="#2b9348",
            hover_color="#1b4332",
            width=180,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._on_confirm_crop
        )
        self.btn_confirm_crop.pack(side="left", padx=5)

        self.btn_cancel_crop = ctk.CTkButton(
            self.crop_toolbar,
            text="❌ Cancel (Esc)",
            fg_color="#d90429",
            hover_color="#8d99ae",
            width=100,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.exit_crop_mode
        )
        self.btn_cancel_crop.pack(side="left", padx=5)

        self.crop_toolbar.place_forget()

    def enter_crop_mode(self, on_confirm_callback=None):
        if self.current_pil_img is None:
            return

        self.is_cropping = True
        self.on_confirm_crop_cb = on_confirm_callback
        self.crop_toolbar.place(relx=0.5, rely=0.05, anchor="n")
        self.crop_toolbar.lift()

        # Default crop box to 80% center
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        margin_x = canvas_w * 0.15
        margin_y = canvas_h * 0.15
        self.crop_box = (margin_x, margin_y, canvas_w - margin_x, canvas_h - margin_y)
        self._update_crop_rect_draw()

    def exit_crop_mode(self):
        self.is_cropping = False
        self.crop_toolbar.place_forget()
        if self.crop_rect_id is not None:
            self.canvas.delete(self.crop_rect_id)
            self.crop_rect_id = None
        self.crop_box = None

    def _create_anno_toolbar(self):
        self.anno_toolbar = ctk.CTkFrame(
            self, fg_color="#1a1a24", corner_radius=8,
            border_width=2, border_color="#ffb703", height=44, width=640
        )
        self.anno_toolbar.pack_propagate(False)

        lbl_title = ctk.CTkLabel(
            self.anno_toolbar, text="🎯 CORRECT BOUNDING BOX",
            font=ctk.CTkFont(size=12, weight="bold"), text_color="#ffb703"
        )
        lbl_title.pack(side="left", padx=(12, 10))

        lbl_hint = ctk.CTkLabel(
            self.anno_toolbar, 
            text="🟢 Left Drag: Subject  |  🟡 Right Drag: Eye",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#e0e0e0"
        )
        lbl_hint.pack(side="left", padx=(5, 15))

        self.btn_confirm_anno = ctk.CTkButton(
            self.anno_toolbar, text="✔️ Save Annotations",
            fg_color="#2b9348", hover_color="#1b4332", width=140,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._on_confirm_anno
        )
        self.btn_confirm_anno.pack(side="left", padx=5)

        self.btn_cancel_anno = ctk.CTkButton(
            self.anno_toolbar, text="❌ Cancel",
            fg_color="#d90429", hover_color="#8d99ae", width=100,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.exit_anno_mode
        )
        self.btn_cancel_anno.pack(side="left", padx=5)
        self.anno_toolbar.place_forget()

    def _on_anno_class_changed(self, choice: str):
        self.anno_class = choice

    def enter_anno_mode(self, current_image_path: str, on_save_callback=None):
        if self.current_pil_img is None:
            return
        
        self.current_image_path = current_image_path
        self.is_annotating = True
        self.on_save_anno_cb = on_save_callback
        self.anno_subject_box_px = None
        self.anno_eye_box_px = None
        self.anno_toolbar.place(relx=0.5, rely=0.05, anchor="n")
        self.anno_toolbar.lift()

    def exit_anno_mode(self):
        self.is_annotating = False
        self.anno_toolbar.place_forget()
        self._clear_anno_rects()

    def _clear_anno_rects(self):
        for rid in (self.anno_drag_rect_id, self.anno_subject_rect_id, self.anno_eye_rect_id):
            if rid is not None:
                self.canvas.delete(rid)
        self.anno_drag_rect_id = None
        self.anno_subject_rect_id = None
        self.anno_eye_rect_id = None

    def _on_confirm_anno(self):
        if self.on_save_anno_cb and self.current_image_path and self.current_pil_img:
            img_w, img_h = self.current_pil_img.size

            fit = self.current_fit()
            if fit is None:
                return

            # Convert canvas px back to source-image pixels
            def to_img_box(px_box):
                if not px_box:
                    return None
                x1, y1, x2, y2 = px_box
                img_x1, img_y1 = fit.image_from_canvas(x1, y1, img_w, img_h)
                img_x2, img_y2 = fit.image_from_canvas(x2, y2, img_w, img_h)

                return (int(min(img_x1, img_x2)), int(min(img_y1, img_y2)), 
                        int(max(img_x1, img_x2)), int(max(img_y1, img_y2)))

            s_box = to_img_box(self.anno_subject_box_px)
            e_box = to_img_box(self.anno_eye_box_px)
            
            if s_box or e_box:
                self.on_save_anno_cb(
                    self.current_image_path, 
                    img_w, 
                    img_h, 
                    s_box, 
                    e_box,
                    self.current_pil_img
                )
            self.exit_anno_mode()

    def _on_aspect_changed(self, choice: str):
        if self.crop_box:
            x1, y1, x2, y2 = self.crop_box
            w = abs(x2 - x1)
            new_h = self._calc_aspect_height(w, choice)
            if new_h:
                cy = (y1 + y2) / 2.0
                self.crop_box = (x1, cy - (new_h / 2.0), x2, cy + (new_h / 2.0))
                self._update_crop_rect_draw()

    def _calc_aspect_height(self, width: float, choice: str) -> Optional[float]:
        ratios = {
            "1:1 Square": 1.0,
            "16:9 Widescreen": 16.0 / 9.0,
            "4:3 Standard": 4.0 / 3.0,
            "3:2 DSLR": 3.0 / 2.0,
            "9:16 Story/Reel": 9.0 / 16.0,
            "4:5 Portrait": 4.0 / 5.0,
        }
        if choice in ratios:
            return width / ratios[choice]
        return None

    def _update_crop_rect_draw(self):
        if not self.crop_box:
            return
        x1, y1, x2, y2 = self.crop_box
        left, right = min(x1, x2), max(x1, x2)
        top, bottom = min(y1, y2), max(y1, y2)

        if self.crop_rect_id is not None:
            self.canvas.coords(self.crop_rect_id, left, top, right, bottom)
        else:
            self.crop_rect_id = self.canvas.create_rectangle(
                left, top, right, bottom,
                outline="#ffb703", width=3, dash=(6, 4)
            )

    def _on_drag_start(self, event):
        if self.is_cropping:
            self.crop_start_x = event.x
            self.crop_start_y = event.y
            self.crop_box = (event.x, event.y, event.x, event.y)
            self._update_crop_rect_draw()
        elif self.is_annotating:
            self._start_anno_drag(event, target_class="Subject")
        else:
            self.drag_start_x = event.x
            self.drag_start_y = event.y

    def _on_right_drag_start(self, event):
        if self.is_annotating:
            self._start_anno_drag(event, target_class="Eye")

    def _start_anno_drag(self, event, target_class: str):
        self.anno_active_class = target_class
        self.anno_drag_start_x = event.x
        self.anno_drag_start_y = event.y
        if self.anno_drag_rect_id is not None:
            self.canvas.delete(self.anno_drag_rect_id)
        color = "#00ff00" if self.anno_active_class == "Subject" else "#ffb703"
        width = 3 if self.anno_active_class == "Subject" else 2
        self.anno_drag_rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline=color, width=width
        )

    def _on_drag_motion(self, event):
        if self.is_cropping:
            x1 = self.crop_start_x
            y1 = self.crop_start_y
            x2 = event.x
            y2 = event.y

            w = abs(x2 - x1)
            is_shift_held = bool(event.state & 0x0001) or bool(event.state & 0x0004)

            if is_shift_held:
                target_h = w
            else:
                aspect_choice = self.opt_aspect.get()
                target_h = self._calc_aspect_height(w, aspect_choice)

            if target_h:
                y2 = y1 + target_h if y2 >= y1 else y1 - target_h

            self.crop_box = (x1, y1, x2, y2)
            self._update_crop_rect_draw()
        elif self.is_annotating:
            self._motion_anno_drag(event)
        else:
            dx = event.x - self.drag_start_x
            dy = event.y - self.drag_start_y
            self.pan_x += dx
            self.pan_y += dy
            self.drag_start_x = event.x
            self.drag_start_y = event.y

            # Hardware Coords Movement - 60+ FPS Silky Smooth Dragging
            canvas_w = self.canvas.winfo_width()
            canvas_h = self.canvas.winfo_height()
            center_x = (canvas_w / 2) + self.pan_x
            center_y = (canvas_h / 2) + self.pan_y
            if self.canvas_img_id is not None:
                self.canvas.coords(self.canvas_img_id, center_x, center_y)
                self._draw_detection_rect()
            else:
                self.redraw(force_resize=False)

    def _on_right_drag_motion(self, event):
        if self.is_annotating:
            self._motion_anno_drag(event)

    def _motion_anno_drag(self, event):
        if self.anno_drag_rect_id:
            self.canvas.coords(
                self.anno_drag_rect_id,
                self.anno_drag_start_x, self.anno_drag_start_y,
                event.x, event.y
            )

    def _on_drag_release(self, event):
        if self.is_cropping and self.crop_box:
            x1, y1, x2, y2 = self.crop_box
            if abs(x2 - x1) < 10 or abs(y2 - y1) < 10:
                # If tiny click, restore 80% default box
                canvas_w = self.canvas.winfo_width()
                canvas_h = self.canvas.winfo_height()
                mx = canvas_w * 0.15
                my = canvas_h * 0.15
                self.crop_box = (mx, my, canvas_w - mx, canvas_h - my)
                self._update_crop_rect_draw()
        elif self.is_annotating:
            self._release_anno_drag(event)

    def _on_right_drag_release(self, event):
        if self.is_annotating:
            self._release_anno_drag(event)

    def _release_anno_drag(self, event):
        if self.anno_drag_rect_id:
            coords = self.canvas.coords(self.anno_drag_rect_id)
            self.canvas.delete(self.anno_drag_rect_id)
            self.anno_drag_rect_id = None
            
            target_cls = getattr(self, "anno_active_class", "Subject")
            if coords and abs(coords[2] - coords[0]) > 5 and abs(coords[3] - coords[1]) > 5:
                if target_cls == "Subject":
                    self.anno_subject_box_px = tuple(coords)
                    if self.anno_subject_rect_id: self.canvas.delete(self.anno_subject_rect_id)
                    self.anno_subject_rect_id = self.canvas.create_rectangle(
                        *coords, outline="#00ff00", width=3
                    )
                else:
                    self.anno_eye_box_px = tuple(coords)
                    if self.anno_eye_rect_id: self.canvas.delete(self.anno_eye_rect_id)
                    self.anno_eye_rect_id = self.canvas.create_rectangle(
                        *coords, outline="#ffb703", width=2
                    )

    def _on_confirm_crop(self):
        if not self.crop_box or self.current_pil_img is None:
            return

        percentages = self.get_crop_box_percentages()
        cb = self.on_confirm_crop_cb
        self.exit_crop_mode()

        if cb and percentages:
            cb(*percentages)

    def get_crop_box_percentages(self) -> Optional[Tuple[float, float, float, float]]:
        """
        If currently in crop mode with a valid crop box, return (pct_x1, pct_y1, pct_x2, pct_y2)
        relative to the full source image. Returns None if not cropping or box invalid.
        """
        if not self.is_cropping or self.crop_box is None or self.current_pil_img is None:
            return None

        fit = self.current_fit()
        if fit is None:
            return None

        cx1, cy1, cx2, cy2 = self.crop_box
        left_box = min(cx1, cx2)
        right_box = max(cx1, cx2)
        top_box = min(cy1, cy2)
        bottom_box = max(cy1, cy2)

        nx1, ny1 = fit.normalized_from_canvas(left_box, top_box)
        nx2, ny2 = fit.normalized_from_canvas(right_box, bottom_box)

        pct_x1 = max(0.0, min(1.0, nx1))
        pct_y1 = max(0.0, min(1.0, ny1))
        pct_x2 = max(0.0, min(1.0, nx2))
        pct_y2 = max(0.0, min(1.0, ny2))

        if pct_x2 <= pct_x1 or pct_y2 <= pct_y1:
            return None

        return (pct_x1, pct_y1, pct_x2, pct_y2)

    def _cancel_zoom_timer(self):
        if self._zoom_timer is not None:
            try:
                self.after_cancel(self._zoom_timer)
            except Exception:
                pass
            self._zoom_timer = None

    def _on_zoom(self, event):
        if event.delta > 0:
            self.zoom_level *= 1.15
        else:
            self.zoom_level /= 1.15
        self.zoom_level = max(0.2, min(5.0, self.zoom_level))

        # Render instant crop preview while scrolling
        self.redraw(force_resize=True, fast_mode=True)

        # Debounce crisp render 100ms after mouse wheel stops
        self._cancel_zoom_timer()
        self._zoom_timer = self.after(100, lambda: self.redraw(force_resize=True, fast_mode=False))

    def _on_double_click(self, event):
        if self.current_pil_img is None:
            return

        if self.zoom_level <= 1.05:
            # Zoom in to 2.5x centered at click position
            self.zoom_level = 2.5
            canvas_w = self.canvas.winfo_width()
            canvas_h = self.canvas.winfo_height()

            click_offset_x = event.x - (canvas_w / 2)
            click_offset_y = event.y - (canvas_h / 2)
            self.pan_x = -click_offset_x * 1.5
            self.pan_y = -click_offset_y * 1.5
        else:
            # Reset back to fit-to-screen
            self.zoom_level = 1.0
            self.pan_x = 0.0
            self.pan_y = 0.0

        self.redraw(force_resize=True)

    def destroy(self):
        self._cancel_zoom_timer()
        if getattr(self, "_configure_after_id", None) is not None:
            try:
                self.after_cancel(self._configure_after_id)
            except Exception:
                pass
            self._configure_after_id = None
        self._pyramid.clear()
        super().destroy()
