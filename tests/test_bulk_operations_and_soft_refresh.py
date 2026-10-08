import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk
from culler.culler_engine import CullingSession, ImageItem, FlagState
from culler.gui.thumbnail_list import ThumbnailList
from culler.image_loader import ImageLoader
from gui import ImageCullerApp

_ROOT = None


def setUpModule():
    global _ROOT
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    _ROOT = ctk.CTk()
    _ROOT.geometry("900x700+30+30")
    _ROOT.update()


def tearDownModule():
    try:
        _ROOT.destroy()
    except Exception:
        pass


class TestBulkSessionOperations(unittest.TestCase):
    def setUp(self):
        self.session = CullingSession()
        self.session.db = MagicMock()
        self.items = [
            ImageItem(Path(f"D:/Photos/IMG_{i:03d}.JPG"))
            for i in range(5)
        ]
        self.session.items = self.items

    def test_unflag_all_items_batching(self):
        self.items[0].flag = FlagState.PICK
        self.items[1].flag = FlagState.REJECT
        self.items[2].flag = FlagState.PICK
        self.items[3].flag = FlagState.UNFLAGGED
        self.items[4].flag = FlagState.UNFLAGGED

        with patch.object(self.session, "save_item_records") as mock_save_records:
            count = self.session.unflag_all_items()
            self.assertEqual(count, 3)
            for item in self.items:
                self.assertEqual(item.flag, FlagState.UNFLAGGED)
            mock_save_records.assert_called_once()
            called_items = mock_save_records.call_args[0][0]
            self.assertEqual(len(called_items), 3)

    def test_untag_all_items_batching(self):
        self.items[0].add_tag("Tag1")
        self.items[1].add_tag("Tag2")
        self.items[2].add_tag("Tag1")

        with patch.object(self.session, "save_item_records") as mock_save_records:
            count = self.session.untag_all_items()
            self.assertEqual(count, 3)
            for item in self.items:
                self.assertEqual(len(item.tags), 0)
            mock_save_records.assert_called_once()
            called_items = mock_save_records.call_args[0][0]
            self.assertEqual(len(called_items), 3)

    def test_unrate_all_items_batching(self):
        self.items[0].rating = 5
        self.items[1].rating = 3
        self.items[2].rating = 0

        with patch.object(self.session, "save_item_records") as mock_save_records:
            count = self.session.unrate_all_items()
            self.assertEqual(count, 2)
            for item in self.items:
                self.assertEqual(item.rating, 0)
            mock_save_records.assert_called_once()
            called_items = mock_save_records.call_args[0][0]
            self.assertEqual(len(called_items), 2)

    def test_clear_all_metadata_batching(self):
        self.items[0].flag = FlagState.PICK
        self.items[1].rating = 4
        self.items[2].add_tag("Test")
        self.items[3].detection_box = (10, 10, 50, 50)
        self.items[4].eye_box = (20, 20, 30, 30)

        with patch.object(self.session, "save_item_records") as mock_save_records:
            count = self.session.clear_all_metadata()
            self.assertEqual(count, 5)
            for item in self.items:
                self.assertEqual(item.flag, FlagState.UNFLAGGED)
                self.assertEqual(item.rating, 0)
                self.assertEqual(len(item.tags), 0)
                self.assertIsNone(item.detection_box)
                self.assertIsNone(item.eye_box)
            mock_save_records.assert_called_once()


