import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from culler.culler_engine import CullingSession, ImageItem, FlagState, _ProgressThrottle, _emit_progress
from culler.db_manager import DatabaseManager


class TestProgressThrottle(unittest.TestCase):
    """
    A folder scan used to fire one callback per photo, and the GUI turned each into an
    after(0, ...). A 5000-photo folder queued 5000 UI tasks for a bar that moved 0.02%.
    """

    def test_throttle_collapses_bursts(self):
        seen = []
        throttle = _ProgressThrottle(lambda done, total, name="": seen.append(done), interval=10.0)

        for i in range(1, 5001):
            throttle(i, 5000, f"IMG_{i}.JPG")

        # One update per whole percent, not one per photo: 5000 -> <=101.
        self.assertLessEqual(len(seen), 101, "a 5000-photo scan must not emit 5000 updates")
        self.assertGreaterEqual(len(seen), 50)
        self.assertEqual(seen[0], 1)
        self.assertEqual(seen[-1], 5000)

    def test_throttle_scales_with_percent_not_photo_count(self):
        def emit_for(count):
            seen = []
            throttle = _ProgressThrottle(lambda done, total, name="": seen.append(done), interval=10.0)
            for i in range(1, count + 1):
                throttle(i, count, f"IMG_{i}.JPG")
            return len(seen)

        small = emit_for(200)
        large = emit_for(20000)
        self.assertLessEqual(large, 110)
        self.assertLess(large, small * 2)

    def test_throttle_emits_on_percent_change(self):
        seen = []
        throttle = _ProgressThrottle(lambda done, total, name="": seen.append(done), interval=10.0)

        for i in range(1, 101):
            throttle(i, 100)

        self.assertGreater(len(seen), 5, "large percent jumps must still be reported")
        self.assertEqual(seen[-1], 100)

    def test_throttle_allows_callback_when_progress_is_slow(self):
        seen = []
        throttle = _ProgressThrottle(lambda done, total, name="": seen.append(done), interval=0.0)

        for i in range(1, 6):
            throttle(i, 100)
            time.sleep(0.002)

        self.assertEqual(seen, [1, 2, 3, 4, 5])

    def test_throttle_is_a_noop_without_callback(self):
        throttle = _ProgressThrottle(None)
        throttle(1, 10)  # must not raise

    def test_emit_progress_supports_both_signatures(self):
        three = []
        two = []
        _emit_progress(lambda d, t, n="": three.append((d, t, n)), 1, 2, "a.jpg")
        _emit_progress(lambda d, t: two.append((d, t)), 1, 2, "a.jpg")
        self.assertEqual(three, [(1, 2, "a.jpg")])
        self.assertEqual(two, [(1, 2)])

    def test_emit_progress_without_callback(self):
        _emit_progress(None, 1, 2)  # must not raise


class TestBatchDbWrites(unittest.TestCase):
    """
    save_item_record opened a connection and committed per photo (per stacked path).
    """

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = DatabaseManager(db_path=self.db_path)
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _item(self, name, stacked=("ALPHA.ARW", "ALPHA.JPG")):
        paths = [Path(self.temp_dir) / n for n in stacked]
        for p in paths:
            if not p.exists():
                p.write_bytes(b"0" * 16)
        item = ImageItem(paths[0])
        item.stacked_paths = list(paths)
        item.is_stacked = len(paths) > 1
        item.flag = FlagState.UNFLAGGED
        return item

    def test_batch_save_persists_every_path_of_every_item(self):
        items = [self._item(f"S{i}.ARW", (f"S{i}.ARW", f"S{i}.JPG")) for i in range(5)]
        items.append(self._item("SINGLE.JPG", ("SINGLE.JPG",)))

        self.db.save_image_records([
            {"file_path": str(p), "filename": p.name, "flag": "UNFLAGGED"}
            for item in items for p in item.stacked_paths
        ])

        records = self.db.get_all_records_for_dir(self.temp_dir)
        expected = {str(p) for item in items for p in item.stacked_paths}
        self.assertEqual(set(records.keys()), expected)

    def test_batch_save_upserts_existing_rows(self):
        path = Path(self.temp_dir) / "ONE.JPG"
        path.write_bytes(b"0" * 8)

        self.db.save_image_records([{"file_path": str(path), "filename": "ONE.JPG", "flag": "UNFLAGGED", "rating": 0}])
        self.db.save_image_records([{"file_path": str(path), "filename": "ONE.JPG", "flag": "PICK", "rating": 3}])

        records = self.db.get_all_records_for_dir(self.temp_dir)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[str(path)]["flag"], "PICK")
        self.assertEqual(records[str(path)]["rating"], 3)

    def test_batch_save_with_empty_list_is_a_noop(self):
        self.db.save_image_records([])
        self.assertEqual(self.db.get_all_records_for_dir(self.temp_dir), {})

    def test_session_batches_item_records(self):
        session = CullingSession(db_manager=self.db)
        items = [self._item(f"T{i}.ARW", (f"T{i}.ARW", f"T{i}.JPG")) for i in range(4)]

        session.save_item_records(items)

        records = self.db.get_all_records_for_dir(self.temp_dir)
        self.assertEqual(len(records), 8)

    def test_single_save_still_works(self):
        session = CullingSession(db_manager=self.db)
        item = self._item("SOLO.ARW", ("SOLO.ARW", "SOLO.JPG"))

        session.save_item_record(item)

        self.assertEqual(len(self.db.get_all_records_for_dir(self.temp_dir)), 2)

    def test_session_without_db_is_a_noop(self):
        session = CullingSession(db_manager=None)
        session.db = None
        session.save_item_records([self._item("X.JPG", ("X.JPG",))])

    def test_batch_save_opens_fewer_connections_than_rows(self):
        """One connection for the batch, versus one per record before."""
        rows = [{"file_path": str(Path(self.temp_dir) / f"C{i}.JPG"), "filename": f"C{i}.JPG",
                 "flag": "UNFLAGGED"} for i in range(50)]
        for r in rows:
            (Path(self.temp_dir) / r["filename"]).write_bytes(b"0" * 8)

        opened = []
        original = self.db._get_connection

        def counting():
            conn = original()
            opened.append(conn)
            return conn

        self.db._get_connection = counting
        try:
            self.db.save_image_records(rows)
        finally:
            self.db._get_connection = original

        self.assertEqual(len(opened), 1, "a batch must use a single connection")


if __name__ == "__main__":
    unittest.main()