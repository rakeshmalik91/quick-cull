import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from culler.culler_engine import CullingSession, ImageItem, FlagState
from culler.db_manager import DatabaseManager
from culler.folder_watcher import FolderWatcher


class TestDbManagerTabs(unittest.TestCase):
    """
    Unit tests for DatabaseManager open_tabs persistence methods.
    """

    def setUp(self):
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.temp_db_fd)
        self.db = DatabaseManager(db_path=self.temp_db_path)

    def tearDown(self):
        if hasattr(self, "db") and self.db:
            try:
                self.db.close()
            except Exception:
                pass
        if os.path.exists(self.temp_db_path):
            try:
                os.remove(self.temp_db_path)
            except Exception:
                pass

    def test_get_open_tabs_default_empty(self):
        result = self.db.get_open_tabs()
        self.assertEqual(result, {"tabs": [], "active_index": 0})

    def test_save_and_get_open_tabs(self):
        tabs_data = [
            {"directory": "D:/Photos/2024", "tab_label": "2024", "filter_values": {"flag": "Pick"}},
            {"directory": "D:/Photos/2023", "tab_label": "2023", "filter_values": {"flag": "All"}},
        ]
        self.db.save_open_tabs(tabs_data, active_index=1)

        result = self.db.get_open_tabs()
        self.assertEqual(result["tabs"], tabs_data)
        self.assertEqual(result["active_index"], 1)

    def test_get_active_tab_index_default(self):
        self.assertEqual(self.db.get_active_tab_index(), 0)

    def test_get_active_tab_index_stored(self):
        self.db.save_open_tabs([{"directory": "D:/Photos"}], active_index=2)
        self.assertEqual(self.db.get_active_tab_index(), 2)