class TestThumbnailListSoftRefresh(unittest.TestCase):
    def setUp(self):
        self.root = _ROOT
        self.loader = ImageLoader()
        try:
            self.list_widget = ThumbnailList(
                self.root,
                on_select_image=MagicMock(),
                image_loader=self.loader
            )
            self.list_widget.pack(fill="both", expand=True)
            self.list_widget.update()
        except Exception as e:
            self.skipTest(f"Skipping GUI test: {e}")

    def tearDown(self):
        try:
            self.list_widget.shutdown()
        except Exception:
            pass
        try:
            self.list_widget.destroy()
        except Exception:
            pass
        self.root.update()

    def _make_item(self, name="TEST.JPG", flag=FlagState.UNFLAGGED, rating=0):
        p = Path(f"D:/Photos/{name}")
        item = ImageItem(p)
        item.flag = flag
        item.rating = rating
        return item

    def test_soft_update_updates_flag_indicators(self):
        items = [
            self._make_item(f"IMG_{i:03d}.JPG", flag=FlagState.PICK)
            for i in range(4)
        ]
        self.list_widget.update_items(items, selected_idx=0)
        self.list_widget.update()

        slot0 = self.list_widget.row_pool.slot_for_index(0)
        slot1 = self.list_widget.row_pool.slot_for_index(1)
        self.assertIsNotNone(slot0)
        self.assertIsNotNone(slot1)
        self.assertEqual(slot0["applied"].get("flag"), "#2b9348")  # Pick green
        self.assertEqual(slot1["applied"].get("flag"), "#2b9348")

        # Now simulate unflag all (flags become UNFLAGGED on same items)
        for it in items:
            it.flag = FlagState.UNFLAGGED

        self.list_widget.update_items(items, selected_idx=0)
        self.list_widget.update()

        # Both slot 0 and slot 1 indicators must be gray (#4a4e69)
        self.assertEqual(slot0["applied"].get("flag"), "#4a4e69")
        self.assertEqual(slot1["applied"].get("flag"), "#4a4e69")

    def test_soft_update_updates_star_ratings(self):
        items = [
            self._make_item(f"IMG_{i:03d}.JPG", rating=3)
            for i in range(3)
        ]
        self.list_widget.update_items(items, selected_idx=0)
        self.list_widget.update()

        slot0 = self.list_widget.row_pool.slot_for_index(0)
        self.assertIn("★★★", slot0["applied"].get("text", ""))

        # Reset ratings to 0
        for it in items:
            it.rating = 0

        self.list_widget.update_items(items, selected_idx=0)
        self.list_widget.update()

        self.assertNotIn("★", slot0["applied"].get("text", ""))

    def test_soft_update_clears_stale_multi_selection_outlines(self):
        items = [
            self._make_item(f"IMG_{i:03d}.JPG")
            for i in range(4)
        ]
        self.list_widget.update_items(items, selected_idx=0)
        self.list_widget.update()

        # Select indices {0, 1, 2}
        self.list_widget.set_selected_indices({0, 1, 2}, active_idx=0)
        self.list_widget.update()

        slot1 = self.list_widget.row_pool.slot_for_index(1)
        slot2 = self.list_widget.row_pool.slot_for_index(2)
        self.assertEqual(slot1["applied"].get("border"), ("#ffb703", 2))
        self.assertEqual(slot2["applied"].get("border"), ("#ffb703", 2))
        self.assertTrue(slot1["applied"].get("checked"))
        self.assertTrue(slot2["applied"].get("checked"))

        # Update items with selected_idx=0 (such as switching filter or resetting selection)
        self.list_widget.update_items(items, selected_idx=0)
        self.list_widget.update()

        # Slots 1 and 2 must no longer have yellow border or checked state
        self.assertEqual(slot1["applied"].get("border"), ("#3a3a3a", 1))
        self.assertEqual(slot2["applied"].get("border"), ("#3a3a3a", 1))
        self.assertFalse(slot1["applied"].get("checked"))
        self.assertFalse(slot2["applied"].get("checked"))


