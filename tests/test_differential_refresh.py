"""Differential folder refresh (defect D15 / item P1-22).

The load-performance doc listed "manual / triggered refresh = differential, not full"
as R3 and `clear_cache()` on every scan as defect D15. These tests pin the behaviour
that fixes both, plus the rename handling that the doc listed as still open.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from culler.culler_engine import CullingSession, FlagState
from culler.image_loader import ImageLoader


def _write(path: Path, size=(64, 48), fmt="JPEG", color="blue"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format=fmt)
    return path


class _RecordingExif:
    """Stands in for ExifToolWrapper and records every path it is asked about."""

    def __init__(self):
        self.calls = []

    def is_available(self):
        return True

    def get_batch_metadata(self, paths):
        self.calls.append(list(paths))
        return [self._meta(p) for p in paths]

    @staticmethod
    def _meta(path):
        return {
            "file_type": Path(path).suffix.upper().lstrip("."),
            "orientation": 1,
            "width": 64,
            "height": 48,
            "rating": 0,
            "model": "TestCam",
            "lens": "TestLens",
            "iso": "100",
            "shutter_speed": "1/100s",
            "aperture": "f/2.8",
            "focal_length": "35mm",
            "date_taken": "2026:01:01 00:00:00",
        }

    def get_orientation(self, path, cache_key=None):
        return 1

    @property
    def queried_paths(self):
        return [p for call in self.calls for p in call]


class DifferentialRefreshBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.exif = _RecordingExif()
        self.loader = ImageLoader()
        self.db = MagicMock()
        self.db.dataset_dir = None
        self.db.get_records_for_paths.return_value = {}
        self.session = CullingSession(
            exif_wrapper=self.exif,
            db_manager=self.db,
            image_loader=self.loader,
        )

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def scan(self, **kwargs):
        self.exif.calls.clear()
        return self.session.scan_directory(self.dir, **kwargs)


class TestUnchangedRescan(DifferentialRefreshBase):
    def test_second_scan_with_no_change_reads_no_exif(self):
        for i in range(5):
            _write(self.dir / f"P{i:03d}.JPG")

        self.scan()
        first_batch = list(self.exif.queried_paths)
        self.assertEqual(len(first_batch), 5)

        items = self.scan()

        self.assertEqual(self.exif.queried_paths, [],
                         "an unchanged folder must not go back through ExifTool")
        self.assertEqual(len(items), 5)
        self.assertTrue(self.session.last_scan_stats["differential"])

    def test_unchanged_rescan_reuses_the_same_item_objects(self):
        for i in range(4):
            _write(self.dir / f"P{i:03d}.JPG")

        first = self.scan()
        second = self.scan()

        self.assertEqual([id(i) for i in first], [id(i) for i in second],
                         "the grid is bound to these objects; rebuilding them forces a re-render")

    def test_unchanged_rescan_does_not_evict_decoded_pixels(self):
        path = _write(self.dir / "A.JPG")
        self.scan()
        self.loader.get_thumbnail(path, max_size=(80, 80))
        before = self.loader.cache_stats()["thumb_items"]
        self.assertGreater(before, 0)

        self.scan()

        self.assertEqual(self.loader.cache_stats()["thumb_items"], before)
        self.assertIsNotNone(self.loader.get_cached_thumbnail(path))

    def test_scan_never_calls_clear_cache(self):
        """D15: clear_cache() on every scan threw away every other tab's images."""
        _write(self.dir / "A.JPG")
        self.scan()
        self.loader.clear_cache = MagicMock(side_effect=AssertionError("clear_cache on scan"))

        self.scan()

        self.loader.clear_cache.assert_not_called()


class TestOneFileChange(DifferentialRefreshBase):
    def test_only_the_changed_file_is_re_read(self):
        for i in range(10):
            _write(self.dir / f"P{i:03d}.JPG")
        self.scan()

        target = self.dir / "P004.JPG"
        _write(target, color="red")
        os_utime = time.time() + 5
        import os
        os.utime(target, (os_utime, os_utime))

        self.scan()

        self.assertEqual([Path(p).name for p in self.exif.queried_paths], ["P004.JPG"])
        self.assertEqual(self.session.last_scan_stats["changed"], 1)
        self.assertEqual(len(self.session.items), 10)

    def test_changed_file_keeps_its_flag_and_rating(self):
        path = _write(self.dir / "A.JPG")
        _write(self.dir / "B.JPG")
        items = self.scan()
        items[0].flag = FlagState.PICK
        items[0].rating = 4
        items[0].add_tag("Hero")
        self.session.save_item_records(items)

        _write(path, color="red")
        import os
        os.utime(path, (time.time() + 9, time.time() + 9))

        refreshed = self.scan()

        by_name = {it.path.name: it for it in refreshed}
        self.assertEqual(by_name["A.JPG"].flag, FlagState.PICK)
        self.assertEqual(by_name["A.JPG"].rating, 4)
        self.assertTrue(by_name["A.JPG"].has_tag("Hero"))

    def test_changed_file_invalidates_only_its_own_cached_tier(self):
        changed = _write(self.dir / "A.JPG")
        untouched = _write(self.dir / "B.JPG")
        self.scan()
        self.loader.get_thumbnail(changed, max_size=(80, 80))
        self.loader.get_thumbnail(untouched, max_size=(80, 80))

        _write(changed, color="green")
        import os
        os.utime(changed, (time.time() + 9, time.time() + 9))

        self.scan()

        self.assertIsNone(self.loader.get_cached_thumbnail(changed),
                          "an edited file must not serve a stale render")
        self.assertIsNotNone(self.loader.get_cached_thumbnail(untouched),
                             "an untouched file must keep its render")


