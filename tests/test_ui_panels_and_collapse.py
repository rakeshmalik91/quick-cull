import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk

from culler.db_manager import DatabaseManager
from culler.gui.metadata_panel import MetadataPanel, BAG_KEYS
from culler.gui.toolbar import HeaderToolbar


_ROOT = None


def setUpModule():
    global _ROOT
    ctk.set_appearance_mode("dark")
    _ROOT = ctk.CTk()
    _ROOT.geometry("1100x700+30+30")
    _ROOT.update()


def tearDownModule():
    try:
        _ROOT.destroy()
    except Exception:
        pass


def _dummy_callbacks():
    return dict(
        on_set_flag=MagicMock(),
        on_set_rating=MagicMock(),
        on_toggle_tag=MagicMock(),
        on_unflag_all=MagicMock(),
        on_untag_all=MagicMock(),
        on_unrate_all=MagicMock(),
        on_clear_all=MagicMock(),
        on_crop=MagicMock(),
        on_annotate=MagicMock(),
        on_convert_jpg=MagicMock(),
        on_move_picked=MagicMock(),
        on_move_rejected=MagicMock(),
        on_config_output_folders=MagicMock(),
    )


class TestCollapsibleMetadataSections(unittest.TestCase):
    def setUp(self):
        self.panel = MetadataPanel(_ROOT, **_dummy_callbacks())
        self.panel.pack(side="top", fill="x")
        _ROOT.update()

    def tearDown(self):
        try:
            self.panel.destroy()
        except Exception:
            pass
        _ROOT.update()

    def test_all_bags_have_collapse_buttons(self):
        for key in BAG_KEYS:
            self.assertIn(key, self.panel._bag_collapse_btns)
            btn = self.panel._bag_collapse_btns[key]
            self.assertEqual(btn.cget("text"), "▼")
            self.assertFalse(self.panel.is_bag_collapsed(key))

    def test_toggle_bag_collapse(self):
        # Collapse action section
        self.panel.toggle_bag_collapse("action")
        _ROOT.update()

        self.assertTrue(self.panel.is_bag_collapsed("action"))
        self.assertEqual(self.panel._bag_collapse_btns["action"].cget("text"), "▶")
        self.assertEqual(self.panel.action_content.winfo_ismapped(), 0)

        # Expand action section
        self.panel.toggle_bag_collapse("action")
        _ROOT.update()

        self.assertFalse(self.panel.is_bag_collapsed("action"))
        self.assertEqual(self.panel._bag_collapse_btns["action"].cget("text"), "▼")
        self.assertEqual(self.panel.action_content.winfo_ismapped(), 1)

    def test_initial_collapsed_states(self):
        panel2 = MetadataPanel(
            _ROOT,
            **_dummy_callbacks(),
            collapsed_states={"meta": True, "action": True}
        )
        panel2.pack(side="top", fill="x")
        _ROOT.update()
        try:
            self.assertTrue(panel2.is_bag_collapsed("meta"))
            self.assertTrue(panel2.is_bag_collapsed("action"))
            self.assertFalse(panel2.is_bag_collapsed("move"))
            self.assertEqual(panel2._bag_collapse_btns["meta"].cget("text"), "▶")
            self.assertEqual(panel2._bag_collapse_btns["move"].cget("text"), "▼")
        finally:
            panel2.destroy()

    def test_collapse_callback_fires(self):
        reported = []
        self.panel.on_bag_collapse_changed = lambda states: reported.append(dict(states))

        self.panel.set_bag_collapsed("rating", True)
        self.assertEqual(len(reported), 1)
        self.assertTrue(reported[0]["rating"])


class TestUIStateDatabasePersistence(unittest.TestCase):
    def setUp(self):
        import tempfile
        import os
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.temp_db_fd)
        self.db = DatabaseManager(db_path=self.temp_db_path)

    def tearDown(self):
        import os
        if hasattr(self, "db") and self.db:
            try:
                self.db.close()
            except Exception:
                pass
        if hasattr(self, "temp_db_path") and os.path.exists(self.temp_db_path):
            try:
                os.remove(self.temp_db_path)
            except Exception:
                pass

    def test_panel_visibility_persistence(self):
        # Default is True, True
        thumbs, tools = self.db.get_ui_panels_visible()
        self.assertTrue(thumbs)
        self.assertTrue(tools)

        self.db.set_ui_panels_visible(False, True)
        thumbs, tools = self.db.get_ui_panels_visible()
        self.assertFalse(thumbs)
        self.assertTrue(tools)

    def test_panel_width_persistence(self):
        tw, mw = self.db.get_ui_panels_width()
        self.assertEqual((tw, mw), (340, 290))

        self.db.set_ui_panels_width(400, 320)
        tw, mw = self.db.get_ui_panels_width()
        self.assertEqual((tw, mw), (400, 320))

    def test_menubar_visibility_persistence(self):
        self.assertTrue(self.db.get_ui_menubar_visible())
        self.db.set_ui_menubar_visible(False)
        self.assertFalse(self.db.get_ui_menubar_visible())

    def test_meta_panel_collapsed_persistence(self):
        self.assertEqual(self.db.get_meta_panel_collapsed(), {})
        states = {"action": True, "meta": True, "move": False}
        self.db.set_meta_panel_collapsed(states)
        self.assertEqual(self.db.get_meta_panel_collapsed(), states)


class TestHeaderToolbarPanelToggles(unittest.TestCase):
    def test_toolbar_panel_buttons_and_sync(self):
        mock_thumbs = MagicMock()
        mock_tools = MagicMock()
        tb = HeaderToolbar(
            _ROOT,
            on_toggle_thumbs=mock_thumbs,
            on_toggle_tools=mock_tools,
        )
        tb.pack(side="top", fill="x")
        _ROOT.update()
        try:
            self.assertTrue(hasattr(tb, "btn_toggle_thumbs"))
            self.assertTrue(hasattr(tb, "btn_toggle_tools"))
            self.assertEqual(tb.btn_toggle_thumbs.cget("text"), "◀ 🎞️")
            self.assertEqual(tb.btn_toggle_tools.cget("text"), "🛠️ ▶")

            tb.btn_toggle_thumbs.invoke()
            mock_thumbs.assert_called_once()

            tb.btn_toggle_tools.invoke()
            mock_tools.assert_called_once()

            # State sync
            tb.set_panel_visibility_state(thumbs_visible=False, tools_visible=False)
            self.assertEqual(tb.btn_toggle_thumbs.cget("text"), "▶ 🎞️")
            self.assertEqual(tb.btn_toggle_tools.cget("text"), "🛠️ ◀")
        finally:
            tb.destroy()


if __name__ == "__main__":
    unittest.main()
