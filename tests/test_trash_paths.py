import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from culler.culler_engine import CullingSession, ImageItem, FlagState
from culler.image_loader import ImageLoader


class TestTrashErrorHandling(unittest.TestCase):
    """
    The trash paths used to raise instead of handling failure:
      - log_error was called but never imported (NameError on every failure path)
      - ImageLoader.is_raw was referenced but never defined (AttributeError for raw filter)
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db = MagicMock()
        self.session = CullingSession(db_manager=self.db)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_item(self, names):
        paths = [Path(self.temp_dir) / n for n in names]
        for p in paths:
            p.write_bytes(b"0" * 32)
        item = ImageItem(paths[0])
        item.stacked_paths = list(paths)
        item.is_stacked = len(paths) > 1
        item.filename = paths[0].stem
        item.flag = FlagState.UNFLAGGED
        self.session.items = [item]
        return item, paths

    def test_send2trash_failure_does_not_raise(self):
        item, paths = self._make_item(["SHOT.JPG"])

        with patch("send2trash.send2trash", side_effect=OSError("recycle bin unavailable")):
            moved = self.session.move_items_to_trash([item])

        self.assertEqual(moved, 0)
        self.assertTrue(paths[0].exists(), "file must stay on disk when trashing fails")

    def test_specific_file_failure_does_not_raise(self):
        item, paths = self._make_item(["SHOT.JPG", "SHOT.ARW"])

        with patch("send2trash.send2trash", side_effect=OSError("recycle bin unavailable")):
            moved = self.session.move_specific_files_to_trash([item], [paths[0]])

        self.assertEqual(moved, 0)
        self.assertTrue(paths[0].exists())

    def test_raw_format_filter_selects_arw_only(self):
        item, paths = self._make_item(["SHOT.ARW", "SHOT.JPG"])
        sent = []

        def fake_send(path):
            sent.append(Path(path))
            Path(path).unlink()

        with patch("send2trash.send2trash", side_effect=fake_send):
            moved = self.session.move_items_to_trash([item], format_filter="raw")

        self.assertEqual(moved, 1)
        self.assertEqual([p.name for p in sent], ["SHOT.ARW"])
        self.assertFalse(item.is_stacked)
        self.assertEqual(item.path.name, "SHOT.JPG")
        self.assertEqual([p.name for p in item.stacked_paths], ["SHOT.JPG"])

    def test_raw_filter_accepts_explicit_arw_extension(self):
        item, paths = self._make_item(["SHOT.ARW", "SHOT.JPG"])
        sent = []

        def fake_send(path):
            sent.append(Path(path))
            Path(path).unlink()

        with patch("send2trash.send2trash", side_effect=fake_send):
            moved = self.session.move_items_to_trash([item], format_filter=".arw")

        self.assertEqual(moved, 1)
        self.assertEqual([p.name for p in sent], ["SHOT.ARW"])

    def test_is_raw_is_used_for_uppercase_raw_filter(self):
        item, paths = self._make_item(["SHOT.ARW", "SHOT.JPG"])
        sent = []

        def fake_send(path):
            sent.append(Path(path))
            Path(path).unlink()

        with patch("send2trash.send2trash", side_effect=fake_send):
            moved = self.session.move_items_to_trash([item], format_filter="RAW")

        self.assertEqual(moved, 1)
        self.assertEqual([p.name for p in sent], ["SHOT.ARW"])

    def test_jpg_filter_keeps_arw(self):
        item, paths = self._make_item(["SHOT.ARW", "SHOT.JPG"])
        sent = []

        def fake_send(path):
            sent.append(Path(path))
            Path(path).unlink()

        with patch("send2trash.send2trash", side_effect=fake_send):
            moved = self.session.move_items_to_trash([item], format_filter="jpg")

        self.assertEqual(moved, 1)
        self.assertEqual([p.name for p in sent], ["SHOT.JPG"])
        self.assertEqual(item.path.name, "SHOT.ARW")

    def test_is_raw_classmethod_contract(self):
        self.assertTrue(ImageLoader.is_raw("a.ARW"))
        self.assertFalse(ImageLoader.is_raw("a.jpg"))


if __name__ == "__main__":
    unittest.main()