class TestAddAndRemove(DifferentialRefreshBase):
    def test_added_file_gets_its_own_row(self):
        _write(self.dir / "A.JPG")
        self.scan()

        _write(self.dir / "B.JPG")
        items = self.scan()

        self.assertEqual(sorted(it.path.name for it in items), ["A.JPG", "B.JPG"])
        self.assertEqual([Path(p).name for p in self.exif.queried_paths], ["B.JPG"])
        self.assertEqual(self.session.last_scan_stats["added"], 1)

    def test_removed_file_drops_its_row_and_its_db_rows(self):
        _write(self.dir / "A.JPG")
        removed = _write(self.dir / "B.JPG")
        self.scan()

        removed.unlink()
        items = self.scan()

        self.assertEqual([it.path.name for it in items], ["A.JPG"])
        self.db.delete_image_records.assert_called()
        forgotten = self.db.delete_image_records.call_args[0][0]
        self.assertTrue(any("B.JPG" in p for p in forgotten))

    def test_emptied_folder_yields_no_items(self):
        _write(self.dir / "A.JPG")
        self.scan()

        for path in self.dir.glob("*.JPG"):
            path.unlink()

        self.assertEqual(self.scan(), [])

    def test_added_jpg_joins_its_raw_row_and_keeps_the_flags(self):
        raw = self.dir / "SHOT.ARW"
        raw.write_bytes(b"\x00" * 64)
        items = self.scan()
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0].is_stacked)
        items[0].flag = FlagState.REJECT
        items[0].rating = 3

        _write(self.dir / "SHOT.JPG")
        items = self.scan()

        self.assertEqual(len(items), 1, "RAW + JPG is one row, not two")
        self.assertTrue(items[0].is_stacked)
        self.assertEqual(items[0].flag, FlagState.REJECT)
        self.assertEqual(items[0].rating, 3)
        self.assertIn("Stacked", items[0].filename)
        self.assertEqual([p.name for p in items[0].stacked_paths], ["SHOT.ARW", "SHOT.JPG"])


class TestRenameHandling(DifferentialRefreshBase):
    def test_rename_keeps_the_item_and_its_flags(self):
        original = _write(self.dir / "DSC_0001.JPG")
        items = self.scan()
        items[0].flag = FlagState.PICK
        items[0].rating = 5
        items[0].add_tag("Keep")

        renamed = self.dir / "DSC_0001_EDIT.JPG"
        original.rename(renamed)
        refreshed = self.scan()

        self.assertEqual(len(refreshed), 1)
        self.assertEqual(refreshed[0].path.name, "DSC_0001_EDIT.JPG")
        self.assertEqual(refreshed[0].flag, FlagState.PICK)
        self.assertEqual(refreshed[0].rating, 5)
        self.assertTrue(refreshed[0].has_tag("Keep"))
        self.assertEqual(self.session.last_scan_stats["renamed"], 1)

    def test_rename_moves_the_db_row_to_the_new_name(self):
        original = _write(self.dir / "DSC_0002.JPG")
        self.scan()

        original.rename(self.dir / "DSC_0002_v2.JPG")
        self.scan()

        saved_paths = [row["file_path"] for row in self.db.save_image_records.call_args[0][0]]
        self.assertTrue(any(p.endswith("DSC_0002_v2.JPG") for p in saved_paths))
        self.db.delete_image_records.assert_called()

    def test_rename_does_not_look_like_a_brand_new_photo_for_exif(self):
        original = _write(self.dir / "DSC_0003.JPG")
        self.scan()

        original.rename(self.dir / "DSC_0003_crop.JPG")
        self.scan()

        self.assertEqual(len(self.exif.queried_paths), 1,
                         "the renamed file's metadata is re-read once, not treated as an add plus a remove")


