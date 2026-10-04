import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from culler import exif_wrapper as exif_module
from culler.exif_wrapper import ExifToolWrapper


def _noisy_jpeg(size=(1616, 1080), quality=15) -> bytes:
    """A photograph-like JPEG, calibrated to the real preview size.

    A solid-colour image compresses to a few KB, which the extractor rejects as too
    small to be a preview. Noise at quality 15 lands at ~270 KB, matching the
    0.14-0.32 MB previews measured in this workspace.
    """
    width, height = size
    raw = os.urandom(width * height * 3)
    buf = io.BytesIO()
    Image.frombytes("RGB", (width, height), raw).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _make_arw_like(path: Path, preview_offset: int, jpeg_size=(1616, 1080), filler: bytes = b"\x00"):
    """Write a file whose embedded JPEG starts at ``preview_offset``."""
    jpeg = _noisy_jpeg(jpeg_size)
    with open(path, "wb") as f:
        f.write(filler * preview_offset)
        f.write(jpeg)
        f.write(filler * 1024)
    return len(jpeg)


class TestStreamingPreviewExtraction(unittest.TestCase):
    """
    Measured on this workspace: the embedded preview sits ~0.19 MB into a ~68 MB ARW, so
    a bounded streaming read finds it while reading ~1.5% of the bytes. The whole-file
    scan stays as the fallback because the offset is a property of the file, not a rule.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.wrapper = ExifToolWrapper()
        self.wrapper._is_available = False  # skip the ExifTool fallback in most tests

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_finds_preview_near_the_header(self):
        path = Path(self.temp_dir) / "NEAR.ARW"
        jpeg_len = _make_arw_like(path, preview_offset=200_000)

        found = self.wrapper.extract_preview_bytes(str(path))

        self.assertIsNotNone(found)
        self.assertGreater(len(found), 50_000)
        with Image.open(io.BytesIO(found)) as im:
            self.assertEqual(im.size, (1616, 1080))
        self.assertLessEqual(len(found), jpeg_len + 2)

    def test_large_preview_straddling_the_ceiling_is_still_found(self):
        """A candidate that starts near the boundary must be allowed to complete."""
        path = Path(self.temp_dir) / "STRADDLE.ARW"
        big = _noisy_jpeg((4000, 3000), quality=80)  # several MB, past the 1 MB ceiling
        self.assertGreater(len(big), self.wrapper.PREVIEW_SCAN_CEILING_BYTES)
        with open(path, "wb") as f:
            f.write(b"\x00" * 900_000)
            f.write(big)
            f.write(b"\x00" * 4096)
        self.assertLess(900_000 + len(big),
                        self.wrapper.PREVIEW_SCAN_CEILING_BYTES + self.wrapper.PREVIEW_TAIL_BYTES,
                        "fixture must stay inside the ceiling plus tail window")

        opened = {"bytes": 0}
        real_open = open

        def counting_open(file, mode="r", *args, **kwargs):
            if "b" in mode:
                handle = real_open(file, mode, *args, **kwargs)

                class Counting:
                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *exc):
                        return False

                    def read(self_inner, n=-1):
                        data = handle.read(n)
                        opened["bytes"] += len(data)
                        return data

                return Counting()
            return real_open(file, mode, *args, **kwargs)

        with patch("builtins.open", counting_open):
            found = self.wrapper.extract_preview_bytes(str(path))

        self.assertIsNotNone(found, "the straddling preview must still be extracted")
        with Image.open(io.BytesIO(found)) as im:
            self.assertEqual(im.size, (4000, 3000))
        ceiling = (self.wrapper.PREVIEW_SCAN_CEILING_BYTES
                   + self.wrapper.PREVIEW_TAIL_BYTES
                   + self.wrapper.PREVIEW_CHUNK_BYTES)
        self.assertLessEqual(opened["bytes"], ceiling)

    def test_reads_only_the_ceiling(self):
        """A 200 MB file whose preview sits 200 KB in must cost ~512 KB of reads."""
        path = Path(self.temp_dir) / "BIG.ARW"
        jpeg = _noisy_jpeg()
        with open(path, "wb") as f:
            f.write(b"\x00" * 200_000)
            f.write(jpeg)
            f.write(b"\x00" * (200 * 1024 * 1024))
        self.assertGreater(path.stat().st_size, 200 * 1024 * 1024)
        self.assertLess(200_000, self.wrapper.PREVIEW_SCAN_CEILING_BYTES,
                        "fixture must keep the preview inside the streaming window")

        opened = {"bytes": 0}
        real_open = open

        def counting_open(file, mode="r", *args, **kwargs):
            if "b" in mode:
                handle = real_open(file, mode, *args, **kwargs)

                class Counting:
                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *exc):
                        return False

                    def read(self_inner, n=-1):
                        data = handle.read(n)
                        opened["bytes"] += len(data)
                        return data

                return Counting()
            return real_open(file, mode, *args, **kwargs)

        with patch("builtins.open", counting_open):
            found = self.wrapper.extract_preview_bytes(str(path))

        self.assertIsNotNone(found)
        budget = (self.wrapper.PREVIEW_SCAN_CEILING_BYTES
                  + self.wrapper.PREVIEW_TAIL_BYTES
                  + self.wrapper.PREVIEW_CHUNK_BYTES)
        self.assertLessEqual(opened["bytes"], budget,
                             "must not read the whole file when the preview is near the header")
        self.assertLess(opened["bytes"], path.stat().st_size // 4)

    def test_falls_back_to_whole_file_when_preview_is_deep(self):
        path = Path(self.temp_dir) / "DEEP.ARW"
        deep_offset = 5 * 1024 * 1024  # beyond the 1 MB ceiling
        _make_arw_like(path, preview_offset=deep_offset)

        found = self.wrapper.extract_preview_bytes(str(path))

        self.assertIsNotNone(found, "a preview past the ceiling must still be found")
        with Image.open(io.BytesIO(found)) as im:
            self.assertEqual(im.size, (1616, 1080))

    def test_streaming_helper_respects_explicit_ceiling(self):
        path = Path(self.temp_dir) / "HELPER.ARW"
        _make_arw_like(path, preview_offset=200_000)

        original_tail = self.wrapper.PREVIEW_TAIL_BYTES
        self.wrapper.PREVIEW_TAIL_BYTES = 0
        try:
            self.assertIsNone(self.wrapper._extract_preview_streaming(str(path), 1200, ceiling=1024))
        finally:
            self.wrapper.PREVIEW_TAIL_BYTES = original_tail

        self.assertIsNotNone(
            self.wrapper._extract_preview_streaming(str(path), 1200, ceiling=2 * 1024 * 1024)
        )

    def test_missing_file_returns_none(self):
        self.assertIsNone(self.wrapper._extract_preview_streaming(
            str(Path(self.temp_dir) / "GONE.ARW"), 1200))
        with patch.object(self.wrapper, "_find_preview_in_bytes", side_effect=OSError("boom")):
            self.assertIsNone(self.wrapper._extract_preview_streaming(str(path := self.arw_path()), 1200))

    def arw_path(self):
        path = Path(self.temp_dir) / "X.ARW"
        _make_arw_like(path, 1000)
        return path

    def test_no_preview_returns_none(self):
        path = Path(self.temp_dir) / "EMPTY.ARW"
        path.write_bytes(b"\x00" * 4096)
        self.assertIsNone(self.wrapper.extract_preview_bytes(str(path)))

    def test_small_embedded_image_does_not_satisfy_min_width(self):
        path = Path(self.temp_dir) / "SMALL.ARW"
        _make_arw_like(path, preview_offset=1000, jpeg_size=(400, 300))
        self.assertIsNone(self.wrapper.extract_preview_bytes(str(path), min_width=1200))

    def test_find_preview_prefers_first_wide_enough(self):
        first = _noisy_jpeg((400, 300))
        second = _noisy_jpeg((1600, 1200))
        third = _noisy_jpeg((1800, 1200))

        data = first + b"\x00" * 2000 + second + b"\x00" * 2000 + third
        found = self.wrapper._find_preview_in_bytes(data, 1200)
        with Image.open(io.BytesIO(found)) as im:
            self.assertEqual(im.size, (1600, 1200), "first qualifying preview wins")

    def test_find_preview_falls_back_to_widest_when_none_qualify(self):
        small = _noisy_jpeg((900, 600))
        bigger = _noisy_jpeg((1100, 700))

        data = small + b"\x00" * 2000 + bigger
        found = self.wrapper._find_preview_in_bytes(data, 1200)
        with Image.open(io.BytesIO(found)) as im:
            self.assertEqual(im.size, (1100, 700), "best below min_width is still returned")


if __name__ == "__main__":
    unittest.main()