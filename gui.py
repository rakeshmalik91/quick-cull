import os
import sys
import warnings

# Suppress known upstream third-party FutureWarning (e.g. Keras/TF np.object warning)
warnings.filterwarnings("ignore", category=FutureWarning, module="keras.*")
warnings.filterwarnings("ignore", message=".*np\\.object.*", category=FutureWarning)

import threading
import time
import tkinter as tk
from tkinter import filedialog as fd, messagebox as mb, simpledialog
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, List, Dict, Any, Set, Union
from dataclasses import dataclass, field

import customtkinter as ctk
from PIL import Image

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _HAS_DND = True
except Exception:
    DND_FILES = None
    TkinterDnD = None
    _HAS_DND = False

from culler.culler_engine import CullingSession, ImageItem, FlagState, resolve_input_path, find_item_index_by_path
from culler.db_manager import DatabaseManager
from culler.dataset_exporter import save_annotation, save_manual_annotation
from culler.folder_watcher import FolderWatcher, FolderChange
from culler.exif_wrapper import ExifToolWrapper
from culler.image_loader import ImageLoader
from culler.gui.metadata_panel import BAG_SETTINGS_KEY, BAG_COLLAPSED_SETTINGS_KEY
from culler.ml_trainer import train_custom_yolo
from culler.paths import DATASET_DIR
from bootstrap import APP_NAME, SplashScreen, adopt_default_root, apply_window_icon, launch_gui
from culler.gui import HeaderToolbar, ThumbnailList, ImageCanvasViewer, MetadataPanel, MetadataCleanupDialog, SettingsDialog, BlurScanDialog, DuplicateScanDialog, ProgressDialog, TabBar, AboutDialog
from culler.logger import log_info, log_debug, log_error