class TestFallbacks(DifferentialRefreshBase):
    def test_switching_to_a_different_folder_rebuilds(self):
        other = Path(tempfile.mkdtemp())
        try:
            _write(self.dir / "A.JPG")
            self.scan()
            first_ids = {id(i) for i in self.session.items}

            _write(other / "Z.JPG")
            items = self.session.scan_directory(other)

            self.assertEqual([it.path.name for it in items], ["Z.JPG"])
            self.assertNotEqual({id(i) for i in items}, first_ids)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_changing_stack_mode_rebuilds(self):
        raw = self.dir / "SHOT.ARW"
        raw.write_bytes(b"\x00" * 64)
        _write(self.dir / "SHOT.JPG")
        items = self.scan()
        self.assertEqual(len(items), 1)

        items = self.session.scan_directory(self.dir, stack_raw_jpg=False)
        self.assertEqual(len(items), 2, "unstacked mode must not reuse the stacked row")

    def test_recursive_flag_change_rebuilds(self):
        _write(self.dir / "A.JPG")
        _write(self.dir / "sub" / "B.JPG")
        flat = self.scan()
        self.assertEqual(len(flat), 1)

        deep = self.session.scan_directory(self.dir, recursive=True)
        self.assertEqual(len(deep), 2)

    def test_progress_callback_reports_zero_then_total_on_a_noop(self):
        for i in range(3):
            _write(self.dir / f"P{i}.JPG")
        self.scan()

        seen = []
        self.scan(progress_callback=lambda done, total: seen.append((done, total)))

        self.assertEqual(seen[0][0], 0)
        self.assertEqual(seen[-1], (3, 3))

    def test_progress_callback_accepts_two_arguments(self):
        _write(self.dir / "A.JPG")
        seen = []
        self.session.scan_directory(self.dir, progress_callback=lambda d, t: seen.append((d, t)))
        self.assertTrue(seen[-1][0] == seen[-1][1])


class TestSessionRelease(DifferentialRefreshBase):
    def test_release_drops_items_and_the_manifest(self):
        for i in range(5):
            _write(self.dir / f"R{i}.JPG")
        self.scan()
        self.assertEqual(len(self.session.items), 5)

        self.session.release()

        self.assertEqual(self.session.items, [])
        self.assertIsNone(self.session._index,
                          "the manifest must go too, or a reopened folder diffs against stale state")
        self.assertEqual(self.session._expected_row_count, None)

    def test_release_keeps_the_session_usable(self):
        for i in range(3):
            _write(self.dir / f"U{i}.JPG")
        self.scan()
        self.session.release()

        items = self.session.scan_directory(self.dir)

        self.assertEqual(len(items), 3, "a released session must still be able to scan")
        self.assertTrue(all(item.metadata for item in items))


class TestScanEfficiency(DifferentialRefreshBase):
    def test_item_construction_reuses_the_scan_stat(self):
        """The scan's manifest already carries the size; the item must not re-stat."""
        from culler.culler_engine import ImageItem

        path = _write(self.dir / "A.JPG")

        real_stat = os.stat
        real_path_stat = Path.stat

        def forbidden(*args, **kwargs):
            raise AssertionError("ImageItem re-stat'ed a file the scan already measured")

        os.stat = forbidden
        Path.stat = forbidden
        try:
            item = ImageItem(path, size_bytes=4242, resolved=True)
        finally:
            os.stat = real_stat
            Path.stat = real_path_stat

        self.assertEqual(item.size_bytes, 4242)

    def test_a_scan_does_not_stat_any_photo_path(self):
        """One scandir stat per file, and the row build reuses it (defect §2.1)."""
        for i in range(8):
            _write(self.dir / f"P{i:03d}.JPG")

        import culler.folder_index as folder_index

        real_stat = os.stat
        photo_stats = []

        def counting(path, *args, **kwargs):
            if str(path).upper().endswith((".JPG", ".ARW")):
                photo_stats.append(str(path))
            return real_stat(path, *args, **kwargs)

        folder_index.os.stat = counting
        try:
            self.scan()
        finally:
            folder_index.os.stat = real_stat

        self.assertEqual(photo_stats, [],
                         "a photo path is stat'ed through os.stat only by Path.resolve()/stat, "
                         "which the scan no longer needs to call per item")

    def test_unchanged_rescan_does_not_stat_any_photo_path(self):
        for i in range(6):
            _write(self.dir / f"P{i:03d}.JPG")
        self.scan()

        import culler.folder_index as folder_index

        real_stat = os.stat
        photo_stats = []

        def counting(path, *args, **kwargs):
            if str(path).upper().endswith((".JPG", ".ARW")):
                photo_stats.append(str(path))
            return real_stat(path, *args, **kwargs)

        folder_index.os.stat = counting
        try:
            self.scan()
        finally:
            folder_index.os.stat = real_stat

        self.assertEqual(photo_stats, [])


if __name__ == "__main__":
    unittest.main()