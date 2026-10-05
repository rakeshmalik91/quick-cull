import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from culler.culler_engine import CullingSession, ImageItem
from culler.exif_wrapper import ExifToolWrapper
from culler.image_loader import ImageLoader


def _items(count, tag):
    items = []
    for i in range(count):
        p = Path(f"D:/Photos/{tag}_{i:04d}.JPG")
        it = ImageItem.__new__(ImageItem)
        it.path, it.stacked_paths, it.is_stacked = p, [p], False
        it.filename, it.format_name, it.flag, it.rating = p.name, "JPEG Image", "UNFLAGGED", 0
        items.append(it)
    return items


class TestSharedDecodeServices(unittest.TestCase):
    """
    Tabs are separate sessions but must share one decode cache, otherwise the same photo
    is decoded once per tab and switching tabs discards the previous tab's images.
    """

    def setUp(self):
        self.exif = ExifToolWrapper()
        self.loader = ImageLoader(exif_wrapper=self.exif)
        self.sessions = [
            CullingSession(db_manager=MagicMock(), exif_wrapper=self.exif, image_loader=self.loader)
            for _ in range(3)
        ]

    def test_sessions_share_one_loader_and_exiftool(self):
        self.assertEqual(len({id(s.image_loader) for s in self.sessions}), 1)
        self.assertEqual(len({id(s.exif_wrapper) for s in self.sessions}), 1)

    def test_sessions_without_shared_services_still_work(self):
        """Backwards compatible: a session built alone gets its own services."""
        solo = CullingSession(db_manager=MagicMock())
        self.assertIsNotNone(solo.image_loader)
        self.assertIsNotNone(solo.exif_wrapper)

    def test_one_decode_is_visible_from_every_tab(self):
        thumb = Image.new("RGB", (400, 267), "blue")
        shared = Path("D:/Photos/SHARED.JPG")
        self.loader._store_thumb(self.loader.tier_key(shared, 0.10, "camera"), thumb)

        for session in self.sessions:
            self.assertEqual(session.image_loader.get_cached_thumbnail(shared).size, (400, 267))

    def test_photo_in_two_tabs_is_decoded_once(self):
        temp_dir = tempfile.mkdtemp()
        try:
            path = Path(temp_dir) / "OVERLAP.JPG"
            Image.new("RGB", (600, 400), "blue").save(path, format="JPEG")

            decodes = []
            original = ImageLoader._decode_and_store_full

            def counting(self_, file_path_str, cache_key, raw_scale, white_balance, content=None):
                decodes.append(cache_key)
                return original(self_, file_path_str, cache_key, raw_scale, white_balance, content)

            ImageLoader._decode_and_store_full = counting
            try:
                results = [s.image_loader.load_full_image(path, 0.25, "camera")
                           for s in self.sessions]
            finally:
                ImageLoader._decode_and_store_full = original

            self.assertTrue(all(r is not None for r in results))
            self.assertEqual(len(decodes), 1, "a shared photo must decode once, not once per tab")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestThumbCacheByteBudget(unittest.TestCase):
    """
    Measured on the reference machine: a cached 400x267 thumbnail is ~320 KB, and the
    three open tabs hold 503 photos (161 MB). An item cap of 180 evicted the first rows
    of the two larger tabs, so those rows re-decoded from ARW on every switch back.
    """

    def setUp(self):
        self.loader = ImageLoader()

    def test_budget_holds_a_realistic_multi_tab_working_set(self):
        thumb = Image.new("RGB", (400, 267), "blue")
        per_thumb = ImageLoader.estimate_bytes(thumb)

        total = 106 + 194 + 203  # the three open tabs
        self.assertLessEqual(
            total * per_thumb, self.loader.MAX_THUMB_CACHE_BYTES,
            "budget must hold every thumbnail of a realistic multi-tab session",
        )

    def test_all_three_tab_sizes_stay_resident(self):
        thumb = Image.new("RGB", (400, 267), "blue")
        resident = {}
        for tag, count in (("T1", 106), ("T2", 194), ("T3", 203)):
            for item in _items(count, tag):
                self.loader._store_thumb(self.loader.tier_key(item.path, 0.10, "camera"), thumb)
            resident[tag] = sum(
                1 for it in _items(count, tag)
                if self.loader.tier_key(it.path, 0.10, "camera") in self.loader._thumb_cache
            )

        self.assertEqual(resident, {"T1": 106, "T2": 194, "T3": 203})

    def test_byte_budget_evicts_before_item_cap(self):
        self.loader.MAX_THUMB_CACHE_BYTES = 4 * 1024 * 1024
        self.loader.MAX_THUMB_CACHE = 1000

        for i in range(8):
            self.loader._store_thumb(self.loader.tier_key(f"D:/Photos/BIG_{i}.JPG", 0.10, "camera"),
                                    Image.new("RGB", (1200, 800)))

        stats = self.loader.cache_stats()
        self.assertLessEqual(stats["thumb_bytes"], 4 * 1024 * 1024)
        self.assertEqual(stats["thumb_items"], 1)

    def test_item_cap_still_applies_to_tiny_images(self):
        self.loader.MAX_THUMB_CACHE_BYTES = 512 * 1024 * 1024
        self.loader.MAX_THUMB_CACHE = 5
        for i in range(12):
            self.loader._store_thumb(self.loader.tier_key(f"D:/Photos/S_{i}.JPG", 0.10, "camera"),
                                    Image.new("RGB", (8, 8)))
        self.assertEqual(len(self.loader._thumb_cache), 5)

    def test_stats_report_thumb_budget(self):
        stats = self.loader.cache_stats()
        self.assertIn("thumb_budget_bytes", stats)
        self.assertEqual(stats["thumb_budget_bytes"], self.loader.MAX_THUMB_CACHE_BYTES)


class TestTabWiringUsesSharedServices(unittest.TestCase):
    """
    Locks the gui.py wiring: tab sessions must be built from the app-level services, and
    those must exist before any tab is created (tab restore runs after _create_components).
    """

    def test_create_tab_info_passes_shared_services(self):
        from gui import ImageCullerApp

        app = MagicMock()
        app.db = MagicMock()
        app.exif_wrapper = shared_exif = ExifToolWrapper()
        app.image_loader = shared_loader = ImageLoader(exif_wrapper=shared_exif)

        tab = ImageCullerApp._create_tab_info(app, "D:/Photos/SOME_FOLDER")

        self.assertIs(tab["session"].image_loader, shared_loader)
        self.assertIs(tab["session"].exif_wrapper, shared_exif)

    def test_components_create_services_before_tabs(self):
        import inspect

        from gui import ImageCullerApp

        components_src = inspect.getsource(ImageCullerApp._create_components)
        self.assertIn("self.image_loader = ImageLoader", components_src,
                      "shared decode services belong to _create_components")
        self.assertIn("self.exif_wrapper = ExifToolWrapper()", components_src)

        init_src = inspect.getsource(ImageCullerApp.__init__)
        self.assertLess(init_src.index("_create_components"), init_src.index("_restore_tabs_state"),
                        "shared services must exist before tabs are restored")


if __name__ == "__main__":
    unittest.main()