class TestImageCullerAppTabLogic(unittest.TestCase):
    """
    Unit tests for ImageCullerApp tab management logic (_create_tab_info, _get_active_tab,
    _save_active_tab_state, _switch_tab, _close_tab, _add_tab, _persist_tabs_state).
    """

    def setUp(self):
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.temp_db_fd)
        self.db = DatabaseManager(db_path=self.temp_db_path)

    def tearDown(self):
        if hasattr(self, "db") and self.db:
            try:
                self.db.close()
            except Exception:
                pass
        if os.path.exists(self.temp_db_path):
            try:
                os.remove(self.temp_db_path)
            except Exception:
                pass

    def _make_app(self):
        from gui import ImageCullerApp
        app = MagicMock()
        app.db = self.db
        app.tabs = []
        app.active_tab_index = -1
        app.current_items = []
        app.current_index = -1
        app.selected_indices = set()
        app.selection_anchor_idx = 0
        app.toolbar = MagicMock()
        app.toolbar.get_filter_values.return_value = {
            "flag": "All",
            "rating": [],
            "format": "All Formats",
            "tag": []
        }
        app.thumb_list = MagicMock()
        app.viewer = MagicMock()
        app.meta_panel = MagicMock()
        app.tab_bar = MagicMock()
        app.tab_bar.add_tab = MagicMock(return_value=0)
        app._create_tab_info = lambda directory, *args, **kwargs: ImageCullerApp._create_tab_info(app, directory, *args, **kwargs)
        app._get_active_tab = lambda: ImageCullerApp._get_active_tab(app)
        app._get_active_session = lambda: ImageCullerApp._get_active_session(app)
        app._save_active_tab_state = lambda: ImageCullerApp._save_active_tab_state(app)
        app._switch_tab = lambda index: ImageCullerApp._switch_tab(app, index)
        app._close_tab = lambda index: ImageCullerApp._close_tab(app, index)
        app._add_tab = lambda directory, *args, **kwargs: ImageCullerApp._add_tab(app, directory, *args, **kwargs)
        app._persist_tabs_state = lambda: ImageCullerApp._persist_tabs_state(app)
        app._restore_tabs_state = lambda: ImageCullerApp._restore_tabs_state(app)
        app._apply_tab_state = lambda tab: ImageCullerApp._apply_tab_state(app, tab)
        app._load_tab_directory = MagicMock()
        return app

    def test_create_tab_info(self):
        from gui import ImageCullerApp

        app = self._make_app()
        tab = ImageCullerApp._create_tab_info(app, "D:/Photos/2024")

        self.assertEqual(tab["directory"], str(Path("D:/Photos/2024").resolve()))
        self.assertEqual(tab["tab_label"], "2024")
        self.assertIn("session", tab)
        self.assertIn("filter_values", tab)
        self.assertEqual(tab["filter_values"]["flag"], "All")
        self.assertEqual(tab["current_items"], [])
        self.assertEqual(tab["current_index"], -1)
        self.assertEqual(tab["selected_indices"], set())
        self.assertEqual(tab["selection_anchor_idx"], 0)
        self.assertFalse(tab["is_loaded"])

    def test_add_tab_appends_and_activates(self):
        from gui import ImageCullerApp

        app = self._make_app()
        app._load_tab_directory = MagicMock()
        app._persist_tabs_state = MagicMock()

        ImageCullerApp._add_tab(app, "D:/Photos/A")

        self.assertEqual(len(app.tabs), 1)
        self.assertEqual(app.tabs[0]["directory"], str(Path("D:/Photos/A").resolve()))
        self.assertEqual(app.active_tab_index, 0)
        app.tab_bar.add_tab.assert_called_once_with("A")
        app._load_tab_directory.assert_called_once_with(app.tabs[0], show_progress=True)
        app._persist_tabs_state.assert_called_once()

    def test_close_tab_removes_and_readjusts_active(self):
        from gui import ImageCullerApp

        app = self._make_app()
        app._persist_tabs_state = MagicMock()
        ImageCullerApp._add_tab(app, "D:/Photos/A")
        ImageCullerApp._add_tab(app, "D:/Photos/B")
        ImageCullerApp._add_tab(app, "D:/Photos/C")

        app.tab_bar.reset_mock()
        app._persist_tabs_state.reset_mock()

        ImageCullerApp._close_tab(app, 1)

        self.assertEqual(len(app.tabs), 2)
        self.assertEqual(app.tabs[0]["directory"], str(Path("D:/Photos/A").resolve()))
        self.assertEqual(app.tabs[1]["directory"], str(Path("D:/Photos/C").resolve()))
        app.tab_bar.remove_tab.assert_called_once_with(1)
        self.assertEqual(app.active_tab_index, 1)
        app._persist_tabs_state.assert_called_once()

    def test_close_active_tab_switches_to_neighbor(self):
        from gui import ImageCullerApp

        app = self._make_app()
        app._persist_tabs_state = MagicMock()
        app._load_tab_directory = MagicMock()
        app._apply_tab_state = MagicMock()
        ImageCullerApp._add_tab(app, "D:/Photos/A")
        ImageCullerApp._add_tab(app, "D:/Photos/B")

        app.tab_bar.reset_mock()
        app._persist_tabs_state.reset_mock()

        app.tabs[0]["is_loaded"] = True
        app.active_tab_index = 1
        ImageCullerApp._close_tab(app, 1)

        self.assertEqual(app.active_tab_index, 0)
        app._apply_tab_state.assert_called_once_with(app.tabs[0])

    def test_close_tab_when_only_one_does_nothing(self):
        from gui import ImageCullerApp

        app = self._make_app()
        ImageCullerApp._add_tab(app, "D:/Photos/A")

        app.tab_bar.reset_mock()
        ImageCullerApp._close_tab(app, 0)

        self.assertEqual(len(app.tabs), 1)
        app.tab_bar.remove_tab.assert_not_called()

    def test_switch_tab_saves_and_applies_state(self):
        from gui import ImageCullerApp

        app = self._make_app()
        app._persist_tabs_state = MagicMock()
        app._load_tab_directory = MagicMock()
        app._apply_tab_state = MagicMock()
        app._save_active_tab_state = MagicMock()

        ImageCullerApp._add_tab(app, "D:/Photos/A")
        ImageCullerApp._add_tab(app, "D:/Photos/B")
        app.tab_bar.reset_mock()
        app._persist_tabs_state.reset_mock()

        app.active_tab_index = 0
        tab_b = app.tabs[1]
        tab_b["is_loaded"] = True
        ImageCullerApp._switch_tab(app, 1)

        app._save_active_tab_state.assert_called_once()
        app.tab_bar.set_active.assert_called_once_with(1)
        app._apply_tab_state.assert_called_once_with(tab_b)
        app._persist_tabs_state.assert_called_once()
        self.assertEqual(app.active_tab_index, 1)

    def test_switch_tab_loads_if_not_loaded(self):
        from gui import ImageCullerApp

        app = self._make_app()
        app._persist_tabs_state = MagicMock()
        app._load_tab_directory = MagicMock()
        app._apply_tab_state = MagicMock()

        ImageCullerApp._add_tab(app, "D:/Photos/A")
        ImageCullerApp._add_tab(app, "D:/Photos/B")
        app.tab_bar.reset_mock()
        app._persist_tabs_state.reset_mock()
        app._load_tab_directory.reset_mock()

        app.active_tab_index = 0
        tab_b = app.tabs[1]
        tab_b["is_loaded"] = False
        ImageCullerApp._switch_tab(app, 1)

        app._load_tab_directory.assert_called_once_with(tab_b, show_progress=True)
        app._apply_tab_state.assert_called_once_with(tab_b)

    def test_switch_same_index_noop(self):
        from gui import ImageCullerApp

        app = self._make_app()
        app._persist_tabs_state = MagicMock()
        ImageCullerApp._add_tab(app, "D:/Photos/A")
        app._save_active_tab_state = MagicMock()

        ImageCullerApp._switch_tab(app, 0)

        app._save_active_tab_state.assert_not_called()

    def test_save_active_tab_state(self):
        from gui import ImageCullerApp

        app = self._make_app()
        app.current_items = [MagicMock()]
        app.current_index = 0
        app.selected_indices = {0, 1}
        app.selection_anchor_idx = 1

        tab = {
            "directory": "D:/Photos",
            "tab_label": "Photos",
            "session": MagicMock(),
            "filter_values": {},
            "current_items": [],
            "current_index": -1,
            "selected_indices": set(),
            "selection_anchor_idx": 0,
            "is_loaded": False,
        }
        app.tabs = [tab]
        app.active_tab_index = 0

        ImageCullerApp._save_active_tab_state(app)

        self.assertEqual(tab["current_items"], app.current_items)
        self.assertEqual(tab["current_index"], 0)
        self.assertEqual(tab["selected_indices"], {0, 1})
        self.assertEqual(tab["selection_anchor_idx"], 1)
        self.assertEqual(tab["filter_values"]["flag"], "All")
        app.toolbar.get_filter_values.assert_called_once()

    def test_persist_tabs_state_roundtrip(self):
        from gui import ImageCullerApp

        app = self._make_app()
        ImageCullerApp._add_tab(app, "D:/Photos/A")
        ImageCullerApp._add_tab(app, "D:/Photos/B")

        app.tabs[0]["filter_values"] = {"flag": "Pick", "rating": ["5"], "format": ".JPG", "tag": ["Blur"]}
        app.tabs[1]["filter_values"] = {"flag": "All", "rating": [], "format": "All Formats", "tag": []}
        app.active_tab_index = 1

        ImageCullerApp._persist_tabs_state(app)

        loaded = self.db.get_open_tabs()
        self.assertEqual(len(loaded["tabs"]), 2)
        self.assertEqual(loaded["tabs"][0]["directory"], str(Path("D:/Photos/A").resolve()))
        self.assertEqual(loaded["tabs"][0]["filter_values"]["flag"], "Pick")
        self.assertEqual(loaded["tabs"][1]["directory"], str(Path("D:/Photos/B").resolve()))
        self.assertEqual(loaded["active_index"], 1)

    def test_restore_tabs_state(self):
        from gui import ImageCullerApp

        app = self._make_app()
        app._load_tab_directory = MagicMock()
        app._apply_tab_state = MagicMock()

        tabs_payload = [
            {"directory": "D:/Photos/A", "tab_label": "A", "filter_values": {"flag": "Pick"}},
            {"directory": "D:/Photos/B", "tab_label": "B", "filter_values": {"flag": "Reject"}},
        ]
        self.db.save_open_tabs(tabs_payload, active_index=0)

        with patch("os.path.exists", return_value=True):
            ImageCullerApp._restore_tabs_state(app)

        self.assertEqual(len(app.tabs), 2)
        self.assertEqual(app.tabs[0]["tab_label"], "A")
        self.assertEqual(app.tabs[0]["filter_values"]["flag"], "All")
        self.assertEqual(app.tabs[1]["tab_label"], "B")
        self.assertEqual(app.tabs[1]["filter_values"]["flag"], "All")
        self.assertEqual(app.active_tab_index, 0)
        self.assertFalse(app.tabs[0]["is_loaded"])
        self.assertFalse(app.tabs[1]["is_loaded"])
        app._load_tab_directory.assert_called_once_with(app.tabs[0], show_progress=True)

    def test_restore_tabs_skips_missing_directories(self):
        from gui import ImageCullerApp

        app = self._make_app()
        app._load_tab_directory = MagicMock()

        tabs_payload = [
            {"directory": "D:/Photos/Exists", "tab_label": "Exists", "filter_values": {}},
            {"directory": "D:/Photos/Missing", "tab_label": "Missing", "filter_values": {}},
        ]
        self.db.save_open_tabs(tabs_payload, active_index=0)

        with patch("os.path.exists", side_effect=lambda p: "Exists" in p):
            ImageCullerApp._restore_tabs_state(app)

        self.assertEqual(len(app.tabs), 1)
        self.assertEqual(app.tabs[0]["tab_label"], "Exists")

    def test_get_active_tab_returns_correct_tab(self):
        from gui import ImageCullerApp

        app = self._make_app()
        app.tabs = [{"directory": "A"}, {"directory": "B"}, {"directory": "C"}]
        app.active_tab_index = 2

        tab = ImageCullerApp._get_active_tab(app)
        self.assertEqual(tab["directory"], "C")

    def test_get_active_tab_returns_none_when_empty(self):
        from gui import ImageCullerApp

        app = self._make_app()
        app.tabs = []
        app.active_tab_index = -1

        tab = ImageCullerApp._get_active_tab(app)
        self.assertIsNone(tab)

    def test_tab_independent_filter_values(self):
        from gui import ImageCullerApp

        app = self._make_app()
        app.toolbar.get_filter_values.return_value = {"flag": "Pick", "rating": ["5"], "format": ".ARW", "tag": ["Blur"]}

        ImageCullerApp._add_tab(app, "D:/Photos/A")
        ImageCullerApp._add_tab(app, "D:/Photos/B")

        app.toolbar.get_filter_values.return_value = {"flag": "All", "rating": [], "format": "All Formats", "tag": []}
        app.current_items = []
        app.current_index = -1
        app.selected_indices = set()
        app.selection_anchor_idx = 0
        ImageCullerApp._save_active_tab_state(app)

        app.toolbar.get_filter_values.return_value = {"flag": "Reject", "rating": ["1", "2"], "format": ".JPG", "tag": ["Dark"]}
        app.current_items = []
        app.current_index = -1
        app.selected_indices = set()
        app.selection_anchor_idx = 0
        ImageCullerApp._save_active_tab_state(app)

        self.assertEqual(app.tabs[0]["filter_values"]["flag"], "Pick")
        self.assertEqual(app.tabs[0]["filter_values"]["rating"], ["5"])
        self.assertEqual(app.tabs[1]["filter_values"]["flag"], "Reject")
        self.assertEqual(app.tabs[1]["filter_values"]["rating"], ["1", "2"])


