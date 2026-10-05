"""Folder manifest: classification of add/modify/delete/rename, and the scan diff."""

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from culler.folder_index import FileEntry, FolderIndex, entry_key, scan_entries


def _entry(path, size=100, mtime=1_000_000_000):
    return FileEntry(Path(path), size, mtime)


def _index(**entries):
    return FolderIndex({entry_key(e.path): e for e in entries.values()})


class TestScanEntries(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _make(self, name, size=(40, 30), fmt="JPEG"):
        path = self.dir / name
        Image.new("RGB", size, "blue").save(path, format=fmt)
        return path

    def test_only_supported_files_are_listed(self):
        self._make("A.JPG")
        self._make("B.ARW")
        self._make("notes.txt")
        (self.dir / "sub").mkdir()

        entries = scan_entries(self.dir)

        self.assertEqual(sorted(p.name for p in (e.path for e in entries.values())),
                         ["A.JPG", "B.ARW"])

    def test_entries_carry_size_and_mtime(self):
        path = self._make("A.JPG")
        entry = scan_entries(self.dir)[entry_key(path)]

        self.assertEqual(entry.size, path.stat().st_size)
        self.assertEqual(entry.mtime_ns, path.stat().st_mtime_ns)

    def test_missing_directory_yields_nothing(self):
        self.assertEqual(scan_entries(self.dir / "gone"), {})

    def test_keys_are_case_normalised(self):
        path = self._make("MixedCase.JPG")
        keys = scan_entries(self.dir)
        self.assertIn(entry_key(path), keys)
        self.assertIn(os.path.normcase(str(path)), keys)


class TestFolderDiff(unittest.TestCase):
    def test_noop_when_nothing_moved(self):
        index = _index(a=_entry("p/A.JPG"))
        diff = index.diff({entry_key("p/A.JPG"): _entry("p/A.JPG")})
        self.assertTrue(diff.is_noop)
        self.assertEqual(len(diff.unchanged), 1)

    def test_added_file(self):
        index = _index(a=_entry("p/A.JPG"))
        diff = index.diff({
            entry_key("p/A.JPG"): _entry("p/A.JPG"),
            entry_key("p/B.JPG"): _entry("p/B.JPG"),
        })
        self.assertEqual([e.path.name for e in diff.added], ["B.JPG"])
        self.assertTrue(diff.is_noop is False)

    def test_removed_file(self):
        index = _index(a=_entry("p/A.JPG"), b=_entry("p/B.JPG"))
        diff = index.diff({entry_key("p/A.JPG"): _entry("p/A.JPG")})
        self.assertEqual([e.path.name for e in diff.removed], ["B.JPG"])
        self.assertEqual([p.name for p in diff.vanished_paths], ["B.JPG"])

    def test_modified_file_is_changed_not_add_plus_remove(self):
        index = _index(a=_entry("p/A.JPG", size=100, mtime=1))
        diff = index.diff({entry_key("p/A.JPG"): _entry("p/A.JPG", size=120, mtime=2)})
        self.assertEqual([e.path.name for e in diff.changed], ["A.JPG"])
        self.assertEqual(diff.added, ())
        self.assertEqual(diff.removed, ())
        self.assertEqual([p.name for p in diff.touched_paths], ["A.JPG"])
        self.assertEqual([p.name for p in diff.stale_paths], ["A.JPG"])

    def test_same_size_different_mtime_is_changed(self):
        index = _index(a=_entry("p/A.JPG", size=100, mtime=1))
        diff = index.diff({entry_key("p/A.JPG"): _entry("p/A.JPG", size=100, mtime=9)})
        self.assertEqual(len(diff.changed), 1)

    def test_rename_is_detected_and_reported_once(self):
        index = _index(a=_entry("p/OLD.JPG", size=500, mtime=7))
        diff = index.diff({entry_key("p/NEW.JPG"): _entry("p/NEW.JPG", size=500, mtime=7)})

        self.assertEqual(len(diff.renamed), 1)
        old, new = diff.renamed[0]
        self.assertEqual(old.path.name, "OLD.JPG")
        self.assertEqual(new.path.name, "NEW.JPG")
        self.assertEqual(diff.added, ())
        self.assertEqual(diff.removed, ())

    def test_rename_counts_as_touched_and_stale(self):
        index = _index(a=_entry("p/OLD.JPG", size=500, mtime=7))
        diff = index.diff({entry_key("p/NEW.JPG"): _entry("p/NEW.JPG", size=500, mtime=7)})
        self.assertEqual([p.name for p in diff.touched_paths], ["NEW.JPG"])
        self.assertEqual([p.name for p in diff.stale_paths], ["OLD.JPG"])

    def test_ambiguous_rename_is_not_guessed(self):
        """Two removals and two identical additions must not be paired arbitrarily."""
        index = _index(
            a=_entry("p/OLD1.JPG", size=500, mtime=7),
            b=_entry("p/OLD2.JPG", size=500, mtime=7),
        )
        diff = index.diff({
            entry_key("p/NEW1.JPG"): _entry("p/NEW1.JPG", size=500, mtime=7),
            entry_key("p/NEW2.JPG"): _entry("p/NEW2.JPG", size=500, mtime=7),
        })

        self.assertEqual(diff.renamed, ())
        self.assertEqual(len(diff.added), 2)
        self.assertEqual(len(diff.removed), 2)

    def test_case_only_rename_is_a_noop(self):
        """normcase makes Photo.ARW and photo.arw the same key, so nothing changes."""
        index = _index(a=_entry("p/Photo.ARW"))
        diff = index.diff({entry_key("p/photo.arw"): _entry("p/photo.arw")})
        self.assertTrue(diff.is_noop)

    def test_commit_replaces_the_manifest(self):
        index = _index(a=_entry("p/A.JPG"))
        new_entries = {entry_key("p/A.JPG"): _entry("p/A.JPG"), entry_key("p/B.JPG"): _entry("p/B.JPG")}
        index.commit(new_entries)

        self.assertEqual(len(index), 2)
        self.assertTrue(index.diff(new_entries).is_noop)


class TestSummary(unittest.TestCase):
    def test_summary_mentions_every_bucket(self):
        index = _index(a=_entry("p/A.JPG", size=1, mtime=1))
        diff = index.diff({
            entry_key("p/A.JPG"): _entry("p/A.JPG", size=2, mtime=2),
            entry_key("p/C.JPG"): _entry("p/C.JPG", size=3, mtime=3),
            entry_key("p/R.JPG"): _entry("p/R.JPG", size=9, mtime=9),
        })
        text = diff.summary()
        self.assertIn("+2 added", text)
        self.assertIn("~1 modified", text)


if __name__ == "__main__":
    unittest.main()