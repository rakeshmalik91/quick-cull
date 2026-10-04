import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from culler import exif_wrapper as exif_module
from culler.culler_engine import CullingSession, ImageItem, FlagState
from culler.exif_wrapper import ExifToolWrapper


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestWriteRatingsBatch(unittest.TestCase):
    """
    Sync used to spawn one exiftool per photo. exiftool applies one rating to every
    path in an invocation, so files are grouped by rating value.
    """

    def setUp(self):
        self.wrapper = ExifToolWrapper()
        self.wrapper._is_available = True
        self.record = []

    def _capture(self, returncode=0):
        def fake_run(cmd, **kwargs):
            self.record.append(list(cmd))
            return _FakeCompleted(returncode=returncode)

        return fake_run

    def test_same_rating_uses_one_invocation(self):
        entries = [(f"D:/Photos/A{i}.ARW", 3) for i in range(20)]
        with patch.object(exif_module, "_run_cli", self._capture()):
            results = self.wrapper.write_ratings_batch(entries)

        self.assertEqual(len(self.record), 1, "one call per rating value, not per file")
        self.assertTrue(all(results.values()))
        self.assertEqual(len(results), 20)

    def test_distinct_ratings_are_grouped(self):
        entries = [(f"D:/Photos/A{i}.ARW", (i % 3) + 1) for i in range(12)]
        with patch.object(exif_module, "_run_cli", self._capture()):
            results = self.wrapper.write_ratings_batch(entries)

        ratings = sorted(int(a.split("=", 1)[1]) for a in self.record[0] if "Rating=" in a)
        self.assertEqual(len(self.record), 3, "three distinct ratings -> three calls")
        for cmd in self.record:
            self.assertIn("-overwrite_original", cmd)

    def test_argv_is_chunked_for_large_groups(self):
        count = ExifToolWrapper.BATCH_ARGV_LIMIT + 25
        entries = [(f"D:/Photos/B{i}.ARW", 5) for i in range(count)]
        with patch.object(exif_module, "_run_cli", self._capture()):
            self.wrapper.write_ratings_batch(entries)

        self.assertEqual(len(self.record), 2, "large rating groups split under the argv cap")
        for cmd in self.record:
            paths = [a for a in cmd[1:] if not a.startswith("-")]
            self.assertLessEqual(len(paths), ExifToolWrapper.BATCH_ARGV_LIMIT)

    def test_failure_marks_only_that_group(self):
        def selective(cmd, **kwargs):
            self.record.append(list(cmd))
            ok = "-XMP:Rating=1" in cmd
            return _FakeCompleted(returncode=0 if ok else 1)

        entries = [(f"D:/Photos/GOOD{i}.ARW", 1) for i in range(3)]
        entries += [(f"D:/Photos/BAD{i}.ARW", 4) for i in range(3)]
        with patch.object(exif_module, "_run_cli", selective):
            results = self.wrapper.write_ratings_batch(entries)

        self.assertTrue(all(results[f"D:/Photos/GOOD{i}.ARW"] for i in range(3)))
        self.assertFalse(any(results[f"D:/Photos/BAD{i}.ARW"] for i in range(3)))

    def test_out_of_range_rating_is_rejected(self):
        with patch.object(exif_module, "_run_cli", self._capture()):
            results = self.wrapper.write_ratings_batch([("D:/Photos/X.ARW", 9)])
        self.assertEqual(self.record, [])
        self.assertFalse(results["D:/Photos/X.ARW"])

    def test_empty_input(self):
        self.assertEqual(self.wrapper.write_ratings_batch([]), {})

    def test_unavailable_exiftool(self):
        wrapper = ExifToolWrapper()
        wrapper._is_available = False
        results = wrapper.write_ratings_batch([("D:/Photos/X.ARW", 3)])
        self.assertEqual(results, {"D:/Photos/X.ARW": False})


class TestSyncExifRatingsUsesBatch(unittest.TestCase):
    """CullingSession.sync_exif_ratings must go through the batch writer."""

    def setUp(self):
        self.session = CullingSession(db_manager=MagicMock())
        self.session.exif_wrapper = MagicMock()

    def _item(self, name, rating, stacked=None):
        stacked = stacked or (f"{name}.ARW", f"{name}.JPG")
        paths = [Path(f"D:/Photos/{n}") for n in stacked]
        item = ImageItem.__new__(ImageItem)
        item.path = paths[0]
        item.stacked_paths = paths
        item.is_stacked = len(paths) > 1
        item.rating = rating
        item.flag = FlagState.UNFLAGGED
        item.filename = paths[0].name
        return item

    def test_uses_batch_writer_once(self):
        self.session.items = [self._item(f"IMG{i}", 3) for i in range(5)]
        self.session.exif_wrapper.write_ratings_batch.return_value = {
            str(p): True for item in self.session.items for p in item.stacked_paths
        }

        count = self.session.sync_exif_ratings()

        self.assertEqual(self.session.exif_wrapper.write_ratings_batch.call_count, 1)
        self.session.exif_wrapper.write_rating.assert_not_called()
        self.assertEqual(count, 10, "five stacked pairs = ten files written")

    def test_unrated_items_are_skipped(self):
        self.session.items = [self._item("A", 0), self._item("B", 2)]
        self.session.exif_wrapper.write_ratings_batch.return_value = {"D:/Photos/B.ARW": True, "D:/Photos/B.JPG": True}

        count = self.session.sync_exif_ratings()

        entries = self.session.exif_wrapper.write_ratings_batch.call_args[0][0]
        self.assertEqual({rating for _p, rating in entries}, {2})
        self.assertEqual(count, 2)

    def test_no_ratings_does_not_call_exiftool(self):
        self.session.items = [self._item("A", 0)]
        self.assertEqual(self.session.sync_exif_ratings(), 0)
        self.session.exif_wrapper.write_ratings_batch.assert_not_called()

    def test_falls_back_to_per_file_writer(self):
        wrapper = MagicMock(spec=["write_rating"])
        wrapper.write_rating.return_value = True
        self.session.exif_wrapper = wrapper
        self.session.items = [self._item("A", 4)]

        count = self.session.sync_exif_ratings()

        self.assertEqual(count, 2)
        self.assertEqual(wrapper.write_rating.call_count, 2)


if __name__ == "__main__":
    unittest.main()