class TestTabLoadStats(unittest.TestCase):
    """
    Unit tests for per-tab folder/thumbnail load stats stored in the tab dict.
    """

    def setUp(self):
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.temp_db_fd)
        self.db = DatabaseManager(db_path=self.temp_db_path)

    def tearDown(self):
        if hasattr(self, "db") and self.db:
            try:
                self.db.close()
            except Exception:
                pass
        if os.path.exists(self.temp_db_path):
            try:
                os.remove(self.temp_db_path)
            except Exception:
                pass

    def _make_app(self):
        from gui import ImageCullerApp

        app = MagicMock()
        app.db = self.db
        app.tabs = []
        app.active_tab_index = -1
        app.current_items = []
        app.current_index = -1
        app.selected_indices = set()
        app.selection_anchor_idx = 0
        app.toolbar = MagicMock()
        app.thumb_list = MagicMock()
        app.viewer = MagicMock()
        app.meta_panel = MagicMock()
        app.tab_bar = MagicMock()
        app._create_tab_info = lambda directory, *a, **kw: ImageCullerApp._create_tab_info(app, directory, *a, **kw)
        app._get_active_tab = lambda: ImageCullerApp._get_active_tab(app)
        app._save_active_tab_state = lambda: ImageCullerApp._save_active_tab_state(app)
        app._apply_tab_state = lambda tab: ImageCullerApp._apply_tab_state(app, tab)
        app._persist_tabs_state = MagicMock()
        app._switch_tab = lambda index: ImageCullerApp._switch_tab(app, index)
        app._load_tab_directory = MagicMock()
        return app

    def test_create_tab_info_starts_with_empty_stats(self):
        from gui import ImageCullerApp

        app = self._make_app()
        ImageCullerApp._add_tab(app, "D:/Photos/A")
        self.assertEqual(app.tabs[0]["load_stats"], {"folder": None, "thumb": None})

    def test_apply_tab_state_restores_saved_stats(self):
        from gui import ImageCullerApp

        app = self._make_app()
        ImageCullerApp._add_tab(app, "D:/Photos/A")
        ImageCullerApp._add_tab(app, "D:/Photos/B")
        app.active_tab_index = 0

        app.tabs[0]["load_stats"] = {"folder": 4.5, "thumb": 1.25}
        app.tabs[0]["current_items"] = [ImageItem(Path("D:/Photos/A/IMG_0001.JPG"))]
        app.tabs[0]["current_index"] = 0

        ImageCullerApp._apply_tab_state(app, app.tabs[0])

        app.thumb_list.show_load_stats.assert_called_once_with(4.5, 1.25)
        app.thumb_list.begin_thumb_timing.assert_not_called()

    def test_apply_tab_state_times_first_thumb_render(self):
        from gui import ImageCullerApp

        app = self._make_app()
        ImageCullerApp._add_tab(app, "D:/Photos/A")
        app.active_tab_index = 0

        app.tabs[0]["load_stats"] = {"folder": 4.5, "thumb": None}
        app.tabs[0]["current_items"] = [ImageItem(Path("D:/Photos/A/IMG_0001.JPG"))]
        app.tabs[0]["current_index"] = 0

        ImageCullerApp._apply_tab_state(app, app.tabs[0])

        app.thumb_list.show_load_stats.assert_called_once_with(4.5, None)
        app.thumb_list.begin_thumb_timing.assert_called_once()

    def test_switch_tab_does_not_carry_stats_over(self):
        from gui import ImageCullerApp

        app = self._make_app()
        ImageCullerApp._add_tab(app, "D:/Photos/A")
        ImageCullerApp._add_tab(app, "D:/Photos/B")
        app.active_tab_index = 0

        app.tabs[0]["load_stats"] = {"folder": 4.5, "thumb": 1.25}
        app.tabs[0]["is_loaded"] = True
        app.tabs[0]["current_items"] = [ImageItem(Path("D:/Photos/A/IMG_0001.JPG"))]
        app.tabs[0]["current_index"] = 0
        app.tabs[1]["is_loaded"] = True
        app.tabs[1]["load_stats"] = {"folder": 9.0, "thumb": 2.5}
        app.tabs[1]["current_items"] = [ImageItem(Path("D:/Photos/B/IMG_0001.JPG"))]
        app.tabs[1]["current_index"] = 0

        ImageCullerApp._switch_tab(app, 1)

        app.thumb_list.show_load_stats.assert_called_once_with(9.0, 2.5)
        self.assertEqual(app.tabs[0]["load_stats"], {"folder": 4.5, "thumb": 1.25})

    def test_on_load_stats_changed_writes_to_active_tab(self):
        from gui import ImageCullerApp

        app = self._make_app()
        ImageCullerApp._add_tab(app, "D:/Photos/A")
        ImageCullerApp._add_tab(app, "D:/Photos/B")
        app.active_tab_index = 1

        ImageCullerApp._on_load_stats_changed(app, {"folder": 9.0, "thumb": 2.5})

        self.assertEqual(app.tabs[1]["load_stats"], {"folder": 9.0, "thumb": 2.5})
        self.assertEqual(app.tabs[0]["load_stats"], {"folder": None, "thumb": None})

    def test_on_load_stats_changed_ignores_missing_tab(self):
        from gui import ImageCullerApp

        app = self._make_app()
        app.active_tab_index = -1
        ImageCullerApp._on_load_stats_changed(app, {"folder": 1.0, "thumb": 2.0})


