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


class TestCullingEngine(unittest.TestCase):
    """
    Automated Unit Test Suite for CullingSession & ImageItem engine logic.
    """

    def setUp(self):
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.temp_db_fd)
        self.db = DatabaseManager(db_path=self.temp_db_path)
        self.session = CullingSession(db_manager=self.db)

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

    def test_extract_base_stem(self):
        """
        Verify extracting root photo stems across RAW, JPG, and edited variant patterns.
        """
        self.assertEqual(CullingSession.extract_base_stem("PORTRAIT_1"), "portrait")
        self.assertEqual(CullingSession.extract_base_stem("SUNSET_crop"), "sunset")
        self.assertEqual(CullingSession.extract_base_stem("LANDSCAPE_edited"), "landscape")
        self.assertEqual(CullingSession.extract_base_stem("PHOTO-Edit"), "photo")

    def test_item_flag_rating_tag_mutations(self):
        """
        Verify setting flags, star ratings, and tags on ImageItem objects.
        """
        p = Path("D:/Photos/DSC_0001.JPG")
        item = ImageItem(p)

        item.flag = FlagState.PICK
        item.rating = 4
        item.add_tag("landscape")
        item.add_tag("favorite")

        self.assertEqual(item.flag, FlagState.PICK)
        self.assertEqual(item.rating, 4)
        self.assertTrue(item.has_tag("landscape"))
        self.assertTrue(item.has_tag("favorite"))
        self.assertEqual(item.tags_str, "Favorite, Landscape")

        item.remove_tag("landscape")
        self.assertFalse(item.has_tag("landscape"))
        self.assertEqual(item.tags_str, "Favorite")

    def test_filtering_items_by_flag_rating_format(self):
        """
        Verify get_filtered_items filtering by flag (Pick/Reject/Unflagged), rating (1-5 stars), and format (.ARW/.JPG).
        """
        item1 = ImageItem(Path("D:/Photos/DSC_0001.ARW"))
        item1.flag = FlagState.PICK
        item1.rating = 5

        item2 = ImageItem(Path("D:/Photos/DSC_0002.JPG"))
        item2.flag = FlagState.REJECT
        item2.rating = 2

        item3 = ImageItem(Path("D:/Photos/DSC_0003.JPG"))
        item3.flag = FlagState.UNFLAGGED
        item3.rating = 0
        item3.add_tag("Blur")

        self.session.items = [item1, item2, item3]

        # Filter Pick
        picks = self.session.get_filtered_items(flag_filter="Pick")
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].path.name, "DSC_0001.ARW")

        # Filter Rating >= 3
        rated = self.session.get_filtered_items(rating_filter=3)
        self.assertEqual(len(rated), 1)
        self.assertEqual(rated[0].rating, 5)

        # Filter Format .ARW
        raws = self.session.get_filtered_items(format_filter=".ARW")
        self.assertEqual(len(raws), 1)
        self.assertEqual(raws[0].path.name, "DSC_0001.ARW")

        # Filter Tag Blur
        blurred = self.session.get_filtered_items(tag_filter="Blur")
        self.assertEqual(len(blurred), 1)
        self.assertEqual(blurred[0].path.name, "DSC_0003.JPG")

    def test_unflag_unrate_untag_all(self):
        """
        Verify unflag_all_items, unrate_all_items, and untag_all_items batch operations.
        """
        item1 = ImageItem(Path("D:/Photos/DSC_0001.JPG"))
        item1.flag = FlagState.PICK
        item1.rating = 4
        item1.add_tag("portrait")

        item2 = ImageItem(Path("D:/Photos/DSC_0002.JPG"))
        item2.flag = FlagState.REJECT
        item2.rating = 5
        item2.add_tag("action")

        self.session.items = [item1, item2]

        # Unflag all
        unflagged_count = self.session.unflag_all_items()
        self.assertEqual(unflagged_count, 2)
        self.assertEqual(item1.flag, FlagState.UNFLAGGED)
        self.assertEqual(item2.flag, FlagState.UNFLAGGED)

        # Unrate all
        unrated_count = self.session.unrate_all_items()
        self.assertEqual(unrated_count, 2)
        self.assertEqual(item1.rating, 0)
        self.assertEqual(item2.rating, 0)

        # Untag all
        untagged_count = self.session.untag_all_items()
        self.assertEqual(untagged_count, 2)
        self.assertFalse(item1.tags)
        self.assertFalse(item2.tags)

    @patch("send2trash.send2trash")
    def test_move_items_to_trash(self, mock_send2trash):
        """
        Verify move_items_to_trash invokes send2trash and removes items from session.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            f1 = tmp_path / "TEST_001.JPG"
            f2 = tmp_path / "TEST_001.ARW"
            f1.touch()
            f2.touch()

            item = ImageItem(f1)
            item.stacked_paths = [f1, f2]
            item.is_stacked = True
            self.session.items = [item]

            moved_count = self.session.move_items_to_trash([item])
            self.assertEqual(moved_count, 2)
            self.assertEqual(mock_send2trash.call_count, 2)
            self.assertEqual(len(self.session.items), 0)

    @patch("send2trash.send2trash")
    def test_delete_only_jpg_item(self, mock_send2trash):
        """
        Verify that selecting only a standalone JPG photo and moving to trash works cleanly.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            f_jpg = tmp_path / "PHOTO_ONLY.JPG"
            f_jpg.touch()

            jpg_item = ImageItem(f_jpg)
            self.session.items = [jpg_item]

            moved_count = self.session.move_items_to_trash([jpg_item])
            self.assertEqual(moved_count, 1)
            self.assertEqual(mock_send2trash.call_count, 1)
            self.assertEqual(len(self.session.items), 0)

    @patch("send2trash.send2trash")
    def test_delete_stacked_jpg_only_preserves_raw(self, mock_send2trash):
        """
        Verify that specifying format_filter='.jpg' deletes ONLY the JPG file from a stacked pair while preserving the ARW RAW file.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            f1 = tmp_path / "PAIR_001.ARW"
            f2 = tmp_path / "PAIR_001.JPG"
            f1.touch()
            f2.touch()

            item = ImageItem(f1)
            item.stacked_paths = [f1, f2]
            item.is_stacked = True
            self.session.items = [item]

            moved_count = self.session.move_items_to_trash([item], format_filter="JPG")
            self.assertEqual(moved_count, 1)
            self.assertEqual(mock_send2trash.call_count, 1)
            mock_send2trash.assert_called_once_with(str(f2))
            self.assertEqual(len(self.session.items), 1)
            self.assertEqual(self.session.items[0].path, f1)
            self.assertFalse(self.session.items[0].is_stacked)

    def test_detector_config_loader(self):
        """
        Verify that get_blur_methods_config and get_duplicate_methods_config load algorithm configs from JSON correctly.
        """
        from culler.detectors.config_loader import get_blur_methods_config, get_duplicate_methods_config
        blur_cfg = get_blur_methods_config()
        dup_cfg = get_duplicate_methods_config()

        self.assertIn("ai_subject", blur_cfg)
        self.assertIn("laplacian", blur_cfg)
        self.assertIn("dhash", dup_cfg)
        self.assertEqual(len(blur_cfg), 3)
        self.assertEqual(len(dup_cfg), 3)

    def test_clear_all_metadata(self):
        """
        Verify that clear_all_metadata resets flags, tags, ratings, detection boxes, and eye boxes across all items.
        """
        item = ImageItem(Path("D:/Photos/IMG_001.JPG"))
        item.flag = FlagState.PICK
        item.rating = 5
        item.add_tag("Blur")
        item.detection_box = (0.1, 0.2, 0.8, 0.9)
        item.eye_box = (0.2, 0.25, 0.7, 0.5)
        self.session.items = [item]

        count = self.session.clear_all_metadata()
        self.assertEqual(count, 1)
        self.assertEqual(item.flag, FlagState.UNFLAGGED)
        self.assertEqual(item.rating, 0)
        self.assertEqual(len(item.tags), 0)
        self.assertIsNone(item.detection_box)
        self.assertIsNone(item.eye_box)

    def test_detection_box_db_persistence(self):
        """
        Verify detection_box and eye_box are persisted to and restored from the database.
        """
        dir_path = Path("D:/Photos/BoxTest").resolve()
        file_path = str(dir_path / "IMG_002.JPG")
        box = (0.15, 0.25, 0.85, 0.95)
        eye_box = (0.25, 0.30, 0.75, 0.55)
        self.db.save_image_record(
            file_path=file_path,
            filename="IMG_002.JPG",
            flag="unflagged",
            rating=0,
            sharpness=0.0,
            tags="",
            detection_box=box,
            eye_box=eye_box
        )

        records = self.db.get_all_records_for_dir(str(dir_path))
        self.assertIn(file_path, records)
        self.assertIsNotNone(records[file_path]["detection_box"])
        self.assertIsNotNone(records[file_path]["eye_box"])
        restored_box = records[file_path]["detection_box"]
        restored_eye = records[file_path]["eye_box"]
        self.assertAlmostEqual(restored_box[0], 0.15, places=4)
        self.assertAlmostEqual(restored_eye[0], 0.25, places=4)

    def test_detection_box_cleared_in_db(self):
        """
        Verify that saving an item with detection_box=None and eye_box=None clears them in the DB.
        """
        dir_path = Path("D:/Photos/BoxClear").resolve()
        file_path = str(dir_path / "IMG_003.JPG")
        box = (0.1, 0.2, 0.8, 0.9)
        eye_box = (0.2, 0.25, 0.7, 0.5)
        self.db.save_image_record(
            file_path=file_path,
            filename="IMG_003.JPG",
            flag="unflagged",
            detection_box=box,
            eye_box=eye_box
        )

        # Now clear it
        self.db.save_image_record(
            file_path=file_path,
            filename="IMG_003.JPG",
            flag="unflagged",
            detection_box=None,
            eye_box=None
        )

        records = self.db.get_all_records_for_dir(str(dir_path))
        self.assertIn(file_path, records)
        self.assertIsNone(records[file_path]["detection_box"])
        self.assertIsNone(records[file_path]["eye_box"])

    @patch("culler.culler_engine.load_manual_annotations")
    def test_manual_bounding_box_loaded_from_dataset(self, mock_load_annotations):
        """
        Verify that manual bounding boxes are loaded from _DATASET/annotations.json instead of SQLite DB.
        """
        photo_dir = Path(tempfile.mkdtemp())
        photo_path = photo_dir / "PHOTO_001.JPG"
        photo_path.touch()

        mock_load_annotations.return_value = {
            str(photo_path.resolve()): {
                "manual_detection_box": (0.1, 0.2, 0.6, 0.7),
                "manual_eye_box": (0.3, 0.4, 0.5, 0.55),
                "filename": "PHOTO_001.JPG"
            }
        }

        try:
            items = self.session.scan_directory(photo_dir)
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item.manual_detection_box, (0.1, 0.2, 0.6, 0.7))
            self.assertEqual(item.manual_eye_box, (0.3, 0.4, 0.5, 0.55))
        finally:
            shutil.rmtree(photo_dir, ignore_errors=True)

    @patch.object(CullingSession, "compute_sharpness_scores")
    def test_scan_for_blur_sensitivity_one_percent(self, mock_compute_scores):
        """
        Verify scan_for_blur handles 1% sensitivity cutoff cleanly.
        """
        items = []
        for i in range(100):
            item = ImageItem(Path(f"D:/Photos/IMG_{i:03d}.JPG"))
            item.sharpness_score = float(i + 1)
            items.append(item)

        self.session.items = items
        flagged = self.session.scan_for_blur(bottom_percentile=1.0, method="laplacian")

        # 1% of 100 items is exactly 1 item (the lowest sharpness score 1.0)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].path.name, "IMG_000.JPG")
        self.assertEqual(flagged[0].flag, FlagState.REJECT)
        self.assertTrue(flagged[0].has_tag("Blur"))

    @patch.object(CullingSession, "compute_sharpness_scores")
    def test_scan_for_blur_safe_mode_preserves_unique_blurry_photo(self, mock_compute_scores):
        """
        Verify safe_mode=True preserves unique blurry photos if no sharper duplicate exists,
        and only rejects blurry photos that have a sharper duplicate.
        """
        # Create 3 items:
        # Item 1: Blurry photo, dhash=0x1111 (unique capture, no duplicate)
        item1 = ImageItem(Path("D:/Photos/UniqueBlurry.JPG"))
        item1.sharpness_score = 10.0
        item1.dhash = 0x1111111111111111

        # Item 2: Blurry photo, dhash=0xAAAA (has duplicate Item 3)
        item2 = ImageItem(Path("D:/Photos/DupBlurry.JPG"))
        item2.sharpness_score = 20.0
        item2.dhash = 0xAAAAAAAAAAAAAAAA

        # Item 3: Sharp photo, dhash=0xAAAA (sharper duplicate of Item 2)
        item3 = ImageItem(Path("D:/Photos/DupSharp.JPG"))
        item3.sharpness_score = 500.0
        item3.dhash = 0xAAAAAAAAAAAAAAAA

        self.session.items = [item1, item2, item3]

        # In safe_mode=True with 67% cutoff (bottom 2 items = item1 & item2):
        # item1 (unique) should be preserved (not rejected).
        # item2 (has sharper dup item3) should be rejected.
        flagged = self.session.scan_for_blur(bottom_percentile=67.0, method="laplacian", safe_mode=True)

        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].path.name, "DupBlurry.JPG")
        self.assertEqual(flagged[0].flag, FlagState.REJECT)
        self.assertTrue(flagged[0].has_tag("Blur"))
        self.assertEqual(item1.flag, FlagState.UNFLAGGED)
        self.assertTrue(item1.has_tag("Blur"))

    def test_scan_directory_discovers_arw_and_placeholder_items(self):
        """
        Verify scan_directory counts ARW files and notifies on_discovered callback with placeholder items.
        """
        temp_dir = tempfile.mkdtemp()
        try:
            # Create a mix of ARW and JPG files
            (Path(temp_dir) / "photo1.arw").write_bytes(b"dummy arw")
            (Path(temp_dir) / "photo2.ARW").write_bytes(b"dummy arw")
            (Path(temp_dir) / "photo3.jpg").write_bytes(b"dummy jpg")

            discovered = []
            arw_counts = []

            def on_discovered(items, arw_count):
                discovered.append(list(items))
                arw_counts.append(arw_count)

            with patch.object(self.session, "_reconcile_items", return_value=[]):
                self.session.scan_directory(temp_dir, on_discovered=on_discovered)

            self.assertEqual(len(discovered), 1)
            self.assertEqual(len(arw_counts), 1)
            self.assertEqual(arw_counts[0], 2)
            self.assertEqual(self.session.arw_count, 2)
            self.assertEqual(len(discovered[0]), 3)
            self.assertEqual(len(self.session.placeholder_items), 3)
            # Verify placeholders are default unstacked size
            self.assertTrue(all(getattr(it, "is_placeholder", False) for it in self.session.placeholder_items))
            self.assertFalse(any(getattr(it, "is_stacked", False) for it in self.session.placeholder_items))
            self.assertTrue(all(len(it.stacked_paths) == 1 for it in self.session.placeholder_items))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_summary_stats_includes_arw_count(self):
        """
        Verify get_summary_stats returns arw_count accurately.
        """
        item1 = ImageItem(Path("D:/Photos/DSC001.ARW"))
        item1.stacked_paths = [Path("D:/Photos/DSC001.ARW")]
        item2 = ImageItem(Path("D:/Photos/DSC002.JPG"))
        item2.stacked_paths = [Path("D:/Photos/DSC002.JPG")]

        self.session.items = [item1, item2]
        self.session.arw_count = 1

        stats = self.session.get_summary_stats()
        self.assertEqual(stats["total_images"], 2)
        self.assertEqual(stats["arw_count"], 1)


if __name__ == "__main__":
    unittest.main()

