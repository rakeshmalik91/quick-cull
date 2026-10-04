import json
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

from culler import dataset_exporter
from culler.dataset_exporter import load_manual_annotations, clear_manual_annotation_cache
from culler.image_loader import ImageLoader


def _jpeg_bytes(directory, name, size=(1600, 1200)):
    path = Path(directory) / name
    Image.new("RGB", size, "green").save(path, format="JPEG")
    return path.read_bytes()


class TestArwPreviewCache(unittest.TestCase):
    """
    Embedded preview extraction reads the whole ARW; it must not repeat for every
    thumbnail and full decode of the same file.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.arw = Path(self.temp_dir) / "SHOT.ARW"
        self.arw.write_bytes(b"\x00" * 4096)

        self.wrapper = MagicMock()
        self.wrapper.is_available.return_value = True
        self.wrapper.get_orientation.return_value = 1
        self.loader = ImageLoader(exif_wrapper=self.wrapper)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_preview_extracted_once_for_repeated_decodes(self):
        data = _jpeg_bytes(self.temp_dir, "preview.jpg")
        calls = []
        self.wrapper.extract_preview_bytes.side_effect = lambda path, **kw: (calls.append(path), data)[1]

        for _ in range(3):
            self.loader._get_cached_preview_bytes(self.arw)

        self.assertEqual(len(calls), 1)

    def test_preview_cache_invalidated_when_file_changes(self):
        data = _jpeg_bytes(self.temp_dir, "preview.jpg")
        calls = []
        self.wrapper.extract_preview_bytes.side_effect = lambda path, **kw: (calls.append(path), data)[1]

        self.loader._get_cached_preview_bytes(self.arw)
        time.sleep(0.02)
        self.arw.write_bytes(b"\x00" * 8192)
        self.loader._get_cached_preview_bytes(self.arw)

        self.assertEqual(len(calls), 2, "a modified ARW must re-extract")

    def test_arw_decode_path_uses_the_cache(self):
        """
        Guards the call site: two decodes of the same ARW must extract the preview once.
        """
        self.wrapper.extract_preview_bytes.side_effect = lambda path, **kw: _jpeg_bytes(self.temp_dir, "p.jpg")

        first = self.loader._load_arw_image(str(self.arw), raw_scale=0.25, white_balance="camera")
        second = self.loader._load_arw_image(str(self.arw), raw_scale=0.25, white_balance="camera")

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(self.wrapper.extract_preview_bytes.call_count, 1)
        self.assertEqual(len(self.loader._preview_cache), 1)

    def test_missing_file_does_not_crash(self):
        self.wrapper.extract_preview_bytes.side_effect = lambda path, **kw: b""
        self.loader._get_cached_preview_bytes(Path(self.temp_dir) / "GONE.ARW")
        self.assertEqual(len(self.loader._preview_cache), 0)

    def test_clear_cache_drops_preview_cache(self):
        self.wrapper.extract_preview_bytes.side_effect = lambda path, **kw: _jpeg_bytes(self.temp_dir, "p.jpg")

        self.loader._get_cached_preview_bytes(self.arw)
        self.assertEqual(len(self.loader._preview_cache), 1)

        self.loader.clear_cache()
        self.assertEqual(len(self.loader._preview_cache), 0)

    def test_preview_cache_is_capped(self):
        self.wrapper.extract_preview_bytes.side_effect = lambda path, **kw: _jpeg_bytes(self.temp_dir, "p.jpg")

        cap = self.loader.MAX_PREVIEW_CACHE
        for i in range(cap + 4):
            p = Path(self.temp_dir) / f"M{i}.ARW"
            p.write_bytes(b"\x00" * (4096 + i))
            self.loader._get_cached_preview_bytes(p)

        self.assertLessEqual(len(self.loader._preview_cache), cap)


class TestManualAnnotationsCache(unittest.TestCase):
    """
    load_manual_annotations re-read and re-resolved every key on each directory scan.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.dataset_dir = Path(self.temp_dir) / "_DATASET"
        self.dataset_dir.mkdir(parents=True, exist_ok=True)
        self.image = Path(self.temp_dir) / "SHOT.JPG"
        self.image.write_bytes(b"\x00" * 16)
        clear_manual_annotation_cache()

    def tearDown(self):
        clear_manual_annotation_cache()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_annotations(self, records):
        with open(self.dataset_dir / "annotations.json", "w", encoding="utf-8") as f:
            json.dump(records, f)

    def test_repeated_load_is_served_from_cache(self):
        self._write_annotations({
            str(self.image): {
                "filename": "SHOT.JPG",
                "manual_detection_box": [0.1, 0.1, 0.5, 0.5],
                "manual_eye_box": None,
                "updated_at": "now",
            }
        })

        first = load_manual_annotations(dataset_dir=str(self.dataset_dir))
        second = load_manual_annotations(dataset_dir=str(self.dataset_dir))

        self.assertIs(first, second, "second load must reuse the memoised dict")
        self.assertEqual(len(dataset_exporter._ANNOTATION_CACHE), 1)
        entry = list(first.values())[0]
        self.assertEqual(entry["manual_detection_box"], (0.1, 0.1, 0.5, 0.5))

    def test_cache_invalidated_when_file_changes(self):
        self._write_annotations({str(self.image): {"filename": "SHOT.JPG"}})
        first = load_manual_annotations(dataset_dir=str(self.dataset_dir))
        self.assertEqual(len(first), 1)

        time.sleep(0.02)
        second_image = Path(self.temp_dir) / "OTHER.JPG"
        second_image.write_bytes(b"\x00" * 16)
        self._write_annotations({
            str(self.image): {"filename": "SHOT.JPG"},
            str(second_image): {"filename": "OTHER.JPG"},
        })

        second = load_manual_annotations(dataset_dir=str(self.dataset_dir))
        self.assertEqual(len(second), 2)

    def test_saving_annotation_invalidates_cache(self):
        self._write_annotations({str(self.image): {"filename": "SHOT.JPG"}})
        load_manual_annotations(dataset_dir=str(self.dataset_dir))

        dataset_exporter.save_manual_annotation(
            str(self.image), manual_detection_box=(0.2, 0.2, 0.6, 0.6),
            manual_eye_box=None, dataset_dir=str(self.dataset_dir),
        )

        reloaded = load_manual_annotations(dataset_dir=str(self.dataset_dir))
        entry = list(reloaded.values())[0]
        self.assertEqual(entry["manual_detection_box"], (0.2, 0.2, 0.6, 0.6))

    def test_deleting_annotation_invalidates_cache(self):
        self._write_annotations({str(self.image): {"filename": "SHOT.JPG"}})
        load_manual_annotations(dataset_dir=str(self.dataset_dir))

        dataset_exporter.delete_manual_annotation(str(self.image), dataset_dir=str(self.dataset_dir))

        self.assertEqual(load_manual_annotations(dataset_dir=str(self.dataset_dir)), {})

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_manual_annotations(dataset_dir=str(Path(self.temp_dir) / "nope")), {})


if __name__ == "__main__":
    unittest.main()