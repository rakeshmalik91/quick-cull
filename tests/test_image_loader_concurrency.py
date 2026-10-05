import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from culler.image_loader import ImageLoader


class TestFullCacheByteBudget(unittest.TestCase):
    """
    The preview/full cache was capped by item count, so 30 large decodes could hold
    gigabytes. It is now capped by bytes first.
    """

    def setUp(self):
        self.loader = ImageLoader()

    def test_estimate_bytes(self):
        self.assertEqual(ImageLoader.estimate_bytes(Image.new("RGB", (100, 50))), 100 * 50 * 3)
        self.assertEqual(ImageLoader.estimate_bytes(None), 0)

    def test_evicts_until_under_byte_budget(self):
        budget = 4 * 1024 * 1024
        self.loader.MAX_FULL_CACHE_BYTES = budget
        self.loader.MAX_FULL_CACHE = 1000

        # 1200x800 RGB ~= 2.74 MB each, so only one fits in a 4 MB budget.
        for i in range(6):
            self.loader._store_full(self.loader.tier_key(f"D:/Photos/BIG_{i}.JPG", 0.25, "camera"),
                                    Image.new("RGB", (1200, 800)))

        stats = self.loader.cache_stats()
        self.assertLessEqual(stats["full_bytes"], budget)
        self.assertLessEqual(stats["full_items"], 6)
        self.assertEqual(stats["full_items"], 1, "oldest entries must be evicted first")

    def test_eviction_keeps_most_recent(self):
        self.loader.MAX_FULL_CACHE_BYTES = 4 * 1024 * 1024
        self.loader.MAX_FULL_CACHE = 1000
        for i in range(4):
            self.loader._store_full(self.loader.tier_key(f"D:/Photos/KEEP_{i}.JPG", 0.25, "camera"),
                                    Image.new("RGB", (1200, 800)))

        self.assertIn(self.loader.tier_key("D:/Photos/KEEP_3.JPG", 0.25, "camera"), self.loader._full_cache)
        self.assertNotIn(self.loader.tier_key("D:/Photos/KEEP_0.JPG", 0.25, "camera"), self.loader._full_cache)

    def test_item_cap_still_applies_for_small_images(self):
        self.loader.MAX_FULL_CACHE_BYTES = 512 * 1024 * 1024
        self.loader.MAX_FULL_CACHE = 3
        for i in range(6):
            self.loader._store_full(self.loader.tier_key(f"D:/Photos/SMALL_{i}.JPG", 0.25, "camera"),
                                    Image.new("RGB", (16, 16)))

        self.assertEqual(len(self.loader._full_cache), 3)

    def test_cache_stats_reports_budget(self):
        self.loader._store_full(self.loader.tier_key("D:/Photos/S.JPG", 0.25, "camera"), Image.new("RGB", (10, 10)))
        stats = self.loader.cache_stats()
        for key in ("thumb_items", "thumb_bytes", "full_items", "full_bytes", "full_budget_bytes"):
            self.assertIn(key, stats)
        self.assertEqual(stats["full_items"], 1)


class TestSingleFlight(unittest.TestCase):
    """
    Concurrent requests for the same key must decode once; the rest wait for the result.
    """

    def setUp(self):
        self.loader = ImageLoader()
        self.temp_dir = tempfile.mkdtemp()
        self.path = Path(self.temp_dir) / "SHOT.JPG"
        Image.new("RGB", (600, 400), "blue").save(self.path, format="JPEG", quality=95)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_concurrent_requests_decode_once(self):
        calls = []
        original_decode = ImageLoader._decode_and_store_full

        def slow_decode(self_, file_path_str, cache_key, raw_scale, white_balance, content=None):
            calls.append(cache_key)
            time.sleep(0.25)
            return original_decode(self_, file_path_str, cache_key, raw_scale, white_balance, content)

        ImageLoader._decode_and_store_full = slow_decode
        try:
            results = []
            errors = []

            def worker():
                try:
                    results.append(self.loader.load_full_image(self.path, 0.25, "camera"))
                except Exception as exc:  # pragma: no cover - surfaced by assertion
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        finally:
            ImageLoader._decode_and_store_full = original_decode

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        self.assertEqual(len(calls), 1, "the image must be decoded exactly once")
        self.assertTrue(all(r is not None for r in results))
        self.assertEqual(results[0].size, results[-1].size)

    def test_waiter_gets_cached_result_not_none(self):
        original_decode = ImageLoader._decode_and_store_full

        def slow_decode(self_, file_path_str, cache_key, raw_scale, white_balance, content=None):
            time.sleep(0.2)
            return original_decode(self_, file_path_str, cache_key, raw_scale, white_balance, content)

        ImageLoader._decode_and_store_full = slow_decode
        try:
            results = []
            threads = [threading.Thread(target=lambda: results.append(
                self.loader.load_full_image(self.path, 0.25, "camera"))) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        finally:
            ImageLoader._decode_and_store_full = original_decode

        self.assertEqual(len([r for r in results if r is not None]), 4)

    def test_inflight_registry_is_cleaned_up(self):
        self.loader.load_full_image(self.path, 0.25, "camera")
        self.assertEqual(self.loader._inflight, {})

    def test_failed_decode_does_not_wedge_waiters(self):
        original_decode = ImageLoader._decode_and_store_full

        def failing_decode(self_, *args, **kwargs):
            raise RuntimeError("decode exploded")

        ImageLoader._decode_and_store_full = failing_decode
        try:
            with self.assertRaises(RuntimeError):
                self.loader.load_full_image(self.path, 0.25, "camera")
        finally:
            ImageLoader._decode_and_store_full = original_decode

        self.assertEqual(self.loader._inflight, {}, "failed decode must release the slot")
        self.assertIsNotNone(self.loader.load_full_image(self.path, 0.25, "camera"))

    def test_counters_track_hits_and_misses(self):
        self.loader.load_full_image(self.path, 0.25, "camera")
        self.loader.load_full_image(self.path, 0.25, "camera")

        self.assertEqual(self.loader.stats["full_hits"], 1)
        self.assertEqual(self.loader.stats["full_misses"], 1)


class TestCacheConcurrencyStress(unittest.TestCase):
    """
    Hammer the shared caches from many threads; the invariants must hold afterwards.
    """

    def test_parallel_thumbnail_and_full_access_keeps_caches_consistent(self):
        loader = ImageLoader()
        temp_dir = tempfile.mkdtemp()
        try:
            paths = []
            for i in range(12):
                p = Path(temp_dir) / f"S{i}.JPG"
                Image.new("RGB", (300, 200), (i * 10, 20, 30)).save(p, format="JPEG")
                paths.append(p)

            errors = []

            def worker(seed):
                try:
                    for p in paths:
                        loader.get_thumbnail(p, max_size=(80, 80), raw_scale=0.10)
                        loader.get_cached_thumbnail(p)
                        loader.load_full_image(p, 0.25, "camera")
                        loader.get_cached_full_image(p, 0.25, "camera")
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

            self.assertEqual(errors, [])
            self.assertLessEqual(len(loader._thumb_cache), loader.MAX_THUMB_CACHE)
            self.assertLessEqual(len(loader._full_cache), loader.MAX_FULL_CACHE)
            stats = loader.cache_stats()
            self.assertLessEqual(stats["full_bytes"], loader.MAX_FULL_CACHE_BYTES)
            self.assertEqual(loader._inflight, {})
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()