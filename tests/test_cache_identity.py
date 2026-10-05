"""Cache-key identity, bounded metadata caches, and full-cache locking.

Covers the fixes for D6 (unbounded orientation cache), P1-23 (content-identity cache
keys) and the two locking/accounting gaps found while resolving them.
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

import culler.exif_wrapper as exif_module
from culler.exif_wrapper import ExifToolWrapper
from culler.image_loader import ImageLoader


def _jpg(directory, name, size=(80, 60), color="blue"):
    path = Path(directory) / name
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


class TestOrientationCacheIsBounded(unittest.TestCase):
    """D6: the orientation cache grew by one entry per file it had ever decoded."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.wrapper = ExifToolWrapper()
        self.wrapper._is_available = False  # no subprocess fallback in the unit test

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_cache_never_exceeds_its_cap(self):
        cap = self.wrapper.MAX_ORIENTATION_CACHE
        self.wrapper.MAX_ORIENTATION_CACHE = 12
        paths = [_jpg(self.dir, f"O{i:04d}.JPG") for i in range(40)]

        for path in paths:
            self.wrapper.get_orientation(str(path))

        self.assertLessEqual(len(self.wrapper._orientation_cache), 12)

    def test_a_recently_used_entry_survives_eviction(self):
        self.wrapper.MAX_ORIENTATION_CACHE = 3
        keep = str(_jpg(self.dir, "KEEP.JPG"))
        self.wrapper.get_orientation(keep)
        self.wrapper.get_orientation(str(_jpg(self.dir, "A.JPG")))
        self.wrapper.get_orientation(str(_jpg(self.dir, "B.JPG")))

        # Touch it, then push two more entries past the cap. An LRU keeps it; an
        # insertion-ordered eviction would drop the oldest entry, which is now KEEP.
        self.wrapper.get_orientation(keep)
        self.wrapper.get_orientation(str(_jpg(self.dir, "C.JPG")))
        self.wrapper.get_orientation(str(_jpg(self.dir, "D.JPG")))

        self.assertIn(ExifToolWrapper.content_identity(keep), self.wrapper._orientation_cache)
        self.assertEqual(len(self.wrapper._orientation_cache), 3)

    def test_repeated_lookups_do_not_grow_the_cache(self):
        path = str(_jpg(self.dir, "SAME.JPG"))
        for _ in range(20):
            self.wrapper.get_orientation(path)
        self.assertEqual(len(self.wrapper._orientation_cache), 1)

    def test_editing_the_file_invalidates_the_cached_orientation(self):
        path = _jpg(self.dir, "EDIT.JPG")
        first = self.wrapper.get_orientation(str(path))
        self.assertEqual(len(self.wrapper._orientation_cache), 1)

        _jpg(self.dir, "EDIT.JPG", color="red")
        os.utime(path, (time.time() + 10, time.time() + 10))

        self.wrapper.get_orientation(str(path))
        self.assertEqual(len(self.wrapper._orientation_cache), 2,
                         "a changed file must not read as the cached one")

    def test_invalidate_orientation_paths_drops_only_those_files(self):
        a = str(_jpg(self.dir, "A.JPG"))
        b = str(_jpg(self.dir, "B.JPG"))
        self.wrapper.get_orientation(a)
        self.wrapper.get_orientation(b)

        removed = self.wrapper.invalidate_orientation_paths([a])

        self.assertEqual(removed, 1)
        self.assertNotIn(ExifToolWrapper.content_identity(a), self.wrapper._orientation_cache)
        self.assertIn(ExifToolWrapper.content_identity(b), self.wrapper._orientation_cache)

    def test_concurrent_lookups_do_not_corrupt_the_cache(self):
        paths = [str(_jpg(self.dir, f"P{i:03d}.JPG")) for i in range(30)]
        errors = []

        def worker():
            try:
                for path in paths:
                    self.wrapper.get_orientation(path)
            except Exception as exc:  # pragma: no cover - surfaced by assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertLessEqual(len(self.wrapper._orientation_cache), self.wrapper.MAX_ORIENTATION_CACHE)

    def test_missing_file_returns_one_without_spawning_exiftool(self):
        calls = []
        with patch.object(exif_module, "_run_cli", lambda cmd, **kw: calls.append(cmd)):
            self.wrapper._is_available = True
            self.assertEqual(self.wrapper.get_orientation(str(self.dir / "gone.jpg")), 1)
        self.assertEqual(calls, [], "a missing file must not cost a subprocess")

    def test_identity_is_one_stat(self):
        path = _jpg(self.dir, "STAT.JPG")
        real_stat = os.stat
        calls = []

        def counting(p, *a, **k):
            calls.append(str(p))
            return real_stat(p, *a, **k)

        with patch.object(exif_module.os, "stat", counting):
            ExifToolWrapper.content_identity(path)

        self.assertEqual(len(calls), 1)


