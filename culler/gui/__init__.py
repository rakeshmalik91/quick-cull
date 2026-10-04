"""
GUI components for Quick Cull.
"""

import os
import sys


def _ensure_tcl_env():
    if "TCL_LIBRARY" not in os.environ:
        tcl_dir = os.path.join(sys.prefix, "tcl", "tcl8.6")
        if os.path.exists(tcl_dir):
            os.environ["TCL_LIBRARY"] = tcl_dir
        else:
            lib_tcl = os.path.join(sys.prefix, "Lib", "tcl8.6")
            if os.path.exists(lib_tcl):
                os.environ["TCL_LIBRARY"] = lib_tcl

    if "TK_LIBRARY" not in os.environ:
        tk_dir = os.path.join(sys.prefix, "tcl", "tk8.6")
        if os.path.exists(tk_dir):
            os.environ["TK_LIBRARY"] = tk_dir
        else:
            lib_tk = os.path.join(sys.prefix, "Lib", "tk8.6")
            if os.path.exists(lib_tk):
                os.environ["TK_LIBRARY"] = lib_tk


def _apply_fast_scroll_patch():
    import sys
    import customtkinter as ctk

    if getattr(ctk.CTkScrollableFrame, "_fast_scroll_patched", False):
        return

    def _fast_mouse_wheel_all(self, event):
        if self._check_if_valid_scroll(event.widget):
            if sys.platform.startswith("win"):
                # Default CTk is -int(event.delta / 6) = 20 units (very slow).
                # 0.5 factor gives 240 units per notch (~2.5 full thumbnail rows per notch).
                scroll_units = -int(event.delta / 0.5)
                if self._shift_pressed:
                    if self._parent_canvas.xview() != (0.0, 1.0):
                        self._parent_canvas.xview("scroll", scroll_units, "units")
                else:
                    if self._parent_canvas.yview() != (0.0, 1.0):
                        self._parent_canvas.yview("scroll", scroll_units, "units")
            elif sys.platform == "darwin":
                scroll_units = -int(event.delta * 10)
                if self._shift_pressed:
                    if self._parent_canvas.xview() != (0.0, 1.0):
                        self._parent_canvas.xview("scroll", scroll_units, "units")
                else:
                    if self._parent_canvas.yview() != (0.0, 1.0):
                        self._parent_canvas.yview("scroll", scroll_units, "units")
            else:
                mult = -10 if getattr(event, "num", None) == 4 else 10
                if self._shift_pressed:
                    if self._parent_canvas.xview() != (0.0, 1.0):
                        self._parent_canvas.xview_scroll(mult, "units")
                else:
                    if self._parent_canvas.yview() != (0.0, 1.0):
                        self._parent_canvas.yview_scroll(mult, "units")

    ctk.CTkScrollableFrame._mouse_wheel_all = _fast_mouse_wheel_all
    ctk.CTkScrollableFrame._fast_scroll_patched = True


_ensure_tcl_env()
_apply_fast_scroll_patch()

from .toolbar import HeaderToolbar
from .thumbnail_list import ThumbnailList
from .canvas_viewer import ImageCanvasViewer
from .metadata_panel import MetadataPanel
from .cleanup_dialog import MetadataCleanupDialog
from .settings_dialog import SettingsDialog
from .blur_dialog import BlurScanDialog
from .duplicate_dialog import DuplicateScanDialog
from .progress_dialog import ProgressDialog
from .tooltip import ToolTip
from .tab_bar import TabBar
from .about_dialog import AboutDialog

__all__ = [
    "HeaderToolbar",
    "ThumbnailList",
    "ImageCanvasViewer",
    "MetadataPanel",
    "MetadataCleanupDialog",
    "SettingsDialog",
    "BlurScanDialog",
    "DuplicateScanDialog",
    "ProgressDialog",
    "ToolTip",
    "TabBar",
    "AboutDialog",
]
