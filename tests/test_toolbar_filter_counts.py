import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk

from culler.gui.toolbar import HeaderToolbar
from culler.culler_engine import ImageItem, FlagState, CullingSession
from gui import ImageCullerApp

_ROOT = None


def setUpModule():
    global _ROOT
    ctk.set_appearance_mode("dark")
    _ROOT = ctk.CTk()
    _ROOT.geometry("1100x700+30+30")
    _ROOT.update()


def tearDownModule():
    global _ROOT
    if _ROOT is not None:
        try:
            _ROOT.destroy()
        except Exception:
            pass
        _ROOT = None


class TestToolbarFilterCounts(unittest.TestCase):
    def setUp(self):
        self.toolbar = HeaderToolbar(_ROOT)
        self.toolbar.pack()
        _ROOT.update()

    def tearDown(self):
        self.toolbar.destroy()

    def test_default_values_no_counts(self):
        # Default state before any folder or counts loaded
        values = getattr(self.toolbar.seg_filter, "_value_list", [])
        self.assertEqual(values, ["All", "Pick", "Reject", "Unflagged"])
        self.assertEqual(self.toolbar.seg_filter.get(), "All")
        self.assertEqual(self.toolbar.get_flag_filter(), "All")

    def test_update_filter_counts_only_pick_and_reject(self):
        # Counts provided: should format Pick and Reject only
        counts = {"All": 100, "Pick": 25, "Reject": 8, "Unflagged": 67}
        self.toolbar.update_filter_counts(counts)

        values = getattr(self.toolbar.seg_filter, "_value_list", [])
        self.assertEqual(values, ["All", "Pick (25)", "Reject (8)", "Unflagged"])

        # Flag retrieval should still return clean base flag
        self.assertEqual(self.toolbar.seg_filter.get(), "All")
        self.assertEqual(self.toolbar.get_flag_filter(), "All")
        self.assertEqual(self.toolbar.get_filter_values()["flag"], "All")

    def test_setting_and_getting_flag_with_counts(self):
        counts = {"All": 50, "Pick": 10, "Reject": 4, "Unflagged": 36}
        self.toolbar.update_filter_counts(counts)

        # Setting "Pick" should select the "Pick (10)" button
        self.toolbar.set_flag_filter("Pick")
        self.assertEqual(self.toolbar.seg_filter.get(), "Pick")
        self.assertEqual(self.toolbar.seg_filter._current_value, "Pick (10)")

        # Setting "Reject" via seg_filter.set directly
        self.toolbar.seg_filter.set("Reject")
        self.assertEqual(self.toolbar.seg_filter.get(), "Reject")
        self.assertEqual(self.toolbar.seg_filter._current_value, "Reject (4)")

        # Setting "Unflagged"
        self.toolbar.seg_filter.set("Unflagged")
        self.assertEqual(self.toolbar.seg_filter.get(), "Unflagged")
        self.assertEqual(self.toolbar.seg_filter._current_value, "Unflagged")

    def test_update_counts_preserves_active_selection(self):
        self.toolbar.seg_filter.set("Pick")
        self.assertEqual(self.toolbar.seg_filter.get(), "Pick")

        # Counts update while "Pick" is selected
        self.toolbar.update_filter_counts({"Pick": 15, "Reject": 3})
        self.assertEqual(self.toolbar.seg_filter.get(), "Pick")
        self.assertEqual(self.toolbar.seg_filter._current_value, "Pick (15)")

        # Second counts update
        self.toolbar.update_filter_counts({"Pick": 16, "Reject": 2})
        self.assertEqual(self.toolbar.seg_filter.get(), "Pick")
        self.assertEqual(self.toolbar.seg_filter._current_value, "Pick (16)")

    def test_reset_counts_to_none(self):
        self.toolbar.update_filter_counts({"Pick": 5, "Reject": 2})
        self.assertEqual(getattr(self.toolbar.seg_filter, "_value_list", []), ["All", "Pick (5)", "Reject (2)", "Unflagged"])

        # Resetting to None restores clean labels
        self.toolbar.update_filter_counts(None)
        self.assertEqual(getattr(self.toolbar.seg_filter, "_value_list", []), ["All", "Pick", "Reject", "Unflagged"])