class TestFolderChangeReload(unittest.TestCase):
    """
    Unit tests for automatic tab reload when a watched folder changes on disk.
    """

    def setUp(self):
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.temp_db_fd)
        self.db = DatabaseManager(db_path=self.temp_db_path)
        self.temp_dir = tempfile.mkdtemp()
        self.directory = Path(self.temp_dir)

    def tearDown(self):
        if hasattr(self, "db") and self.db:
            try:
                self.db.close()
            except Exception:
                pass
        if os.path.exists(self.temp_db_path):
            try:
                os.remove(self.temp_db_path)
            except Exception:
                pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_app(self):
        from gui import ImageCullerApp
        from culler.folder_watcher import FolderChange

        app = MagicMock()
        app.db = self.db
        app.tabs = []
        app.active_tab_index = -1
        app.current_items = []
        app.current_index = -1
        app.selected_indices = set()
        app.selection_anchor_idx = 0
        app.toolbar = MagicMock()
        app.thumb_list = MagicMock()
        app.viewer = MagicMock()
        app.meta_panel = MagicMock()
        app.tab_bar = MagicMock()
        app.folder_watcher = FolderWatcher(settle_seconds=0.0, write_grace_seconds=0.0)
        app.folder_watcher.start()
        app.addCleanup(app.folder_watcher.stop, 0.1)
        self.change_cls = FolderChange
        app._update_status = MagicMock()
        app._load_directory = MagicMock()
        app._load_tab_directory = MagicMock()
        app._get_active_tab = lambda: ImageCullerApp._get_active_tab(app)
        app._watch_tab_directory = lambda tab: ImageCullerApp._watch_tab_directory(app, tab)
        app._on_folder_changed = lambda tab, ch: ImageCullerApp._on_folder_changed(app, tab, ch)
        app._suppress_folder_watch = lambda d, s=None: ImageCullerApp._suppress_folder_watch(app, d, s)
        app._unwatch_tab_directory = lambda tab, d=None: ImageCullerApp._unwatch_tab_directory(app, tab, d)
        app._close_tab = lambda index: ImageCullerApp._close_tab(app, index)
        app._create_tab_info = lambda directory, *a, **kw: ImageCullerApp._create_tab_info(app, directory, *a, **kw)
        return app

    def _make_tab(self, app, name="A"):
        tab_info = app._create_tab_info(str(self.directory))
        tab_info["session"].directory = self.directory
        tab_info["is_loaded"] = True
        tab_info["tab_label"] = name
        app.tabs.append(tab_info)
        return tab_info

    def test_watch_tab_directory_registers_watch(self):
        app = self._make_app()
        tab = self._make_tab(app)

        app._watch_tab_directory(tab)

        self.assertTrue(app.folder_watcher.is_watching(self.directory))

    def test_watch_tab_directory_ignores_missing_folder(self):
        app = self._make_app()
        tab = self._make_tab(app)
        tab["session"].directory = self.directory / "missing"

        app._watch_tab_directory(tab)

        self.assertEqual(len(app.folder_watcher.watched_directories()), 0)

    def test_folder_change_reloads_active_tab(self):
        app = self._make_app()
        tab = self._make_tab(app)
        app.active_tab_index = 0

        change = self.change_cls(directory=self.directory, removed=("A.JPG", "B.JPG"))
        app._on_folder_changed(tab, change)

        app._load_directory.assert_called_once_with(str(self.directory))
        app._load_tab_directory.assert_not_called()
        app._update_status.assert_called_once()
        self.assertIn("-2 removed", app._update_status.call_args[0][0])

    def test_folder_change_reloads_background_tab_quietly(self):
        app = self._make_app()
        active = self._make_tab(app, "A")
        background = self._make_tab(app, "B")
        app.active_tab_index = 0

        change = self.change_cls(directory=self.directory, added=("C.JPG",))
        app._on_folder_changed(background, change)

        app._load_tab_directory.assert_called_once_with(background, show_progress=False)
        app._load_directory.assert_not_called()

    def test_folder_change_ignored_while_tab_loading(self):
        app = self._make_app()
        tab = self._make_tab(app)
        app.active_tab_index = 0
        tab["loading"] = True

        app._watch_tab_directory(tab)
        change = self.change_cls(directory=self.directory, added=("C.JPG",))
        app._on_folder_changed(tab, change)

        app._load_directory.assert_not_called()
        app._load_tab_directory.assert_not_called()

    def test_folder_change_for_closed_tab_unwatches(self):
        app = self._make_app()
        tab = self._make_tab(app)
        app.active_tab_index = 0
        app._watch_tab_directory(tab)
        app.tabs.remove(tab)

        change = self.change_cls(directory=self.directory, removed=("A.JPG",))
        app._on_folder_changed(tab, change)

        self.assertFalse(app.folder_watcher.is_watching(self.directory))
        app._load_directory.assert_not_called()

    def test_folder_change_for_stale_directory_unwatches(self):
        app = self._make_app()
        tab = self._make_tab(app)
        app.active_tab_index = 0
        app._watch_tab_directory(tab)
        tab["directory"] = str(self.directory / "elsewhere")

        change = self.change_cls(directory=self.directory, removed=("A.JPG",))
        app._on_folder_changed(tab, change)

        self.assertFalse(app.folder_watcher.is_watching(self.directory))
        app._load_directory.assert_not_called()

    def test_close_tab_unwatches_directory(self):
        app = self._make_app()
        other_dir = Path(tempfile.mkdtemp(dir=self.temp_dir))
        first = self._make_tab(app, "A")
        second = self._make_tab(app, "B")
        second["session"].directory = other_dir
        app.active_tab_index = 1
        app._watch_tab_directory(first)
        app._watch_tab_directory(second)

        app._close_tab(0)

        self.assertFalse(app.folder_watcher.is_watching(self.directory))
        self.assertTrue(app.folder_watcher.is_watching(other_dir))

    def test_close_tab_keeps_watch_when_another_tab_shares_directory(self):
        app = self._make_app()
        first = self._make_tab(app, "A")
        second = self._make_tab(app, "B")
        app.active_tab_index = 1
        app._watch_tab_directory(first)
        app._watch_tab_directory(second)

        app._close_tab(0)

        self.assertTrue(app.folder_watcher.is_watching(self.directory))

    def test_external_change_is_marshalled_to_gui_thread(self):
        app = self._make_app()
        tab = self._make_tab(app)
        app.active_tab_index = 0
        app._watch_tab_directory(tab)

        (self.directory / "A.JPG").write_bytes(b"0")
        app.folder_watcher.poll_now()
        app.folder_watcher.poll_now()

        self.assertEqual(app.after.call_count, 1)
        self.assertEqual(app.after.call_args[0][0], 0)

    def test_suppress_folder_watch_prevents_reload(self):
        app = self._make_app()
        tab = self._make_tab(app)
        app.active_tab_index = 0
        app._watch_tab_directory(tab)

        (self.directory / "A.JPG").write_bytes(b"0")
        app.folder_watcher.poll_now()
        app.folder_watcher.poll_now()
        self.assertEqual(app.after.call_count, 1, "unsuppressed change must be reported")

        app._suppress_folder_watch(self.directory, 60.0)
        (self.directory / "B.JPG").write_bytes(b"0")
        app.folder_watcher.poll_now()
        app.folder_watcher.poll_now()

        self.assertEqual(app.after.call_count, 1, "suppressed change must be adopted silently")
        app._load_directory.assert_not_called()

        app.folder_watcher.resync(self.directory)
        (self.directory / "C.JPG").write_bytes(b"0")
        app.folder_watcher.poll_now()
        app.folder_watcher.poll_now()
        self.assertEqual(app.after.call_count, 2, "resync must re-enable detection")


