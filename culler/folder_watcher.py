"""
Folder change detection for scanned photo directories.

The app must notice when photos are added, deleted, or modified outside of the
application (for example while the folder is open in Explorer or a backup tool)
and reload the affected tab.

Design notes:
- Polling, not an OS-level watcher, so no third-party dependency is required.
  One ``os.scandir`` per watched directory per interval is cheap.
- Only supported photo files in the directory root are tracked. Scans are
  non-recursive, so the app's own output subfolders (``_SELECTED``,
  ``_REJECTED``, ``_Trash``) never influence the item list.
- A change is reported once it has been stable for a full poll cycle and its
  mtime has settled, so copying in a large RAW produces a single ``added`` event
  instead of one mid-write and another when the writer closes the file. Windows
  refreshes a file's mtime only when its last handle closes, so mid-write
  detection comes from ``st_size`` growth rather than from mtime alone.
- The application itself mutates the scanned folder (move picked/rejected,
  trash, EXIF rating write-back, JPG conversion). Those paths call
  :meth:`FolderWatcher.suppress` so the app's own writes are adopted silently
  instead of triggering a second, redundant reload.
"""

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .image_loader import ImageLoader
from .logger import log_debug, log_error


@dataclass(frozen=True)
class FolderChange:
    """A settled change to a watched directory."""

    directory: Path
    added: Tuple[str, ...] = ()
    removed: Tuple[str, ...] = ()
    modified: Tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.added) + len(self.removed) + len(self.modified)

    def summary(self) -> str:
        return (
            f"{self.directory.name}: +{len(self.added)} added, "
            f"-{len(self.removed)} removed, ~{len(self.modified)} modified"
        )


@dataclass
class _WatchState:
    directory: Path
    on_change: Callable[[FolderChange], None]
    snapshot: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    previous: Optional[Dict[str, Tuple[int, int]]] = None
    pending_since: Optional[float] = None
    suppress_until: float = 0.0


