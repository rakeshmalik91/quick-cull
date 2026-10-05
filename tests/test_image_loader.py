import os
import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from culler.image_loader import ImageLoader


class TestImageLoader(unittest.TestCase):
    """
    Automated Unit Test Suite for ImageLoader (supported formats, thumbnail caching, and fast downscaling).
    """

    def setUp(self):
        self.loader = ImageLoader()

    def test_supported_extensions(self):
        """
        Verify supported image format detection (.ARW, .JPG, .PNG, .HEIC).
        """
        self.assertTrue(ImageLoader.is_supported(Path("sample.arw")))
        self.assertTrue(ImageLoader.is_supported(Path("sample.ARW")))
        self.assertTrue(ImageLoader.is_supported(Path("photo.jpg")))
        self.assertTrue(ImageLoader.is_supported(Path("photo.JPG")))
        self.assertTrue(ImageLoader.is_supported(Path("image.png")))
        self.assertTrue(ImageLoader.is_supported(Path("image.heic")))

        self.assertFalse(ImageLoader.is_supported(Path("document.pdf")))
        self.assertFalse(ImageLoader.is_supported(Path("script.py")))
        self.assertFalse(ImageLoader.is_supported(Path("data.txt")))

    def test_is_raw(self):
        """
        Verify RAW detection used by the format_filter='raw' trash path.
        """
        self.assertTrue(ImageLoader.is_raw(Path("shot.ARW")))
        self.assertTrue(ImageLoader.is_raw(Path("shot.arw")))
        self.assertFalse(ImageLoader.is_raw(Path("shot.JPG")))
        self.assertFalse(ImageLoader.is_raw(Path("shot.png")))
        self.assertFalse(ImageLoader.is_raw(Path("shot.heic")))

    def test_thumbnail_ram_caching(self):
        """
        Verify storing and retrieving thumbnails in RAM cache.
        """
        test_path = Path("D:/Photos/TEST_CACHE.JPG")
        dummy_img = Image.new("RGB", (400, 400), color="blue")

        self.loader._store_thumb(self.loader.tier_key(test_path, 0.10, "camera"), dummy_img)

        cached = self.loader.get_cached_thumbnail(test_path)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.size, (400, 400))

    def test_get_cached_thumbnail_returns_a_copy(self):
        """
        Verify callers cannot mutate the cached canonical through the returned image.
        """
        test_path = Path("D:/Photos/TEST_COPY.JPG")
        self.loader._store_thumb(self.loader.tier_key(test_path, 0.10, "camera"), Image.new("RGB", (400, 400), "blue"))

        first = self.loader.get_cached_thumbnail(test_path)
        first.paste((255, 0, 0), (0, 0, 10, 10))

        second = self.loader.get_cached_thumbnail(test_path)
        self.assertEqual(second.getpixel((5, 5)), (0, 0, 255))

    def test_get_cached_thumbnail_ignores_unknown_variant(self):
        """
        A cached render for a different scale/white balance must not be served for the
        default variant (regression: the old path-only index returned it).
        """
        test_path = Path("D:/Photos/TEST_VARIANT.JPG")
        self.loader._store_thumb(self.loader.tier_key(test_path, 0.25, "camera"), Image.new("RGB", (400, 400), "red"))

        self.assertIsNone(
            self.loader.get_cached_thumbnail(test_path),
            "0.25/camera is not a navigation variant",
        )

        self.loader._store_thumb(self.loader.tier_key(test_path, 0.10, "camera"), Image.new("RGB", (400, 400), "red"))
        self.assertIsNotNone(self.loader.get_cached_thumbnail(test_path))

    def test_thumbnail_cache_uses_composite_key(self):
        """
        Verify scale and white balance are part of the thumbnail cache key.
        """
        test_path = Path("D:/Photos/TEST_COMPOSITE.JPG")
        camera = Image.new("RGB", (400, 400), "red")
        auto = Image.new("RGB", (400, 400), "green")

        self.loader._store_thumb(self.loader.tier_key(test_path, 0.10, "camera"), camera)
        self.loader._store_thumb(self.loader.tier_key(test_path, 0.10, "auto"), auto)

        self.assertEqual(len(self.loader._thumb_cache), 2)
        self.assertIs(self.loader._thumb_cache[self.loader.tier_key(test_path, 0.10, "auto")], auto)

    def test_full_image_ram_caching(self):
        """
        Verify storing and retrieving full-resolution preview images in RAM cache.
        """
        test_path = Path("D:/Photos/TEST_FULL.JPG")
        dummy_img = Image.new("RGB", (1920, 1080), color="green")

        cache_key = self.loader.tier_key(test_path, 0.25, "camera")
        self.loader._store_full(cache_key, dummy_img)

        cached = self.loader.get_cached_full_image(test_path, raw_scale=0.25, white_balance="camera")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.size, (1920, 1080))

    def test_clear_cache(self):
        """
        Verify clear_cache empties both thumbnail and full image RAM caches.
        """
        test_path = Path("D:/Photos/TEST.JPG")
        dummy_img = Image.new("RGB", (50, 50))

        self.loader._store_thumb(self.loader.tier_key(test_path, 0.10, "camera"), dummy_img)
        self.loader._store_full(self.loader.tier_key(test_path, 0.25, "camera"), dummy_img)

        self.loader.clear_cache()

        self.assertEqual(len(self.loader._thumb_cache), 0)
        self.assertEqual(len(self.loader._full_cache), 0)
        self.assertFalse(hasattr(self.loader, "_thumb_cache_index"))

    def test_exif_orientation_handling(self):
        """
        Verify EXIF orientation rotation transforms via apply_exif_orientation.
        """
        img = Image.new("RGB", (300, 200), color="red")
        transposed = ImageLoader.apply_exif_orientation(img)
        self.assertIsNotNone(transposed)
        self.assertEqual(transposed.size, (300, 200))

        # Test injecting orientation 6 (Rotate 90 CW) -> 300x200 landscape becomes 200x300 portrait
        portrait_img = ImageLoader.apply_exif_orientation(img, orientation=6)
        self.assertEqual(portrait_img.size, (200, 300))

    def test_get_orientation(self):
        """
        Verify get_orientation defaults cleanly to 1 for unknown or non-existent files.
        """
        orient = self.loader.exif_wrapper.get_orientation("non_existent_file.jpg")
        self.assertEqual(orient, 1)

    def test_thumbnail_downscale_from_cache(self):
        """
        Verify get_thumbnail downscales from canonical cache size to requested max_size.
        """
        test_path = Path("D:/Photos/TEST_DOWNSCALE.JPG")
        dummy_img = Image.new("RGB", (400, 400), color="green")

        self.loader._store_thumb(self.loader.tier_key(test_path, 0.10, "camera"), dummy_img)

        thumb = self.loader.get_thumbnail(test_path, max_size=(80, 80), raw_scale=0.10, white_balance="camera")
        self.assertIsNotNone(thumb)
        self.assertEqual(thumb.size, (80, 80))

    def test_thumbnail_cache_evicts_oldest_past_cap(self):
        """
        Verify the thumbnail cache stays within MAX_THUMB_CACHE entries.
        """
        cap = self.loader.MAX_THUMB_CACHE
        for i in range(cap + 5):
            self.loader._store_thumb(self.loader.tier_key(f"D:/Photos/EVICT_{i:04d}.JPG", 0.10, "camera"),
                                    Image.new("RGB", (80, 80)))

        self.assertEqual(len(self.loader._thumb_cache), cap)
        self.assertNotIn(self.loader.tier_key("D:/Photos/EVICT_0000.JPG", 0.10, "camera"), self.loader._thumb_cache)
        self.assertIn(self.loader.tier_key("D:/Photos/EVICT_%04d.JPG" % (cap + 4), 0.10, "camera"), self.loader._thumb_cache)


