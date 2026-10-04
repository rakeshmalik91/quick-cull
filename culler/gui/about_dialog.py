import customtkinter as ctk

COPYRIGHT = "\u00a9 Rakesh Malik 2026"
CO_AUTHORS = ["Gemini 3.8 Flash", "Spaace Bunny Alpha", "Claude Opus 4.6"]
TITLE = "About"
SUBTITLE = "A fast local photo culling workspace"


class AboutDialog(ctk.CTkToplevel):
    """Small, non-blocking About window with authorship and credits."""

    def __init__(self, master, app_name: str = "Quick Cull", version: str = ""):
        super().__init__(master)

        self.title(TITLE)
        self.configure(fg_color="#1a1a1a")
        self.resizable(False, False)
        self.transient(master)

        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkLabel(
            self,
            text=app_name,
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color="#ffffff",
        )
        header.grid(row=0, column=0, padx=28, pady=(22, 0))

        if version:
            version_label = ctk.CTkLabel(
                self,
                text=f"version {version}",
                font=ctk.CTkFont(size=11),
                text_color="#888888",
            )
            version_label.grid(row=1, column=0, padx=28, pady=(0, 12))
        else:
            header.grid_configure(pady=(22, 12))

        subtitle = ctk.CTkLabel(
            self,
            text=SUBTITLE,
            font=ctk.CTkFont(size=11),
            text_color="#888888",
        )
        subtitle.grid(row=2, column=0, padx=28, pady=(0, 14))

        divider = ctk.CTkFrame(self, height=1, fg_color="#333333")
        divider.grid(row=3, column=0, sticky="ew", padx=28, pady=(0, 14))

        copyright_label = ctk.CTkLabel(
            self,
            text=COPYRIGHT,
            font=ctk.CTkFont(size=13),
            text_color="#dddddd",
        )
        copyright_label.grid(row=4, column=0, padx=28, pady=(0, 16))

        co_author_label = ctk.CTkLabel(
            self,
            text="Co authored by:",
            font=ctk.CTkFont(size=11),
            text_color="#888888",
        )
        co_author_label.grid(row=5, column=0, padx=28, pady=(0, 6))

        for index, name in enumerate(CO_AUTHORS):
            row = ctk.CTkLabel(
                self,
                text=name,
                font=ctk.CTkFont(size=13),
                text_color="#cccccc",
            )
            row.grid(row=6 + index, column=0, padx=28, pady=2)

        close_button = ctk.CTkButton(
            self,
            text="Close",
            width=96,
            height=30,
            fg_color="#2b2b2b",
            hover_color="#3a3a3a",
            font=ctk.CTkFont(size=12),
            command=self._on_close,
        )
        close_button.grid(row=6 + len(CO_AUTHORS), column=0, padx=28, pady=(20, 24))

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda _event: self._on_close())
        self.bind("<Return>", lambda _event: self._on_close())

        self.update_idletasks()
        self._center_over(master)
        self.deiconify()
        self.lift()
        self.focus_force()

    def _center_over(self, master):
        try:
            parent_x = master.winfo_rootx()
            parent_y = master.winfo_rooty()
            parent_w = master.winfo_width()
            parent_h = master.winfo_height()
        except Exception:
            return

        width = self.winfo_reqwidth()
        height = self.winfo_reqheight()
        x = parent_x + (parent_w - width) // 2
        y = parent_y + (parent_h - height) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _on_close(self):
        self.grab_release()
        self.destroy()