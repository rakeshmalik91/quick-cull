import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from culler.folder_watcher import FolderWatcher, FolderChange


def _write_image(directory: Path, name: str, size: int = 8) -> Path:
    path = directory / name
    path.write_bytes(b"0" * size)
    return path


def _backdate(path: Path, seconds: float = 7200.0) -> None:
    """Pretend the file was last written long ago, so it counts as settled."""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


class TestFolderWatcher(unittest.TestCase):
    """
    Unit tests for FolderWatcher change detection, debounce, and suppression.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.directory = Path(self.temp_dir)
        self.changes = []
        self.watcher = FolderWatcher(poll_interval=3600, settle_seconds=0.0, write_grace_seconds=0.0)

    def tearDown(self):
        self.watcher.stop(timeout=0.1)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _watch(self) -> bool:
        return self.watcher.watch(self.directory, self.changes.append)

    def _poll_twice(self) -> None:
        """Advance two poll cycles so a change counts as stable and is reported."""
        self.watcher.poll_now()
        time.sleep(0.01)
        self.watcher.poll_now()

    def test_watch_returns_false_for_missing_directory(self):
        missing = self.directory / "nope"
        self.assertFalse(self.watcher.watch(missing, self.changes.append))
        self.assertFalse(self.watcher.is_watching(missing))

    def test_watch_takes_baseline_without_reporting(self):
        _write_image(self.directory, "A.JPG")
        self.assertTrue(self._watch())
        self.watcher.poll_now()
        self.assertEqual(self.changes, [])

    def test_added_file_is_reported(self):
        _write_image(self.directory, "A.JPG")
        self._watch()

        _write_image(self.directory, "B.JPG")
        self._poll_twice()

        self.assertEqual(len(self.changes), 1)
        self.assertEqual(self.changes[0].added, ("B.JPG",))
        self.assertEqual(self.changes[0].removed, ())
        self.assertEqual(self.changes[0].total, 1)

    def test_removed_files_are_reported(self):
        keep = _write_image(self.directory, "A.JPG")
        gone_a = _write_image(self.directory, "B.JPG")
        gone_b = _write_image(self.directory, "C.ARW")
        self._watch()

        gone_a.unlink()
        gone_b.unlink()
        self._poll_twice()

        self.assertEqual(len(self.changes), 1)
        self.assertEqual(self.changes[0].removed, ("B.JPG", "C.ARW"))
        self.assertEqual(self.changes[0].added, ())
        self.assertTrue(keep.exists())

    def test_modified_file_is_reported(self):
        path = _write_image(self.directory, "A.JPG", size=8)
        self._watch()

        _write_image(self.directory, "A.JPG", size=64)
        self._poll_twice()

        self.assertEqual(len(self.changes), 1)
        self.assertEqual(self.changes[0].modified, ("A.JPG",))
        self.assertEqual(self.changes[0].added, ())

    def test_unsupported_and_subfolder_changes_are_ignored(self):
        _write_image(self.directory, "A.JPG")
        self._watch()

        (self.directory / "notes.txt").write_text("hello")
        sub = self.directory / "_SELECTED"
        os.makedirs(sub)
        _write_image(sub, "MOVED.JPG")
        self.watcher.poll_now()

        self.assertEqual(self.changes, [])

    def test_change_is_reported_only_once(self):
        self._watch()
        _write_image(self.directory, "A.JPG")
        self._poll_twice()

        self.assertEqual(len(self.changes), 1)
        self.watcher.poll_now()
        self.watcher.poll_now()
        self.assertEqual(len(self.changes), 1)

    def test_partially_written_file_is_reported_once(self):
        """
        A file still being written must not be reported as added and then again as
        modified once the write lands.
        """
        watcher = FolderWatcher(poll_interval=3600, settle_seconds=0.0, write_grace_seconds=0.0)
        try:
            reported = []
            watcher.watch(self.directory, reported.append)

            for size in (4096, 8192, 16384):
                _write_image(self.directory, "A.JPG", size=size)
                watcher.poll_now()
            self.assertEqual(reported, [], "growing file must not be reported while in flight")

            watcher.poll_now()
            watcher.poll_now()

            self.assertEqual(len(reported), 1)
            self.assertEqual(reported[0].added, ("A.JPG",))
            self.assertEqual(reported[0].modified, ())
        finally:
            watcher.stop(timeout=0.1)

    def test_edited_file_is_reported_as_modified(self):
        watcher = FolderWatcher(poll_interval=3600, settle_seconds=0.0, write_grace_seconds=0.0)
        try:
            reported = []
            _write_image(self.directory, "A.JPG", size=8)
            watcher.watch(self.directory, reported.append)

            _write_image(self.directory, "A.JPG", size=64)
            watcher.poll_now()
            watcher.poll_now()

            self.assertEqual(len(reported), 1)
            self.assertEqual(reported[0].modified, ("A.JPG",))
            self.assertEqual(reported[0].added, ())
        finally:
            watcher.stop(timeout=0.1)

    def test_change_needs_a_stable_second_poll(self):
        watcher = FolderWatcher(poll_interval=3600, settle_seconds=0.0, write_grace_seconds=0.0)
        try:
            reported = []
            watcher.watch(self.directory, reported.append)

            _write_image(self.directory, "A.JPG", size=8)
            watcher.poll_now()
            self.assertEqual(reported, [], "one poll is not enough to call a change stable")

            _write_image(self.directory, "A.JPG", size=16)
            watcher.poll_now()
            watcher.poll_now()
            self.assertEqual(len(reported), 1)
            self.assertEqual(reported[0].added, ("A.JPG",))
        finally:
            watcher.stop(timeout=0.1)

    def test_snapshot_reads_live_size_not_scandir_cache(self):
        """
        os.scandir returns a stale size on Windows for a file that is still being
        written, which made a growing photo look stable and got it reported
        mid-copy. The snapshot must stat each path directly.
        """
        path = _write_image(self.directory, "A.ARW", size=1024)
        snapshot = FolderWatcher._snapshot(self.directory)
        self.assertEqual(snapshot["A.ARW"][0], 1024)

        _write_image(self.directory, "A.ARW", size=4096)
        self.assertEqual(FolderWatcher._snapshot(self.directory)["A.ARW"][0], 4096)
        self.assertEqual(path.stat().st_size, 4096)

    def test_reported_add_is_not_followed_by_close_time_mtime_bump(self):
        """
        Windows refreshes mtime when a writer closes the file. With the grace
        armed, that bump must not turn one addition into a second event.
        """
        watcher = FolderWatcher(poll_interval=3600, settle_seconds=0.0, write_grace_seconds=3600.0)
        try:
            reported = []
            path = self.directory / "COPY.ARW"
            watcher.watch(self.directory, reported.append)

            _write_image(self.directory, "COPY.ARW", size=8192)
            watcher.poll_now()
            watcher.poll_now()
            self.assertEqual(reported, [], "file is still settling")

            _backdate(path)
            watcher.poll_now()
            watcher.poll_now()
            self.assertEqual(len(reported), 1)

            # Writer closes the handle: NTFS refreshes mtime here.
            os.utime(path, None)
            watcher.poll_now()
            watcher.poll_now()
            self.assertEqual(len(reported), 1, "close-time mtime bump must not re-report")
        finally:
            watcher.stop(timeout=0.1)

    def test_deletion_is_reported_after_stable_poll(self):
        watcher = FolderWatcher(poll_interval=3600, settle_seconds=0.0, write_grace_seconds=0.0)
        try:
            reported = []
            path = _write_image(self.directory, "A.JPG")
            watcher.watch(self.directory, reported.append)

            path.unlink()
            watcher.poll_now()
            watcher.poll_now()

            self.assertEqual(len(reported), 1)
            self.assertEqual(reported[0].removed, ("A.JPG",))
        finally:
            watcher.stop(timeout=0.1)

    def test_unstable_change_is_not_reported(self):
        debounced = FolderWatcher(poll_interval=3600, settle_seconds=60.0, write_grace_seconds=0.0)
        try:
            debounced.watch(self.directory, self.changes.append)
            _write_image(self.directory, "A.JPG")
            debounced.poll_now()
            debounced.poll_now()
            self.assertEqual(self.changes, [])
        finally:
            debounced.stop(timeout=0.1)

    def test_missing_directory_snapshot_is_empty(self):
        _write_image(self.directory, "A.JPG")
        self._watch()
        shutil.rmtree(self.temp_dir)

        self._poll_twice()
        self.assertEqual(len(self.changes), 1)
        self.assertEqual(self.changes[0].removed, ("A.JPG",))

    def test_suppressed_changes_are_adopted_silently(self):
        _write_image(self.directory, "A.JPG")
        self._watch()

        self.watcher.suppress(self.directory, seconds=60.0)
        _write_image(self.directory, "B.JPG")
        self.watcher.poll_now()
        self.assertEqual(self.changes, [])

        self.watcher.resync(self.directory)
        _write_image(self.directory, "C.JPG")
        self._poll_twice()
        self.assertEqual(len(self.changes), 1)
        self.assertEqual(self.changes[0].added, ("C.JPG",))

    def test_suppression_window_is_extended_not_shortened(self):
        self._watch()
        self.watcher.suppress(self.directory, seconds=60.0)
        self.watcher.suppress(self.directory, seconds=0.0)

        _write_image(self.directory, "A.JPG")
        self.watcher.poll_now()
        self.assertEqual(self.changes, [])

    def test_resync_adopts_current_state(self):
        _write_image(self.directory, "A.JPG")
        self._watch()

        _write_image(self.directory, "B.JPG")
        self.watcher.resync(self.directory)
        self.watcher.poll_now()

        self.assertEqual(self.changes, [])

    def test_unwatch_stops_reporting(self):
        self._watch()
        self.watcher.unwatch(self.directory)
        self.assertFalse(self.watcher.is_watching(self.directory))

        _write_image(self.directory, "A.JPG")
        self.watcher.poll_now()
        self.assertEqual(self.changes, [])

    def test_watch_replaces_callback_for_same_directory(self):
        other = []
        self._watch()
        self.assertTrue(self.watcher.watch(self.directory, other.append))

        _write_image(self.directory, "A.JPG")
        self._poll_twice()

        self.assertEqual(self.changes, [])
        self.assertEqual(len(other), 1)

    def test_watched_directories_lists_watches(self):
        self._watch()
        self.assertEqual(len(self.watcher.watched_directories()), 1)

    def test_callback_error_does_not_kill_polling(self):
        calls = []

        def boom(_change):
            calls.append("boom")
            raise RuntimeError("callback failed")

        self.watcher.watch(self.directory, boom)
        _write_image(self.directory, "A.JPG")
        self._poll_twice()

        self.assertEqual(calls, ["boom"])

        later = []
        self.watcher.watch(self.directory, later.append)
        _write_image(self.directory, "B.JPG")
        self._poll_twice()
        self.assertEqual(len(later), 1)

    def test_background_thread_reports_changes(self):
        threaded = FolderWatcher(poll_interval=0.05, settle_seconds=0.0, write_grace_seconds=0.0)
        try:
            threaded.watch(self.directory, self.changes.append)
            threaded.start()
            self.assertTrue(threaded.is_running)
            _write_image(self.directory, "A.JPG")
            deadline = time.monotonic() + 5.0
            while not self.changes and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(len(self.changes), 1)
            self.assertEqual(self.changes[0].added, ("A.JPG",))
        finally:
            threaded.stop(timeout=1.0)
        self.assertFalse(threaded.is_running)

    def test_watch_does_not_start_thread_until_start(self):
        self._watch()
        self.assertFalse(self.watcher.is_running)
        self.watcher.start()
        self.assertTrue(self.watcher.is_running)

    def test_summary_is_human_readable(self):
        change = FolderChange(directory=self.directory, added=("B.JPG",), removed=("A.ARW",))
        summary = change.summary()
        self.assertIn("+1 added", summary)
        self.assertIn("-1 removed", summary)


class TestFolderChange(unittest.TestCase):
    def test_total_counts_all_kinds(self):
        change = FolderChange(
            directory=Path("D:/Photos"),
            added=("A.JPG",),
            removed=("B.ARW", "C.NEF"),
            modified=("D.JPG",),
        )
        self.assertEqual(change.total, 4)


if __name__ == "__main__":
    unittest.main()