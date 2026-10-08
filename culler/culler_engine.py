import csv
import json
import os
import re
import shutil
import threading
import time
from enum import Enum
from pathlib import Path
from typing import List, Dict, Any, Optional, Set, Callable, Union, Tuple
from concurrent.futures import ThreadPoolExecutor
from PIL import Image

from .exif_wrapper import ExifToolWrapper
from .folder_index import FileEntry, FolderDiff, FolderIndex, entry_key, scan_entries
from .image_loader import ImageLoader
from .db_manager import DatabaseManager
from .dataset_exporter import load_manual_annotations
from .logger import log_error

Union_Path_Str = Union[Path, str]


class _ProgressThrottle:
    """Emit at most one progress update per interval (or per percent step).

    Folder scans call back once per photo, and the GUI turns each callback into an
    ``after(0, ...)``; a 5000-photo folder queued 5000 UI tasks for a progress bar that
    only changes by 0.02% between them.
    """

    def __init__(self, callback: Optional[Callable[..., None]], interval: float = 0.1):
        self._callback = callback
        self._interval = interval
        self._last_emit = 0.0
        self._last_done = -1
        self._last_total = -1

    def __call__(self, done: int, total: int, filename: str = "") -> None:
        if self._callback is None:
            return

        now = time.monotonic()
        percent = int(done * 100 / total) if total else 0
        percent_changed = percent != int(self._last_done * 100 / self._last_total) if self._last_total else True
        if done != self._last_done and not percent_changed and (now - self._last_emit) < self._interval:
            return

        self._last_emit = now
        self._last_done = done
        self._last_total = total

        try:
            self._callback(done, total, filename)
        except TypeError:
            self._callback(done, total)


    def flush(self, done: int, total: int, filename: str = "") -> None:
        """Emit unconditionally, bypassing the interval.

        A loop that finishes inside one interval otherwise leaves the progress bar at
        whatever the first sample said - 0 of N - while the scan is already done.
        """
        if self._callback is None:
            return
        self._last_emit = time.monotonic()
        self._last_done = done
        self._last_total = total
        try:
            self._callback(done, total, filename)
        except TypeError:
            self._callback(done, total)


def _emit_progress(callback: Optional[Callable[..., None]], done: int, total: int,
                   filename: str = "") -> None:
    """Fire a progress callback with the 2- or 3-argument signature it accepts."""
    if callback is None:
        return
    try:
        callback(done, total, filename)
    except TypeError:
        callback(done, total)


def resolve_input_path(path_input: Union_Path_Str) -> Tuple[Path, Optional[Path]]:
    """
    Resolves an input path parameter (which can be a directory or an image file).

    Returns:
        Tuple[Path, Optional[Path]]: (containing_folder_path, target_image_path)
        - If input is a directory: (directory_path.resolve(), None)
        - If input is a file: (file_path.parent.resolve(), file_path.resolve())

    Raises:
        FileNotFoundError: If the input path does not exist on disk.
    """
    p = Path(path_input).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Path does not exist: {path_input}")

    if p.is_dir():
        return p, None
    else:
        return p.parent, p


def find_item_index_by_path(items: List["ImageItem"], target_path: Union_Path_Str) -> Tuple[int, Optional[Path]]:
    """
    Finds the index and exact matched path of an ImageItem matching the target path.
    Matches by exact path, stacked variant path, case-insensitive path, or filename.

    Returns:
        Tuple[int, Optional[Path]]: (index, matched_path) if found, or (-1, None) if not found.
    """
    if not items or not target_path:
        return -1, None

    resolved_target = Path(target_path).resolve()
    target_str_lower = str(resolved_target).lower()
    target_name_lower = resolved_target.name.lower()

    # Pass 1: Exact path match (direct primary or stacked variant)
    for idx, item in enumerate(items):
        if item.path.resolve() == resolved_target:
            return idx, item.path
        if item.is_stacked and item.stacked_paths:
            for sp in item.stacked_paths:
                if sp.resolve() == resolved_target:
                    return idx, sp

    # Pass 2: Case-insensitive full path match (Windows safe)
    for idx, item in enumerate(items):
        if str(item.path.resolve()).lower() == target_str_lower:
            return idx, item.path
        if item.is_stacked and item.stacked_paths:
            for sp in item.stacked_paths:
                if str(sp.resolve()).lower() == target_str_lower:
                    return idx, sp

    # Pass 3: Filename match within same directory (or stem fallback)
    for idx, item in enumerate(items):
        if item.path.name.lower() == target_name_lower:
            return idx, item.path
        if item.is_stacked and item.stacked_paths:
            for sp in item.stacked_paths:
                if sp.name.lower() == target_name_lower:
                    return idx, sp

    return -1, None


class FlagState(Enum):
    UNFLAGGED = "UNFLAGGED"
    PICK = "PICK"
    REJECT = "REJECT"

    def __str__(self):
        return self.value


class _RowSpec:
    """The folder scan's view of one grid row, before it becomes an ``ImageItem``."""

    __slots__ = ("primary", "stacked_paths", "filename", "format_name", "size_bytes", "entry")

    def __init__(self, primary: Path, stacked_paths: List[Path], filename: str,
                 format_name: str, size_bytes: int, entry: Optional[FileEntry] = None):
        self.primary = primary
        self.stacked_paths = stacked_paths
        self.filename = filename
        self.format_name = format_name
        self.size_bytes = size_bytes
        self.entry = entry

    @property
    def primary_key(self) -> str:
        return entry_key(self.primary)

    @property
    def path_keys(self) -> List[str]:
        return [entry_key(p) for p in self.stacked_paths]


class ImageItem:
    """
    Represents a single image item in a culling session.
    Supports RAW+JPG pair stacking and customizable tagging (Blur, Duplicate, Dark, Over-exposed, Custom).
    """

    def __init__(self, file_path: Union_Path_Str, size_bytes: Optional[int] = None,
                 resolved: bool = False):
        path = Path(file_path)
        # A scan already holds canonical absolute paths and has stat'ed every file, so
        # it passes both: on Windows Path.resolve() costs two extra filesystem calls per
        # photo, which was the last per-item syscall a scan still paid.
        self.path = path if resolved else path.resolve()
        self.filename = self.path.name
        self.extension = self.path.suffix.lower()
        self.format_name = ImageLoader.get_format_type(self.path)
        # The scan's manifest already carries each file's size; accepting it here saves
        # another stat (and, before, a separate exists() probe) per photo.
        if size_bytes is not None:
            self.size_bytes = int(size_bytes)
        else:
            try:
                self.size_bytes = self.path.stat().st_size
            except OSError:
                self.size_bytes = 0

        self.stacked_paths: List[Path] = [self.path]
        self.is_stacked: bool = False

        self.flag: FlagState = FlagState.UNFLAGGED
        self.rating: int = 0  # 0 to 5
        self.sharpness_score: float = 0.0
        self.tags: Set[str] = set()
        self.dhash: Optional[int] = None
        self.metadata: Dict[str, Any] = {}
        self.detection_box: Optional[Tuple[float, float, float, float]] = None
        self.eye_box: Optional[Tuple[float, float, float, float]] = None
        self.manual_detection_box: Optional[Tuple[float, float, float, float]] = None
        self.manual_eye_box: Optional[Tuple[float, float, float, float]] = None

    @property
    def tags_str(self) -> str:
        return ", ".join(sorted(self.tags))

    def add_tag(self, tag: str):
        t = tag.strip().title()
        if t:
            self.tags.add(t)

    def remove_tag(self, tag: str):
        t = tag.strip().title()
        if t in self.tags:
            self.tags.remove(t)

    def has_tag(self, tag: str) -> bool:
        return tag.strip().title() in self.tags

    @property
    def formatted_size(self) -> str:
        size = self.size_bytes
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "filename": self.filename,
            "path": str(self.path),
            "stacked_paths": [str(p) for p in self.stacked_paths],
            "is_stacked": self.is_stacked,
            "format": self.format_name,
            "size": self.formatted_size,
            "flag": self.flag.value,
            "rating": self.rating,
            "sharpness": self.sharpness_score,
            "tags": self.tags_str,
            "camera": self.metadata.get("model", "N/A"),
            "lens": self.metadata.get("lens", "N/A"),
            "iso": self.metadata.get("iso", "N/A"),
            "shutter": self.metadata.get("shutter_speed", "N/A"),
            "aperture": self.metadata.get("aperture", "N/A"),
            "focal_length": self.metadata.get("focal_length", "N/A"),
            "date_taken": self.metadata.get("date_taken", "N/A"),
        }