# Set modern dark UI theme
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class ImageCullerApp(ctk.CTk):
    """
    Main Application Window for Quick Cull.
    Features: Multi-tab folder management, Fast ARW/RAW+JPG culling, 0ms RAM pre-fetch buffer navigation,
    Scan for Blur, Scan for Duplicates, Tagging (Blur, Duplicate, Dark, Over-exposed, Custom),
    and SQLite metadata folder hierarchy cleanup.
    """

    @property
    def dataset_dir(self) -> Path:
        """Returns the _DATASET directory associated with the active workspace."""
        if self.db:
            return self.db.dataset_dir
        return DATASET_DIR

    def _update_window_title(self):
        ws_name = self.db.db_path.name if self.db and hasattr(self.db, "db_path") else "default.fpc-workspace"
        self.title(f"{APP_NAME} - [{ws_name}]")

    def __init__(
        self,
        initial_path: Optional[str] = None,
        show_splash: bool = True,
        splash_screen: Optional[Any] = None,
        workspace_path: Optional[str] = None
    ):
        super().__init__()
        # The splash owns tkinter's default root until it closes; take it over
        # so this window's fonts and widgets are created on its own interpreter.
        adopt_default_root(self)
        apply_window_icon(self)

        target_ws = workspace_path
        folder_or_img = initial_path
        if initial_path and (str(initial_path).endswith(".fpc-workspace") or str(initial_path).endswith(".db")):
            target_ws = initial_path
            folder_or_img = None

        if not getattr(self, "db", None) or target_ws is not None:
            self.db = DatabaseManager(target_ws)

        self._update_window_title()

        self.tabs: List[Dict[str, Any]] = []
        self.active_tab_index: int = -1

        # Restore window geometry (size & position) from DB
        w, h, x, y, is_max = self.db.get_window_geometry()
        if x is not None and y is not None:
            self.geometry(f"{w}x{h}+{x}+{y}")
        else:
            self.geometry(f"{w}x{h}")

        if is_max:
            self.after(10, lambda: self.state("zoomed"))

        self._load_request_id: int = 0
        self._load_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="imgload")
        # Speculative work is strictly subordinate to the photo on screen: one thread,
        # so a burst of navigation cannot fan out into a decode of every neighbour.
        self._prefetch_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prefetch")
        self._prefetch_generation: int = 0

        self.current_items: List[ImageItem] = []
        self.current_index: int = -1
        self.selected_indices: Set[int] = set()
        self.selection_anchor_idx: int = 0

        # Start with main window hidden while splash screen is active
        self.withdraw()

        splash = splash_screen
        if splash is None and show_splash:
            try:
                splash = SplashScreen()
                if initial_path:
                    folder_label = os.path.basename(initial_path.rstrip("/\\")) or initial_path
                    splash.set_status(f"Loading {folder_label}...")
                else:
                    splash.set_status("Initializing workspace...")
            except Exception:
                splash = None

        if splash:
            splash.set_status("Creating components...")

        self._create_components()
        self._bind_events()

        if splash:
            splash.set_status("Restoring workspace...")

        # Restore open tabs from DB (lazy load active if no folder_or_img)
        self._restore_tabs_state(auto_load_active=(folder_or_img is None))

        # Close splash and reveal main window
        if splash:
            splash.close()

        self.deiconify()
        self.lift()
        self.focus_force()
        self.update_idletasks()
        try:
            self.update()
        except Exception:
            pass

        if folder_or_img:
            self.after(10, lambda p=folder_or_img: self.open_path(p))

    def _get_active_tab(self) -> Optional[Dict[str, Any]]:
        if 0 <= self.active_tab_index < len(self.tabs):
            return self.tabs[self.active_tab_index]
        return None

    def _get_active_session(self) -> Optional[CullingSession]:
        tab = self._get_active_tab()
        return tab["session"] if tab else None

    def _save_active_tab_state(self):
        tab = self._get_active_tab()
        if not tab:
            return
        tab["filter_values"] = self.toolbar.get_filter_values()
        tab["selected_indices"] = set(self.selected_indices)
        tab["selection_anchor_idx"] = self.selection_anchor_idx
        tab["current_items"] = list(self.current_items)
        tab["current_index"] = self.current_index

    def _create_tab_info(self, directory: str, target_image: Optional[Path] = None) -> Dict[str, Any]:
        folder_name = os.path.basename(directory) or directory
        return {
            "directory": str(Path(directory).resolve()),
            "tab_label": folder_name,
            "session": CullingSession(
                db_manager=self.db,
                exif_wrapper=self.exif_wrapper,
                image_loader=self.image_loader,
            ),
            "filter_values": {
                "flag": "All",
                "rating": [],
                "format": "All Formats",
                "tag": []
            },
            "current_items": [],
            "current_index": -1,
            "selected_indices": set(),
            "selection_anchor_idx": 0,
            "is_loaded": False,
            "loading": False,
            "load_total": 0,
            "load_current": 0,
            "load_stats": {"folder": None, "thumb": None},
            "pending_target_image": target_image,
        }

    def _restore_tabs_state(self, auto_load_active: bool = True):
        saved = self.db.get_open_tabs()
        if not saved or not isinstance(saved, dict):
            saved = {"tabs": [], "active_index": 0}

        tabs_data = saved.get("tabs", [])
        active_idx = saved.get("active_index", 0)

        if not tabs_data:
            return

        restored_tabs = []
        for t in tabs_data:
            if not isinstance(t, dict):
                continue
            directory = t.get("directory", "")
            if not directory or not os.path.exists(directory):
                continue
            tab_info = self._create_tab_info(directory)
            tab_info["tab_label"] = t.get("tab_label", tab_info["tab_label"]).replace(" ⟳", "")
            saved_filters = t.get("filter_values", tab_info["filter_values"])
            if isinstance(saved_filters, dict):
                saved_filters["flag"] = "All"
            tab_info["filter_values"] = saved_filters
            tab_info["is_loaded"] = False
            restored_tabs.append(tab_info)

        if not restored_tabs:
            return

        self.tabs = restored_tabs
        active_idx = min(active_idx, len(self.tabs) - 1)
        self.active_tab_index = active_idx

        for i, t in enumerate(self.tabs):
            self.tab_bar.add_tab(t["tab_label"])

        self.tab_bar.set_active(active_idx)
        self.tab_bar.update_idletasks()
        try:
            self.tab_bar.update()
        except Exception:
            pass

        if auto_load_active:
            # Lazy load: only load active tab on startup, others load on demand
            tab = self.tabs[active_idx]
            self._load_tab_directory(tab, show_progress=True)

        # Re-assert active tab color after loading indicator may have updated label
        def _restore_active_color():
            self.tab_bar.set_active(self.active_tab_index)
            self.tab_bar.update_idletasks()

        self.after(1, _restore_active_color)
        self.after(100, _restore_active_color)

    def _add_tab(self, directory: str, target_image: Optional[Path] = None):
        tab_info = self._create_tab_info(directory, target_image=target_image)
        self.tabs.append(tab_info)
        idx = self.tab_bar.add_tab(tab_info["tab_label"])
        self.tab_bar.set_active(len(self.tabs) - 1)
        self.active_tab_index = len(self.tabs) - 1
        self._load_tab_directory(tab_info, show_progress=True)
        self._persist_tabs_state()

    def open_path(self, path_input: Union[str, Path]):
        """
        Opens a folder or image file path in the app.
        If a folder is passed: opens the folder in a tab (or switches to it if already open).
        If an image file is passed: opens the containing folder and automatically selects that image.
        """
        try:
            folder_path, target_image = resolve_input_path(path_input)
        except Exception as e:
            log_error(f"Failed to open path '{path_input}': {e}")
            self._update_status(f"Error: Path does not exist - {path_input}")
            return

        matching_tab_idx = None
        folder_resolved = folder_path.resolve()
        for idx, tab in enumerate(self.tabs):
            tab_dir = Path(tab.get("directory", "")).resolve()
            if tab_dir == folder_resolved:
                matching_tab_idx = idx
                break

        if matching_tab_idx is not None:
            tab = self.tabs[matching_tab_idx]
            if target_image:
                tab["pending_target_image"] = target_image

            if matching_tab_idx != self.active_tab_index:
                self._switch_tab(matching_tab_idx)
            else:
                # Active tab is already this one
                if target_image and tab.get("is_loaded"):
                    tab["pending_target_image"] = None
                    found_idx, matched_sub = find_item_index_by_path(self.current_items, target_image)
                    if found_idx < 0 and tab.get("session") and tab["session"].items:
                        # Reset filter to All if filtered out
                        self.toolbar.apply_filter_values({"flag": "All", "rating": [], "format": "All Formats", "tag": []})
                        self._on_filter_changed()
                        found_idx, matched_sub = find_item_index_by_path(self.current_items, target_image)

                    if found_idx >= 0:
                        self._select_image(found_idx, target_path=matched_sub, from_click=False)
                        self.thumb_list.set_selected_indices(self.selected_indices, found_idx, active_path=matched_sub, auto_scroll=True)
                elif not tab.get("is_loaded") and not tab.get("loading"):
                    self._load_tab_directory(tab, show_progress=True)
        else:
            self._add_tab(str(folder_path), target_image=target_image)

    def _setup_drag_drop(self):
        """Enable folder / image drag-and-drop anywhere on the GUI to open tabs."""
        if not _HAS_DND:
            return
        try:
            TkinterDnD.require(self)
        except Exception as e:
            log_error(f"tkdnd not available; drag-and-drop disabled: {e}")
            return
        # NOTE: the CTk root window (a tkinter.Tk) is not a BaseWidget subclass,
        # so drop_target_register/dnd_bind are unavailable on it. The top-level
        # containers below inherit from BaseWidget and together tile the entire
        # window, so drops anywhere on the GUI are captured (tkdnd routes a drop
        # on any descendant to the nearest registered ancestor container).
        drop_targets = [self.tab_bar, self.toolbar, self.main_container, self.status_bar]
        for target in drop_targets:
            try:
                target.drop_target_register(DND_FILES)
                target.dnd_bind("<<Drop>>", self._on_drag_drop)
            except Exception as e:
                log_error(f"Failed to register drag-and-drop on {target}: {e}")
        log_debug("Drag-and-drop enabled for folders and image files.")

    def _parse_drop_data(self, data: str) -> List[Path]:
        """Parse tkdnd <<Drop>> data into a de-duplicated list of existing Path objects."""
        if not data:
            return []
        try:
            raw_paths = self.tk.splitlist(data)
        except Exception:
            raw_paths = (data,)
        paths: List[Path] = []
        seen: Set[str] = set()
        for rp in raw_paths:
            rp = rp.strip()
            if not rp:
                continue
            try:
                p = Path(rp).expanduser()
            except Exception:
                continue
            if not p.exists():
                continue
            key = str(p.resolve())
            if key in seen:
                continue
            seen.add(key)
            paths.append(p)
        return paths

    def _on_drag_drop(self, event):
        """Handle a drag-and-drop <<Drop>> of folders and/or image files."""
        try:
            paths = self._parse_drop_data(event.data)
        except Exception as e:
            log_error(f"Failed to parse dropped paths: {e}")
            return
        if not paths:
            return
        handled = 0
        for p in paths:
            if self._handle_dropped_path(p):
                handled += 1
        if handled:
            self._update_status(f"Opened {handled} item(s) via drag-and-drop.")
        else:
            self._update_status("Drag-and-drop ignored: no supported folders/images found.")

    def _handle_dropped_path(self, path: Path) -> bool:
        """Open a dropped folder as a tab, or open a dropped image's parent folder.

        Returns True if the path was handled (folder or supported image file).
        """
        try:
            if path.is_dir():
                self.open_path(path)
                return True
            if path.is_file() and ImageLoader.is_supported(path):
                self.open_path(path)
                return True
        except Exception as e:
            log_error(f"Failed to handle dropped path '{path}': {e}")
        return False

    def _release_tab_resources(self, tab: Dict[str, Any]) -> None:
        """Drop everything a closed tab was holding.

        The session holds an ``ImageItem`` per photo (metadata dict, stacked paths, tags),
        the shared ``ImageLoader`` holds decoded pixels for every file it ever touched, and
        the grid holds a ``CTkImage`` per painted thumbnail. Closing a tab without any of
        that means the memory keeps growing for the life of the process, which is exactly
        what a user who opens and closes folders notices.
        """
        session = tab.get("session")
        if session is None:
            return

        paths = []
        for item in list(session.items):
            paths.extend(item.stacked_paths)
            if item.path not in item.stacked_paths:
                paths.append(item.path)

        try:
            self.image_loader.invalidate_paths(paths)
        except Exception:
            log_error("Failed to release image caches for a closed tab", exc_info=True)

        try:
            self.thumb_list.forget_paths(paths)
        except Exception:
            log_error("Failed to release thumbnail widgets for a closed tab", exc_info=True)

        try:
            session.release()
        except Exception:
            log_error("Failed to release a closed tab's session", exc_info=True)

    def _close_tab(self, index: int):
        if not (0 <= index < len(self.tabs)):
            return

        tab = self.tabs[index]
        session = tab["session"]
        closed_dir = str(session.directory) if session and session.directory else None

        if tab.get("loading"):
            # The scan worker keeps a reference to this tab dict and will fire
            # _on_tab_scan_complete into it; let it finish, but stop it touching the UI.
            tab["_released"] = True

        self.tab_bar.remove_tab(index)
        self.tabs.pop(index)

        if closed_dir:
            still_open = any(
                t.get("session") and t["session"].directory and str(t["session"].directory) == closed_dir
                for t in self.tabs
            )
            if not still_open:
                self.folder_watcher.unwatch(closed_dir)

        self._release_tab_resources(tab)

        if self.active_tab_index == index:
            # 0 with no tabs left is the app's empty-workspace convention; _get_active_tab
            # returns None either way.
            self.active_tab_index = max(0, min(index, len(self.tabs) - 1))
            if self.tabs:
                self.tab_bar.set_active(self.active_tab_index)
                target = self.tabs[self.active_tab_index]
                if not target["is_loaded"]:
                    self._load_tab_directory(target, show_progress=True)
                else:
                    self._apply_tab_state(target)
            else:
                self._show_no_tabs_state()
        elif self.active_tab_index > index:
            self.active_tab_index -= 1

        self._sync_loading_progress()
        self._persist_tabs_state()

    def _close_all_tabs(self):
        """Close every tab and release all of their memory."""
        if not self.tabs:
            return

        tabs, self.tabs = self.tabs, []
        for tab in tabs:
            session = tab.get("session")
            directory = str(session.directory) if session and session.directory else None
            tab["_released"] = True
            if directory:
                self.folder_watcher.unwatch(directory)
            self._release_tab_resources(tab)

        self.tab_bar.remove_all_tabs()
        self.active_tab_index = 0
        self._show_no_tabs_state()
        self._sync_loading_progress()
        self._persist_tabs_state()
        self._update_status("Closed all tabs and released their memory.")

    def _show_no_tabs_state(self):
        """Reset the workspace to empty after the last tab is closed."""
        self.current_items = []
        self.current_index = -1
        self.selected_indices = set()
        self.selection_anchor_idx = 0

        try:
            self.thumb_list.set_image_loader(None)
            self.thumb_list.update_items([], selected_idx=-1)
        except Exception:
            log_error("Failed to clear the grid after closing tabs", exc_info=True)

        try:
            self.viewer.clear()
            self.meta_panel.clear()
        except Exception:
            log_error("Failed to clear the viewer after closing tabs", exc_info=True)

        self._sync_loading_progress()
        self._update_status("No folder open. Use + to open one.")

    def _switch_tab(self, index: int):
        if index == self.active_tab_index:
            return
        if not (0 <= index < len(self.tabs)):
            return

        self._save_active_tab_state()
        self.active_tab_index = index
        self.tab_bar.set_active(index)
        target = self.tabs[index]

        if not target["is_loaded"]:
            self._apply_tab_state(target)
            self._load_tab_directory(target, show_progress=True)
        else:
            self._apply_tab_state(target)

        self._sync_loading_progress()
        self._persist_tabs_state()

    def _apply_tab_state(self, tab: Dict[str, Any]):
        session = tab["session"]
        self.current_items = list(tab.get("current_items", []))
        self.current_index = tab.get("current_index", -1)
        self.selected_indices = set(tab.get("selected_indices", set()))
        self.selection_anchor_idx = tab.get("selection_anchor_idx", 0)

        self.thumb_list.set_image_loader(session.image_loader)
        self.toolbar.apply_filter_values(tab.get("filter_values", {}))
        self.meta_panel.update_output_folders(
            self.db.get_picked_folder(),
            self.db.get_rejected_folder()
        )

        load_stats = tab.get("load_stats") or {}
        self.thumb_list.show_load_stats(load_stats.get("folder"), load_stats.get("thumb"))

        white_balance = self.toolbar.get_white_balance()
        self.thumb_list.update_items(self.current_items, selected_idx=self.current_index, white_balance=white_balance)

        if self.current_items and load_stats.get("thumb") is None:
            self.thumb_list.begin_thumb_timing()

        if self.current_items and 0 <= self.current_index < len(self.current_items):
            self._select_image(self.current_index, from_click=False)
        else:
            self.viewer.clear()
            self.meta_panel.clear()

        self._update_status(f"Tab: {tab['tab_label']} | {len(self.current_items)} photos loaded")

    def _unwatch_tab_directory(self, tab: Dict[str, Any], new_directory=None):
        """Stop watching a tab's previous folder once it points somewhere else."""
        session = tab.get("session")
        previous = session.directory if session else None
        if not previous:
            return
        if new_directory is not None:
            try:
                if Path(previous) == Path(new_directory):
                    return
            except (TypeError, ValueError):
                return
        previous_str = str(previous)
        still_open = any(
            t is not tab and t.get("session") and t["session"].directory
            and str(t["session"].directory) == previous_str
            for t in self.tabs
        )
        if not still_open:
            self.folder_watcher.unwatch(previous_str)

    def _watch_tab_directory(self, tab: Dict[str, Any]):
        """Watch a loaded tab's folder so external edits trigger a reload."""
        session = tab.get("session")
        directory = session.directory if session else None
        if not directory or not directory.exists():
            return
        try:
            self.folder_watcher.watch(
                directory,
                lambda change, owner=tab: self.after(
                    0, lambda ch=change, own=owner: self._on_folder_changed(own, ch)
                ),
            )
        except Exception:
            log_error("Failed to watch directory for changes", exc_info=True)

    def _on_folder_changed(self, tab: Dict[str, Any], change: FolderChange):
        """Refresh a tab whose folder changed on disk outside the app.

        Goes through the tab's own loader, not ``_load_directory``: a watcher-triggered
        reload must not throw away the tab's filters, selection and scroll position, and
        the session's manifest makes the rescan differential, so the cost is one scandir
        pass plus EXIF for the files that actually changed.
        """
        if tab not in self.tabs:
            self.folder_watcher.unwatch(change.directory)
            return

        current_dir = tab.get("directory")
        if not current_dir or Path(current_dir) != change.directory:
            self.folder_watcher.unwatch(change.directory)
            return

        if tab.get("loading"):
            self.folder_watcher.resync(change.directory)
            return

        self._update_status(f"Folder changed ({change.summary()}), reloading...")

        is_active = tab is self._get_active_tab()
        self._load_tab_directory(tab, show_progress=is_active)

    def _suppress_folder_watch(self, directory, seconds: Optional[float] = None):
        """Keep the app's own writes to a folder from triggering a redundant reload."""
        if not directory:
            return
        try:
            self.folder_watcher.suppress(directory, seconds)
        except Exception:
            log_error("Failed to suppress folder watch", exc_info=True)

    def _on_load_stats_changed(self, stats: Dict[str, Optional[float]]):
        """Persist the thumbnail list's timing totals onto the active tab."""
        tab = self._get_active_tab()
        if tab is None:
            return
        tab["load_stats"] = {"folder": stats.get("folder"), "thumb": stats.get("thumb")}

    def _load_tab_directory(self, tab: Dict[str, Any], show_progress: bool = True):
        directory = tab["directory"]
        if not directory or not os.path.exists(directory):
            return
        if tab.get("loading"):
            return

        self._unwatch_tab_directory(tab, directory)

        tab["loading"] = True
        tab["load_total"] = 0
        tab["load_current"] = 0
        tab["arw_count"] = 0
        tab["_placeholders_loaded"] = False
        tab["_load_started_at"] = time.monotonic()
        self._update_tab_loading_indicator(tab)

        if tab is self._get_active_tab():
            self.thumb_list.start_load_timing(tab["_load_started_at"])
            if not tab.get("current_items") and self.current_items:
                self.current_items = []
                self.thumb_list.update_items([])

        white_balance = self.toolbar.get_white_balance()

        def on_discovered(items: List[ImageItem], arw_count: int):
            tab["arw_count"] = arw_count
            tab["load_total"] = len(items)
            if not tab.get("_released"):
                if not tab.get("_placeholders_loaded"):
                    tab["_placeholders_loaded"] = True
                    self.after(0, lambda: self._preload_placeholder_items(tab, white_balance, items=items, arw_count=arw_count))
                elif tab is self._get_active_tab():
                    self.after(0, self._sync_loading_progress)

        def on_progress(current: int, total: int, filename: str = ""):
            tab["load_current"] = current
            tab["load_total"] = total
            if tab is self._get_active_tab() and not tab.get("_released"):
                if current == 0 and total > 0 and not tab.get("_placeholders_loaded"):
                    tab["_placeholders_loaded"] = True
                    self.after(50, lambda: self._preload_placeholder_items(tab, white_balance))
                self.after(0, self._sync_loading_progress)

        def worker():
            started_at = time.monotonic()
            try:
                tab["session"].scan_directory(
                    directory,
                    stack_raw_jpg=True,
                    progress_callback=on_progress,
                    on_discovered=on_discovered,
                )
                tab["load_stats"]["folder"] = time.monotonic() - started_at
                tab["is_loaded"] = True
                tab["loading"] = False
                self.after(0, lambda: self._on_tab_scan_complete(tab))
            except Exception as e:
                tab["load_stats"]["folder"] = time.monotonic() - started_at
                tab["loading"] = False
                self.after(0, lambda err=e: self._on_tab_scan_error(tab, err))

        threading.Thread(target=worker, daemon=True).start()

    def _preload_placeholder_items(
        self,
        tab: Dict[str, Any],
        white_balance: str = "camera",
        items: Optional[List[ImageItem]] = None,
        arw_count: Optional[int] = None
    ):
        if tab.get("_released") or tab not in self.tabs:
            return
        if arw_count is not None:
            tab["arw_count"] = arw_count
        session = tab.get("session")
        if items is None:
            if not session:
                return
            items = getattr(session, "placeholder_items", None) or getattr(session, "items", None)
            if not items:
                return

        placeholder_items = []
        for item in items:
            pi = ImageItem(item.path)
            pi.filename = item.filename or item.path.name
            pi.format_name = getattr(item, "format_name", item.path.suffix.upper().lstrip("."))
            pi.flag = item.flag
            pi.rating = item.rating
            pi.is_stacked = False
            pi.stacked_paths = [item.path]
            pi.is_placeholder = True
            placeholder_items.append(pi)

        sel_idx = 0
        pending_target = tab.get("pending_target_image")
        if pending_target:
            f_idx, _ = find_item_index_by_path(placeholder_items, pending_target)
            if f_idx >= 0:
                sel_idx = f_idx

        tab["current_items"] = placeholder_items
        tab["current_index"] = sel_idx
        if tab is self._get_active_tab():
            self.current_items = placeholder_items
            self.current_index = sel_idx
            if session:
                self.thumb_list.set_image_loader(session.image_loader)
            self.thumb_list.update_items(placeholder_items, selected_idx=sel_idx, white_balance=white_balance)
            if placeholder_items and 0 <= sel_idx < len(placeholder_items):
                self._select_image(sel_idx, from_click=False)
            self._sync_loading_progress()

    def _on_tab_scan_complete(self, tab: Dict[str, Any]):
        # A tab closed mid-scan: the worker thread still holds a reference to its dict.
        if tab.get("_released") or tab not in self.tabs:
            return
        self._update_tab_loading_indicator(tab)
        self._watch_tab_directory(tab)

        session = tab["session"]
        self._apply_tab_filter_values(tab)

        if tab is not self._get_active_tab():
            return

        self.thumb_list.finish_folder_timing()
        self.thumb_list.set_image_loader(session.image_loader)
        self._on_filter_changed()
        stats = tab["session"].get_summary_stats()
        arw_count = stats.get("arw_count", tab.get("arw_count", 0))
        arw_info = f" ({arw_count} ARW)" if arw_count > 0 else ""
        if stats['total_images'] == 0:
            folder_str = str(tab["session"].directory) if tab["session"].directory else "selected directory"
            self._update_status(f"No supported photo files found in {folder_str}.")
        else:
            self._update_status(
                f"Loaded {stats['total_images']} photos{arw_info} ({stats['total_size_mb']} MB) | "
                f"Picked: {stats['picked']}, Rejected: {stats['rejected']}, Unflagged: {stats['unflagged']}"
            )

    def _on_tab_scan_error(self, tab: Dict[str, Any], err: Exception):
        if tab.get("_released") or tab not in self.tabs:
            return
        self._update_tab_loading_indicator(tab)
        self.thumb_list.finish_load_timing()
        self._update_status("Error loading directory.")

    def _sync_loading_progress(self):
        """Reflect scan progress in the status bar."""
        tab = self._get_active_tab()
        if not tab or not tab.get("loading"):
            return

        total = tab.get("load_total", 0)
        current = tab.get("load_current", 0)
        arw_count = tab.get("arw_count", 0)
        arw_text = f" ({arw_count} ARW)" if arw_count > 0 else ""

        if total > 0:
            if current == 0:
                self._update_status(f"Found {arw_count} ARW ({total} photos) | Reading metadata..." if arw_count > 0 else f"Found {total} photos | Reading metadata...")
            else:
                self._update_status(f"Loading metadata {current}/{total}{arw_text}...")
        else:
            self._update_status("Scanning directory...")

    def _update_tab_loading_indicator(self, tab: Dict[str, Any]):
        idx = self.tabs.index(tab) if tab in self.tabs else -1
        if idx < 0:
            return
        base_label = tab.get("tab_label", "")
        if not base_label:
            return
        if tab.get("loading") and not base_label.endswith(" ⟳"):
            tab["tab_label"] = base_label + " ⟳"
        elif not tab.get("loading") and base_label.endswith(" ⟳"):
            tab["tab_label"] = base_label[:-2]
        self.tab_bar.set_label(idx, tab["tab_label"])

    def _apply_tab_filter_values(self, tab: Dict[str, Any]):
        session = tab["session"]
        if not session or not session.items:
            tab["current_items"] = []
            tab["current_index"] = -1
            return

        filter_vals = tab.get("filter_values", {})

        rating_selected = filter_vals.get("rating", [])
        rating_filter_set = None
        if rating_selected:
            rating_filter_set = set()
            for r_str in rating_selected:
                if r_str == "Unrated":
                    rating_filter_set.add(0)
                else:
                    try:
                        rating_filter_set.add(int(r_str.replace("★", "").strip()))
                    except Exception:
                        pass

        fmt_val = filter_vals.get("format", "All Formats")
        if ".ARW" in fmt_val.upper() or "ARW" in fmt_val.upper():
            fmt_val = ".ARW"
        elif ".JPG" in fmt_val.upper() or "JPG" in fmt_val.upper():
            fmt_val = ".JPG"
        elif ".PNG" in fmt_val.upper() or "PNG" in fmt_val.upper():
            fmt_val = ".PNG"
        elif ".HEIC" in fmt_val.upper() or "HEIC" in fmt_val.upper():
            fmt_val = ".HEIC"
        else:
            fmt_val = "All"

        tag_selected = filter_vals.get("tag", [])
        tag_filter = tag_selected if tag_selected else None

        tab["current_items"] = session.get_filtered_items(
            flag_filter=filter_vals.get("flag", "All"),
            rating_filter=rating_filter_set,
            format_filter=fmt_val,
            tag_filter=tag_filter
        )
        if tab.get("pending_target_image") and tab["current_items"]:
            f_idx, _ = find_item_index_by_path(tab["current_items"], tab["pending_target_image"])
            tab["current_index"] = f_idx if f_idx >= 0 else 0
        else:
            tab["current_index"] = 0 if tab["current_items"] else -1

    def _persist_tabs_state(self):
        self._save_active_tab_state()
        payload = []
        for t in self.tabs:
            payload.append({
                "directory": t["directory"],
                "tab_label": t["tab_label"],
                "filter_values": t.get("filter_values", {}),
            })
        self.db.save_open_tabs(payload, self.active_tab_index)

    def _on_tab_selected(self, index: int):
        self._switch_tab(index)

    def _on_tab_closed(self, index: int):
        self._close_tab(index)

    def _on_tab_reordered(self, from_idx: int, to_idx: int):
        self._save_active_tab_state()
        self.tab_bar.reorder(from_idx, to_idx)
        tab = self.tabs.pop(from_idx)
        self.tabs.insert(to_idx, tab)
        if self.active_tab_index == from_idx:
            self.active_tab_index = to_idx
        elif from_idx < to_idx and self.active_tab_index > from_idx and self.active_tab_index <= to_idx:
            self.active_tab_index -= 1
        elif from_idx > to_idx and self.active_tab_index >= to_idx and self.active_tab_index < from_idx:
            self.active_tab_index += 1
        self._persist_tabs_state()

    def _on_new_tab(self):
        folder = fd.askdirectory(title="Select Photo Directory to Cull")
        if folder:
            self._add_tab(folder)

    def _on_about_clicked(self):
        if getattr(self, "_about_dialog", None) is not None:
            try:
                if self._about_dialog.winfo_exists():
                    self._about_dialog.lift()
                    self._about_dialog.focus_force()
                    return
            except Exception:
                pass
            self._about_dialog = None

        import culler
        dialog = AboutDialog(self, app_name="Quick Cull", version=getattr(culler, "__version__", ""))
        self._about_dialog = dialog

    def _create_components(self):
        init_scale = self.db.get_raw_scale()
        init_wb = self.db.get_white_balance()

        # Restore UI Layout State (Panel visibility, panel widths, menubar visibility)
        self._thumbnail_panel_visible, self._tool_panel_visible = self.db.get_ui_panels_visible()
        self._thumbnail_panel_width, self._tool_panel_width = self.db.get_ui_panels_width()
        self._menubar_visible = self.db.get_ui_menubar_visible()

        # Native Menu Bar
        self._create_menubar()
        if not self._menubar_visible:
            self.config(menu="")

        # Tab Bar
        self.tab_bar = TabBar(
            self,
            on_tab_selected=self._on_tab_selected,
            on_tab_closed=self._on_tab_closed,
            on_tab_reordered=self._on_tab_reordered,
            on_new_tab=self._on_new_tab,
            on_close_all=self._close_all_tabs,
            on_about=self._on_about_clicked
        )
        self.tab_bar.pack(side="top", fill="x", padx=0, pady=0)
        self._about_dialog: Optional[AboutDialog] = None

        # Top Header Toolbar
        self.toolbar = HeaderToolbar(
            self,
            on_open_explorer=self._on_open_explorer,
            on_refresh=self._on_refresh_directory,
            on_filter_change=self._on_filter_changed,
            on_raw_settings_change=self._on_raw_settings_changed,
            on_load_100_percent=self._on_load_100_percent,
            on_scan_blur=self._on_scan_blur,
            on_scan_duplicates=self._on_scan_duplicates,
            on_open_settings=self._on_open_settings,
            on_toggle_thumbs=self.toggle_thumbnail_panel,
            on_toggle_tools=self.toggle_tool_panel,
            initial_raw_scale=init_scale,
            initial_wb=init_wb
        )
        self.toolbar.pack(side="top", fill="x", padx=5, pady=5)

        # Detects photos added/removed/edited on disk and reloads the owning tab
        self.folder_watcher = FolderWatcher()
        self.folder_watcher.start()

        # Shared decode services: every tab's session uses these, so one photo is
        # decoded once and switching tabs keeps the decoded images of the other tabs.
        self.exif_wrapper = ExifToolWrapper()
        self.image_loader = ImageLoader(exif_wrapper=self.exif_wrapper)

        # Main Container
        self.main_container = ctk.CTkFrame(self, corner_radius=0)
        self.main_container.pack(side="top", fill="both", expand=True, padx=5, pady=2)

        # Left Thumbnail List
        self.thumb_list = ThumbnailList(
            self.main_container,
            width=self._thumbnail_panel_width,
            on_select_image=self._select_image,
            on_select_all=self._select_all,
            on_select_none=self._select_none,
            on_load_stats_changed=self._on_load_stats_changed
        )
        if self._thumbnail_panel_visible:
            self.thumb_list.pack(side="left", fill="y", padx=3, pady=3)

        # Center Canvas Viewer
        self.viewer = ImageCanvasViewer(self.main_container)
        self.viewer.pack(side="left", fill="both", expand=True, padx=3, pady=3)

        # Right Metadata & Action Panel
        init_picked_folder = self.db.get_picked_folder()
        init_rejected_folder = self.db.get_rejected_folder()

        self.meta_panel = MetadataPanel(
            self.main_container,
            width=self._tool_panel_width,
            on_set_flag=self._set_current_flag,
            on_set_rating=self._set_current_rating,
            on_toggle_tag=self._on_toggle_tag,
            on_unflag_all=self._on_unflag_all,
            on_untag_all=self._on_untag_all,
            on_unrate_all=self._on_unrate_all,
            on_clear_all=self._on_clear_all,
            on_crop=self._on_trigger_crop,
            on_annotate=self._on_trigger_annotate,
            on_convert_jpg=self._on_convert_jpg,
            on_move_picked=self._on_move_picked,
            on_move_rejected=self._on_move_rejected,
            on_trash_rejected=self._on_delete_all_rejected_to_trash,
            on_config_output_folders=self._on_config_output_folders,
            initial_picked_folder=init_picked_folder,
            initial_rejected_folder=init_rejected_folder,
            bag_order=self._load_meta_panel_bag_order(),
            on_bag_order_changed=self._save_meta_panel_bag_order,
            collapsed_states=self._load_meta_panel_bag_collapsed(),
            on_bag_collapse_changed=self._save_meta_panel_bag_collapsed
        )
        if self._tool_panel_visible:
            self.meta_panel.pack(side="right", fill="y", padx=3, pady=3, before=self.viewer)
        self.meta_panel.refresh_tag_buttons(self.db.get_custom_tags())

        self._sync_panel_toggle_buttons()

        # Status Bar
        self.status_bar = ctk.CTkFrame(self, height=30, corner_radius=0)
        self.status_bar.pack(side="bottom", fill="x")

        self.lbl_status = ctk.CTkLabel(
            self.status_bar, text="Ready. Open a directory to begin culling.", anchor="w"
        )
        self.lbl_status.pack(side="left", padx=10, pady=4)

        # Right Side Background Prefetch Indicator Box
        self.prefetch_frame = ctk.CTkFrame(self.status_bar, fg_color="transparent")
        self.prefetch_frame.pack(side="right", padx=10, pady=2)

        self.lbl_prefetch = ctk.CTkLabel(
            self.prefetch_frame,
            text="",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#2b9348"
        )
        self.lbl_prefetch.pack(side="left", padx=(0, 6))

        self.prefetch_bar = ctk.CTkProgressBar(
            self.prefetch_frame,
            width=110,
            height=12,
            progress_color="#2b9348"
        )
        self.prefetch_bar.pack(side="left")
        self.prefetch_bar.set(1.0)

        self._setup_drag_drop()

    def _bind_events(self):
        # Navigation & Multi-Selection Bindings
        # Single Step (+/- 1)
        self.bind("<Right>", lambda e: self._navigate(1, is_shift=False))
        self.bind("<Left>", lambda e: self._navigate(-1, is_shift=False))
        self.bind("<Down>", lambda e: self._navigate(1, is_shift=False))
        self.bind("<Up>", lambda e: self._navigate(-1, is_shift=False))

        # 100% Full Resolution Shortcut
        self.bind("a", lambda e: self._on_load_100_percent())
        self.bind("A", lambda e: self._on_load_100_percent())

        # Shift + Navigation (Multi-select range +/- 1)
        self.bind("<Shift-Right>", lambda e: self._navigate(1, is_shift=True))
        self.bind("<Shift-Left>", lambda e: self._navigate(-1, is_shift=True))
        self.bind("<Shift-Down>", lambda e: self._navigate(1, is_shift=True))
        self.bind("<Shift-Up>", lambda e: self._navigate(-1, is_shift=True))

        # Ctrl + Navigation (+/- 10 photos)
        self.bind("<Control-Right>", lambda e: self._navigate(10, is_shift=False))
        self.bind("<Control-Left>", lambda e: self._navigate(-10, is_shift=False))
        self.bind("<Control-Down>", lambda e: self._navigate(10, is_shift=False))
        self.bind("<Control-Up>", lambda e: self._navigate(-10, is_shift=False))
        self.bind("<Prior>", lambda e: self._navigate(-10, is_shift=False))
        self.bind("<Next>", lambda e: self._navigate(10, is_shift=False))

        # Shift + Ctrl + Navigation (Multi-select range +/- 10 photos)
        self.bind("<Shift-Control-Right>", lambda e: self._navigate(10, is_shift=True))
        self.bind("<Shift-Control-Left>", lambda e: self._navigate(-10, is_shift=True))
        self.bind("<Shift-Control-Down>", lambda e: self._navigate(10, is_shift=True))
        self.bind("<Shift-Control-Up>", lambda e: self._navigate(-10, is_shift=True))

        # Home & End Jump
        self.bind("<Home>", lambda e: self._navigate_first(is_shift=False))
        self.bind("<End>", lambda e: self._navigate_last(is_shift=False))
        self.bind("<Shift-Home>", lambda e: self._navigate_first(is_shift=True))
        self.bind("<Shift-End>", lambda e: self._navigate_last(is_shift=True))

        # Trash / Delete Shortcuts
        self.bind("<Delete>", lambda e: self._on_delete_selected_to_trash())
        self.bind("d", lambda e: self._on_d_key_pressed())
        self.bind("D", lambda e: self._on_delete_all_rejected_to_trash() if (getattr(e, "state", 0) & 0x0001) else self._on_d_key_pressed())
        self.bind("<Shift-Key-D>", lambda e: self._on_delete_all_rejected_to_trash())
        self.bind("<Shift-Key-d>", lambda e: self._on_delete_all_rejected_to_trash())

        # Culling Flags & Ratings
        self.bind("p", lambda e: self._set_current_flag(FlagState.PICK))
        self.bind("P", lambda e: self._on_unpick_current() if (getattr(e, "state", 0) & 0x0001) else self._set_current_flag(FlagState.PICK))
        self.bind("<Shift-Key-P>", lambda e: self._on_unpick_current())
        self.bind("<Shift-Key-p>", lambda e: self._on_unpick_current())

        self.bind("x", lambda e: self._set_current_flag(FlagState.REJECT))
        self.bind("X", lambda e: self._on_unreject_current() if (getattr(e, "state", 0) & 0x0001) else self._set_current_flag(FlagState.REJECT))
        self.bind("<Shift-Key-X>", lambda e: self._on_unreject_current())
        self.bind("<Shift-Key-x>", lambda e: self._on_unreject_current())

        self.bind("u", lambda e: self._set_current_flag(FlagState.UNFLAGGED))
        self.bind("U", lambda e: self._set_current_flag(FlagState.UNFLAGGED))

        self.bind("c", lambda e: self._on_trigger_crop())
        self.bind("C", lambda e: self._on_trigger_crop())
        self.bind("<Control-c>", lambda e: self._on_copy_image_to_clipboard())
        self.bind("<Control-C>", lambda e: self._on_copy_image_to_clipboard())
        self.bind("<Control-s>", lambda e: self._on_save_as())
        self.bind("<Control-S>", lambda e: self._on_save_as())
        self.bind("<Return>", lambda e: self._on_return_pressed())
        self.bind("<KP_Enter>", lambda e: self._on_return_pressed())
        self.bind("<Escape>", lambda e: self._on_escape_pressed())

        self.bind("b", lambda e: self._on_trigger_annotate())
        self.bind("B", lambda e: self._on_trigger_annotate())

        for star in range(6):
            self.bind(str(star), lambda e, s=star: self._set_current_rating(s))

        # Panel & Menubar Toggles & Tab Navigation
        self.bind("<F8>", lambda e: self.toggle_thumbnail_panel())
        self.bind("<Control-b>", lambda e: self.toggle_thumbnail_panel())
        self.bind("<Control-B>", lambda e: self.toggle_thumbnail_panel())
        self.bind("<F9>", lambda e: self.toggle_tool_panel())
        self.bind("<Control-j>", lambda e: self.toggle_tool_panel())
        self.bind("<Control-J>", lambda e: self.toggle_tool_panel())
        self.bind("<Tab>", self._on_tab_key)
        self.bind("<Alt-m>", lambda e: self.toggle_menubar())
        self.bind("<Alt-M>", lambda e: self.toggle_menubar())
        self.bind("<F5>", lambda e: self._on_refresh_directory())
        self.bind("<Control-o>", lambda e: self._on_new_tab())
        self.bind("<Control-O>", lambda e: self._on_new_tab())
        self.bind("<Control-w>", lambda e: self._on_close_active_tab())
        self.bind("<Control-W>", lambda e: self._on_close_active_tab())

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        try:
            self.folder_watcher.stop()
        except Exception:
            pass

        for pool_name in ("_load_pool", "_prefetch_pool"):
            pool = getattr(self, pool_name, None)
            if pool is None:
                continue
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

        try:
            import glob
            import os
            for f in glob.glob("yolo*.pt"):
                try:
                    os.remove(f)
                except Exception:
                    pass
        except Exception:
            pass
            
        try:
            is_max = (self.state() == "zoomed")
            w = self.winfo_width()
            h = self.winfo_height()
            x = self.winfo_x()
            y = self.winfo_y()
            self.db.save_window_geometry(w, h, x, y, is_max)
        except Exception:
            pass

        try:
            self._save_ui_state()
        except Exception:
            pass

        try:
            if hasattr(self, "thumb_list"):
                self.thumb_list.shutdown()
        except Exception:
            pass

        try:
            self._persist_tabs_state()
            for tab in self.tabs:
                tab["session"].image_loader.clear_cache()
        except Exception:
            pass

        try:
            self.destroy()
        except Exception:
            pass

        os._exit(0)

    def _update_status(self, text: str):
        self.lbl_status.configure(text=text)

    def _on_refresh_directory(self):
        tab = self._get_active_tab()
        if tab and tab.get("session") and tab["session"].directory and tab["session"].directory.exists():
            self._load_directory(str(tab["session"].directory))
        else:
            mb.showinfo("Refresh Directory", "No active photo folder opened.")

    def _load_directory(self, folder_path: str, target_image: Optional[Path] = None):
        tab = self._get_active_tab()
        if not tab:
            mb.showinfo("No Tab", "Open a directory in a tab first.")
            return

        self.db.set_setting("last_directory", str(folder_path))
        self._unwatch_tab_directory(tab, folder_path)
        tab["directory"] = str(Path(folder_path).resolve())
        tab["tab_label"] = os.path.basename(folder_path) or folder_path
        tab["is_loaded"] = False
        tab["current_items"] = []
        tab["current_index"] = -1
        tab["selected_indices"] = set()
        tab["filter_values"] = {
            "flag": "All",
            "rating": [],
            "format": "All Formats",
            "tag": []
        }
        tab["loading"] = True
        tab["load_total"] = 0
        tab["load_current"] = 0
        tab["arw_count"] = 0
        tab["_placeholders_loaded"] = False
        tab["pending_target_image"] = target_image
        tab["_load_started_at"] = time.monotonic()

        self.current_items = []
        self.current_index = -1
        self.selected_indices = set()
        self.thumb_list.update_items([])
        self.viewer.clear()
        self.meta_panel.clear()

        self.tab_bar.set_label(self.active_tab_index, tab["tab_label"] + " ⟳")
        self._update_status(f"Scanning directory: {folder_path}...")
        self.thumb_list.start_load_timing(tab["_load_started_at"])

        white_balance = self.toolbar.get_white_balance()

        def on_discovered(items: List[ImageItem], arw_count: int):
            tab["arw_count"] = arw_count
            tab["load_total"] = len(items)
            if not tab.get("_released"):
                if not tab.get("_placeholders_loaded"):
                    tab["_placeholders_loaded"] = True
                    self.after(0, lambda: self._preload_placeholder_items(tab, white_balance, items=items, arw_count=arw_count))
                elif tab is self._get_active_tab():
                    self.after(0, self._sync_loading_progress)

        def on_progress(current: int, total: int, filename: str = ""):
            tab["load_current"] = current
            tab["load_total"] = total
            if tab is self._get_active_tab() and not tab.get("_released"):
                if current == 0 and total > 0 and not tab.get("_placeholders_loaded"):
                    tab["_placeholders_loaded"] = True
                    self.after(50, lambda: self._preload_placeholder_items(tab, white_balance))
                self.after(0, self._sync_loading_progress)

        def worker():
            started_at = time.monotonic()
            try:
                tab["session"].scan_directory(
                    folder_path,
                    stack_raw_jpg=True,
                    progress_callback=on_progress,
                    on_discovered=on_discovered,
                )
                tab["load_stats"]["folder"] = time.monotonic() - started_at
                tab["is_loaded"] = True
                tab["loading"] = False
                self.after(0, lambda: self._on_scan_complete(tab))
            except Exception as e:
                tab["load_stats"]["folder"] = time.monotonic() - started_at
                tab["loading"] = False
                self.after(0, lambda err=e: self._on_scan_error(tab, err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_scan_complete(self, tab: Dict[str, Any]):
        self._update_tab_loading_indicator(tab)
        self._watch_tab_directory(tab)

        if tab is not self._get_active_tab():
            return

        self.thumb_list.finish_folder_timing()

        self._on_filter_changed(trigger_source="operation")
        stats = tab["session"].get_summary_stats()
        arw_count = stats.get("arw_count", tab.get("arw_count", 0))
        arw_info = f" ({arw_count} ARW)" if arw_count > 0 else ""
        if stats['total_images'] == 0:
            folder_str = str(tab["session"].directory) if tab["session"].directory else "selected directory"
            mb.showwarning(
                "No Photos Found",
                f"No supported photo files (.ARW, .JPG, .PNG, .HEIC, .CR2, .NEF, etc.) were found in:\n\n{folder_str}"
            )
            self._update_status(f"No supported photo files found in {folder_str}.")
        else:
            self._update_status(
                f"Loaded {stats['total_images']} photos{arw_info} ({stats['total_size_mb']} MB) | "
                f"Picked: {stats['picked']}, Rejected: {stats['rejected']}, Unflagged: {stats['unflagged']}"
            )

    def _on_scan_error(self, tab: Dict[str, Any], err: Exception):
        self._update_tab_loading_indicator(tab)
        self.thumb_list.finish_load_timing()
        self._update_status("Error loading directory.")

    def _on_filter_changed(self, trigger_source: str = "filter"):
        tab = self._get_active_tab()
        session = self._get_active_session()
        if not tab or not session:
            return

        filter_vals = self.toolbar.get_filter_values()

        rating_selected = filter_vals["rating"]
        rating_filter_set = None
        if rating_selected:
            rating_filter_set = set()
            for r_str in rating_selected:
                if r_str == "Unrated":
                    rating_filter_set.add(0)
                else:
                    try:
                        rating_filter_set.add(int(r_str.replace("★", "").strip()))
                    except Exception:
                        pass

        fmt_val = filter_vals["format"]
        if ".ARW" in fmt_val.upper() or "ARW" in fmt_val.upper(): fmt_val = ".ARW"
        elif ".JPG" in fmt_val.upper() or "JPG" in fmt_val.upper(): fmt_val = ".JPG"
        elif ".PNG" in fmt_val.upper() or "PNG" in fmt_val.upper(): fmt_val = ".PNG"
        elif ".HEIC" in fmt_val.upper() or "HEIC" in fmt_val.upper(): fmt_val = ".HEIC"
        else: fmt_val = "All"

        tag_selected = filter_vals.get("tag", [])
        tag_filter = tag_selected if tag_selected else None

        prev_selected_path = None
        if self.current_items and 0 <= self.current_index < len(self.current_items):
            prev_selected_path = self.current_items[self.current_index].path

        self.current_items = session.get_filtered_items(
            flag_filter=filter_vals["flag"],
            rating_filter=rating_filter_set,
            format_filter=fmt_val,
            tag_filter=tag_filter
        )
        tab["current_items"] = self.current_items

        # When an operation (Delete, Move, scan, reload, etc.) makes the thumbnail list empty,
        # automatically switch to All filter so remaining photos are displayed.
        if not self.current_items and trigger_source != "filter":
            current_flag = self.toolbar.seg_filter.get()
            if current_flag != "All":
                log_info(f"Operation ({trigger_source}) made '{current_flag}' filter empty -> automatically moving to 'All' filter")
                self.toolbar.seg_filter.set("All")
                filter_vals["flag"] = "All"
                tab["filter_values"]["flag"] = "All"
                self.current_items = session.get_filtered_items(
                    flag_filter="All",
                    rating_filter=rating_filter_set,
                    format_filter=fmt_val,
                    tag_filter=tag_filter
                )
                tab["current_items"] = self.current_items

            if not self.current_items and session.items and (rating_filter_set or fmt_val != "All" or tag_filter):
                self.toolbar.rating_filter.reset()
                self.toolbar.tag_filter.reset()
                self.toolbar.opt_format.set("All Formats")
                tab["filter_values"] = {
                    "flag": "All",
                    "rating": [],
                    "format": "All Formats",
                    "tag": []
                }
                rating_filter_set = None
                fmt_val = "All"
                tag_filter = None
                self.current_items = session.get_filtered_items(
                    flag_filter="All",
                    rating_filter=None,
                    format_filter="All",
                    tag_filter=None
                )
                tab["current_items"] = self.current_items

        target_idx = 0
        target_sub_path = None
        pending_target = tab.get("pending_target_image")
        if pending_target:
            tab["pending_target_image"] = None
            found_idx, matched_sub = find_item_index_by_path(self.current_items, pending_target)
            if found_idx >= 0:
                target_idx = found_idx
                target_sub_path = matched_sub
        elif prev_selected_path and self.current_items:
            for idx, item in enumerate(self.current_items):
                if item.path == prev_selected_path or prev_selected_path in item.stacked_paths:
                    target_idx = idx
                    break

        log_info(f"_on_filter_changed: filter='{filter_vals['flag']}', rating={rating_filter_set}, format='{fmt_val}', tag='{tag_filter}' -> {len(self.current_items)} items matched")

        white_balance = self.toolbar.get_white_balance()

        self.thumb_list.update_items(
            self.current_items,
            selected_idx=target_idx,
            white_balance=white_balance
        )

        if self.current_items:
            self._select_image(target_idx, target_path=target_sub_path, from_click=False)
        else:
            self.selected_indices = set()
            self.viewer.clear()
            self.meta_panel.clear()
            self._update_status("No photos match current filter criteria.")

    def _select_all(self):
        if not self.current_items:
            return
        self.selected_indices = set(range(len(self.current_items)))
        cur_idx = self.current_index if 0 <= self.current_index < len(self.current_items) else 0
        cur_item = self.current_items[cur_idx]
        self.thumb_list.set_selected_indices(self.selected_indices, cur_idx, active_path=cur_item.path)
        self._update_status(f"Selected all {len(self.current_items)} photos.")

    def _select_none(self):
        if not self.current_items:
            return
        cur_idx = self.current_index if 0 <= self.current_index < len(self.current_items) else 0
        self.selected_indices = {cur_idx}
        self.selection_anchor_idx = cur_idx
        cur_item = self.current_items[cur_idx]
        self.thumb_list.set_selected_indices(self.selected_indices, cur_idx, active_path=cur_item.path)
        self._update_status(f"Selection cleared to active photo ({cur_idx + 1}/{len(self.current_items)}).")

    def _select_image(self, index: int, target_path: Optional[Path] = None, is_continuous: bool = False, is_ctrl: bool = False, is_shift: bool = False, from_click: bool = False):
        tab = self._get_active_tab()
        session = self._get_active_session()
        if not tab or not session or not self.current_items:
            return

        if not (0 <= index < len(self.current_items)):
            return

        self.current_index = index
        tab["current_index"] = index
        item = self.current_items[index]
        load_path = target_path or item.path

        if is_ctrl:
            if index in self.selected_indices and len(self.selected_indices) > 1:
                self.selected_indices.remove(index)
            else:
                self.selected_indices.add(index)
            self.selection_anchor_idx = index
            tab["selected_indices"] = set(self.selected_indices)
        elif is_shift:
            anchor = getattr(self, "selection_anchor_idx", index)
            start_i, end_i = min(anchor, index), max(anchor, index)
            self.selected_indices = set(range(start_i, end_i + 1))
            tab["selected_indices"] = set(self.selected_indices)
        else:
            self.selected_indices = {index}
            self.selection_anchor_idx = index
            tab["selected_indices"] = {index}

        tab["selection_anchor_idx"] = self.selection_anchor_idx
        self._load_request_id += 1
        req_id = self._load_request_id

        display_name = load_path.name if load_path else item.filename
        sel_info = f" [{len(self.selected_indices)} selected]" if len(self.selected_indices) > 1 else ""
        self._update_status(f"Displaying: {display_name} ({index + 1}/{len(self.current_items)}) [{item.format_name}]{sel_info}")

        cached_thumb = session.image_loader.get_cached_thumbnail(load_path)
        if cached_thumb:
            self.viewer.set_image(cached_thumb, preserve_zoom=True)

        # Update thumbnail list immediately so selection highlight moves instantaneously
        self.thumb_list.set_selected_indices(
            self.selected_indices,
            index,
            active_path=load_path,
            auto_scroll=(not from_click)
        )

        def do_meta_update():
            self._meta_timer = None
            cur_idx = self.current_index
            if 0 <= cur_idx < len(self.current_items):
                self.meta_panel.update_metadata(self.current_items[cur_idx])

        if hasattr(self, "_meta_timer") and self._meta_timer is not None:
            try:
                self.after_cancel(self._meta_timer)
            except Exception:
                pass
            self._meta_timer = None

        if is_continuous:
            self._meta_timer = self.after(80, do_meta_update)
        else:
            self.meta_panel.update_metadata(item)

        raw_scale = self.toolbar.get_raw_scale()
        white_balance = self.toolbar.get_white_balance()

        cached_img = session.image_loader.get_cached_full_image(load_path, raw_scale, white_balance)
        if cached_img:
            self._on_image_loaded(item, cached_img, full_res=False, active_path=load_path, req_id=req_id)
            self._prefetch_surrounding_images(index, is_continuous=is_continuous)
            return

        def start_background_load():
            if req_id != self._load_request_id:
                return

            def load_worker():
                if req_id != self._load_request_id:
                    return

                if not cached_thumb:
                    fast_thumb = session.image_loader.get_thumbnail(
                        load_path,
                        max_size=(400, 400),
                        raw_scale=0.10,
                        white_balance=white_balance
                    )
                    if fast_thumb:
                        # Re-check inside the callback: a newer navigation may have
                        # started between scheduling and running, and a late preview of
                        # the previous photo would otherwise overwrite the current one.
                        self.after(0, lambda img=fast_thumb: self._apply_fast_preview(img, req_id))

                if req_id != self._load_request_id:
                    return

                pil_img = session.image_loader.load_full_image(
                    load_path,
                    raw_scale=raw_scale,
                    white_balance=white_balance
                )
                if req_id == self._load_request_id:
                    self.after(0, lambda: self._on_image_loaded(item, pil_img, full_res=False, active_path=load_path, req_id=req_id))
                    self._prefetch_surrounding_images(index, is_continuous=is_continuous)

            # A bounded pool, not a thread per navigation: holding the arrow key down
            # used to start a new OS thread for every photo, all of which stayed alive
            # until their decode finished. Two workers keep the current photo moving and
            # let a superseded one drain instead of piling up.
            self._load_pool.submit(load_worker)

        if hasattr(self, "_nav_timer") and self._nav_timer is not None:
            try:
                self.after_cancel(self._nav_timer)
            except Exception:
                pass
            self._nav_timer = None

        if is_continuous:
            self._nav_timer = self.after(150, start_background_load)
        else:
            start_background_load()

    def _apply_fast_preview(self, pil_img: Optional[Image.Image], req_id: int) -> bool:
        """Show a fast preview, dropping it if navigation has already moved on.

        Guards the gap between scheduling this on the loading thread and running it on
        the UI thread: without the re-check, a preview of photo N can land after photo
        N+1 is already displayed and stay there until N+1 finishes decoding.
        """
        if req_id != self._load_request_id:
            return False
        self.viewer.set_image(pil_img, preserve_zoom=True)
        return True

    def _prefetch_surrounding_images(self, center_idx: int, is_continuous: bool = False):
        session = self._get_active_session()
        if not session or not self.current_items:
            return

        current_req = self._load_request_id
        raw_scale = self.toolbar.get_raw_scale()
        white_balance = self.toolbar.get_white_balance()

        if hasattr(self, "_prefetch_timer") and self._prefetch_timer is not None:
            try:
                self.after_cancel(self._prefetch_timer)
            except Exception:
                pass
            self._prefetch_timer = None

        def start_prefetch():
            if current_req != self._load_request_id:
                return

            self.after(0, lambda: self._update_prefetch_progress(0.1, is_done=False))

            def prefetch_worker():
                items = self.current_items
                # Only the immediate neighbours, and only their primary path: the
                # stacked variants are reachable by clicking, and prefetching them
                # tripled the decode count of every single arrow-key press.
                targets = [
                    items[i].path
                    for i in (center_idx + 1, center_idx - 1, center_idx + 2)
                    if 0 <= i < len(items)
                ]

                for position, path in enumerate(targets, start=1):
                    if current_req != self._load_request_id:
                        return
                    try:
                        session.image_loader.load_full_image(
                            path,
                            raw_scale=raw_scale,
                            white_balance=white_balance
                        )
                    except Exception:
                        log_debug(f"Prefetch decode failed for {path.name}")
                    if current_req == self._load_request_id:
                        fraction = position / float(len(targets))
                        self.after(0, lambda f=fraction: self._update_prefetch_progress(f, is_done=False))

                if current_req == self._load_request_id:
                    self.after(0, lambda: self._update_prefetch_progress(1.0, is_done=True))

            # One worker: the pool makes the newest request the only one queued, so
            # navigation supersedes prefetch instead of running alongside it.
            self._prefetch_pool.submit(prefetch_worker)

        if is_continuous:
            self._prefetch_timer = self.after(80, start_prefetch)
        else:
            start_prefetch()

    def _update_prefetch_progress(self, fraction: float, is_done: bool):
        self.prefetch_bar.set(fraction)
        if is_done:
            self.prefetch_bar.configure(progress_color="#2b9348")
        else:
            self.prefetch_bar.configure(progress_color="#ffb703")

    def _on_image_loaded(self, item: ImageItem, pil_img: Optional[Image.Image], full_res: bool = False, active_path: Optional[Path] = None, req_id: Optional[int] = None):
        if req_id is not None and req_id != self._load_request_id:
            return
        self.viewer.set_image(pil_img)
        show_boxes = self.db.get_show_bounding_boxes() if self.db else True
        if show_boxes and (getattr(item, 'detection_box', None) or getattr(item, 'eye_box', None) or getattr(item, 'manual_detection_box', None) or getattr(item, 'manual_eye_box', None)):
            self.viewer.set_detection_box(item.detection_box, item.eye_box, item.manual_detection_box, item.manual_eye_box)
        else:
            self.viewer.clear_detection_box()
        self.meta_panel.update_metadata(item)
        res_str = "100% Full Resolution" if full_res else "Preview"
        display_name = active_path.name if active_path else item.filename
        self._update_status(f"Displaying: {display_name} [{item.format_name} - {res_str}]")

    def _set_current_flag(self, flag: FlagState):
        session = self._get_active_session()
        if self.current_index < 0 or not self.current_items or not session:
            return
        target_indices = self.selected_indices if self.selected_indices else {self.current_index}
        for idx in target_indices:
            if 0 <= idx < len(self.current_items):
                item = self.current_items[idx]
                item.flag = flag
                session.save_item_record(item)
                self.thumb_list.update_single_item_status(idx, item)

        cur_item = self.current_items[self.current_index]
        self.meta_panel.update_metadata(cur_item)
        count_str = f" across {len(target_indices)} photos" if len(target_indices) > 1 else ""
        self._update_status(f"Flagged {cur_item.filename} as {flag.value}{count_str}")

    def _on_unreject_current(self):
        session = self._get_active_session()
        if self.current_index < 0 or not self.current_items or not session:
            return
        target_indices = self.selected_indices if self.selected_indices else {self.current_index}
        unrejected_count = 0
        for idx in target_indices:
            if 0 <= idx < len(self.current_items):
                item = self.current_items[idx]
                if item.flag == FlagState.REJECT:
                    item.flag = FlagState.UNFLAGGED
                    session.save_item_record(item)
                    self.thumb_list.update_single_item_status(idx, item)
                    unrejected_count += 1

        cur_item = self.current_items[self.current_index]
        self.meta_panel.update_metadata(cur_item)
        if unrejected_count > 0:
            count_str = f" across {unrejected_count} photos" if len(target_indices) > 1 else ""
            self._update_status(f"Unrejected {cur_item.filename}{count_str}")
        else:
            self._update_status(f"Selected photo(s) are not flagged as REJECT")

    def _on_unpick_current(self):
        session = self._get_active_session()
        if self.current_index < 0 or not self.current_items or not session:
            return
        target_indices = self.selected_indices if self.selected_indices else {self.current_index}
        unpicked_count = 0
        for idx in target_indices:
            if 0 <= idx < len(self.current_items):
                item = self.current_items[idx]
                if item.flag == FlagState.PICK:
                    item.flag = FlagState.UNFLAGGED
                    session.save_item_record(item)
                    self.thumb_list.update_single_item_status(idx, item)
                    unpicked_count += 1

        cur_item = self.current_items[self.current_index]
        self.meta_panel.update_metadata(cur_item)
        if unpicked_count > 0:
            count_str = f" across {unpicked_count} photos" if len(target_indices) > 1 else ""
            self._update_status(f"Unpicked {cur_item.filename}{count_str}")
        else:
            self._update_status(f"Selected photo(s) are not flagged as PICK")

    def _set_current_rating(self, rating: int):
        session = self._get_active_session()
        if self.current_index < 0 or not self.current_items or not session:
            return
        target_indices = self.selected_indices if self.selected_indices else {self.current_index}
        for idx in target_indices:
            if 0 <= idx < len(self.current_items):
                item = self.current_items[idx]
                item.rating = rating
                session.save_item_record(item)
                self.thumb_list.update_single_item_status(idx, item)

        cur_item = self.current_items[self.current_index]
        self.meta_panel.update_metadata(cur_item)
        count_str = f" across {len(target_indices)} photos" if len(target_indices) > 1 else ""
        self._update_status(f"Set rating for {cur_item.filename} to {rating} stars{count_str}")

    def _on_toggle_tag(self, tag_name: str):
        session = self._get_active_session()
        if self.current_index < 0 or not self.current_items or not session:
            return
        target_indices = self.selected_indices if self.selected_indices else {self.current_index}
        for idx in target_indices:
            if 0 <= idx < len(self.current_items):
                item = self.current_items[idx]
                if item.has_tag(tag_name):
                    item.remove_tag(tag_name)
                else:
                    item.add_tag(tag_name)
                session.save_item_record(item)

        cur_item = self.current_items[self.current_index]
        self.meta_panel.update_metadata(cur_item)
        self._update_status(f"Toggled tag '{tag_name}' for {len(target_indices)} photo(s)")

    def _navigate(self, delta: int, is_shift: bool = False):
        if not self.current_items:
            return
        is_cont = (abs(delta) == 1 and not is_shift)
        new_idx = max(0, min(len(self.current_items) - 1, self.current_index + delta))
        if 0 <= new_idx < len(self.current_items):
            self._select_image(new_idx, is_continuous=is_cont, is_shift=is_shift)

    def _navigate_first(self, is_shift: bool = False):
        if not self.current_items:
            return
        self._select_image(0, is_continuous=False, is_shift=is_shift)

    def _navigate_last(self, is_shift: bool = False):
        if not self.current_items:
            return
        self._select_image(len(self.current_items) - 1, is_continuous=False, is_shift=is_shift)

    def _on_d_key_pressed(self):
        """
        Shortcut 'd' / 'D' moves selected photos to Trash!
        """
        self._on_delete_selected_to_trash()

    def _on_delete_selected_to_trash(self):
        if not self.current_items:
            return

        target_indices = sorted(self.selected_indices) if self.selected_indices else [self.current_index]
        target_items = [self.current_items[i] for i in target_indices if 0 <= i < len(self.current_items)]

        if not target_items:
            return

        fmt_filter = self.toolbar.get_format_filter() if hasattr(self, "toolbar") else "All"
        stacked_count = sum(1 for it in target_items if it.is_stacked)

        if fmt_filter and fmt_filter.upper() == "JPG":
            paths_to_delete = []
            for item in target_items:
                for p in item.stacked_paths:
                    if p.suffix.lower() in (".jpg", ".jpeg"):
                        paths_to_delete.append(p)
            if paths_to_delete:
                self._confirm_and_delete_files(target_items, paths_to_delete)
            else:
                mb.showinfo("No JPG Files", "No JPG files found in the selected items.")
        elif fmt_filter and fmt_filter.upper() in ("RAW", "ARW"):
            paths_to_delete = []
            for item in target_items:
                for p in item.stacked_paths:
                    if p.suffix.lower() == ".arw":
                        paths_to_delete.append(p)
            if paths_to_delete:
                self._confirm_and_delete_files(target_items, paths_to_delete)
            else:
                mb.showinfo("No RAW Files", "No RAW files found in the selected items.")
        elif stacked_count > 0:
            dialog = DeleteStackedDialog(self, target_items=target_items)
            if dialog.result is None:
                return
            self._confirm_and_delete_files(target_items, dialog.result)
        else:
            self._confirm_and_delete_files(target_items, [p for item in target_items for p in item.stacked_paths])

    def _confirm_and_delete_files(self, items: List['ImageItem'], paths: List[Path], is_batch_rejected: bool = False):
        session = self._get_active_session()
        if not paths or not session:
            return

        title = "Move Rejected Photos to Trash" if is_batch_rejected else "Move Files to Trash"
        if len(paths) <= 5:
            path_list = "\n".join(f"  • {p.name}" for p in paths)
            msg = f"Move {len(paths)} file(s) to Recycle Bin / Trash?\n\n{path_list}"
        else:
            preview = "\n".join(f"  • {p.name}" for p in paths[:5])
            msg = f"Move {len(paths)} file(s) to Recycle Bin / Trash?\n\n{preview}\n  ... and {len(paths) - 5} more"

        confirm = mb.askyesno(title=title, message=msg, icon="warning")
        if not confirm:
            return

        moved_count = session.move_specific_files_to_trash(items, paths)
        self.folder_watcher.resync(session.directory)
        self.selected_indices = set()
        self._update_status(f"Moved {moved_count} file(s) to Recycle Bin / Trash.")
        self._on_filter_changed(trigger_source="delete")

    def _on_delete_all_rejected_to_trash(self):
        tab = self._get_active_tab()
        session = self._get_active_session()
        if not session or not tab or not session.directory:
            mb.showinfo("Delete Rejected Photos", "No active directory loaded.")
            return

        rejected_items = [it for it in session.items if it.flag == FlagState.REJECT]
        if not rejected_items:
            mb.showinfo("No Rejected Photos", "No photos are flagged as REJECT to move to Recycle Bin / Trash.")
            return

        fmt_filter = self.toolbar.get_format_filter() if hasattr(self, "toolbar") else "All"
        stacked_count = sum(1 for it in rejected_items if it.is_stacked)

        if fmt_filter and fmt_filter.upper() == "JPG":
            paths_to_delete = []
            for item in rejected_items:
                for p in item.stacked_paths:
                    if p.suffix.lower() in (".jpg", ".jpeg"):
                        paths_to_delete.append(p)
            if paths_to_delete:
                self._confirm_and_delete_files(rejected_items, paths_to_delete, is_batch_rejected=True)
            else:
                mb.showinfo("No JPG Files", "No JPG files found in the rejected photos.")
        elif fmt_filter and fmt_filter.upper() in ("RAW", "ARW"):
            paths_to_delete = []
            for item in rejected_items:
                for p in item.stacked_paths:
                    if p.suffix.lower() == ".arw" or ImageLoader.is_raw(p):
                        paths_to_delete.append(p)
            if paths_to_delete:
                self._confirm_and_delete_files(rejected_items, paths_to_delete, is_batch_rejected=True)
            else:
                mb.showinfo("No RAW Files", "No RAW files found in the rejected photos.")
        elif stacked_count > 0:
            dialog = DeleteStackedDialog(self, target_items=rejected_items)
            if dialog.result is None:
                return
            self._confirm_and_delete_files(rejected_items, dialog.result, is_batch_rejected=True)
        else:
            paths_to_delete = [p for item in rejected_items for p in item.stacked_paths]
            self._confirm_and_delete_files(rejected_items, paths_to_delete, is_batch_rejected=True)

    def _on_raw_settings_changed(self):
        scale = self.toolbar.get_raw_scale()
        wb = self.toolbar.get_white_balance()

        self.db.set_raw_scale(scale)
        self.db.set_white_balance(wb)

        self._update_status(f"Updated RAW Settings -> Scale: {int(scale*100)}%, WB: {wb.title()}")
        if 0 <= self.current_index < len(self.current_items):
            self._select_image(self.current_index)

    def _on_load_100_percent(self):
        session = self._get_active_session()
        if self.current_index < 0 or not self.current_items or not session:
            return

        item = self.current_items[self.current_index]
        wb = self.toolbar.get_white_balance()

        self._update_status(f"Loading 100% Full Resolution for {item.filename}...")
        self.viewer.show_loading(f"🔍 Loading 100% Full Resolution: {item.filename}")

        def worker():
            pil_img = session.image_loader.load_full_image(item.path, raw_scale=1.0, white_balance=wb)
            self.after(0, lambda: self._on_100_percent_loaded(item, pil_img))

        threading.Thread(target=worker, daemon=True).start()

    def _on_100_percent_loaded(self, item: ImageItem, pil_img: Optional[Image.Image]):
        self.viewer.hide_loading()
        self._on_image_loaded(item, pil_img, full_res=True)

    def _on_scan_blur(self):
        session = self._get_active_session()
        if not session or not session.items:
            mb.showinfo("Scan for Blur", "No directory loaded to scan.")
            return

        init_method = self.db.get_blur_method()
        init_perc = self.db.get_blur_percentile()
        init_flag = self.db.get_blur_flag_action()
        init_tag = self.db.get_blur_tag_action()
        init_star = self.db.get_blur_rating_action()
        init_subject = self.db.get_blur_subject_detect()
        init_safe_blur = self.db.get_safe_blur_scan()
        init_clear = self.db.get_clear_before_scan()
        init_eye = self.db.get_eye_detection_method()

        BlurScanDialog(
            self,
            on_run=self._run_blur_scan,
            initial_percentile=init_perc,
            initial_method=init_method,
            initial_flag_action=init_flag,
            initial_tag_action=init_tag,
            initial_rating_action=init_star,
            initial_file_type="ARW",
            initial_subject_detect=init_subject,
            initial_safe_blur=init_safe_blur,
            initial_clear_before_scan=init_clear,
            initial_eye_detection_method=init_eye
        )

    def _run_blur_scan(
        self,
        bottom_percentile: float,
        method: str,
        flag_action: str = "Reject",
        tag_action: Optional[str] = "Blur",
        rating_action: Optional[int] = None,
        file_type_filter: str = "ARW",
        subject_detect: bool = False,
        safe_blur: bool = True,
        clear_before_scan: bool = True,
        eye_detection_method: str = "auto"
    ):
        self.db.set_blur_method(method)
        self.db.set_blur_percentile(bottom_percentile)
        self.db.set_blur_flag_action(flag_action)
        self.db.set_blur_tag_action(tag_action or "")
        self.db.set_blur_rating_action(f"{rating_action} Star{'s' if rating_action and rating_action > 1 else ''}" if rating_action is not None else "None")
        self.db.set_blur_subject_detect(subject_detect)
        self.db.set_safe_blur_scan(safe_blur)
        self.db.set_clear_before_scan(clear_before_scan)
        self.db.set_eye_detection_method(eye_detection_method)

        if clear_before_scan:
            session = self._get_active_session()
            if session and session.items:
                session.clear_all_metadata()

        status_msg = f"Scanning directory for blurry photos (Method: {method}, Cutoff: {int(bottom_percentile)}%)"
        if subject_detect:
            status_msg += " + Subject Detection"
        if safe_blur:
            status_msg += " [Safe Mode]"
        self._update_status(status_msg)

        cancel_event = threading.Event()

        prog_dialog = ProgressDialog(
            self,
            title_text="🔍 Scan for Blur Progress",
            header_text=f"🔍 Scanning for Blurry Photos ({method.upper()})...",
            on_cancel=lambda: cancel_event.set()
        )

        def progress_cb(completed: int, total: int, fn: str = ""):
            if not prog_dialog.is_cancelled:
                self.after(0, lambda c=completed, t=total, f=fn: prog_dialog.update_progress(c, t, f))

        def worker():
            session = self._get_active_session()
            if not session:
                return

            dataset_dir = self.dataset_dir
            dataset_yaml = dataset_dir / "dataset.yaml"
            needs_training = dataset_dir / ".needs_training"
            custom_model = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "lib" / "models" / "yolo_custom.pt"
            
            should_train = dataset_yaml.exists() and needs_training.exists()
            if should_train:
                self.after(0, lambda: prog_dialog.set_count_label("Epoch"))
                train_dir = dataset_dir / "images" / "train"
                photo_count = sum(1 for f in train_dir.iterdir() if f.is_file()) if train_dir.exists() else 0
                self.after(0, lambda: prog_dialog.set_training_photo_count(photo_count))
                self.after(0, lambda: prog_dialog.update_progress(0, 100, "Fine-tuning Custom YOLO Model (This may take a few minutes)..."))
                def on_prog(*args):
                    if len(args) == 3:
                        c, t, msg = args
                        self.after(0, lambda c=c, t=t, m=msg: prog_dialog.update_progress(c, t, f"Training: {m}"))
                    elif len(args) == 1:
                        m = args[0]
                        self.after(0, lambda m=m: prog_dialog.update_progress(0, 100, f"Training: {m}"))
                    log_info(f"Training Progress: {args}")
                
                success = train_custom_yolo(
                    dataset_dir=str(dataset_dir),
                    epochs=25,
                    on_progress=on_prog,
                    sync=True
                )
                if success:
                    needs_training.unlink(missing_ok=True)
                    if hasattr(session, "image_loader"):
                        session.image_loader._yolo_model = None

            if cancel_event.is_set():
                return

            self.after(0, lambda: prog_dialog.set_count_label("Photos"))
            self.after(0, lambda: prog_dialog.set_training_photo_count(None))

            flagged = session.scan_for_blur(
                bottom_percentile=bottom_percentile,
                method=method,
                flag_action=flag_action,
                tag_action=tag_action,
                rating_action=rating_action,
                file_type_filter=file_type_filter,
                progress_callback=progress_cb,
                cancel_event=cancel_event,
                subject_detect=subject_detect,
                safe_mode=safe_blur
            )

            if not cancel_event.is_set():
                self.after(0, lambda: self._on_scan_blur_complete(len(flagged), prog_dialog, subject_detect=subject_detect, tag_action=tag_action))

        threading.Thread(target=worker, daemon=True).start()

    def _on_scan_blur_complete(self, count: int, prog_dialog: Optional[ProgressDialog] = None, subject_detect: bool = False, tag_action: Optional[str] = "Blur"):
        if prog_dialog and prog_dialog.winfo_exists():
            try:
                prog_dialog.destroy()
            except Exception:
                pass
        self.toolbar.seg_filter.set("All")
        if tag_action:
            self.toolbar.tag_filter.reset()
            self.toolbar.tag_filter._selected.add(tag_action)
            self.toolbar.tag_filter._update_label()
        self._on_filter_changed()
        if count > 0:
            mb.showinfo("Scan for Blur Complete", f"Identified {count} blurry photos and applied configured actions.")
        else:
            self._update_status("Blur scan complete — no blurry photos found.")

        if subject_detect and self.current_items and 0 <= self.current_index < len(self.current_items):
            cur_item = self.current_items[self.current_index]
            show_boxes = self.db.get_show_bounding_boxes() if self.db else True
            if show_boxes and (cur_item.detection_box or cur_item.eye_box or getattr(cur_item, 'manual_detection_box', None) or getattr(cur_item, 'manual_eye_box', None)):
                self.viewer.set_detection_box(cur_item.detection_box, cur_item.eye_box, cur_item.manual_detection_box, cur_item.manual_eye_box)
            else:
                self.viewer.clear_detection_box()

    def _on_scan_duplicates(self):
        session = self._get_active_session()
        if not session or not session.items:
            mb.showinfo("Scan for Duplicates", "No directory loaded to scan.")
            return

        init_method = self.db.get_duplicate_method()
        init_thresh = self.db.get_duplicate_threshold()
        init_flag = self.db.get_duplicate_flag_action()
        init_tag = self.db.get_duplicate_tag_action()
        init_star = self.db.get_duplicate_rating_action()
        init_clear = self.db.get_clear_before_scan()

        DuplicateScanDialog(
            self,
            on_run=self._run_duplicate_scan,
            initial_threshold=init_thresh,
            initial_method=init_method,
            initial_flag_action=init_flag,
            initial_tag_action=init_tag,
            initial_rating_action=init_star,
            initial_clear_before_scan=init_clear
        )

    def _run_duplicate_scan(
        self,
        threshold: float,
        method: str,
        flag_action: str = "Reject",
        tag_action: Optional[str] = "Duplicate",
        rating_action: Optional[int] = None,
        keeper_flag: str = "Pick",
        keeper_tag: Optional[str] = None,
        keeper_rating: Optional[int] = None,
        keeper_method: str = "sharpest",
        file_type_filter: str = "ARW",
        clear_before_scan: bool = True
    ):
        self.db.set_duplicate_method(method)
        self.db.set_duplicate_threshold(threshold)
        self.db.set_duplicate_flag_action(flag_action)
        self.db.set_duplicate_tag_action(tag_action or "")
        self.db.set_duplicate_rating_action(f"{rating_action} Star{'s' if rating_action and rating_action > 1 else ''}" if rating_action is not None else "None")
        self.db.set_clear_before_scan(clear_before_scan)

        if clear_before_scan:
            session = self._get_active_session()
            if session and session.items:
                session.clear_all_metadata()

        self._update_status(f"Scanning directory for duplicates (Method: {method}, Threshold: {threshold})...")

        cancel_event = threading.Event()

        prog_dialog = ProgressDialog(
            self,
            title_text="👯 Scan for Duplicates Progress",
            header_text=f"👯 Scanning Duplicate Photos ({method.upper()})...",
            on_cancel=lambda: cancel_event.set()
        )

        def progress_cb(completed: int, total: int, fn: str = ""):
            if not prog_dialog.is_cancelled:
                self.after(0, lambda c=completed, t=total, f=fn: prog_dialog.update_progress(c, t, f))

        def worker():
            session = self._get_active_session()
            if not session:
                return
            flagged = session.scan_for_duplicates(
                method=method,
                threshold=threshold,
                flag_action=flag_action,
                tag_action=tag_action,
                rating_action=rating_action,
                keeper_flag=keeper_flag,
                keeper_tag=keeper_tag,
                keeper_rating=keeper_rating,
                keeper_method=keeper_method,
                file_type_filter=file_type_filter,
                progress_callback=progress_cb,
                cancel_event=cancel_event
            )
            if not cancel_event.is_set():
                self.after(0, lambda: self._on_scan_dups_complete(len(flagged), prog_dialog, target_flag_filter=flag_action))

        threading.Thread(target=worker, daemon=True).start()

    def _on_scan_dups_complete(self, count: int, prog_dialog: Optional[ProgressDialog] = None, target_flag_filter: str = "Reject"):
        if prog_dialog and prog_dialog.winfo_exists():
            try:
                prog_dialog.destroy()
            except Exception:
                pass
        if count > 0:
            if target_flag_filter in ["All", "Pick", "Reject", "Unflagged"]:
                self.toolbar.seg_filter.set(target_flag_filter)
            self._on_filter_changed()
            mb.showinfo("Scan for Duplicates Complete", f"Identified {count} duplicate photos and applied configured actions.")
        else:
            self._update_status("Duplicate scan complete — no duplicates found.")

    def _on_unflag_all(self):
        session = self._get_active_session()
        if not session or not session.items:
            return

        ans = mb.askyesno("Unflag All Images", "Are you sure you want to reset all flags to UNFLAGGED?")
        if ans:
            count = session.unflag_all_items()
            self.toolbar.seg_filter.set("All")
            self._on_filter_changed()
            self._update_status(f"Unflagged {count} images across current directory.")

    def _on_untag_all(self):
        session = self._get_active_session()
        if not session or not session.items:
            return

        ans = mb.askyesno("Untag All Images", "Are you sure you want to remove all tags from all images?")
        if ans:
            count = session.untag_all_items()
            self._on_filter_changed()
            self._update_status(f"Removed all tags across {count} images.")

    def _on_unrate_all(self):
        session = self._get_active_session()
        if not session or not session.items:
            return

        ans = mb.askyesno("Remove All Ratings", "Are you sure you want to reset all star ratings to 0?")
        if ans:
            count = session.unrate_all_items()
            self._on_filter_changed()
            self._update_status(f"Reset star ratings to 0 across {count} images.")

    def _on_clear_all(self):
        session = self._get_active_session()
        if not session or not session.items:
            return

        ans = mb.askyesno("Clear All Metadata", "Are you sure you want to reset Flags, Tags, Star Ratings, AND Subject Bounding Boxes for ALL photos?")
        if ans:
            count = session.clear_all_metadata()
            self.toolbar.seg_filter.set("All")
            self.viewer.clear_detection_box()
            self._on_filter_changed()
            self._update_status(f"Cleared flags, tags, ratings, and subject bounding boxes across {count} photos.")

    def _on_trigger_crop(self):
        if self.current_index < 0 or not self.current_items:
            return
        if self.viewer.is_cropping:
            self.viewer.exit_crop_mode()
            self._update_status("Cancelled Manual Crop Mode.")
        else:
            self.viewer.enter_crop_mode(on_confirm_callback=self._on_save_crop)
            self._update_status("Entered Manual Crop Mode (Hold Shift for 1:1 Square, Esc to Cancel).")

    def _on_escape_pressed(self):
        if hasattr(self, "viewer") and self.viewer.is_cropping:
            self.viewer.exit_crop_mode()
            self._update_status("Cancelled Manual Crop Mode.")
        elif hasattr(self, "viewer") and getattr(self.viewer, "is_annotating", False):
            self.viewer.exit_anno_mode()
            self._update_status("Cancelled Annotation Mode.")

    def _on_return_pressed(self):
        if hasattr(self, "viewer") and self.viewer.is_cropping:
            self.viewer._on_confirm_crop()
        elif hasattr(self, "viewer") and getattr(self.viewer, "is_annotating", False):
            self.viewer._on_confirm_anno()

    def _on_trigger_annotate(self):
        if self.current_index < 0 or not self.current_items:
            return
        if getattr(self.viewer, "is_annotating", False):
            self.viewer.exit_anno_mode()
            self._update_status("Cancelled YOLO Annotation Mode.")
        else:
            item = self.current_items[self.current_index]
            self.viewer.enter_anno_mode(str(item.path), on_save_callback=self._on_save_annotate)
            self._update_status("Entered Annotation Mode. Draw boxes and click Save.")

    def _on_save_annotate(self, image_path: str, img_w: int, img_h: int, s_box: tuple, e_box: tuple, pil_image=None):
        dataset_dir = self.dataset_dir
        success = save_annotation(image_path, img_w, img_h, s_box, e_box, dataset_dir=str(dataset_dir), pil_image=pil_image)
        if success:
            (dataset_dir / ".needs_training").touch()
            self._update_status(f"✅ Saved YOLO annotations for {Path(image_path).name} to {dataset_dir}")
            
            # Save the manual boxes back to the DB and display them in the viewer
            if self.current_items and 0 <= self.current_index < len(self.current_items):
                item = self.current_items[self.current_index]
                if str(item.path) == image_path:
                    # s_box and e_box are normalized (x,y,w,h) strings usually?
                    # Wait! In gui.py _on_save_annotate, s_box is a tuple of (x1, y1, x2, y2) normalized?
                    # No, s_box is in original pixel coordinates? No, wait!
                    # What is s_box? In gui.py: _on_save_annotate(image_path, img_w, img_h, s_box, e_box).
                    # save_annotation normalizes them. So s_box is (px_x1, px_y1, px_x2, px_y2).
                    # detection_box in ImageItem expects normalized (nx1, ny1, nx2, ny2).
                    
                    if s_box:
                        x1, y1, x2, y2 = s_box
                        item.manual_detection_box = (x1/img_w, y1/img_h, x2/img_w, y2/img_h)
                    else:
                        item.manual_detection_box = None
                        
                    if e_box:
                        ex1, ey1, ex2, ey2 = e_box
                        item.manual_eye_box = (ex1/img_w, ey1/img_h, ex2/img_w, ey2/img_h)
                    else:
                        item.manual_eye_box = None
                        
                    item.add_tag("Manual-Anno")
                    
                    session = self._get_active_session()
                    if session and self.db:
                        import json
                        self.db.save_image_record(
                            file_path=str(item.path),
                            filename=item.filename,
                            flag=item.flag.value,
                            rating=item.rating,
                            sharpness=item.sharpness_score,
                            tags=json.dumps(list(item.tags)) if item.tags else "",
                            detection_box=item.detection_box,
                            eye_box=item.eye_box
                        )
                    save_manual_annotation(
                        image_path=str(item.path),
                        manual_detection_box=item.manual_detection_box,
                        manual_eye_box=item.manual_eye_box,
                        dataset_dir=str(dataset_dir)
                    )
                        
                    show_boxes = self.db.get_show_bounding_boxes() if self.db else True
                    if show_boxes:
                        self.viewer.set_detection_box(item.detection_box, item.eye_box, item.manual_detection_box, item.manual_eye_box)
                    else:
                        self.viewer.clear_detection_box()
                    self.meta_panel.refresh_tag_buttons(self.db.get_custom_tags() if self.db else [])
                    self.meta_panel.update_metadata(item)
                    
        else:
            mb.showerror("Annotation Error", "Failed to save annotations.")

    def _on_save_crop(self, pct_x1: float, pct_y1: float, pct_x2: float, pct_y2: float):
        session = self._get_active_session()
        if self.current_index < 0 or not self.current_items or not session:
            return

        item = self.current_items[self.current_index]
        default_name = f"{item.path.stem}_cropped.jpg"

        dest = fd.asksaveasfilename(
            title="Save Cropped Image As...",
            initialfile=default_name,
            filetypes=[
                ("JPEG Image (*.jpg;*.jpeg)", "*.jpg;*.jpeg"),
                ("PNG Image (*.png)", "*.png"),
                ("WEBP Image (*.webp)", "*.webp")
            ],
            defaultextension=".jpg"
        )
        if not dest:
            self._update_status("Crop save cancelled.")
            return

        target_path = Path(dest)
        wb = self.toolbar.get_white_balance()

        self._update_status(f"Saving cropped image to {target_path.name}...")
        self.viewer.show_loading(f"✂️ Cropping & Saving Image: {target_path.name}")

        def worker():
            session = self._get_active_session()
            if not session:
                return
            try:
                full_img = session.image_loader.load_full_image(item.path, raw_scale=1.0, white_balance=wb)
                if full_img is None:
                    self.after(0, lambda: self._on_save_error("Failed to load source image for crop."))
                    return

                iw, ih = full_img.size
                src_x1 = max(0, int(iw * pct_x1))
                src_y1 = max(0, int(ih * pct_y1))
                src_x2 = min(iw, int(iw * pct_x2))
                src_y2 = min(ih, int(ih * pct_y2))

                if src_x2 <= src_x1 or src_y2 <= src_y1:
                    src_x1, src_y1, src_x2, src_y2 = 0, 0, iw, ih

                cropped = full_img.crop((src_x1, src_y1, src_x2, src_y2))
                target_path.parent.mkdir(parents=True, exist_ok=True)

                fmt_ext = target_path.suffix.lower()
                save_fmt = "PNG" if fmt_ext == ".png" else ("WEBP" if fmt_ext == ".webp" else "JPEG")

                if save_fmt == "JPEG" and cropped.mode in ("RGBA", "P"):
                    cropped = cropped.convert("RGB")

                cropped.save(target_path, format=save_fmt, quality=95)
                self.after(0, lambda: self._on_save_success(f"Cropped image saved successfully:\n{target_path}"))
            except Exception as e:
                self.after(0, lambda err=str(e): self._on_save_error(f"Error saving cropped image: {err}"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_save_as(self):
        if hasattr(self, "viewer") and self.viewer.is_cropping:
            self.viewer._on_confirm_crop()
            return

        if self.current_index < 0 or not self.current_items:
            mb.showinfo("Save Image As", "No image selected.")
            return

        item = self.current_items[self.current_index]
        default_name = item.path.with_suffix(".jpg").name

        dest = fd.asksaveasfilename(
            title="Save Active Image As...",
            initialfile=default_name,
            filetypes=[
                ("JPEG Image (*.jpg;*.jpeg)", "*.jpg;*.jpeg"),
                ("PNG Image (*.png)", "*.png"),
                ("WEBP Image (*.webp)", "*.webp")
            ],
            defaultextension=".jpg"
        )
        if not dest:
            self._update_status("Save As cancelled.")
            return

        target_path = Path(dest)
        wb = self.toolbar.get_white_balance()

        self._update_status(f"Saving {item.filename} as {target_path.name}...")
        self.viewer.show_loading(f"💾 Saving Image: {target_path.name}")

        def worker():
            session = self._get_active_session()
            if not session:
                return
            try:
                full_img = session.image_loader.load_full_image(item.path, raw_scale=1.0, white_balance=wb)
                if full_img is None:
                    self.after(0, lambda: self._on_save_error("Failed to load source image."))
                    return

                target_path.parent.mkdir(parents=True, exist_ok=True)
                fmt_ext = target_path.suffix.lower()
                save_fmt = "PNG" if fmt_ext == ".png" else ("WEBP" if fmt_ext == ".webp" else "JPEG")

                if save_fmt == "JPEG" and full_img.mode in ("RGBA", "P"):
                    full_img = full_img.convert("RGB")

                full_img.save(target_path, format=save_fmt, quality=95)
                self.after(0, lambda: self._on_save_success(f"Image saved successfully:\n{target_path}"))
            except Exception as e:
                self.after(0, lambda err=str(e): self._on_save_error(f"Error saving image: {err}"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_save_success(self, msg: str):
        self.viewer.hide_loading()
        mb.showinfo("Save Image Success", msg)
        self._update_status("Saved image successfully.")

    def _on_save_error(self, err_msg: str):
        self.viewer.hide_loading()
        mb.showerror("Save Image Error", err_msg)
        self._update_status("Failed to save image.")

    def _on_convert_jpg(self):
        if not self.current_items:
            return

        target_items = [self.current_items[idx] for idx in sorted(self.selected_indices) if 0 <= idx < len(self.current_items)]
        if not target_items and 0 <= self.current_index < len(self.current_items):
            target_items = [self.current_items[self.current_index]]

        if not target_items:
            return

        save_folder = self.db.get_jpg_save_folder()

        if len(target_items) == 1:
            item = target_items[0]
            suggested_name = item.path.with_suffix(".jpg").name
            initial_dir = self.db.get_jpg_save_folder()
            if initial_dir in ("source", "<source>", "") or not initial_dir:
                initial_dir = str(item.path.parent)
            else:
                initial_dir = str(Path(initial_dir).resolve())

            res_path = fd.asksaveasfilename(
                title="Save JPG As",
                initialdir=initial_dir,
                initialfile=suggested_name,
                filetypes=[("JPEG files", "*.jpg"), ("All files", "*.*")],
                defaultextension=".jpg"
            )
            if not res_path:
                self._update_status("JPG conversion cancelled.")
                return

            out_path = Path(res_path)
            if out_path.suffix.lower() not in ('.jpg', '.jpeg'):
                out_path = out_path.with_suffix('.jpg')

            self._update_status(f"Converting {item.filename} to JPG...")
            self.viewer.show_loading(f"🖼️ Converting to JPG: {item.filename}")

            def worker():
                session = self._get_active_session()
                if not session:
                    return
                ok, reason, res_path = session.convert_item_to_jpg(item, target_path=out_path, overwrite=True)
                self.after(0, lambda: self._on_convert_complete(ok, reason, res_path))

            threading.Thread(target=worker, daemon=True).start()
        else:
            ans = mb.askyesno(
                "Convert Selected to JPG",
                f"Convert all {len(target_items)} selected photos to JPG format?"
            )
            if not ans:
                return

            self._update_status(f"Converting {len(target_items)} selected photos to JPG...")
            self.viewer.show_loading(f"🖼️ Converting {len(target_items)} selected photos to JPG...")

            def worker_batch():
                session = self._get_active_session()
                if not session:
                    return
                success_count = 0
                total = len(target_items)
                for idx, item in enumerate(target_items):
                    if save_folder in ("source", "<source>", "") or not save_folder:
                        out_dir = item.path.parent
                    else:
                        out_dir = Path(save_folder)

                    ok, _, _ = session.convert_item_to_jpg(item, output_dir=out_dir, overwrite=True)
                    if ok:
                        success_count += 1
                    frac = (idx + 1) / float(total)
                    self.after(0, lambda i=idx+1, t=total, f=frac: self.viewer.update_loading_progress(i, t, f))

                self.after(0, lambda: self._on_batch_convert_complete(success_count, total))

            threading.Thread(target=worker_batch, daemon=True).start()

    def _on_convert_complete(self, ok: bool, reason: str, res_path: Path):
        self.viewer.hide_loading()
        if ok:
            if reason == "already_jpg":
                mb.showinfo("Convert to JPG", "Selected file is already a JPG.")
            else:
                mb.showinfo("Convert to JPG Success", f"Successfully saved JPG:\n{res_path}")
                self._update_status(f"Saved JPG to {res_path.name}")
                tab = self._get_active_tab()
                if tab and tab["session"].directory:
                    self._suppress_folder_watch(tab["session"].directory)
                    self._load_directory(str(tab["session"].directory))
        else:
            mb.showerror("Convert to JPG Failed", f"Failed to convert image: {reason}")
            self._update_status("JPG conversion failed.")

    def _on_batch_convert_complete(self, success_count: int, total_count: int):
        self.viewer.hide_loading()
        mb.showinfo("Convert to JPG Complete", f"Successfully converted {success_count} of {total_count} selected photos to JPG.")
        self._update_status(f"Converted {success_count} selected photos to JPG.")
        tab = self._get_active_tab()
        if tab and tab["session"].directory:
            self._suppress_folder_watch(tab["session"].directory)
            self._load_directory(str(tab["session"].directory))

    def _on_set_jpg_folder(self):
        current_val = self.db.get_jpg_save_folder()
        choice = simpledialog.askstring(
            "JPG Save Folder Setting",
            "Set JPG destination directory:\n• Enter '<source>' to save in same directory as RAW\n• Or enter a custom folder path:",
            initialvalue=current_val
        )
        if choice and choice.strip():
            val = choice.strip()
            if val.lower() in ("source", "<source>"):
                val = "<source>"
            self.db.set_jpg_save_folder(val)
            self._update_status(f"Updated JPG Save Folder Setting: '{val}'")
            mb.showinfo("Setting Updated", f"JPG Save Destination set to: {val}")

    def _on_cleanup_metadata(self):
        log_info("Opening SettingsDialog on Workspace tab")
        SettingsDialog(
            self,
            self.db,
            initial_tab="Workspace",
            on_save=self._on_settings_saved,
            on_cleanup_complete=self._on_cleanup_done
        )

    def _on_cleanup_done(self, deleted_count: int, cleaned_folders: List[str]):
        self._update_status(f"Cleaned up {deleted_count} database metadata records across {len(cleaned_folders)} folders.")
        tab = self._get_active_tab()
        if tab and tab["session"].directory:
            cur_dir_str = str(tab["session"].directory).lower()
            for cf in cleaned_folders:
                if cf.lower() in cur_dir_str or cur_dir_str in cf.lower():
                    self._load_directory(str(tab["session"].directory))
                    break

    def _on_move_picked(self):
        tab = self._get_active_tab()
        session = self._get_active_session()
        if not session or not tab or not session.directory:
            mb.showinfo("Move Picked Images", "No active directory loaded.")
            return

        folder_name = self.db.get_picked_folder()
        ans = mb.askyesno(
            "Move Picked Images",
            f"Move all images flagged as PICK into destination subfolder '{folder_name}'?"
        )
        if ans:
            try:
                moved = session.move_items_by_flag(FlagState.PICK, folder_name)
                mb.showinfo("Move Picked Complete", f"Successfully moved {len(moved)} PICK files into '{folder_name}'.")
                self._suppress_folder_watch(session.directory)
                self.selected_indices = set()
                self._load_directory(str(session.directory))
            except Exception as e:
                mb.showerror("Move Error", f"Failed to move picked files: {e}")

    def _on_move_rejected(self):
        tab = self._get_active_tab()
        session = self._get_active_session()
        if not session or not tab or not session.directory:
            mb.showinfo("Move Rejected Images", "No active directory loaded.")
            return

        folder_name = self.db.get_rejected_folder()
        ans = mb.askyesno(
            "Move Rejected Images",
            f"Move all images flagged as REJECT into destination subfolder '{folder_name}'?"
        )
        if ans:
            try:
                moved = session.move_items_by_flag(FlagState.REJECT, folder_name)
                mb.showinfo("Move Rejected Complete", f"Successfully moved {len(moved)} REJECT files into '{folder_name}'.")
                self._suppress_folder_watch(session.directory)
                self.selected_indices = set()
                self._load_directory(str(session.directory))
            except Exception as e:
                mb.showerror("Move Error", f"Failed to move rejected files: {e}")

    def _load_meta_panel_bag_order(self):
        """Restore the order the user dragged the action bags into."""
        try:
            stored = self.db.get_setting(BAG_SETTINGS_KEY, None)
        except Exception:
            log_error("Failed to read the metadata panel section order", exc_info=True)
            return None
        if isinstance(stored, list):
            return [k for k in stored if isinstance(k, str)]
        return None

    def _save_meta_panel_bag_order(self, order: List[str]):
        try:
            self.db.set_setting(BAG_SETTINGS_KEY, list(order))
        except Exception:
            log_error("Failed to save the metadata panel section order", exc_info=True)

    def _load_meta_panel_bag_collapsed(self):
        """Restore which sections were collapsed/expanded."""
        try:
            return self.db.get_meta_panel_collapsed()
        except Exception:
            log_error("Failed to read the metadata panel collapsed state", exc_info=True)
            return None

    def _save_meta_panel_bag_collapsed(self, states: Dict[str, bool]):
        try:
            self.db.set_meta_panel_collapsed(states)
        except Exception:
            log_error("Failed to save the metadata panel collapsed state", exc_info=True)

    def toggle_thumbnail_panel(self, show: Optional[bool] = None):
        """Show or hide the left thumbnail panel."""
        if show is None:
            self._thumbnail_panel_visible = not getattr(self, "_thumbnail_panel_visible", True)
        else:
            self._thumbnail_panel_visible = bool(show)

        if hasattr(self, "_var_show_thumbs"):
            self._var_show_thumbs.set(self._thumbnail_panel_visible)

        if hasattr(self, "thumb_list") and hasattr(self, "viewer"):
            if self._thumbnail_panel_visible:
                self.thumb_list.pack(side="left", fill="y", padx=3, pady=3, before=self.viewer)
            else:
                self.thumb_list.pack_forget()

        self._sync_panel_toggle_buttons()
        self._save_ui_state()

    def toggle_tool_panel(self, show: Optional[bool] = None):
        """Show or hide the right tool / metadata panel."""
        if show is None:
            self._tool_panel_visible = not getattr(self, "_tool_panel_visible", True)
        else:
            self._tool_panel_visible = bool(show)

        if hasattr(self, "_var_show_tools"):
            self._var_show_tools.set(self._tool_panel_visible)

        if hasattr(self, "meta_panel") and hasattr(self, "viewer"):
            if self._tool_panel_visible:
                self.meta_panel.pack(side="right", fill="y", padx=3, pady=3, before=self.viewer)
            else:
                self.meta_panel.pack_forget()

        self._sync_panel_toggle_buttons()
        self._save_ui_state()

    def toggle_both_panels(self):
        """Cinema mode: toggles both panels together."""
        any_visible = getattr(self, "_thumbnail_panel_visible", True) or getattr(self, "_tool_panel_visible", True)
        target = not any_visible
        self.toggle_thumbnail_panel(target)
        self.toggle_tool_panel(target)

    def toggle_menubar(self, show: Optional[bool] = None):
        """Show or hide the top native menu bar."""
        if show is None:
            self._menubar_visible = not getattr(self, "_menubar_visible", True)
        else:
            self._menubar_visible = bool(show)

        if hasattr(self, "_var_show_menubar"):
            self._var_show_menubar.set(self._menubar_visible)

        if self._menubar_visible and hasattr(self, "menubar"):
            self.config(menu=self.menubar)
        else:
            self.config(menu="")
        self._save_ui_state()

    def _sync_panel_toggle_buttons(self):
        if hasattr(self, "toolbar"):
            self.toolbar.set_panel_visibility_state(
                getattr(self, "_thumbnail_panel_visible", True),
                getattr(self, "_tool_panel_visible", True)
            )

    def _save_ui_state(self):
        try:
            self.db.set_ui_panels_visible(
                getattr(self, "_thumbnail_panel_visible", True),
                getattr(self, "_tool_panel_visible", True)
            )
            self.db.set_ui_menubar_visible(getattr(self, "_menubar_visible", True))
            if hasattr(self, "thumb_list"):
                w = self.thumb_list.winfo_width()
                if w > 50:
                    self._thumbnail_panel_width = w
            if hasattr(self, "meta_panel"):
                w = self.meta_panel.winfo_width()
                if w > 50:
                    self._tool_panel_width = w
                self.db.set_meta_panel_collapsed(self.meta_panel.bag_collapsed_state())
            self.db.set_ui_panels_width(
                getattr(self, "_thumbnail_panel_width", 340),
                getattr(self, "_tool_panel_width", 290)
            )
        except Exception:
            log_error("Failed to save UI state", exc_info=True)

    def _on_tab_key(self, event=None):
        self.toggle_both_panels()
        return "break"

    def _on_close_active_tab(self):
        if 0 <= self.active_tab_index < len(self.tabs):
            self._on_tab_closed(self.active_tab_index)

    def _create_menubar(self):
        self.menubar = tk.Menu(self)

        # File Menu
        file_menu = tk.Menu(self.menubar, tearoff=0)
        file_menu.add_command(label="Open Folder...", command=self._on_new_tab, accelerator="Ctrl+O")
        file_menu.add_command(label="Close Current Tab", command=self._on_close_active_tab, accelerator="Ctrl+W")
        file_menu.add_command(label="Close All Tabs", command=self._close_all_tabs)
        file_menu.add_separator()
        file_menu.add_command(label="Open Folder in File Explorer", command=self._on_open_explorer)
        file_menu.add_command(label="Settings...", command=self._on_open_settings, accelerator="Ctrl+,")
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close, accelerator="Alt+F4")
        self.menubar.add_cascade(label="File", menu=file_menu)

        # Edit Menu
        edit_menu = tk.Menu(self.menubar, tearoff=0)
        edit_menu.add_command(label="Select All", command=self._select_all, accelerator="Ctrl+A")
        edit_menu.add_command(label="Select None", command=self._select_none, accelerator="Ctrl+D")
        edit_menu.add_command(label="Copy Image to Clipboard", command=self._on_copy_image_to_clipboard, accelerator="Ctrl+C")
        self.menubar.add_cascade(label="Edit", menu=edit_menu)

        # View Menu
        view_menu = tk.Menu(self.menubar, tearoff=0)
        self._var_show_thumbs = tk.BooleanVar(value=getattr(self, "_thumbnail_panel_visible", True))
        self._var_show_tools = tk.BooleanVar(value=getattr(self, "_tool_panel_visible", True))
        self._var_show_menubar = tk.BooleanVar(value=getattr(self, "_menubar_visible", True))

        view_menu.add_checkbutton(
            label="Show Thumbnail Panel",
            variable=self._var_show_thumbs,
            command=lambda: self.toggle_thumbnail_panel(self._var_show_thumbs.get()),
            accelerator="F8"
        )
        view_menu.add_checkbutton(
            label="Show Tool Panel",
            variable=self._var_show_tools,
            command=lambda: self.toggle_tool_panel(self._var_show_tools.get()),
            accelerator="F9"
        )
        view_menu.add_command(label="Toggle Both Panels (Cinema Mode)", command=self.toggle_both_panels, accelerator="Tab")
        view_menu.add_separator()
        view_menu.add_command(label="Refresh Directory", command=self._on_refresh_directory, accelerator="F5")
        view_menu.add_command(label="Load 100% Full Resolution", command=self._on_load_100_percent, accelerator="A")
        view_menu.add_separator()
        view_menu.add_checkbutton(
            label="Show Menu Bar",
            variable=self._var_show_menubar,
            command=lambda: self.toggle_menubar(self._var_show_menubar.get()),
            accelerator="Alt+M"
        )
        self.menubar.add_cascade(label="View", menu=view_menu)

        # Cull Menu
        cull_menu = tk.Menu(self.menubar, tearoff=0)
        cull_menu.add_command(label="Pick Image", command=lambda: self._set_current_flag(FlagState.PICK), accelerator="P")
        cull_menu.add_command(label="UnPick Image", command=self._on_unpick_current, accelerator="Shift+P")
        cull_menu.add_command(label="Reject Image", command=lambda: self._set_current_flag(FlagState.REJECT), accelerator="X")
        cull_menu.add_command(label="UnReject Image", command=self._on_unreject_current, accelerator="Shift+X")
        cull_menu.add_command(label="Unflag Image", command=lambda: self._set_current_flag(FlagState.UNFLAGGED), accelerator="U")
        cull_menu.add_separator()
        cull_menu.add_command(label="Move Picked Photos...", command=self._on_move_picked)
        cull_menu.add_command(label="Move Rejected Photos...", command=self._on_move_rejected)
        cull_menu.add_command(label="Delete Selected to Trash", command=self._on_delete_selected_to_trash, accelerator="Delete")
        cull_menu.add_command(label="Delete All Rejected to Trash", command=self._on_delete_all_rejected_to_trash, accelerator="Shift+D")
        self.menubar.add_cascade(label="Cull", menu=cull_menu)

        # Tools Menu
        tools_menu = tk.Menu(self.menubar, tearoff=0)
        tools_menu.add_command(label="Scan for Blurry Photos...", command=self._on_scan_blur)
        tools_menu.add_command(label="Scan for Duplicates...", command=self._on_scan_duplicates)
        tools_menu.add_separator()
        tools_menu.add_command(label="Crop Active Photo", command=self._on_trigger_crop, accelerator="C")
        tools_menu.add_command(label="Annotate / Correct Bounding Box", command=self._on_trigger_annotate, accelerator="B")
        tools_menu.add_command(label="Convert Selected to JPG", command=self._on_save_as, accelerator="Ctrl+S")
        tools_menu.add_separator()
        tools_menu.add_command(label="Clean Up Metadata Cache...", command=self._on_cleanup_metadata)
        self.menubar.add_cascade(label="Tools", menu=tools_menu)

        # Help Menu
        help_menu = tk.Menu(self.menubar, tearoff=0)
        help_menu.add_command(label="About Quick Cull...", command=self._on_about_clicked)
        self.menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=self.menubar)

    def _on_config_output_folders(self):
        self._on_open_settings()

    def _on_open_settings(self):
        SettingsDialog(
            self,
            self.db,
            initial_tab="General",
            on_save=self._on_settings_saved,
            on_cleanup_metadata=self._on_cleanup_metadata,
            on_cleanup_complete=self._on_cleanup_done
        )

    def _on_settings_saved(self):
        new_p = self.db.get_picked_folder()
        new_r = self.db.get_rejected_folder()
        self.meta_panel.update_output_folders(new_p, new_r)

        scale_val = self.db.get_raw_scale()
        sc_str = "25%"
        if scale_val == 0.10: sc_str = "10%"
        elif scale_val == 0.15: sc_str = "15%"
        elif scale_val == 0.20: sc_str = "20%"
        elif scale_val == 0.50: sc_str = "50%"
        elif scale_val == 1.00: sc_str = "100%"
        self.toolbar.opt_raw_scale.set(sc_str)

        wb_val = self.db.get_white_balance()
        self.toolbar.opt_wb.set("Camera" if wb_val == "camera" else "Auto")

        self._update_status(f"Settings saved. Picked: '{new_p}', Rejected: '{new_r}', RAW Scale: {sc_str}")

        custom_tags = self.db.get_custom_tags()
        self.meta_panel.refresh_tag_buttons(custom_tags)
        self.toolbar.update_tag_options(custom_tags)

        # Update viewer bounding boxes based on new settings
        if self.current_items and 0 <= self.current_index < len(self.current_items):
            item = self.current_items[self.current_index]
            show_boxes = self.db.get_show_bounding_boxes()
            if show_boxes and (getattr(item, 'detection_box', None) or getattr(item, 'eye_box', None) or getattr(item, 'manual_detection_box', None) or getattr(item, 'manual_eye_box', None)):
                self.viewer.set_detection_box(item.detection_box, item.eye_box, item.manual_detection_box, item.manual_eye_box)
            else:
                self.viewer.clear_detection_box()

        tab = self._get_active_tab()
        if tab and tab["session"].directory:
            self._load_directory(str(tab["session"].directory))

    def _batch_move(self, flag: FlagState, folder_name: str):
        session = self._get_active_session()
        if not session or not session.directory:
            return

        ans = mb.askyesno(
            f"Move {flag.value} Images",
            f"Move all images flagged as {flag.value} into subfolder '{folder_name}'?"
        )
        if ans:
            try:
                moved = session.move_items_by_flag(flag, folder_name)
                mb.showinfo("Batch Move Complete", f"Moved {len(moved)} files into subfolder '{folder_name}'.")
                self._suppress_folder_watch(session.directory)
                self._load_directory(str(session.directory))
            except Exception as e:
                mb.showerror("Batch Move Error", f"Failed to move files: {e}")

    def _export_manifest(self):
        tab = self._get_active_tab()
        if not tab or not tab["session"].items or not tab["session"].directory:
            return

        out_file = tab["session"].directory / "culling_manifest.json"
        try:
            res_path = tab["session"].export_manifest(out_file, format_type="json")
            mb.showinfo("Export Manifest Success", f"Culling manifest saved to:\n{res_path}")
            self._update_status("Exported manifest JSON.")
        except Exception as e:
            mb.showerror("Export Manifest Failed", f"Error exporting manifest: {e}")

    def _sync_exif_ratings(self):
        session = self._get_active_session()
        if not session or not session.items:
            return

        self._update_status("Syncing star ratings to EXIF metadata...")
        self.viewer.show_loading("Syncing Star Ratings to EXIF...")
        self._suppress_folder_watch(session.directory, FolderWatcher.EXIF_SYNC_SUPPRESS_SECONDS)

        def worker():
            count = session.sync_exif_ratings()
            self.after(0, lambda: self._on_sync_complete(count))

        threading.Thread(target=worker, daemon=True).start()

    def _on_sync_complete(self, count: int):
        self.viewer.hide_loading()
        session = self._get_active_session()
        if session and session.directory:
            self.folder_watcher.resync(session.directory)
        mb.showinfo("Sync Complete", f"Successfully synced star ratings to EXIF metadata for {count} files.")
        self._update_status(f"Synced EXIF ratings for {count} files.")

    def _on_load_100_percent(self):
        session = self._get_active_session()
        if not self.current_items or not (0 <= self.current_index < len(self.current_items)):
            return

        cur_item = self.current_items[self.current_index]

        self._update_status(f"🔍 Loading 100% Full Resolution: {cur_item.filename}...")
        self.viewer.show_loading(f"🔍 Decoding 100% Full Resolution: {cur_item.filename}")

        def worker():
            white_balance = self.toolbar.get_white_balance() if hasattr(self, "toolbar") else "camera"
            img = session.image_loader.load_full_image(cur_item.path, raw_scale=1.00, white_balance=white_balance) if session else None
            if img:
                def update_ui():
                    self.viewer.set_image(img, preserve_zoom=False)
                    self._update_status(f"🔍 Loaded 100% Full Resolution: {cur_item.filename}")

                self.after(0, update_ui)
            else:
                self.after(0, lambda: self.viewer.hide_loading())

        threading.Thread(target=worker, daemon=True).start()

    def _on_copy_image_to_clipboard(self):
        session = self._get_active_session()
        if not session or not self.current_items or not (0 <= self.current_index < len(self.current_items)):
            return

        cur_item = self.current_items[self.current_index]
        self._update_status(f"Copying {cur_item.filename} to Clipboard...")

        def worker():
            session = self._get_active_session()
            if not session:
                return
            crop_pcts = self.viewer.get_crop_box_percentages()

            raw_scale = self.toolbar.get_raw_scale()
            white_balance = self.toolbar.get_white_balance()

            img = session.image_loader.get_cached_full_image(cur_item.path, raw_scale=raw_scale, white_balance=white_balance)
            if img is None:
                img = session.image_loader.load_full_image(cur_item.path, raw_scale=raw_scale, white_balance=white_balance)

            if img:
                if crop_pcts is not None:
                    w, h = img.size
                    px1 = max(0, min(w, int(crop_pcts[0] * w)))
                    py1 = max(0, min(h, int(crop_pcts[1] * h)))
                    px2 = max(0, min(w, int(crop_pcts[2] * w)))
                    py2 = max(0, min(h, int(crop_pcts[3] * h)))

                    if px2 > px1 and py2 > py1:
                        img = img.crop((px1, py1, px2, py2))

                    ok = copy_image_to_clipboard(img)
                    if ok:
                        self.after(0, lambda: self._update_status(f"📋 Copied Cropped Selection of '{cur_item.filename}' as JPEG to Clipboard!"))
                    else:
                        self.after(0, lambda: self._update_status("❌ Failed to copy cropped image to Clipboard."))
                else:
                    ok = copy_image_to_clipboard(img)
                    if ok:
                        self.after(0, lambda: self._update_status(f"📋 Copied '{cur_item.filename}' as JPEG to Clipboard!"))
                    else:
                        self.after(0, lambda: self._update_status("❌ Failed to copy image to Clipboard."))
            else:
                self.after(0, lambda: self._update_status("❌ Could not decode image for Clipboard."))

        threading.Thread(target=worker, daemon=True).start()

    def _on_open_explorer(self):
        tab = self._get_active_tab()
        if tab and tab.get("session") and tab["session"].directory and tab["session"].directory.exists():
            open_folder_in_explorer(tab["session"].directory)
            self._update_status(f"Opened folder in File Explorer: {tab['session'].directory.name}")
        else:
            mb.showinfo("Open Folder", "No active photo folder opened.")


def copy_image_to_clipboard(pil_img: Image.Image, temp_jpg_path: Optional[Path] = None) -> bool:
    """
    Copy a PIL Image to the Windows System Clipboard using dual payloads:
    1. CF_DIB: Bitmap pixel data (for Photoshop, Paint, GIMP, Word, PowerPoint)
    2. CF_HDROP: Temp JPEG file path payload (for Discord, Slack, WhatsApp, Telegram, Browsers, File Explorer)
    """
    if pil_img is None:
        return False

    try:
        import io
        import os
        import ctypes
        import tempfile

        rgb_img = pil_img.convert("RGB")

        # 1. Generate CF_DIB bitmap data
        output = io.BytesIO()
        rgb_img.save(output, "BMP")
        data = output.getvalue()[14:]  # Strip 14-byte BMP header
        output.close()

        # 2. Save temporary JPEG file for file-drop clipboard readers (Discord, Slack, WhatsApp)
        if temp_jpg_path is None:
            tmp_fd, tmp_file_str = tempfile.mkstemp(suffix=".jpg", prefix="culler_clip_")
            os.close(tmp_fd)
            temp_jpg_path = Path(tmp_file_str)

        rgb_img.save(str(temp_jpg_path), "JPEG", quality=95)

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # Define 64-bit safe Win32 API function signatures
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_bool
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = ctypes.c_bool
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = ctypes.c_bool

        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p

        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_bool

        CF_DIB = 8
        CF_HDROP = 15
        GMEM_MOVEABLE = 0x0002
        GMEM_ZEROINIT = 0x0040

        if not user32.OpenClipboard(None):
            log_error("[Clipboard Error] OpenClipboard failed")
            return False

        dib_ok = False
        hdrop_ok = False

        try:
            user32.EmptyClipboard()

            # A) Set CF_DIB (Bitmap pixel data)
            h_dib = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(data))
            if h_dib:
                ptr = kernel32.GlobalLock(h_dib)
                if ptr:
                    ctypes.memmove(ptr, data, len(data))
                    kernel32.GlobalUnlock(h_dib)
                    res = user32.SetClipboardData(CF_DIB, h_dib)
                    if res:
                        dib_ok = True

            # B) Set CF_HDROP (File drop payload for Discord, Slack, WhatsApp, Telegram, Browsers, Explorer)
            abs_path_str = str(temp_jpg_path.resolve()) + "\0\0"
            path_bytes = abs_path_str.encode("utf-16le")
            header = (20).to_bytes(4, "little") + b"\x00" * 12 + (1).to_bytes(4, "little")
            drop_data = header + path_bytes

            h_hdrop = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(drop_data))
            if h_hdrop:
                ptr = kernel32.GlobalLock(h_hdrop)
                if ptr:
                    ctypes.memmove(ptr, drop_data, len(drop_data))
                    kernel32.GlobalUnlock(h_hdrop)
                    res = user32.SetClipboardData(CF_HDROP, h_hdrop)
                    if res:
                        hdrop_ok = True

            log_info(f"Copy image to clipboard success: CF_DIB={dib_ok}, CF_HDROP={hdrop_ok}")
            return dib_ok or hdrop_ok
        finally:
            user32.CloseClipboard()
    except Exception as e:
        log_error(f"Error copying image to clipboard: {e}")
        return False


class DeleteStackedDialog(ctk.CTkToplevel):
    """
    Modal dialog showing individual files with checkboxes for selective deletion.
    """

    def __init__(self, master, target_items: List['ImageItem']):
        super().__init__(master)
        self.result: Optional[List[Path]] = None
        self._check_vars: Dict[Path, ctk.BooleanVar] = {}

        self.title("🗑️ Select Files to Delete")
        self.geometry("600x480")
        self.resizable(True, True)

        self.transient(master)
        self.grab_set()

        self.bind("<Escape>", lambda e: self._on_cancel())

        all_paths: List[Path] = []
        for item in target_items:
            for p in item.stacked_paths:
                all_paths.append(p)

        lbl_title = ctk.CTkLabel(
            self,
            text=f"Select files to move to Recycle Bin / Trash ({len(all_paths)} files)",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        lbl_title.pack(anchor="w", padx=15, pady=(12, 6))

        btn_bar = ctk.CTkFrame(self, fg_color="transparent")
        btn_bar.pack(fill="x", padx=15, pady=(0, 6))

        btn_sel_all = ctk.CTkButton(
            btn_bar, text="Select All", width=90, height=26,
            fg_color="#1f538d", hover_color="#14375e",
            font=ctk.CTkFont(size=11), command=lambda: self._set_all(True)
        )
        btn_sel_all.pack(side="left", padx=(0, 4))

        btn_desel = ctk.CTkButton(
            btn_bar, text="Deselect All", width=90, height=26,
            fg_color="#1f538d", hover_color="#14375e",
            font=ctk.CTkFont(size=11), command=lambda: self._set_all(False)
        )
        btn_desel.pack(side="left", padx=4)

        self.lbl_count = ctk.CTkLabel(
            btn_bar, text="0 selected", font=ctk.CTkFont(size=11, weight="bold")
        )
        self.lbl_count.pack(side="right", padx=5)

        scroll_frame = ctk.CTkScrollableFrame(self, corner_radius=6, fg_color="#1e1e1e", border_width=1, border_color="#383838")
        scroll_frame.pack(fill="both", expand=True, padx=15, pady=5)

        for p in all_paths:
            ext = p.suffix.lower()
            if ext == ".arw":
                badge = "RAW"
                badge_color = "#1f538d"
            elif ext in (".jpg", ".jpeg"):
                badge = "JPG"
                badge_color = "#1b4332"
            else:
                badge = ext.upper().lstrip(".")
                badge_color = "#888888"

            row = ctk.CTkFrame(scroll_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)

            var = ctk.BooleanVar(value=True)
            self._check_vars[p] = var

            chk = ctk.CTkCheckBox(
                row, text="", variable=var, width=24,
                checkbox_width=18, checkbox_height=18,
                command=lambda v=var: self._update_count()
            )
            chk.pack(side="left", padx=(4, 8), pady=4)

            badge_lbl = ctk.CTkLabel(
                row, text=badge, width=40, height=22,
                font=ctk.CTkFont(size=10, weight="bold"),
                fg_color=badge_color, corner_radius=4,
                anchor="center"
            )
            badge_lbl.pack(side="left", padx=(0, 8))

            name_lbl = ctk.CTkLabel(
                row, text=p.name, font=ctk.CTkFont(size=11),
                anchor="w"
            )
            name_lbl.pack(side="left", fill="x", expand=True)

        self._update_count()

        bottom_bar = ctk.CTkFrame(self, fg_color="transparent")
        bottom_bar.pack(fill="x", padx=15, pady=(8, 12))

        btn_cancel = ctk.CTkButton(
            bottom_bar, text="Cancel", width=90, height=32,
            fg_color="#1f538d", hover_color="#14375e",
            command=self._on_cancel
        )
        btn_cancel.pack(side="right", padx=(4, 0))

        btn_delete = ctk.CTkButton(
            bottom_bar, text="🗑️ Delete Selected", width=140, height=32,
            fg_color="#5c0612", hover_color="#d90429",
            font=ctk.CTkFont(weight="bold", size=12),
            command=self._on_delete
        )
        btn_delete.pack(side="right", padx=(0, 4))

        self.after(10, self._center_window)
        self.wait_window()

    def _center_window(self):
        self.update_idletasks()
        try:
            pw = self.master.winfo_width()
            ph = self.master.winfo_height()
            px = self.master.winfo_x()
            py = self.master.winfo_y()
            w = self.winfo_width()
            h = self.winfo_height()
            x = px + (pw - w) // 2
            y = py + (ph - h) // 2
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _set_all(self, value: bool):
        for var in self._check_vars.values():
            var.set(value)
        self._update_count()

    def _update_count(self):
        count = sum(1 for var in self._check_vars.values() if var.get())
        self.lbl_count.configure(text=f"{count} selected")

    def _on_delete(self):
        selected = [p for p, var in self._check_vars.items() if var.get()]
        if not selected:
            mb.showwarning("No Files Selected", "Please select at least one file to delete.")
            return
        self.result = selected
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.destroy()


def open_folder_in_explorer(folder_path: Path):
    """
    Open directory in OS File Manager (Windows Explorer, macOS Finder, Linux File Manager).
    """
    p = Path(folder_path).resolve()
    if not p.exists():
        return

    if sys.platform == "win32":
        os.startfile(str(p))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


if __name__ == "__main__":
    # Go through the bootstrap launcher so the splash is painted before the
    # heavy imports run (see bootstrap.launch_gui).
    launch_gui(initial_path=sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None)