class TestComputeFlagCounts(unittest.TestCase):
    def test_compute_flag_counts_distribution(self):
        item1 = ImageItem(Path("a.jpg"))
        item1.flag = FlagState.PICK

        item2 = ImageItem(Path("b.jpg"))
        item2.flag = FlagState.PICK

        item3 = ImageItem(Path("c.jpg"))
        item3.flag = FlagState.REJECT

        item4 = ImageItem(Path("d.jpg"))
        item4.flag = FlagState.UNFLAGGED

        session = MagicMock(spec=CullingSession)
        session.items = [item1, item2, item3, item4]

        counts = ImageCullerApp._compute_flag_counts(session)
        self.assertEqual(counts["All"], 4)
        self.assertEqual(counts["Pick"], 2)
        self.assertEqual(counts["Reject"], 1)
        self.assertEqual(counts["Unflagged"], 1)

    def test_compute_flag_counts_empty_or_none(self):
        self.assertEqual(
            ImageCullerApp._compute_flag_counts(None),
            {"All": 0, "Pick": 0, "Reject": 0, "Unflagged": 0}
        )

        session = MagicMock(spec=CullingSession)
        session.items = []
        self.assertEqual(
            ImageCullerApp._compute_flag_counts(session),
            {"All": 0, "Pick": 0, "Reject": 0, "Unflagged": 0}
        )


class TestAppToolbarCountsIntegration(unittest.TestCase):
    def test_flag_change_updates_toolbar_filter_counts(self):
        item1 = ImageItem(Path("test1.jpg"))
        item1.flag = FlagState.UNFLAGGED

        item2 = ImageItem(Path("test2.jpg"))
        item2.flag = FlagState.UNFLAGGED

        session = MagicMock(spec=CullingSession)
        session.items = [item1, item2]

        app = MagicMock()
        app._get_active_session.return_value = session
        app.toolbar.get_filter_values.return_value = {
            "flag": "All", "rating": [], "format": "All", "tag": []
        }
        app._update_toolbar_filter_counts = lambda: ImageCullerApp._update_toolbar_filter_counts(app)

        # 1. Update counts via _update_toolbar_filter_counts
        ImageCullerApp._update_toolbar_filter_counts(app)
        app.toolbar.update_filter_counts.assert_called_with({
            "All": 2, "Pick": 0, "Reject": 0, "Unflagged": 2
        })

        # 2. Flag first photo as PICK
        app.current_items = [item1, item2]
        app.current_index = 0
        app.selected_indices = {0}
        ImageCullerApp._set_current_flag(app, FlagState.PICK)
        self.assertEqual(item1.flag, FlagState.PICK)
        app.toolbar.update_filter_counts.assert_called_with({
            "All": 2, "Pick": 1, "Reject": 0, "Unflagged": 1
        })

        # 3. Unpick first photo
        ImageCullerApp._on_unpick_current(app)
        self.assertEqual(item1.flag, FlagState.UNFLAGGED)
        app.toolbar.update_filter_counts.assert_called_with({
            "All": 2, "Pick": 0, "Reject": 0, "Unflagged": 2
        })

        # 4. Flag second photo as REJECT
        app.current_index = 1
        app.selected_indices = {1}
        ImageCullerApp._set_current_flag(app, FlagState.REJECT)
        self.assertEqual(item2.flag, FlagState.REJECT)
        app.toolbar.update_filter_counts.assert_called_with({
            "All": 2, "Pick": 0, "Reject": 1, "Unflagged": 1
        })

        # 5. Unreject second photo
        ImageCullerApp._on_unreject_current(app)
        self.assertEqual(item2.flag, FlagState.UNFLAGGED)
        app.toolbar.update_filter_counts.assert_called_with({
            "All": 2, "Pick": 0, "Reject": 0, "Unflagged": 2
        })


if __name__ == "__main__":
    unittest.main()