class CullingSession:
    """
    Manages image files, metadata reading, flagging, ratings, filtering,
    tagging (Scan for Blur & Scan for Duplicate), batch operations, and reporting.
    """

    def __init__(
        self,
        exif_wrapper: Optional[ExifToolWrapper] = None,
        db_manager: Optional[DatabaseManager] = None,
        image_loader: Optional[ImageLoader] = None,
    ):
        # Sessions are per tab, but decoding services are shared app-wide: one cache
        # means a photo shown in two tabs is decoded once, and switching tabs does not
        # throw away the decoded images of the tab you are leaving.
        self.exif_wrapper = exif_wrapper or ExifToolWrapper()
        self.db = db_manager or DatabaseManager()
        self.image_loader = image_loader or ImageLoader(exif_wrapper=self.exif_wrapper)
        self.items: List[ImageItem] = []
        self.placeholder_items: List[ImageItem] = []
        self.arw_count: int = 0
        self.directory: Optional[Path] = None
        # Manifest of the last scan, so a refresh of the same folder is differential.
        self._index: Optional[FolderIndex] = None
        self._indexed_dir: Optional[Path] = None
        self._scan_options: Optional[Tuple[bool, bool]] = None
        self._expected_row_count: Optional[int] = None
        self.last_scan_stats: Dict[str, Any] = {}

    @staticmethod
    def extract_base_stem(stem: str) -> str:
        """
        Extract primary base photo stem from filenames with generic prefixes/suffixes:
        Prefixes: "Copy of ", "Copy (1) of ", "Edited - ", "Crop of "
        Suffixes: " (1)", " (copy)", " - Copy", "_1", "_crop", "_edit", "-v2"
        """
        import re
        s = stem.strip()
        prefix_pattern = re.compile(
            r"^(?:copy\s*(?:\(\d+\))?\s*of\s*|edit(?:ed)?\s*[-_of]*\s*|crop\s*[-_of]*\s*)",
            re.IGNORECASE
        )
        suffix_pattern = re.compile(
            r"(?:[\s_-]+(?:\d+|copy|edit(?:ed)?|crop|v\d+|final|hdr|bw|export|enhanced)|\(\s*\d+\s*\)|\(\s*copy\s*\))+$",
            re.IGNORECASE
        )

        while True:
            prev = s
            s = prefix_pattern.sub("", s).strip()
            s = suffix_pattern.sub("", s).strip()
            if s == prev or not s:
                break

        return (s if s else stem.strip()).lower()

    def find_item(self, target_path: Union_Path_Str) -> Tuple[int, Optional[Path]]:
        """
        Finds the index and matched path of the ImageItem in this session matching the target path.
        """
        return find_item_index_by_path(self.items, target_path)

    def release(self) -> None:
        """Drop the items and manifest this session is holding.

        Called when the tab is closed. An ``ImageItem`` carries a metadata dict and its
        stacked paths, so a session for a few thousand photos is not free to keep.
        """
        self.items = []
        self.placeholder_items = []
        self.arw_count = 0
        self._index = None
        self._indexed_dir = None
        self._scan_options = None
        self._expected_row_count = None
        self.last_scan_stats = {}

    def scan_directory(
        self,
        directory_path: Union_Path_Str,
        recursive: bool = False,
        stack_raw_jpg: bool = True,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        on_discovered: Optional[Callable[[List[ImageItem], int], None]] = None,
    ) -> List[ImageItem]:
        """
        Scan a directory for supported image formats (ARW, JPG, PNG, HEIC).
        Automatically stacks matching RAW, JPG, and edited variant pairs into 1 item.
        Reads EXIF metadata in high-speed batches using ExifTool.
        Restores saved flags, ratings, & tags from local SQLite database.

        A rescan of the same folder with the same options is differential: the session
        keeps a manifest of ``(size, mtime_ns)`` per file, so unchanged photos keep
        their item object, their decoded pixels and their EXIF, and only what really
        changed is re-read. A folder that produced no change at all returns immediately
        after one ``scandir`` pass, with no ExifTool call and no cache eviction.
        """
        dir_path = Path(directory_path).resolve()
        if not dir_path.exists() or not dir_path.is_dir():
            raise ValueError(f"Directory standard path does not exist: {directory_path}")

        self.directory = dir_path
        entries = scan_entries(dir_path, recursive)
        arw_count = sum(1 for e in entries.values() if e.path.suffix.lower() == ".arw")
        self.arw_count = arw_count

        options = (bool(recursive), bool(stack_raw_jpg))
        can_diff = (
            self._index is not None
            and self._indexed_dir == dir_path
            and self._scan_options == options
            and self._expected_row_count is not None
        )

        if not entries:
            if can_diff and self.items:
                self._forget_records([p for it in self.items for p in it.stacked_paths])
            self.items = []
            self.placeholder_items = []
            self._reset_manifest(options)
            _emit_progress(progress_callback, 0, 0)
            return self.items

        diff: Optional[FolderDiff] = None
        previous_items: List[ImageItem] = []
        if can_diff:
            diff = self._index.diff(entries)
            previous_items = list(self.items)
            if diff.is_noop and len(previous_items) == self._expected_row_count:
                # Nothing on disk moved: no rebuild, no EXIF, no cache eviction, and the
                # item objects the grid is bound to stay valid.
                _emit_progress(progress_callback, 0, len(self.items))
                _emit_progress(progress_callback, len(self.items), len(self.items))
                self.last_scan_stats = self._stats(len(entries), diff)
                return self.items

        row_specs = self._build_row_specs(entries, stack_raw_jpg)
        self._expected_row_count = len(row_specs)

        # Build placeholder items immediately from the row specs so the UI can display
        # placeholder thumbnails and total counts before EXIF reading completes.
        # Placeholders are kept at default size (unstacked) while loading.
        placeholder_items: List[ImageItem] = []
        for spec in row_specs:
            pi = ImageItem(spec.primary, size_bytes=spec.size_bytes, resolved=True)
            pi.filename = spec.filename
            pi.format_name = spec.format_name
            pi.stacked_paths = list(spec.stacked_paths) if getattr(spec, "stacked_paths", None) else [spec.primary]
            pi.is_stacked = False
            pi.is_placeholder = True
            placeholder_items.append(pi)

        # Pre-load saved DB records or previous donor state onto placeholder items
        # so Pick/Reject counts, flags, star ratings, and tags reflect immediately upon initial UI render.
        init_records = self._records_for(placeholder_items)
        prev_donors: Dict[str, ImageItem] = {}
        if previous_items:
            for item in previous_items:
                for p in item.stacked_paths:
                    prev_donors.setdefault(entry_key(p), item)

        for pi in placeholder_items:
            donor = prev_donors.get(entry_key(pi.path))
            if donor is not None:
                self._adopt_state(pi, donor)
            else:
                rec = self._lookup_record(init_records, pi)
                if rec:
                    self._overlay_record(pi, rec)

        self.placeholder_items = placeholder_items
        self.items = placeholder_items

        if on_discovered is not None:
            try:
                on_discovered(placeholder_items, arw_count)
            except Exception as e:
                log_debug(f"on_discovered callback failed: {e}")

        _emit_progress(progress_callback, 0, len(row_specs), f"Found {arw_count} ARW ({len(row_specs)} photos)")

        donors_list = previous_items if previous_items else placeholder_items
        items = self._reconcile_items(row_specs, donors_list, diff, progress_callback)

        # Only files whose bytes changed lose their decoded pixels; everything else stays
        # resident, so a one-file reload does not re-decode the whole folder.
        if diff is not None and diff.stale_paths:
            self.image_loader.invalidate_paths(diff.stale_paths)
        if diff is not None and diff.vanished_paths:
            self._forget_records(diff.vanished_paths)
        self._index = FolderIndex.from_entries(entries)
        self._indexed_dir = dir_path
        self._scan_options = options
        self.items = items
        # After the assignment: the rename rows are built from the new names.
        if diff is not None and diff.renamed:
            self._persist_renames(diff.renamed)
        self.last_scan_stats = self._stats(len(entries), diff)
        return items

    def _stats(self, file_count: int, diff: Optional[FolderDiff]) -> Dict[str, Any]:
        return {
            "files": file_count,
            "items": len(self.items),
            "differential": diff is not None,
            "added": len(diff.added) if diff else file_count,
            "removed": len(diff.removed) if diff else 0,
            "changed": len(diff.changed) if diff else 0,
            "renamed": len(diff.renamed) if diff else 0,
        }

    def _reset_manifest(self, options) -> None:
        self._index = None
        self._indexed_dir = None
        self._scan_options = options
        self._expected_row_count = 0

    def _build_row_specs(
        self,
        entries: Dict[str, FileEntry],
        stack_raw_jpg: bool,
    ) -> List[_RowSpec]:
        """Group the scan into rows: one per photo, or one per RAW+JPG stack."""
        specs: List[_RowSpec] = []
        ordered = [entries[k] for k in sorted(entries)]

        if not stack_raw_jpg:
            for entry in ordered:
                specs.append(_RowSpec(entry.path, [entry.path], entry.path.name,
                                      ImageLoader.get_format_type(entry.path),
                                      entry.size, entry))
            return specs

        groups: Dict[Tuple[Path, str], List[FileEntry]] = {}
        for entry in ordered:
            base_stem = self.extract_base_stem(entry.path.stem)
            groups.setdefault((entry.path.parent, base_stem), []).append(entry)

        for (_parent, base_stem), group in groups.items():
            raw_paths = [e for e in group if e.path.suffix.lower() == ".arw"]

            if len(raw_paths) == 1 and len(group) > 1:
                group.sort(key=lambda e: (
                    0 if e.path.suffix.lower() == ".arw" else
                    1 if e.path.suffix.lower() in (".jpg", ".jpeg") else
                    2
                ))
                primary = group[0]
                raw_n = sum(1 for e in group if e.path.suffix.lower() == ".arw")
                jpg_n = sum(1 for e in group if e.path.suffix.lower() in (".jpg", ".jpeg"))
                comp = ", ".join(p for p in (f"{raw_n} ARW" if raw_n else "",
                                             f"{jpg_n} JPG" if jpg_n else "") if p)
                display_stem = self.extract_base_stem(primary.path.stem).upper()
                specs.append(_RowSpec(
                    primary=primary.path,
                    stacked_paths=[e.path for e in group],
                    filename=f"{display_stem} [Stacked: {comp}]",
                    format_name=f"Stacked ({comp})",
                    size_bytes=sum(e.size for e in group),
                    entry=primary,
                ))
            else:
                for entry in group:
                    specs.append(_RowSpec(entry.path, [entry.path], entry.path.name,
                                          ImageLoader.get_format_type(entry.path),
                                          entry.size, entry))

        specs.sort(key=lambda s: s.primary.name.lower())
        return specs

    @staticmethod
    def _row_shape(spec: _RowSpec) -> Tuple:
        return (tuple(entry_key(p) for p in spec.stacked_paths), spec.filename)

    def _reconcile_items(
        self,
        specs: List[_RowSpec],
        previous_items: List[ImageItem],
        diff: Optional[FolderDiff],
        progress_callback: Optional[Callable[[int, int], None]],
    ) -> List[ImageItem]:
        """Reuse item objects for unchanged rows, build the rest, read EXIF once."""
        total = len(specs)
        _emit_progress(progress_callback, 0, total, f"Found {total} photos")
        throttled = _ProgressThrottle(progress_callback)

        donors: Dict[str, ImageItem] = {}
        for item in previous_items:
            for p in item.stacked_paths:
                donors.setdefault(entry_key(p), item)

        # A renamed file has no row under its new name, so point the new name at the
        # state of the old one before looking for a donor: without this a rename is
        # indistinguishable from an unrelated new photo and loses its flags.
        rename_sources: Dict[str, str] = {}
        if diff is not None:
            for old, new in diff.renamed:
                rename_sources[entry_key(new.path)] = entry_key(old.path)

        touched_keys = set()
        if diff is not None:
            touched_keys = {entry_key(p) for p in diff.touched_paths}

        # Which rows still need an EXIF read: a full rebuild reads all of them, a
        # differential one only reads the rows that gained or changed a file.
        needs_exif: Dict[str, _RowSpec] = {}
        for spec in specs:
            if diff is None or any(k in touched_keys for k in spec.path_keys):
                needs_exif[spec.primary_key] = spec

        items: List[ImageItem] = []
        reused_ids: set = set()
        carried_ids: set = set()
        for spec in specs:
            donor = donors.get(spec.primary_key)
            if donor is None:
                source_key = rename_sources.get(spec.primary_key)
                donor = donors.get(source_key) if source_key else None
            if donor is not None and self._row_shape_of_item(donor) == self._row_shape(spec):
                # Identical row, unchanged files: keep the very same object, so the grid
                # stays bound to it and its decoded thumbnails stay valid.
                donor.size_bytes = spec.size_bytes
                donor.is_placeholder = False
                donor.is_stacked = len(spec.stacked_paths) > 1
                reused_ids.add(id(donor))
                items.append(donor)
                continue
            donor = donor or next((donors[k] for k in spec.path_keys if k in donors), None)
            built = self._build_item(spec, donor)
            built.is_placeholder = False
            items.append(built)
            if donor is not None:
                carried_ids.add(id(items[-1]))

        # EXIF for the rows that need it, in one batched call.
        metadata_by_key: Dict[str, Dict[str, Any]] = {}
        if needs_exif:
            paths = [spec.primary for spec in needs_exif.values()]
            metadata_list = self.exif_wrapper.get_batch_metadata([str(p) for p in paths])
            for path, meta in zip(paths, metadata_list):
                metadata_by_key[entry_key(path)] = meta

        # A row with no prior state is the only one whose rating should come from the
        # file: once the app has decided a rating, re-reading EXIF must not undo it.
        # Rows whose files changed still get their saved record applied, because that is
        # what the full-scan path did and what keeps a rating stable across a reload.
        state_ids = reused_ids | carried_ids
        needs_record = [it for it in items if id(it) not in state_ids or entry_key(it.path) in metadata_by_key]
        records = self._records_for(needs_record)

        manual_annos: Optional[Dict[str, Dict[str, Any]]] = None
        anno_index: Optional[Dict[str, Dict[str, Any]]] = None
        for index, item in enumerate(items):
            had_state = id(item) in state_ids
            meta = metadata_by_key.get(entry_key(item.path))

            if meta is not None:
                item.metadata = meta
                if not had_state:
                    item.rating = meta.get("rating", 0)

            if not had_state:
                self._overlay_record(item, self._lookup_record(records, item))
            elif meta is not None:
                # If item had prior state (e.g. from donor), only populate in-camera rating from EXIF if item had no rating
                if item.rating == 0 and meta.get("rating", 0) > 0:
                    rec = self._lookup_record(records, item)
                    if not rec or rec.get("rating", 0) == 0:
                        item.rating = meta.get("rating", 0)

            if item.manual_detection_box is None:
                if manual_annos is None:
                    manual_annos = load_manual_annotations(
                        dataset_dir=str(self.db.dataset_dir) if self.db else None
                    )
                    anno_index = self._annotation_index(manual_annos)
                self._overlay_manual_annotation(item, manual_annos or {}, anno_index)

            throttled(index + 1, total, item.filename)

        throttled.flush(total, total)
        return items

    @staticmethod
    def _row_shape_of_item(item: ImageItem) -> Tuple:
        return (tuple(entry_key(p) for p in item.stacked_paths), item.filename)

    def _build_item(self, spec: _RowSpec, donor: Optional[ImageItem]) -> ImageItem:
        """Create an item for a row, carrying over state from the row it replaces."""
        item = ImageItem(spec.primary, size_bytes=spec.size_bytes, resolved=True)
        item.filename = spec.filename
        item.format_name = spec.format_name
        item.stacked_paths = list(spec.stacked_paths)
        item.is_stacked = len(spec.stacked_paths) > 1
        if donor is not None:
            self._adopt_state(item, donor)
        return item

    @staticmethod
    def _adopt_state(item: ImageItem, donor: ImageItem) -> None:
        """Carry flags, rating, scores, tags and boxes from the item being replaced.

        This is what makes a rename, or a JPG joining its RAW, keep the photo's
        culling decisions instead of resetting it to a fresh, unflagged row.
        """
        item.flag = donor.flag
        item.rating = donor.rating
        item.sharpness_score = donor.sharpness_score
        item.tags = set(donor.tags)
        item.dhash = donor.dhash
        item.metadata = dict(donor.metadata) if donor.metadata else {}
        item.detection_box = donor.detection_box
        item.eye_box = donor.eye_box
        item.manual_detection_box = donor.manual_detection_box
        item.manual_eye_box = donor.manual_eye_box

    def _records_for(self, items: List[ImageItem]) -> Dict[str, Dict[str, Any]]:
        if self.db is None or not items:
            return {}
        getter = getattr(self.db, "get_records_for_paths", None)
        if getter is None:
            return self.db.get_all_records_for_dir(str(self.directory)) if self.directory else {}
        paths = []
        for it in items:
            paths.append(str(it.path))
            for p in getattr(it, "stacked_paths", []):
                paths.append(str(p))
        return getter(paths)

    @staticmethod
    def _lookup_record(records: Dict[str, Dict[str, Any]], item: ImageItem) -> Dict[str, Any]:
        """DB rows are keyed by the resolved path; match on both forms."""
        if not records:
            return {}
        for candidate in (str(item.path), str(item.path.resolve())):
            rec = records.get(candidate)
            if rec is not None:
                return rec
        for p in getattr(item, "stacked_paths", []):
            try:
                for candidate in (str(p), str(p.resolve())):
                    rec = records.get(candidate)
                    if rec is not None:
                        return rec
            except Exception:
                pass
        return {}

    def _overlay_record(self, item: ImageItem, rec: Dict[str, Any]) -> None:
        if not rec:
            return
        try:
            item.flag = FlagState(rec["flag"])
        except (ValueError, KeyError, TypeError):
            pass
        item.rating = rec.get("rating", 0)
        if rec.get("sharpness", 0.0) > 0:
            item.sharpness_score = rec["sharpness"]
        tags_raw = rec.get("tags", "")
        item.tags.clear()
        if tags_raw:
            for t in tags_raw.split(","):
                if t.strip():
                    item.add_tag(t.strip())
        if rec.get("detection_box"):
            item.detection_box = rec["detection_box"]
        if rec.get("eye_box"):
            item.eye_box = rec["eye_box"]

    @staticmethod
    def _annotation_index(manual_annos: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Case-normalised view of the annotation keys.

        The file is keyed by resolved path, and matching an item against it used to mean
        ``Path.resolve()`` per item, i.e. two filesystem calls per photo on Windows, for
        a lookup that almost never hits anyway.
        """
        import os

        return {os.path.normcase(str(k)): v for k, v in manual_annos.items()}

    @classmethod
    def _overlay_manual_annotation(cls, item: ImageItem, manual_annos: Dict[str, Dict[str, Any]],
                                   anno_index: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        import os

        anno = manual_annos.get(str(item.path))
        if anno is None:
            index = anno_index if anno_index is not None else cls._annotation_index(manual_annos)
            anno = index.get(os.path.normcase(str(item.path)))
        if not anno:
            return
        item.manual_detection_box = anno.get("manual_detection_box")
        item.manual_eye_box = anno.get("manual_eye_box")

    def _forget_records(self, paths: List[Path]) -> None:
        if self.db is None or not paths:
            return
        forget = getattr(self.db, "delete_image_records", None)
        if forget is None:
            return
        try:
            forget([str(p) for p in paths])
        except Exception:
            log_error("Failed to remove records for files that disappeared", exc_info=True)

    def _persist_renames(self, renamed: List[Tuple[FileEntry, FileEntry]]) -> None:
        """Point the DB rows of renamed files at their new names."""
        if self.db is None or not renamed:
            return
        save = getattr(self.db, "save_image_records", None)
        if save is None:
            return
        owners: Dict[str, ImageItem] = {}
        for it in self.items:
            for p in it.stacked_paths:
                owners.setdefault(entry_key(p), it)

        rows = []
        for _old, new in renamed:
            owner = owners.get(entry_key(new.path))
            if owner is None:
                continue
            rows.append({
                "file_path": str(new.path),
                "filename": new.path.name,
                "flag": owner.flag.value,
                "rating": owner.rating,
                "sharpness": owner.sharpness_score,
                "tags": owner.tags_str,
                "detection_box": owner.detection_box,
                "eye_box": owner.eye_box,
            })
        if rows:
            try:
                save(rows)
            except Exception:
                log_error("Failed to persist renamed files", exc_info=True)

    def save_item_record(self, item: ImageItem):
        """
        Save/update image item record in SQLite DB (handles stacked pairs, tags, & detection boxes).
        """
        self.save_item_records([item])

    def save_item_records(self, items: List[ImageItem]):
        """Persist many items in a single connection and transaction.

        A folder scan upserts every photo, so per-item transactions made scanning cost
        one connection plus one commit per photo.
        """
        if not self.db:
            return

        records: List[Dict[str, Any]] = []
        for item in items:
            for p in item.stacked_paths:
                records.append({
                    "file_path": str(p),
                    "filename": p.name,
                    "flag": item.flag.value,
                    "rating": item.rating,
                    "sharpness": item.sharpness_score,
                    "tags": item.tags_str,
                    "detection_box": item.detection_box,
                    "eye_box": item.eye_box,
                })

        if records:
            self.db.save_image_records(records)

    def unflag_all_items(self) -> int:
        """
        Reset all item flags in session to UNFLAGGED and update DB.
        """
        changed_items = []
        for item in self.items:
            if item.flag != FlagState.UNFLAGGED:
                item.flag = FlagState.UNFLAGGED
                changed_items.append(item)
        if changed_items:
            self.save_item_records(changed_items)
        return len(changed_items)

    def untag_all_items(self) -> int:
        """
        Remove all tags from every item in session and update DB.
        """
        changed_items = []
        for item in self.items:
            if item.tags:
                item.tags.clear()
                changed_items.append(item)
        if changed_items:
            self.save_item_records(changed_items)
        return len(changed_items)

    def unrate_all_items(self) -> int:
        """
        Reset all star ratings to 0 across all items in session and update DB.
        """
        changed_items = []
        for item in self.items:
            if item.rating != 0:
                item.rating = 0
                changed_items.append(item)
        if changed_items:
            self.save_item_records(changed_items)
        return len(changed_items)

    def clear_all_metadata(self) -> int:
        """
        Reset flags to UNFLAGGED, remove all tags, set star ratings to 0, and clear detection boxes across all items in session.
        """
        changed_items = []
        for item in self.items:
            changed = False
            if item.flag != FlagState.UNFLAGGED:
                item.flag = FlagState.UNFLAGGED
                changed = True
            if item.tags:
                item.tags.clear()
                changed = True
            if item.rating != 0:
                item.rating = 0
                changed = True
            if item.detection_box is not None:
                item.detection_box = None
                changed = True
            if item.eye_box is not None:
                item.eye_box = None
                changed = True
            if changed:
                changed_items.append(item)
        if changed_items:
            self.save_item_records(changed_items)
        return len(changed_items)

    def move_items_to_trash(self, items: List[ImageItem], format_filter: Optional[str] = None) -> int:
        """
        Safely move items to the OS Recycle Bin / Trash using send2trash.
        If format_filter is specified (e.g. '.jpg' or 'JPG'), only files matching that extension
        are moved to trash, unstacking the item and preserving RAW originals.
        """
        import send2trash
        moved_count = 0
        target_ext = None
        if format_filter and "ALL" not in format_filter.upper():
            target_ext = format_filter.lower()
            if not target_ext.startswith("."):
                if target_ext == "raw":
                    target_ext = ".arw"
                else:
                    target_ext = "." + target_ext

        for item in list(items):
            if target_ext:
                paths_to_delete = [p for p in list(item.stacked_paths) if p.suffix.lower() == target_ext or (target_ext == ".arw" and ImageLoader.is_raw(p))]
                remaining_paths = [p for p in list(item.stacked_paths) if p not in paths_to_delete]

                for p in paths_to_delete:
                    if p.exists():
                        try:
                            send2trash.send2trash(str(p))
                            moved_count += 1
                        except Exception as e:
                            log_error(f"Error moving {p} to trash: {e}")

                if remaining_paths:
                    item.stacked_paths = remaining_paths
                    item.path = remaining_paths[0]
                    item.filename = item.path.name
                    item.extension = item.path.suffix.lower()
                    item.is_stacked = len(remaining_paths) > 1
                    if not item.is_stacked:
                        item.format_name = ImageLoader.get_format_type(item.path)
                    if self.db:
                        self.save_item_record(item)
                else:
                    if self.db:
                        self.db.cleanup_folder_metadata(str(item.path))
                    if item in self.items:
                        self.items.remove(item)
            else:
                for p in list(item.stacked_paths):
                    if p.exists():
                        try:
                            send2trash.send2trash(str(p))
                            moved_count += 1
                        except Exception as e:
                            log_error(f"Error moving {p} to trash: {e}")
                if self.db:
                    self.db.cleanup_folder_metadata(str(item.path))
                if item in self.items:
                    self.items.remove(item)

        return moved_count

    def move_specific_files_to_trash(self, items: List[ImageItem], paths_to_delete: List[Path]) -> int:
        """
        Safely move specific files to the OS Recycle Bin / Trash using send2trash.
        Handles stacked_paths correctly when only some files in a stack are deleted.
        """
        import send2trash
        moved_count = 0
        delete_set = set(p.resolve() for p in paths_to_delete)

        for item in list(items):
            remaining_paths = [p for p in list(item.stacked_paths) if p.resolve() not in delete_set]

            for p in list(item.stacked_paths):
                if p.resolve() in delete_set:
                    if p.exists():
                        try:
                            send2trash.send2trash(str(p))
                            moved_count += 1
                        except Exception as e:
                            log_error(f"Error moving {p} to trash: {e}")

            if remaining_paths:
                item.stacked_paths = remaining_paths
                item.path = remaining_paths[0]
                item.filename = item.path.name
                item.extension = item.path.suffix.lower()
                item.is_stacked = len(remaining_paths) > 1
                if not item.is_stacked:
                    item.format_name = ImageLoader.get_format_type(item.path)
                if self.db:
                    self.save_item_record(item)
            else:
                if self.db:
                    self.db.cleanup_folder_metadata(str(item.path))
                if item in self.items:
                    self.items.remove(item)

        return moved_count

    def compute_sharpness_scores(
        self,
        method: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        file_type_filter: Optional[str] = None,
        subject_detect: bool = False
    ):
        """
        Calculate sharpness score for all loaded items using multi-threading.
        When subject_detect=True and method is AI-based, also extracts subject & eye
        bounding boxes during the same pass (single-pass integration).
        """
        from .detectors.blur_detector import calculate_sharpness as calc_blur

        blur_method = method or (self.db.get_blur_method() if self.db else "laplacian")
        items_to_process = self._filter_items_by_file_type(file_type_filter)
        eye_det_method = self.db.get_eye_detection_method() if self.db else "yolo"

        is_ai_method = blur_method.lower() in (
            "ai_subject", "yolo_subject", "yolo", "yolo_bird_eye",
            "bird_eye_yolo", "yolo_eye", "bird_subject", "local_var"
        )
        extract_boxes = subject_detect or is_ai_method
        yolo_model = self.image_loader.get_yolo_model() if extract_boxes else None
        yolo_pose_model = self.image_loader.get_yolo_pose_model() if (extract_boxes and eye_det_method == "yolo") else None

        yolo_lock = threading.Lock()

        def calc_item(index_and_item):
            idx, item = index_and_item
            try:
                img = self.image_loader.get_thumbnail(str(item.path), max_size=(640, 640))
                if img is None:
                    item.sharpness_score = 0.0
                    return item

                if extract_boxes:
                    with yolo_lock:
                        result = calc_blur(
                            img,
                            method=blur_method,
                            yolo_model=yolo_model,
                            return_box=True,
                            eye_detection_method=eye_det_method,
                            yolo_pose_model=yolo_pose_model
                        )
                    score, dual_box = result
                    item.sharpness_score = score
                    subject_box, eye_box_raw = dual_box
                    if subject_box:
                        try:
                            thumb_w, thumb_h = img.size
                            sx1, sy1, sx2, sy2 = subject_box
                            item.detection_box = (
                                sx1 / thumb_w, sy1 / thumb_h,
                                sx2 / thumb_w, sy2 / thumb_h
                            )
                        except Exception:
                            item.detection_box = None
                    else:
                        item.detection_box = None
                    if eye_box_raw:
                        try:
                            thumb_w, thumb_h = img.size
                            ex1, ey1, ex2, ey2 = eye_box_raw
                            item.eye_box = (
                                ex1 / thumb_w, ey1 / thumb_h,
                                ex2 / thumb_w, ey2 / thumb_h
                            )
                        except Exception:
                            item.eye_box = None
                    else:
                        item.eye_box = None
                else:
                    with yolo_lock:
                        item.sharpness_score = calc_blur(img, method=blur_method, yolo_model=yolo_model, eye_detection_method=eye_det_method)
            except Exception:
                    item.sharpness_score = 0.0
            return item

        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as executor:
            completed = 0
            changed: List[ImageItem] = []
            try:
                for item in executor.map(calc_item, enumerate(items_to_process)):
                    completed += 1
                    changed.append(item)
                    if cancel_event and cancel_event.is_set():
                        break
                    if progress_callback:
                        _emit_progress(progress_callback, completed, len(items_to_process), item.filename)
            finally:
                # One connection and one transaction for the whole pass, instead of one
                # per photo from inside the worker.
                self.save_item_records(changed)

    def _filter_items_by_file_type(self, file_type_filter: Optional[str]) -> List['ImageItem']:
        if not file_type_filter or file_type_filter == "All":
            return self.items
        ext_map = {"ARW": ".arw", "JPG": ".jpg"}
        target_ext = ext_map.get(file_type_filter.upper())
        if target_ext:
            return [item for item in self.items if item.path.suffix.lower() == target_ext]
        return self.items

    def detect_subjects(
        self,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancel_event: Optional[threading.Event] = None
    ) -> List[ImageItem]:
        if not self.items:
            return []

        if cancel_event and cancel_event.is_set():
            return []

        eye_det_method = self.db.get_eye_detection_method() if self.db else "yolo"
        yolo_model = self.image_loader.get_yolo_model()
        if yolo_model is None:
            return []
        yolo_pose_model = self.image_loader.get_yolo_pose_model() if eye_det_method == "yolo" else None

        from .detectors.blur.yolo_subject import compute_ai_subject_sharpness

        yolo_lock = threading.Lock()

        def detect_item(item: ImageItem) -> ImageItem:
            try:
                img = self.image_loader.get_thumbnail(str(item.path), max_size=(400, 400))
                if img is None:
                    item.detection_box = None
                    item.eye_box = None
                    return item

                with yolo_lock:
                    _, dual_box = compute_ai_subject_sharpness(
                        None, img, yolo_model=yolo_model, return_box=True,
                        eye_detection_method=eye_det_method, yolo_pose_model=yolo_pose_model
                    )

                subject_box, eye_box_raw = dual_box
                if subject_box:
                    try:
                        thumb_w, thumb_h = img.size
                        sx1, sy1, sx2, sy2 = subject_box
                        item.detection_box = (
                            sx1 / thumb_w, sy1 / thumb_h,
                            sx2 / thumb_w, sy2 / thumb_h
                        )
                    except Exception:
                        item.detection_box = None
                else:
                    item.detection_box = None
                if eye_box_raw:
                    try:
                        thumb_w, thumb_h = img.size
                        ex1, ey1, ex2, ey2 = eye_box_raw
                        item.eye_box = (
                            ex1 / thumb_w, ey1 / thumb_h,
                            ex2 / thumb_w, ey2 / thumb_h
                        )
                    except Exception:
                        item.eye_box = None
                else:
                    item.eye_box = None
            except Exception:
                item.detection_box = None
                item.eye_box = None
            return item

        with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 2)) as executor:
            completed = 0
            for item in executor.map(detect_item, self.items):
                completed += 1
                self.save_item_record(item)
                if cancel_event and cancel_event.is_set():
                    break
                if progress_callback:
                    try:
                        progress_callback(completed, len(self.items), item.filename)
                    except TypeError:
                        progress_callback(completed, len(self.items))

        return [item for item in self.items if item.detection_box is not None]

    def scan_for_blur(
        self,
        bottom_percentile: float = 15.0,
        method: Optional[str] = None,
        flag_action: str = "Reject",
        tag_action: Optional[str] = "Blur",
        rating_action: Optional[int] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        file_type_filter: Optional[str] = None,
        subject_detect: bool = False,
        safe_mode: bool = False
    ) -> List[ImageItem]:
        """
        Scan for Blur: Analyzes sharpness scores across all photos using selected method.
        Applies configurable flag, tag, and rating actions to detected blurry photos.
        When subject_detect=True with AI method, extracts bounding boxes in the same pass.
        When safe_mode=True, checks for loose duplicates and only rejects photos if a sharper non-blurry version exists.
        """
        if not self.items:
            return []

        if cancel_event and cancel_event.is_set():
            return []

        blur_method = method or (self.db.get_blur_method() if self.db else "laplacian")
        self.compute_sharpness_scores(
            method=blur_method,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            file_type_filter=file_type_filter,
            subject_detect=subject_detect
        )

        if cancel_event and cancel_event.is_set():
            return []

        filtered_items = self._filter_items_by_file_type(file_type_filter)
        sorted_items = sorted(filtered_items, key=lambda x: x.sharpness_score)
        cutoff_index = int(len(sorted_items) * (bottom_percentile / 100.0))
        cutoff_index = max(1, min(cutoff_index, len(sorted_items)))

        flag_map = {
            "Reject": FlagState.REJECT,
            "Pick": FlagState.PICK,
            "Unflagged": FlagState.UNFLAGGED,
        }

        blur_candidate_set = set(sorted_items[:cutoff_index])
        item_hashes = {}

        if safe_mode and blur_candidate_set:
            for it in filtered_items:
                if it.dhash is not None:
                    item_hashes[it] = it.dhash
                else:
                    try:
                        thumb = self.image_loader.get_thumbnail(it.path, max_size=(160, 160))
                        if thumb:
                            h_val = self._compute_dhash(thumb)
                            it.dhash = h_val
                            item_hashes[it] = h_val
                    except Exception:
                        pass

        flagged_blurry = []
        items_to_save = []
        for i in range(cutoff_index):
            if cancel_event and cancel_event.is_set():
                break
            item = sorted_items[i]

            if cancel_event and cancel_event.is_set():
                break

            is_rejected = True
            if safe_mode:
                item_h = item_hashes.get(item)
                if item_h is not None:
                    has_sharper_duplicate = False
                    for other, other_h in item_hashes.items():
                        if other is not item and other not in blur_candidate_set:
                            dist = bin(item_h ^ other_h).count('1')
                            if dist <= 8 and other.sharpness_score > item.sharpness_score:
                                has_sharper_duplicate = True
                                break
                    if not has_sharper_duplicate:
                        is_rejected = False

            tag_val = tag_action or ""
            if tag_val:
                item.add_tag(tag_val)

            if is_rejected:
                if flag_action in flag_map:
                    item.flag = flag_map[flag_action]
                flagged_blurry.append(item)

            if rating_action is not None:
                item.rating = max(0, min(5, rating_action))

            items_to_save.append(item)

        if items_to_save:
            self.save_item_records(items_to_save)

        return flagged_blurry

    def scan_for_duplicates(
        self,
        method: str = "dhash",
        threshold: float = 6.0,
        flag_action: str = "Reject",
        tag_action: Optional[str] = "Duplicate",
        rating_action: Optional[int] = None,
        keeper_flag: str = "Pick",
        keeper_tag: Optional[str] = None,
        keeper_rating: Optional[int] = None,
        keeper_method: str = "sharpest",
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        file_type_filter: Optional[str] = None
    ) -> List[ImageItem]:
        """
        Scan for Duplicates: Detects duplicate, near-identical, or burst-shot photos.
        Delegates detection to culler.detectors.duplicate_detector module.
        Selects keeper using keeper_method, applies configurable flag, tag, and rating actions.
        """
        if not self.items:
            return []

        if cancel_event and cancel_event.is_set():
            return []

        filtered_items = self._filter_items_by_file_type(file_type_filter)

        from .detectors.duplicate_detector import find_duplicates
        groups = find_duplicates(
            filtered_items,
            image_loader=self.image_loader,
            method=method,
            threshold=threshold,
            progress_callback=progress_callback,
            cancel_event=cancel_event
        )

        if cancel_event and cancel_event.is_set():
            return []

        # Ensure sharpness scores are computed for sharpest-based keeper methods
        if keeper_method in ("sharpest", "ai_eye_focus"):
            uncomputed = [item for item in filtered_items if item.sharpness_score == 0.0]
            if uncomputed:
                self.compute_sharpness_scores(cancel_event=cancel_event, file_type_filter=file_type_filter)

        if cancel_event and cancel_event.is_set():
            return []

        flag_map = {
            "Reject": FlagState.REJECT,
            "Pick": FlagState.PICK,
            "Unflagged": FlagState.UNFLAGGED,
        }

        flagged_duplicates = []
        modified_items = []
        for group in groups:
            if cancel_event and cancel_event.is_set():
                break

            group = self._sort_group_by_keeper_method(group, keeper_method)

            keeper = group[0]
            if keeper_flag in flag_map:
                keeper.flag = flag_map[keeper_flag]
            if keeper_tag:
                keeper.add_tag(keeper_tag)
            if keeper_rating is not None:
                keeper.rating = max(0, min(5, keeper_rating))
            modified_items.append(keeper)

            for dup in group[1:]:
                if cancel_event and cancel_event.is_set():
                    break
                if tag_action:
                    dup.add_tag(tag_action)
                
                if flag_action in flag_map:
                    dup.flag = flag_map[flag_action]

                if rating_action is not None:
                    dup.rating = max(0, min(5, rating_action))

                modified_items.append(dup)
                flagged_duplicates.append(dup)

        if modified_items:
            self.save_item_records(modified_items)

        return flagged_duplicates

    def _sort_group_by_keeper_method(self, group: List[ImageItem], keeper_method: str) -> List[ImageItem]:
        """Sort a duplicate group so the best keeper is first, based on the selected method."""
        import datetime

        if keeper_method == "ai_eye_focus":
            eye_det_method = self.db.get_eye_detection_method() if self.db else "auto"
            from .detectors.blur import calculate_sharpness
            ai_scores = {}
            for item in group:
                try:
                    img = self.image_loader.load_full_image(item.path, raw_scale=0.25)
                    if img:
                        ai_scores[item] = calculate_sharpness(img, method="ai_subject", eye_detection_method=eye_det_method)
                    else:
                        ai_scores[item] = item.sharpness_score
                except Exception:
                    ai_scores[item] = item.sharpness_score
            group.sort(key=lambda x: ai_scores.get(x, 0), reverse=True)

        elif keeper_method == "largest":
            def _file_size(item):
                try:
                    return item.path.stat().st_size
                except Exception:
                    return 0
            group.sort(key=_file_size, reverse=True)

        elif keeper_method == "newest":
            def _timestamp(item):
                m = item.metadata
                if m.get("date_taken"):
                    try:
                        dt = datetime.datetime.strptime(str(m["date_taken"]), "%Y:%m:%d %H:%M:%S")
                        return dt.timestamp()
                    except Exception:
                        pass
                try:
                    return item.path.stat().st_mtime
                except Exception:
                    return 0
            group.sort(key=_timestamp, reverse=True)

        elif keeper_method == "oldest":
            def _timestamp(item):
                m = item.metadata
                if m.get("date_taken"):
                    try:
                        dt = datetime.datetime.strptime(str(m["date_taken"]), "%Y:%m:%d %H:%M:%S")
                        return dt.timestamp()
                    except Exception:
                        pass
                try:
                    return item.path.stat().st_mtime
                except Exception:
                    return float('inf')
            group.sort(key=_timestamp, reverse=False)

        else:
            # Default: sharpest
            group.sort(key=lambda x: x.sharpness_score, reverse=True)

        return group

    def _compute_dhash(self, img: Image.Image) -> int:
        """
        Compute 64-bit difference hash (dHash) for fast perceptual image similarity comparison.
        """
        small = img.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
        pixels = list(small.getdata())
        diff = []
        for row in range(8):
            for col in range(8):
                diff.append(pixels[row * 9 + col] > pixels[row * 9 + col + 1])
        val = 0
        for b in diff:
            val = (val << 1) | b
        return val

    def convert_item_to_jpg(
        self,
        item: ImageItem,
        output_dir: Optional[Union_Path_Str] = None,
        overwrite: bool = False,
        target_path: Optional[Union_Path_Str] = None
    ) -> Tuple[bool, str, Path]:
        source_path = item.path

        if target_path:
            target_path = Path(target_path).resolve()
        elif output_dir and str(output_dir).lower() != "source":
            target_dir = Path(output_dir).resolve()
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / source_path.with_suffix(".jpg").name
        else:
            target_path = source_path.with_suffix(".jpg")

        if source_path == target_path:
            return True, "already_jpg", source_path

        if target_path.exists() and not overwrite:
            return False, "exists", target_path

        try:
            pil_img = self.image_loader.load_full_image(source_path, raw_scale=1.0, white_balance="camera")
            if pil_img is None:
                return False, "failed_load", target_path

            pil_img.save(target_path, "JPEG", quality=95, optimize=True)
            return True, "success", target_path
        except Exception as e:
            print(f"Error converting {source_path} to JPG: {e}")
            return False, f"error: {e}", target_path

    def get_filtered_items(
        self,
        flag_filter: Optional[str] = None,
        rating_filter=None,
        format_filter: Optional[str] = None,
        tag_filter=None,
        search_query: Optional[str] = None
    ) -> List[ImageItem]:
        """
        Filter items by flag, rating, format, tag, and search query.
        rating_filter: int (min threshold, legacy), set of ints (multiselect specific ratings), or None (all).
        tag_filter: str (single tag), list of str (OR multiselect), or None (all).
        """
        filtered = self.items

        if flag_filter and flag_filter.upper() != "ALL":
            target_flag = flag_filter.upper()
            filtered = [item for item in filtered if item.flag.value == target_flag]

        if rating_filter is not None:
            if isinstance(rating_filter, set):
                # Multiselect: show items whose rating is in the set (OR logic)
                filtered = [item for item in filtered if item.rating in rating_filter]
            elif isinstance(rating_filter, int) and rating_filter > 0:
                # Legacy: minimum threshold
                filtered = [item for item in filtered if item.rating >= rating_filter]

        if format_filter and "ALL" not in format_filter.upper():
            target_ext = format_filter.lower()
            if not target_ext.startswith("."):
                target_ext = "." + target_ext
            filtered = [item for item in filtered if item.extension == target_ext or (item.is_stacked and target_ext == ".arw")]

        if tag_filter:
            if isinstance(tag_filter, list):
                # Multiselect OR logic: show items that have ANY of the selected tags
                target_tags = {t.strip().lower() for t in tag_filter}
                filtered = [item for item in filtered if any(t.lower() in target_tags for t in item.tags)]
            elif isinstance(tag_filter, str) and "ALL" not in tag_filter.upper():
                # Legacy single tag
                target_tag = tag_filter.strip().lower()
                filtered = [item for item in filtered if any(t.lower() == target_tag for t in item.tags)]

        if search_query:
            query = search_query.strip().lower()
            tokens = [t for t in re.split(r'[\s_\-]+', query) if t]
            filtered = [
                item for item in filtered
                if (
                    query in item.filename.lower()
                    or query in item.tags_str.lower()
                    or (len(tokens) > 1 and all(t in item.filename.lower() or t in item.tags_str.lower() for t in tokens))
                )
            ]

        return filtered

    def move_items_by_flag(self, flag: FlagState, subfolder_name: str) -> List[Path]:
        if not self.directory:
            raise ValueError("No directory active in culling session")

        target_dir = self.directory / subfolder_name
        target_dir.mkdir(parents=True, exist_ok=True)

        moved_paths: List[Path] = []
        items_to_move = [item for item in self.items if item.flag == flag]

        for item in items_to_move:
            for p in item.stacked_paths:
                dest_path = target_dir / p.name
                if p.exists():
                    shutil.move(str(p), str(dest_path))
                    moved_paths.append(dest_path)
            item.path = target_dir / item.stacked_paths[0].name

        return moved_paths

    def delete_rejected_items(self, trash_dir_name: str = "_Trash") -> List[Path]:
        return self.move_items_by_flag(FlagState.REJECT, trash_dir_name)

    def trash_rejected_items(self, format_filter: Optional[str] = None) -> int:
        """
        Safely move all items flagged as REJECT to the OS Recycle Bin / Trash using send2trash.
        """
        rejected_items = [item for item in self.items if item.flag == FlagState.REJECT]
        return self.move_items_to_trash(rejected_items, format_filter=format_filter)

    def sync_exif_ratings(self) -> int:
        """Write star ratings back into the files, one ExifTool call per rating value.

        Ratings are written per distinct value rather than per file: exiftool applies
        one value to every path in an invocation, so a folder of 200 three-star photos
        costs one process instead of 200.
        """
        entries: List[Tuple[str, int]] = []
        for item in self.items:
            if item.rating > 0:
                for p in item.stacked_paths:
                    entries.append((str(p), item.rating))

        if not entries:
            return 0

        if hasattr(self.exif_wrapper, "write_ratings_batch"):
            outcomes = self.exif_wrapper.write_ratings_batch(entries)
        else:
            # Fall back to the per-file writer for a stubbed/older wrapper.
            outcomes = {path: self.exif_wrapper.write_rating(path, rating)
                        for path, rating in entries}
        return sum(1 for ok in outcomes.values() if ok)

    def export_manifest(self, output_path: Union_Path_Str, format_type: str = "json") -> str:
        out_path = Path(output_path)
        data = [item.to_dict() for item in self.items]

        if format_type.lower() == "csv":
            if not out_path.name.endswith(".csv"):
                out_path = out_path.with_suffix(".csv")
            if data:
                fieldnames = list(data[0].keys())
                with open(out_path, mode="w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(data)
        else:
            if not out_path.name.endswith(".json"):
                out_path = out_path.with_suffix(".json")
            with open(out_path, mode="w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

        return str(out_path)

    def get_summary_stats(self) -> Dict[str, Any]:
        total = len(self.items)
        picked = sum(1 for i in self.items if i.flag == FlagState.PICK)
        rejected = sum(1 for i in self.items if i.flag == FlagState.REJECT)
        unflagged = sum(1 for i in self.items if i.flag == FlagState.UNFLAGGED)

        by_format = {}
        for item in self.items:
            fmt = item.format_name
            by_format[fmt] = by_format.get(fmt, 0) + 1

        total_bytes = sum(i.size_bytes for i in self.items)
        size_mb = round(total_bytes / (1024 * 1024), 2)
        arw_count = getattr(self, "arw_count", sum(1 for i in self.items for p in i.stacked_paths if p.suffix.lower() == ".arw"))

        return {
            "total_images": total,
            "arw_count": arw_count,
            "picked": picked,
            "rejected": rejected,
            "unflagged": unflagged,
            "total_size_mb": size_mb,
            "by_format": by_format,
        }