class TestContentIdentityCacheKeys(unittest.TestCase):
    """P1-23: decoded tiers were keyed by path alone."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.loader = ImageLoader()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_thumbnail_misses_when_the_file_is_edited(self):
        path = _jpg(self.dir, "SHOT.JPG")
        first = self.loader.get_thumbnail(path, max_size=(40, 40))
        self.assertIsNotNone(first)

        _jpg(self.dir, "SHOT.JPG", color="red")
        os.utime(path, (time.time() + 10, time.time() + 10))

        self.loader.stats["thumb_hits"] = 0
        self.loader.get_cached_thumbnail(path)
        self.assertEqual(self.loader.stats["thumb_hits"], 0,
                         "an edited file must not serve the previous render")

    def test_full_cache_misses_when_the_file_is_edited(self):
        path = _jpg(self.dir, "FULL.JPG")
        self.loader.load_full_image(path, 0.25, "camera")

        _jpg(self.dir, "FULL.JPG", color="red")
        os.utime(path, (time.time() + 10, time.time() + 10))

        self.assertIsNone(self.loader.get_cached_full_image(path, 0.25, "camera"))

    def test_a_case_rename_is_not_a_fresh_decode(self):
        path = _jpg(self.dir, "CASE.JPG")
        self.loader.load_full_image(path, 0.25, "camera")

        # Windows: same file, different spelling of the name.
        same = os.path.join(os.path.dirname(str(path)), "case.jpg")
        self.assertIsNotNone(self.loader.get_cached_full_image(same, 0.25, "camera"),
                             "normcase keys make a case-only rename the same entry")

    def test_invalidate_paths_drops_every_tier_for_one_file(self):
        path = _jpg(self.dir, "INV.JPG")
        other = _jpg(self.dir, "KEEP.JPG")
        self.loader.get_thumbnail(path, max_size=(40, 40))
        self.loader.get_thumbnail(other, max_size=(40, 40))
        self.loader.load_full_image(path, 0.25, "camera")

        removed = self.loader.invalidate_paths([path])

        self.assertGreaterEqual(removed, 2)
        self.assertIsNone(self.loader.get_cached_thumbnail(path))
        self.assertIsNotNone(self.loader.get_cached_thumbnail(other))
        self.assertIsNone(self.loader.get_cached_full_image(path, 0.25, "camera"))

    def test_invalidate_paths_keeps_byte_totals_consistent(self):
        for i in range(6):
            path = _jpg(self.dir, f"BYTES_{i}.JPG", size=(300, 200))
            self.loader.get_thumbnail(path, max_size=(40, 40))

        self.loader.invalidate_paths([self.dir / f"BYTES_{i}.JPG" for i in range(3)])
        stats = self.loader.cache_stats()

        recomputed = sum(self.loader.estimate_bytes(v) for v in self.loader._thumb_cache.values())
        self.assertEqual(stats["thumb_bytes"], recomputed)

    def test_invalidate_paths_of_an_unknown_file_is_a_noop(self):
        self.assertEqual(self.loader.invalidate_paths([self.dir / "nope.JPG"]), 0)


class TestFullCacheIsThreadSafe(unittest.TestCase):
    """
    D7 was closed for reads and for the thumbnail tier, but the full tier was still
    inserted and evicted without the lock, from up to eight decode workers.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.loader = ImageLoader()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_concurrent_stores_keep_the_tier_within_budget(self):
        self.loader.MAX_FULL_CACHE_BYTES = 4 * 1024 * 1024
        self.loader.MAX_FULL_CACHE = 1000
        errors = []

        def worker(start):
            try:
                for i in range(start, start + 12):
                    path = _jpg(self.dir, f"T{start}_{i}.JPG", size=(120, 90))
                    self.loader.load_full_image(path, 0.25, "camera")
            except Exception as exc:  # pragma: no cover - surfaced by assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n * 12,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [])
        stats = self.loader.cache_stats()
        self.assertLessEqual(stats["full_bytes"], self.loader.MAX_FULL_CACHE_BYTES)
        recomputed = sum(self.loader.estimate_bytes(v) for v in self.loader._full_cache.values())
        self.assertEqual(stats["full_bytes"], recomputed)

    def test_byte_totals_survive_a_reinsert_of_the_same_key(self):
        path = _jpg(self.dir, "SAME.JPG", size=(200, 150))
        self.loader.load_full_image(path, 0.25, "camera")
        before = self.loader.cache_stats()["full_bytes"]

        self.loader._store_full(self.loader.tier_key(path, 0.25, "camera"), path and Image.open(path).convert("RGB"))

        self.assertEqual(self.loader.cache_stats()["full_bytes"], before)


class TestPilFallbackRunsConcurrently(unittest.TestCase):
    def test_missing_exiftool_still_returns_one_record_per_path(self):
        wrapper = ExifToolWrapper()
        wrapper._is_available = False
        dir_path = Path(tempfile.mkdtemp())
        try:
            paths = [str(_jpg(dir_path, f"F{i}.JPG")) for i in range(6)]
            result = wrapper.get_batch_metadata(paths)
            self.assertEqual(len(result), 6)
            self.assertEqual([r["width"] for r in result], [80] * 6)
        finally:
            shutil.rmtree(dir_path, ignore_errors=True)

    def test_write_ratings_batch_logs_instead_of_raising_nameerror(self):
        """The batch writer called log_error without importing it."""
        wrapper = ExifToolWrapper()
        wrapper._is_available = True

        def boom(cmd, **kwargs):
            raise OSError("exiftool exploded")

        with patch.object(exif_module, "_run_cli", boom):
            outcomes = wrapper.write_ratings_batch([("D:/Photos/A.JPG", 3)])

        self.assertEqual(outcomes, {"D:/Photos/A.JPG": False})


if __name__ == "__main__":
    unittest.main()