class TestTabBarDynamicIndex(unittest.TestCase):
    """
    Unit tests for TabBar dynamic index lookups after tab removal and reordering.
    """

    def test_close_btn_click_resolves_correct_index_after_removal(self):
        from culler.gui.tab_bar import TabBar
        closed_indices = []

        bar = TabBar.__new__(TabBar)
        bar.on_tab_closed = lambda idx: closed_indices.append(idx)
        bar._tab_buttons = [MagicMock(), MagicMock(), MagicMock()]
        bar._close_buttons = [MagicMock(), MagicMock(), MagicMock()]
        bar._tab_labels = ["Tab 0", "Tab 1", "Tab 2"]
        bar._tab_count = 3
        bar._active_index = 0
        bar._update_scroll_region = MagicMock()

        btn0, btn1, btn2 = bar._close_buttons[0], bar._close_buttons[1], bar._close_buttons[2]

        # Close middle tab (index 1)
        bar._handle_close_btn_click(btn1)
        self.assertEqual(closed_indices, [1])

        # Remove tab 1
        bar.remove_tab(1)
        self.assertEqual(bar._tab_count, 2)
        self.assertEqual(bar._close_buttons, [btn0, btn2])

        # Click close on what was originally btn2 (now at list index 1)
        closed_indices.clear()
        bar._handle_close_btn_click(btn2)
        self.assertEqual(closed_indices, [1], "Clicking close on btn2 should resolve to new index 1, not old index 2")

    def test_get_index_for_widget_after_reorder(self):
        from culler.gui.tab_bar import TabBar

        bar = TabBar.__new__(TabBar)
        btnA, btnB, btnC = MagicMock(), MagicMock(), MagicMock()
        bar._tab_buttons = [btnA, btnB, btnC]
        bar._close_buttons = [MagicMock(), MagicMock(), MagicMock()]
        bar._tab_labels = ["A", "B", "C"]
        bar._tab_count = 3
        bar._active_index = 0
        bar._update_scroll_region = MagicMock()

        bar.reorder(0, 2)  # Move A to position 2: [B, C, A]
        self.assertEqual(bar._get_index_for_widget(btnA), 2)
        self.assertEqual(bar._get_index_for_widget(btnB), 0)
        self.assertEqual(bar._get_index_for_widget(btnC), 1)


if __name__ == "__main__":
    unittest.main()