class TestGuiBatchOperations(unittest.TestCase):
    def test_set_current_flag_batch_save(self):
        app = MagicMock()
        session = MagicMock()
        app._get_active_session.return_value = session
        items = [ImageItem(Path(f"D:/Photos/IMG_{i:03d}.JPG")) for i in range(5)]
        app.current_items = items
        app.current_index = 0
        app.selected_indices = {1, 2, 3}

        ImageCullerApp._set_current_flag(app, FlagState.PICK)

        self.assertEqual(items[1].flag, FlagState.PICK)
        self.assertEqual(items[2].flag, FlagState.PICK)
        self.assertEqual(items[3].flag, FlagState.PICK)
        session.save_item_records.assert_called_once()
        saved_items = session.save_item_records.call_args[0][0]
        self.assertEqual(len(saved_items), 3)

    def test_set_current_rating_batch_save(self):
        app = MagicMock()
        session = MagicMock()
        app._get_active_session.return_value = session
        items = [ImageItem(Path(f"D:/Photos/IMG_{i:03d}.JPG")) for i in range(5)]
        app.current_items = items
        app.current_index = 0
        app.selected_indices = {0, 4}

        ImageCullerApp._set_current_rating(app, 4)

        self.assertEqual(items[0].rating, 4)
        self.assertEqual(items[4].rating, 4)
        session.save_item_records.assert_called_once()
        saved_items = session.save_item_records.call_args[0][0]
        self.assertEqual(len(saved_items), 2)



