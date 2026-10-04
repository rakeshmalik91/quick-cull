import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from culler import exif_wrapper as exif_module
from culler.exif_wrapper import ExifToolWrapper


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_exiftool(payload_for_paths, record):
    """Build a stand-in for _run_cli that answers like `exiftool -json`.

    `record` collects every invocation so tests can assert on chunking.
    """

    def _run(cmd, **kwargs):
        record.append(list(cmd))
        paths = [str(a) for a in cmd[1:] if not str(a).startswith("-")]
        if cmd[1:2] == ["-ver"]:
            return _FakeCompleted(returncode=0, stdout="12.40\n")
        items = []
        for p in paths:
            payload = payload_for_paths.get(p)
            if payload is None:
                continue
            item = {"SourceFile": p}
            item.update(payload)
            items.append(item)
        return _FakeCompleted(returncode=0, stdout=json.dumps(items))

    return _run


class TestIsAvailableMemoization(unittest.TestCase):
    """
    is_available() sits on the per-image RAW decode path; each probe is a process spawn.
    """

    def test_probes_once_and_caches_result(self):
        wrapper = ExifToolWrapper()
        record = []
        with patch.object(exif_module, "_run_cli", _fake_exiftool({}, record)):
            self.assertTrue(wrapper.is_available())
            self.assertTrue(wrapper.is_available())
            self.assertTrue(wrapper.is_available())

        version_calls = [c for c in record if c[1:2] == ["-ver"]]
        self.assertEqual(len(version_calls), 1, "exiftool -ver must be spawned at most once")

    def test_negative_result_is_also_cached(self):
        wrapper = ExifToolWrapper()
        calls = []

        def always_missing(cmd, **kwargs):
            calls.append(list(cmd))
            raise FileNotFoundError("exiftool")

        with patch.object(exif_module, "_run_cli", always_missing):
            self.assertFalse(wrapper.is_available())
            self.assertFalse(wrapper.is_available())

        self.assertEqual(len(calls), 1)

    def test_reset_allows_reprobe(self):
        wrapper = ExifToolWrapper()
        record = []
        with patch.object(exif_module, "_run_cli", _fake_exiftool({}, record)):
            wrapper.is_available()
            wrapper.reset_availability_cache()
            wrapper.is_available()

        version_calls = [c for c in record if c[1:2] == ["-ver"]]
        self.assertEqual(len(version_calls), 2)