class FolderWatcher:
    """Polls directories in the background and reports settled photo file changes."""

    DEFAULT_POLL_INTERVAL = 1.5
    DEFAULT_SETTLE_SECONDS = 0.5
    DEFAULT_WRITE_GRACE_SECONDS = 2.0
    DEFAULT_SUPPRESS_SECONDS = 5.0
    EXIF_SYNC_SUPPRESS_SECONDS = 300.0

    def __init__(
        self,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        write_grace_seconds: float = DEFAULT_WRITE_GRACE_SECONDS,
    ):
        self._poll_interval = max(0.05, float(poll_interval))
        self._settle_seconds = max(0.0, float(settle_seconds))
        self._write_grace_seconds = max(0.0, float(write_grace_seconds))
        self._states: Dict[str, _WatchState] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ API

    def watch(self, directory, on_change: Callable[[FolderChange], None]) -> bool:
        """Start watching ``directory``, or refresh the baseline if already watched.

        ``on_change`` is invoked from the watcher thread, so a GUI callback must
        marshal to its own thread. Returns False when the directory is missing.
        Does not start the polling thread; call :meth:`start` for that.
        """
        path = Path(directory)
        if not path.exists() or not path.is_dir():
            return False

        try:
            resolved = path.resolve()
        except (OSError, ValueError):
            return False

        key = self._key(resolved)
        with self._lock:
            snapshot = self._snapshot(resolved)
            state = self._states.get(key)
            if state is not None:
                state.on_change = on_change
                state.snapshot = snapshot
                state.pending_since = None
                state.suppress_until = 0.0
            else:
                self._states[key] = _WatchState(
                    directory=resolved, on_change=on_change, snapshot=snapshot
                )

        self._wake_event.set()
        return True

    def start(self) -> None:
        """Start the background polling thread (idempotent)."""
        with self._lock:
            self._ensure_thread_locked()

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def unwatch(self, directory) -> None:
        """Stop watching ``directory``."""
        key = self._key(Path(directory))
        with self._lock:
            self._states.pop(key, None)
        self._wake_event.set()

    def is_watching(self, directory) -> bool:
        key = self._key(Path(directory))
        with self._lock:
            return key in self._states

    def watched_directories(self) -> Tuple[Path, ...]:
        with self._lock:
            return tuple(state.directory for state in self._states.values())

    def resync(self, directory) -> None:
        """Adopt the current on-disk state as the baseline and clear suppression."""
        key = self._key(Path(directory))
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            state.snapshot = self._snapshot(state.directory)
            state.pending_since = None
            state.suppress_until = 0.0
        self._wake_event.set()

    def suppress(self, directory, seconds: Optional[float] = None) -> None:
        """Ignore changes for ``seconds`` while adopting them silently.

        Used around the application's own writes to the scanned folder so they
        do not trigger an automatic reload on top of the reload the app already
        performs (or, for trash/EXIF writes, on top of its in-memory update).
        """
        if seconds is None:
            seconds = self.DEFAULT_SUPPRESS_SECONDS
        key = self._key(Path(directory))
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            state.suppress_until = max(state.suppress_until, time.monotonic() + float(seconds))
            state.pending_since = None
        self._wake_event.set()

    def stop(self, timeout: float = 1.0) -> None:
        """Stop the background thread."""
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    # -------------------------------------------------------------- polling

    def poll_now(self) -> None:
        """Run one poll cycle synchronously (used by tests and manual refreshes).

        Two guards keep one real-world change to one report:

        1. **Stable snapshot.** A change must be identical across a full poll
           cycle *and* persist for ``settle_seconds``. A growing ``st_size`` is
           what reveals a file that is still being written, because Windows does
           not refresh a file's mtime until its last handle closes.
        2. **mtime grace.** An entry whose mtime is younger than
           ``write_grace_seconds`` is still settling and is skipped, which also
           swallows the mtime bump Windows performs when a writer closes the file
           right after the last write.

        The baseline only advances when something is reported, so a file that grew
        into the folder is reported as ``added`` rather than ``modified``.

        State mutation happens under the lock so the background thread and a
        concurrent manual poll cannot report the same change twice. Callbacks run
        after the lock is released.
        """
        now = time.monotonic()
        wall_ns = time.time_ns()
        grace_ns = int(self._write_grace_seconds * 1_000_000_000)
        pending: List[Tuple[Callable[[FolderChange], None], FolderChange]] = []

        with self._lock:
            for state in self._states.values():
                snapshot = self._snapshot(state.directory)

                if now < state.suppress_until:
                    state.snapshot = snapshot
                    state.previous = snapshot
                    state.pending_since = None
                    continue

                if snapshot == state.snapshot:
                    state.previous = snapshot
                    state.pending_since = None
                    continue

                if state.pending_since is None:
                    state.pending_since = now
                    state.previous = snapshot
                    continue

                stable = snapshot == state.previous
                if stable and (now - state.pending_since) >= self._settle_seconds:
                    change = self._diff(state.directory, state.snapshot, snapshot, wall_ns, grace_ns)
                    state.snapshot = snapshot
                    state.previous = snapshot
                    state.pending_since = None
                    if change.total:
                        pending.append((state.on_change, change))
                    else:
                        state.pending_since = now
                    continue

                if not stable:
                    state.pending_since = now
                state.previous = snapshot

        for on_change, change in pending:
            log_debug(f"FolderWatcher detected change: {change.summary()}")
            try:
                on_change(change)
            except Exception:
                log_error(f"FolderWatcher callback failed for {change.directory}", exc_info=True)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_now()
            except Exception:
                log_error("FolderWatcher poll cycle failed", exc_info=True)
            self._wake_event.wait(self._poll_interval)
            self._wake_event.clear()

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="FolderWatcher", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- internals

    @staticmethod
    def _key(directory: Path) -> str:
        try:
            return os.path.normcase(str(Path(directory)))
        except (OSError, ValueError):
            return str(directory)

    @staticmethod
    def _snapshot(directory: Path) -> Dict[str, Tuple[int, int]]:
        entries: Dict[str, Tuple[int, int]] = {}
        try:
            with os.scandir(directory) as scanner:
                for entry in scanner:
                    if not ImageLoader.is_supported(entry.name):
                        continue
                    try:
                        # os.stat, not DirEntry.stat(): on Windows the scandir entry
                        # reports a stale size for a file that is still being written,
                        # which would make a growing file look stable.
                        st = os.stat(entry.path)
                    except OSError:
                        continue
                    entries[entry.name] = (st.st_size, st.st_mtime_ns)
        except (OSError, ValueError):
            return {}
        return entries

    @staticmethod
    def _diff(
        directory: Path,
        old: Dict[str, Tuple[int, int]],
        new: Dict[str, Tuple[int, int]],
        wall_ns: int,
        grace_ns: int,
    ) -> FolderChange:
        old_names = set(old)
        new_names = set(new)
        # Windows refreshes a file's mtime when its last handle closes, so an
        # entry whose mtime is younger than the grace window is treated as still
        # settling. A zero grace reports everything, which is what tests want.
        skip_fresh = grace_ns > 0

        added = []
        modified = []
        for name in new_names - old_names:
            if skip_fresh and wall_ns - new[name][1] <= grace_ns:
                continue
            added.append(name)
        for name in old_names & new_names:
            if old[name] == new[name]:
                continue
            if skip_fresh and wall_ns - new[name][1] <= grace_ns:
                continue
            modified.append(name)

        return FolderChange(
            directory=directory,
            added=tuple(sorted(added)),
            removed=tuple(sorted(old_names - new_names)),
            modified=tuple(sorted(modified)),
        )