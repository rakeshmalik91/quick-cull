"""Manifest of a scanned photo folder, and the diff against what is on disk now.

A folder scan used to be "throw everything away and read it again": clear the shared
decode caches, rebuild every item, and push every path back through ExifTool. Measured
on the reference folders, that is ~1.3 s of ExifTool for a 105-photo ARW folder, and a
watcher-triggered reload caused by a *single* changed file paid all of it.

The manifest keeps ``{normcased path: (size, mtime_ns)}`` from the previous scan so a
refresh can say what actually happened:

    added    new keys                      -> build an item, read EXIF
    removed  keys gone                     -> drop the item, drop its DB rows
    changed  size or mtime differs         -> invalidate cache tiers, re-read EXIF
    renamed  removed + added, same stamps  -> move the path, keep flags and caches
    no-op    nothing differs               -> return the same items, no EXIF at all

Rename detection relies on (size, mtime_ns), which is what the folder watcher already
uses to decide that a file is settled. Filesystems with coarse timestamp resolution can
in principle miss a same-second, same-size rewrite; the caller can force a full rebuild
when that matters.
"""

import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .image_loader import ImageLoader


@dataclass(frozen=True)
class FileEntry:
    """One supported file in a folder, with the stamps the diff is based on."""

    path: Path
    size: int
    mtime_ns: int

    @property
    def key(self) -> str:
        return entry_key(self.path)

    @property
    def stamps(self) -> Tuple[int, int]:
        return (self.size, self.mtime_ns)


def entry_key(path) -> str:
    """Case-normalised path string, so ``Photo.ARW`` and ``photo.arw`` are one file."""
    try:
        return os.path.normcase(str(Path(path)))
    except (OSError, ValueError):
        return str(path)


def scan_entries(directory, recursive: bool = False) -> Dict[str, FileEntry]:
    """One pass over a directory: ``{key: FileEntry}`` for supported photo files.

    Replaces ``glob`` + ``is_file()`` + a later ``stat`` per item: ``os.scandir`` plus
    ``entry.stat()`` gets the regular-file check, the size and the mtime from one
    syscall per entry instead of two or three.
    """
    root = Path(directory)
    entries: Dict[str, FileEntry] = {}

    def _collect(dir_path: Path) -> None:
        try:
            scanner = os.scandir(dir_path)
        except (OSError, ValueError):
            return
        with scanner:
            for entry in scanner:
                if not ImageLoader.is_supported(entry.name):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if not stat_module.S_ISREG(st.st_mode):
                    continue
                path = Path(entry.path)
                entries[entry_key(path)] = FileEntry(path, int(st.st_size), int(st.st_mtime_ns))

    if recursive:
        for dir_path, _dir_names, _files in os.walk(root):
            _collect(Path(dir_path))
    else:
        _collect(root)

    return entries


@dataclass(frozen=True)
class FolderDiff:
    """What changed between a manifest and a fresh scan."""

    added: Tuple[FileEntry, ...] = ()
    removed: Tuple[FileEntry, ...] = ()
    changed: Tuple[FileEntry, ...] = ()
    renamed: Tuple[Tuple[FileEntry, FileEntry], ...] = ()
    unchanged: Tuple[FileEntry, ...] = ()

    @property
    def is_noop(self) -> bool:
        return not (self.added or self.removed or self.changed or self.renamed)

    @property
    def touched_paths(self) -> List[Path]:
        """Paths whose *content* is new or changed: these need EXIF and a fresh decode."""
        paths = [e.path for e in self.added]
        paths.extend(e.path for e in self.changed)
        paths.extend(new.path for _, new in self.renamed)
        return paths

    @property
    def stale_paths(self) -> List[Path]:
        """Paths whose cached decodes must be dropped: changed, gone, or renamed away."""
        paths = [e.path for e in self.changed]
        paths.extend(e.path for e in self.removed)
        paths.extend(old.path for old, _ in self.renamed)
        return paths

    @property
    def vanished_paths(self) -> List[Path]:
        """Paths that no longer exist under that name at all."""
        return [e.path for e in self.removed] + [old.path for old, _ in self.renamed]

    def summary(self) -> str:
        return (
            f"+{len(self.added)} added, -{len(self.removed)} removed, "
            f"~{len(self.changed)} modified, >{len(self.renamed)} renamed"
        )


class FolderIndex:
    """``{key: FileEntry}`` for one folder, with :meth:`diff` against a new scan."""

    def __init__(self, entries: Optional[Dict[str, FileEntry]] = None):
        self._entries: Dict[str, FileEntry] = dict(entries or {})

    @classmethod
    def from_entries(cls, entries: Dict[str, FileEntry]) -> "FolderIndex":
        return cls(entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def get(self, key: str) -> Optional[FileEntry]:
        return self._entries.get(key)

    def paths(self) -> List[Path]:
        return [e.path for e in self._entries.values()]

    def diff(self, current: Dict[str, FileEntry]) -> FolderDiff:
        """Compare the manifest against a fresh scan and classify every difference."""
        old_keys = set(self._entries)
        new_keys = set(current)

        added_keys = new_keys - old_keys
        removed_keys = old_keys - new_keys
        shared = old_keys & new_keys

        added = [current[k] for k in sorted(added_keys)]
        removed = [self._entries[k] for k in sorted(removed_keys)]

        changed: List[FileEntry] = []
        unchanged: List[FileEntry] = []
        for key in sorted(shared):
            before = self._entries[key]
            after = current[key]
            if before.stamps == after.stamps:
                unchanged.append(after)
            else:
                changed.append(after)

        renamed_pairs = self._pair_renames(removed, added)
        if renamed_pairs:
            renamed_old = {entry_key(old.path) for old, _ in renamed_pairs}
            renamed_new = {entry_key(new.path) for _, new in renamed_pairs}
            removed = [e for e in removed if entry_key(e.path) not in renamed_old]
            added = [e for e in added if entry_key(e.path) not in renamed_new]

        return FolderDiff(
            added=tuple(added),
            removed=tuple(removed),
            changed=tuple(changed),
            renamed=tuple(renamed_pairs),
            unchanged=tuple(unchanged),
        )

    @staticmethod
    def _pair_renames(
        removed: Iterable[FileEntry],
        added: Iterable[FileEntry],
    ) -> List[Tuple[FileEntry, FileEntry]]:
        """Match removals to additions by identical (size, mtime_ns).

        Only unambiguous pairs count: if two files could be either one, treating them as
        a rename would attach the wrong flags to the wrong photo, so they are reported as
        a removal plus an addition and the state is carried by group instead.
        """
        removed = list(removed)
        added = list(added)
        if not removed or not added:
            return []

        by_stamps: Dict[Tuple[int, int], List[FileEntry]] = {}
        for entry in added:
            by_stamps.setdefault(entry.stamps, []).append(entry)

        consumed: set = set()
        pairs: List[Tuple[FileEntry, FileEntry]] = []
        for old in removed:
            candidates = [c for c in by_stamps.get(old.stamps, []) if id(c) not in consumed]
            if len(candidates) != 1:
                continue
            consumed.add(id(candidates[0]))
            pairs.append((old, candidates[0]))
        return pairs

    def commit(self, entries: Dict[str, FileEntry]) -> None:
        self._entries = dict(entries)