class TestBatchMetadataChunking(unittest.TestCase):
    """
    A single argv cannot hold a whole folder on Windows; oversized calls used to raise and
    silently degrade into the per-file PIL reader for every file.
    """

    def setUp(self):
        self.wrapper = ExifToolWrapper()
        self.wrapper._is_available = True

    def test_empty_input(self):
        self.assertEqual(self.wrapper.get_batch_metadata([]), [])

    def test_small_batch_is_one_invocation(self):
        paths = [f"D:/Photos/P{i}.JPG" for i in range(5)]
        record = []
        with patch.object(exif_module, "_run_cli", _fake_exiftool({p: {"File:ImageWidth": 100} for p in paths}, record)):
            result = self.wrapper.get_batch_metadata(paths)

        self.assertEqual(len(record), 1)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]["width"], 100)

    def test_large_batch_is_chunked(self):
        count = ExifToolWrapper.BATCH_ARGV_LIMIT * 2 + 7
        paths = [f"D:/Photos/P{i}.JPG" for i in range(count)]
        payload = {p: {"File:ImageWidth": 600, "File:ImageHeight": 400} for p in paths}
        record = []
        with patch.object(exif_module, "_run_cli", _fake_exiftool(payload, record)):
            result = self.wrapper.get_batch_metadata(paths)

        self.assertEqual(len(result), count, "one result per requested path, in order")
        self.assertTrue(all(r["width"] == 600 for r in result))
        self.assertEqual(len(record), 8, "split by concurrency, each chunk under the argv limit")
        for cmd in record:
            path_args = [a for a in cmd[1:] if not a.startswith("-")]
            self.assertLessEqual(len(path_args), ExifToolWrapper.BATCH_ARGV_LIMIT)

    def test_mid_size_folder_splits_by_concurrency(self):
        """A folder smaller than the argv limit still uses several processes."""
        count = 105
        paths = [f"D:/Photos/M{i}.JPG" for i in range(count)]
        payload = {p: {"File:ImageWidth": 100} for p in paths}
        record = []
        with patch.object(exif_module, "_run_cli", _fake_exiftool(payload, record)):
            result = self.wrapper.get_batch_metadata(paths)

        self.assertEqual(len(result), count)
        self.assertGreater(len(record), 1, "105 files should not run as a single serial call")
        self.assertLessEqual(len(record), self.wrapper.BATCH_CONCURRENCY)
        self.assertGreaterEqual(count // len(record), self.wrapper.MIN_CHUNK_PATHS)

    def test_small_folder_stays_a_single_call(self):
        """Below the spawn break-even, splitting would cost more than it saves."""
        count = ExifToolWrapper.BATCH_CONCURRENCY * self.wrapper.MIN_CHUNK_PATHS - 1
        paths = [f"D:/Photos/T{i}.JPG" for i in range(count)]
        payload = {p: {"File:ImageWidth": 100} for p in paths}
        record = []
        with patch.object(exif_module, "_run_cli", _fake_exiftool(payload, record)):
            result = self.wrapper.get_batch_metadata(paths)

        self.assertEqual(len(result), count)
        self.assertEqual(len(record), 1)

    def test_concurrency_one_keeps_single_chunk_behaviour(self):
        self.wrapper.BATCH_CONCURRENCY = 1
        count = 105
        paths = [f"D:/Photos/S{i}.JPG" for i in range(count)]
        record = []
        with patch.object(exif_module, "_run_cli", _fake_exiftool({}, record)):
            self.wrapper.get_batch_metadata(paths)
        self.assertEqual(len(record), 1)

    def test_chunking_keeps_results_aligned_with_input(self):
        first = [f"D:/Photos/A{i}.JPG" for i in range(ExifToolWrapper.BATCH_ARGV_LIMIT)]
        second = [f"D:/Photos/B{i}.JPG" for i in range(5)]
        paths = first + second
        payload = {}
        for i, p in enumerate(first):
            payload[p] = {"File:ImageWidth": 1000 + i}
        for i, p in enumerate(second):
            payload[p] = {"File:ImageWidth": 2000 + i}
        record = []
        with patch.object(exif_module, "_run_cli", _fake_exiftool(payload, record)):
            result = self.wrapper.get_batch_metadata(paths)

        self.assertEqual(len(result), len(paths))
        self.assertEqual(result[0]["width"], 1000)
        self.assertEqual(result[len(first) - 1]["width"], 1000 + len(first) - 1)
        self.assertEqual(result[len(first)]["width"], 2000)
        self.assertEqual(result[-1]["width"], 2000 + 4)

    def test_chunk_argv_stays_under_limit(self):
        long_paths = [("D:/Photos/" + "x" * 90 + f"{i}.JPG") for i in range(400)]
        record = []
        with patch.object(exif_module, "_run_cli", _fake_exiftool({}, record)):
            self.wrapper.get_batch_metadata(long_paths)

        for cmd in record:
            argv_len = sum(len(a) for a in cmd)
            self.assertLess(argv_len, 32767, "argv would exceed the Windows command-line limit")

    def test_missing_exiftool_uses_pil_fallback_once_per_path(self):
        wrapper = ExifToolWrapper()
        wrapper._is_available = False
        paths = [f"D:/Photos/MISSING{i}.JPG" for i in range(3)]
        with patch.object(wrapper, "_get_pil_fallback_metadata", side_effect=lambda p: {"file_type": "JPG"}) as fallback:
            result = wrapper.get_batch_metadata(paths)

        self.assertEqual(len(result), 3)
        self.assertEqual(fallback.call_count, 3)


if __name__ == "__main__":
    unittest.main()