class TestPlaceholderReconciliationAndNonBlockingRefresh(unittest.TestCase):
    def test_reconcile_items_clears_is_placeholder_and_sets_is_stacked(self):
        exif = MagicMock()
        exif.get_batch_metadata.return_value = [{"rating": 0}]
        db = MagicMock()
        db.lookup_records_for_paths.return_value = []
        engine = CullingSession(exif_wrapper=exif, db_manager=db)
        raw_p = Path("D:/Photos/IMG_001.ARW")
        jpg_p = Path("D:/Photos/IMG_001.JPG")
        from culler.culler_engine import _RowSpec, FileEntry

        spec = _RowSpec(
            primary=raw_p,
            stacked_paths=[raw_p, jpg_p],
            filename="IMG_001 [Stacked: 1 ARW, 1 JPG]",
            format_name="Stacked (1 ARW, 1 JPG)",
            size_bytes=35000,
            entry=FileEntry(raw_p, 25000, 1000.0)
        )
        donor = ImageItem(raw_p, size_bytes=25000, resolved=True)
        donor.filename = spec.filename
        donor.stacked_paths = [raw_p, jpg_p]
        donor.is_placeholder = True
        donor.is_stacked = False
        donor.flag = FlagState.PICK

        reconciled = engine._reconcile_items([spec], [donor], None, None)
        self.assertEqual(len(reconciled), 1)
        res = reconciled[0]
        self.assertFalse(res.is_placeholder)
        self.assertTrue(res.is_stacked)
        self.assertEqual(res.flag, FlagState.PICK)

    def test_user_actions_during_scan_are_preserved_on_scan_complete(self):
        app = MagicMock()
        session = MagicMock()
        item0 = ImageItem(Path("D:/Photos/IMG_000.JPG"))
        item1 = ImageItem(Path("D:/Photos/IMG_001.JPG"))
        session.items = [item0, item1]
        session.get_summary_stats.return_value = {
            "total_images": 2, "arw_count": 0, "total_size_mb": 10,
            "picked": 1, "rejected": 1, "unflagged": 0
        }

        tab = {
            "session": session,
            "directory": "D:/Photos",
            "current_items": [item0, item1],
            "placeholder_items": [item0, item1],
            "filter_values": {"flag": "All", "rating": [], "format": "All Formats", "tag": []},
            "_user_actions": {
                item0.path: {"flag": FlagState.REJECT, "rating": 2, "tags": {"trash"}},
                item1.path: {"flag": FlagState.PICK, "rating": 5, "tags": {"keeper"}}
            }
        }
        app.tabs = [tab]
        app._get_active_tab.return_value = tab

        # Mock items returned by scan_directory (initially UNFLAGGED before sync)
        new_item0 = ImageItem(Path("D:/Photos/IMG_000.JPG"))
        new_item1 = ImageItem(Path("D:/Photos/IMG_001.JPG"))
        new_item0.flag = FlagState.UNFLAGGED
        new_item1.flag = FlagState.UNFLAGGED
        session.items = [new_item0, new_item1]

        ImageCullerApp._on_tab_scan_complete(app, tab)

        # The completed session.items must have the user's modifications preserved
        self.assertEqual(session.items[0].flag, FlagState.REJECT)
        self.assertEqual(session.items[0].rating, 2)
        self.assertEqual(session.items[0].tags, {"trash"})

        self.assertEqual(session.items[1].flag, FlagState.PICK)
        self.assertEqual(session.items[1].rating, 5)
        self.assertEqual(session.items[1].tags, {"keeper"})

    def test_user_unflag_during_scan_is_preserved_not_reverted(self):
        app = MagicMock()
        session = MagicMock()
        item0 = ImageItem(Path("D:/Photos/IMG_000.JPG"))
        session.items = [item0]
        session.get_summary_stats.return_value = {
            "total_images": 1, "arw_count": 0, "total_size_mb": 5,
            "picked": 0, "rejected": 0, "unflagged": 1
        }

        # Item was originally PICK in DB, but user explicitly unflagged it during scan
        tab = {
            "session": session,
            "directory": "D:/Photos",
            "current_items": [item0],
            "placeholder_items": [item0],
            "filter_values": {"flag": "All", "rating": [], "format": "All Formats", "tag": []},
            "_user_actions": {
                item0.path: {"flag": FlagState.UNFLAGGED, "rating": 0, "tags": set()}
            }
        }
        app.tabs = [tab]
        app._get_active_tab.return_value = tab

        new_item0 = ImageItem(Path("D:/Photos/IMG_000.JPG"))
        new_item0.flag = FlagState.PICK  # Stale flag from old DB record
        session.items = [new_item0]

        ImageCullerApp._on_tab_scan_complete(app, tab)

        # Must preserve UNFLAGGED and NOT revert to stale PICK
        self.assertEqual(session.items[0].flag, FlagState.UNFLAGGED)

    def test_scan_directory_preloads_db_records_onto_placeholders(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            dir_path = Path(tmp_dir)
            f1 = dir_path / "IMG_0001.JPG"
            f1.write_bytes(b"dummy image data")

            db = MagicMock()
            rec = {
                "file_path": str(f1),
                "flag": "PICK",
                "rating": 5,
                "tags": "wedding,favorite",
                "sharpness": 95.0
            }
            db.get_records_for_paths.return_value = {str(f1): rec, str(f1.resolve()): rec}
            exif = MagicMock()
            exif.get_batch_metadata.return_value = [{}]

            engine = CullingSession(exif_wrapper=exif, db_manager=db)

            discovered_items = []
            def on_discovered(items, arw_count):
                discovered_items.extend(items)

            items = engine.scan_directory(
                dir_path,
                stack_raw_jpg=False,
                on_discovered=on_discovered
            )

            # Check that on_discovered received placeholders with DB records already overlaid
            self.assertEqual(len(discovered_items), 1)
            p_item = discovered_items[0]
            self.assertEqual(p_item.flag, FlagState.PICK)
            self.assertEqual(p_item.rating, 5)
            self.assertIn("Wedding", p_item.tags)
            self.assertIn("Favorite", p_item.tags)

    def test_user_flag_immediately_updates_counts_on_shared_session_items(self):
        app = MagicMock()
        session = MagicMock()
        item0 = ImageItem(Path("D:/Photos/IMG_000.JPG"))
        item1 = ImageItem(Path("D:/Photos/IMG_001.JPG"))
        session.items = [item0, item1]
        session.get_filtered_items.return_value = session.items
        app._get_active_session.return_value = session
        app.current_items = session.items
        app.current_index = 0
        app.selected_indices = {0}
        tab = {"_user_actions": {}}
        app._get_active_tab.return_value = tab

        # Initial counts
        counts_before = ImageCullerApp._compute_flag_counts(session=session)
        self.assertEqual(counts_before["Pick"], 0)

        # Flag item 0 as PICK
        ImageCullerApp._set_current_flag(app, FlagState.PICK)

        # Counts must immediately show Pick: 1 because session.items is shared and updated
        counts_after = ImageCullerApp._compute_flag_counts(session=session)
        self.assertEqual(counts_after["Pick"], 1)
        self.assertEqual(item0.flag, FlagState.PICK)


if __name__ == "__main__":
    unittest.main()