class TestImageLoaderThumbnailMemoryBound(unittest.TestCase):
    """
    The thumbnail tier must never retain a full-resolution buffer (defect D3).
    """

    def setUp(self):
        self.loader = ImageLoader()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_png(self, name: str, size) -> Path:
        path = Path(self.temp_dir) / name
        Image.new("RGB", size, "red").save(path, format="PNG")
        return path

    def test_png_thumbnail_is_not_stored_at_full_resolution(self):
        path = self._write_png("BIG.PNG", (2400, 1600))

        thumb = self.loader.get_thumbnail(path, max_size=(80, 80), raw_scale=0.10)

        self.assertIsNotNone(thumb)
        self.assertLessEqual(max(thumb.size), 80)
        self.assertEqual(len(self.loader._thumb_cache), 1)
        for key, cached in self.loader._thumb_cache.items():
            self.assertLessEqual(
                max(cached.size), self.loader.MAX_THUMB_DIM,
                f"thumbnail tier holds a full-resolution buffer for {key}",
            )

    def test_white_balance_switch_is_a_cache_miss(self):
        """
        Switching white balance must re-render instead of returning the old render (D4).
        """
        path = self._write_png("WB.PNG", (1200, 800))

        calls = []
        original = ImageLoader.load_full_image

        def counting_load(self_, file_path, raw_scale=0.25, white_balance="camera"):
            calls.append((str(file_path), raw_scale, white_balance))
            return original(self_, file_path, raw_scale=raw_scale, white_balance=white_balance)

        ImageLoader.load_full_image = counting_load
        try:
            self.loader.get_thumbnail(path, max_size=(80, 80), raw_scale=0.10, white_balance="camera")
            self.loader.get_thumbnail(path, max_size=(80, 80), raw_scale=0.10, white_balance="auto")
            self.loader.get_thumbnail(path, max_size=(80, 80), raw_scale=0.10, white_balance="camera")
        finally:
            ImageLoader.load_full_image = original

        self.assertEqual(len(calls), 2, "camera/auto must each decode once, and the repeat camera hit cached")
        self.assertEqual({c[2] for c in calls}, {"camera", "auto"})


if __name__ == "__main__":
    unittest.main()