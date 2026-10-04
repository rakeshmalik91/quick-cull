# Thumbnail & Preview Loading/Caching Pipeline

This document details the architecture for loading, generating, caching, and displaying thumbnails and preview images in photo culling software. It covers the core `ImageLoader` class, GUI widget caching, async loading, prefetch strategies, multi-tab isolation, and known performance characteristics.

---

## 🏗️ Architecture Overview

The thumbnail/preview pipeline has four layers:

1. **Core Loading Layer** — `culler/image_loader.py` (`ImageLoader`): PIL-based decoding, RAM caching, and thumbnail generation.
2. **GUI Widget Layer** — `culler/gui/thumbnail_list.py` + `culler/gui/canvas_viewer.py`: CTkImage widget caching, canvas rendering, and async display updates.
3. **Tab Orchestration Layer** — `gui.py`: Per-tab loading state, async directory scanning, progress isolation, and placeholder preloading.
4. **EXIF/Preview Layer** — `culler/exif_wrapper.py`: Embedded ARW preview extraction and orientation resolution.

---

## 🧠 ImageLoader — Core RAM Caching

`ImageLoader` is the central hub for all image decoding. It maintains two LRU caches using `OrderedDict`:

| Cache | Limits | Key | Purpose |
| :--- | :--- | :--- | :--- |
| `_thumb_cache` | `MAX_THUMB_CACHE_BYTES = 192 MB`, then `MAX_THUMB_CACHE = 600` | `(file_path_str, raw_scale, white_balance)` | Canonical-sized thumbnails for sidebar grid |
| `_full_cache` | `MAX_FULL_CACHE_BYTES = 512 MB`, then `MAX_FULL_CACHE = 30` | `(file_path_str, raw_scale, white_balance)` | Full-resolution previews for center viewer and prefetch |
| `_preview_cache` | 8 entries | `(path, mtime_ns, size)` | Embedded ARW preview bytes |

There is no auxiliary index: the composite key is already the dict key, so a hit is one
lookup. An earlier path-only index returned a thumbnail rendered for a different white
balance or scale.

One `ImageLoader` is created per application and shared by every tab
(`gui.py:822-823`), so a photo shown in two tabs is decoded once and switching tabs keeps
the other tabs' decoded images.

### Cache Policies

- **Byte budgets first**: entries are evicted oldest-first until the cache is under its byte
  budget (and then under its item cap), so a folder of large decodes cannot blow past the
  ceiling by item count alone.
- **Single-flight**: concurrent requests for the same key decode once; the rest wait for
  the in-flight result (`INFLIGHT_TIMEOUT_SECONDS` bounds the wait).
- **Thread safety**: every cache mutation happens under one `RLock`, held only for dict
  operations.
- **Copy-on-read**: Every cache hit returns `img.copy()` so callers cannot mutate the cached PIL image.
- **Tier discipline**: a thumbnail entry is never allowed to hold a full-resolution buffer;
  non-JPEG sources are downscaled to 400 px before being cached.
- **Invalidation**: `clear_cache()` purges all caches. It is called at the start of every `scan_directory()` and on app shutdown.
- **Thread safety**: every cache mutation runs under one `RLock`, so compound operations like `if key in d: d[key]` are safe. Per-key single-flight means N concurrent requests for one path decode once.

---

## 🖼️ Thumbnail Generation Pipeline

### JPG / PNG / HEIC — Fast Path (< 5ms)

```python
# culler/image_loader.py: get_thumbnail() lines 279–291
with Image.open(path) as raw_img:
    raw_img.draft("RGB", (max_size[0]*4, max_size[1]*4))  # Hint decoder to use reduced DCT scale
    raw_img = ImageOps.exif_transpose(raw_img)             # Apply EXIF orientation
    img = raw_img.convert("RGB")
    img.load()                                              # Force pixel data into RAM
    if max(img.width, img.height) > 400:
        img.thumbnail((400, 400), BILINEAR)                 # Canonical cache size
```

### ARW — Multi-Strategy Pipeline

`_load_arw_image()` uses a 3-tier fallback strategy:

| Priority | Method | Speed | Quality |
| :--- | :--- | :--- | :--- |
| 1 | **ExifTool embedded preview** | ⭐⭐⭐⭐⭐ | High (1600×1080 embedded JPEG) |
| 2 | **rawpy demosaic** | ⭐⭐ | Full demosaiced detail |
| 3 | **rawpy half-size** | ⭐⭐⭐ | Compromise speed/quality |

For full-resolution requests (`raw_scale >= 1.0`), rawpy is tried first with `half_size=False` for maximum detail.

### Canonical Caching

All thumbnails are cached at a **canonical 400×400** size. If the caller requests a different `max_size` (e.g., 80×80 for the sidebar or 160×160 for dHash), a fast `img.thumbnail(max_size, BILINEAR)` downscale is applied on cache hit.

---

## 🔍 Preview Loading for Center Viewer

The center viewer (`ImageCanvasViewer`) displays a larger preview image. Navigation uses an instant-cache-first strategy:

```python
# gui.py: _select_image() lines 767–817
1. Check get_cached_thumbnail() → instant display if available
2. Check get_cached_full_image() → instant display + trigger prefetch
3. Otherwise: spawn background thread to load_full_image()
   - Meanwhile show fast thumbnail (400×400) as placeholder
```

### Prefetch Buffer

`_prefetch_surrounding_images()` loads adjacent images into `_full_cache` in a daemon thread:

- **Default window**: `[center+1, center+2, center-1]` (3 images)
- **Jumps** (Page Up/Down, Ctrl+Arrow): cold cache, user sees loading delay
- **Arrow key debounce**: 150ms delay before background load starts

---

## 🧩 GUI Widget Caching

### ThumbnailList (`culler/gui/thumbnail_list.py`)

- `_ctk_img_cache: Dict[str, ctk.CTkImage]` maps `path → CTkImage` widget
- `_load_single_thumb_async()` submits `get_thumbnail()` to a `ThreadPoolExecutor(max_workers=4)`
- UI updates are marshaled via `root.after(0, callback)`
- **Never evicted**: the dict grows until `update_items()` is called (which destroys all widgets)

### ImageCanvasViewer (`culler/gui/canvas_viewer.py`)

- `current_pil_img` / `current_tk_img` hold the current preview state
- `redraw()` creates a new `ImageTk.PhotoImage` on every resize/zoom
- **Fast pan path**: when only coordinates change, `redraw()` skips resize and updates canvas coords directly
- **Resampling**: `NEAREST` during rapid zoom scroll (150ms debounce), `BILINEAR` for final crisp render
- **Max render size**: capped at 3500px to prevent memory blow-up on 100MP images

---

## 📸 EXIF Preview Extraction (ARW)

`ExifToolWrapper.extract_preview_bytes()` extracts the embedded JPEG preview from Sony ARW files:

1. **Pure-Python binary scan**: Searches the ARW file for JPEG SOI (`0xFFD8`) markers
2. **ExifTool subprocess fallback**: `exiftool -b -PreviewImage <path>` if binary scan fails

The extracted bytes are opened via `Image.open(io.BytesIO(preview_bytes))` and decoded as a standard PIL image. This is cached in the `_thumb_cache` / `_full_cache` just like any other image.

---

## ⚡ Performance Characteristics

| Operation | Speed | Notes |
| :--- | :--- | :--- |
| Thumbnail cache hit | ~0ms | O(1) index lookup + `.copy()` |
| Thumbnail cache miss (JPG) | < 5ms | `draft()` + `exif_transpose()` |
| Thumbnail cache miss (ARW) | 50–200ms | ExifTool preview or rawpy |
| Full image cache hit | ~0ms | O(1) `OrderedDict` lookup |
| Full image load (JPG, raw_scale=0.25) | 10–30ms | PIL decode + draft |
| Full image load (ARW, rawpy) | 200–800ms | Full demosaic |
| Canvas pan (fast path) | < 1ms | Coords-only update |
| Canvas zoom redraw | 5–15ms | `ImageTk.PhotoImage` recreation |

---

## 🔧 Known Optimization Opportunities

1. **Disk-based thumbnail cache**: No persistent cache across sessions. A `.thumbcache/` directory keyed by `(path, mtime, scale)` would eliminate regeneration.
2. **Prefetch window**: Currently ±2 images. Expanding to ±5 and prefetching on Page Up/Down jumps would reduce cold-cache navigation.
3. **mtime-based cache invalidation**: Cache keys do not include `os.path.getmtime(path)`. Overwritten files may return stale data.
4. ~~Thread safety~~ - **done**: all cache access is behind one `RLock`, with per-key single-flight de-duplication.
5. **Redundant `img.load()`**: In `load_full_image()`, `img.load()` is called unconditionally (line 123) then again in the try/except block (lines 130–132).
6. **CTkImage widget cache never evicted**: `_ctk_img_cache` grows unbounded until `update_items()` is called.
7. **Canvas resize cache**: No secondary cache keyed by `(zoom, width, height)`, so rapid zooming at the same scale re-renders redundantly.

---

## 🗂️ Multi-Tab Isolation & Async Loading

### Tab Data Model

Each tab is a `Dict[str, Any]` stored in `gui.py`'s `self.tabs` list. Key fields:

| Field | Purpose |
| :--- | :--- |
| `session` | `CullingSession` instance with its own `ImageLoader`, DB records, and EXIF metadata |
| `current_items` | Filtered `ImageItem` list for this tab |
| `current_index` | Active photo index |
| `selected_indices` | Multi-selection set |
| `filter_values` | Per-tab filter state (flag, rating, format, tag) |
| `is_loaded` | Whether directory scan completed |
| `loading` | Whether directory scan is in progress |
| `load_total` | Total photos found (for progress bar) |
| `load_current` | Photos processed so far (for progress bar) |
| `tab_label` | Display name; appended with ` ⟳` during loading |

**Critical isolation rule**: Every tab owns its own `CullingSession` and `ImageLoader`. Switching tabs swaps the thumbnail list's `image_loader` reference. There is **no shared mutable state** between tabs for progress, items, or images.

### Async Directory Loading (No Blocking Modal)

Directory scanning happens in a `threading.Thread(target=worker, daemon=True)`:

```python
# gui.py: _load_tab_directory()
def _load_tab_directory(tab, show_progress=True):
    tab["loading"] = True
    tab["load_total"] = 0
    tab["load_current"] = 0
    self._update_tab_loading_indicator(tab)

    def on_progress(current, total, filename=""):
        tab["load_current"] = current
        tab["load_total"] = total
        if tab is self._get_active_tab():
            self.after(0, self._sync_loading_progress)

    def worker():
        tab["session"].scan_directory(directory, progress_callback=on_progress)
        tab["is_loaded"] = True
        tab["loading"] = False
        self.after(0, lambda: self._on_tab_scan_complete(tab))

    threading.Thread(target=worker, daemon=True).start()
```

**No `ProgressDialog` is shown for directory loading.** The old modal progress dialog has been removed entirely. Progress is shown only in the thumbnail list's bottom bar.

### Bottom Progress Bar Layout

`ThumbnailList.progress_frame` is a fixed 36px transparent frame holding two rows:

```python
self.progress_frame                     # height=36, pack_propagate(False)
  ├── lbl_load_timing                   # height=16, "Folder: 4.2s   |   Thumbs: 1.8s"
  └── progress_row                      # height=20, grid_columnconfigure(1, weight=1)
        ├── (0,0) lbl_progress_text     # "12 / 109", then "109 / 109"
        └── (0,1) progress_bar          # sticky="ew"
```

The count/bar row uses `grid` (not `pack`): a `CTkLabel` requests a 28px natural height, which exhausted the 20px row cavity under `pack` and left the bar mispositioned. With `grid`, both children share the row and `CTkProgressBar` centers its own bar vertically.

### Folder & Thumbnail Load Timing

Two independent timers are displayed **above** the progress bar and **stay on screen permanently** after the load finishes (the bar no longer auto-hides; there is no "Done" text):

| Timer | Start | Freeze | Reset |
|---|---|---|---|
| Folder | `start_load_timing(started_at)` from `gui.py` `_load_directory()` / `_load_tab_directory()` | `finish_folder_timing()` in `_on_scan_complete()` / `_on_tab_scan_complete()` | `start_load_timing()` (per load cycle) |
| Thumbs | `start_thumb_timing()` from `ThumbnailList.update_items()` | `freeze_load_timing()` when `_update_progress_ui()` sees all thumbs loaded | `start_thumb_timing(reset=True)` when the item path set changes mid-load |

- A 100ms `after()` tick (`_tick_load_timing`) refreshes `lbl_load_timing` while a cycle is active; `_stop_timing_tick()` ends the tick once the timers are frozen.
- Elapsed values are frozen into `_folder_time_final` / `_thumb_time_final` at their end, so the label keeps showing the **total** duration rather than a running count.
- `freeze_load_timing()` deliberately leaves `_folder_scan_active` alone: the scan can still be running when the thumbnails finish, and `finish_folder_timing()` corrects the value afterwards. `finish_load_timing()` (scan error, empty folder) freezes both.
- `_folder_scan_active` / `_load_cycle_active` gate the freeze and thumb-start calls, so a later filter change or tab switch keeps the totals of the load that produced the current items instead of restarting the clock.
- An empty folder result shows `0 / 0` and the frozen folder time; `_reset_load_timing()` runs only from `destroy()`.

### Per-Tab Load Stats

Stats belong to the **tab**, not to the thumbnail list, which is a single shared widget. Each tab dict carries:

```python
tab["load_stats"] = {"folder": Optional[float], "thumb": Optional[float]}
```

Writers and readers:

| Producer | Detail |
|---|---|
| Scan worker (`_load_tab_directory` / `_load_directory`) | Stores the wall-clock `scan_directory()` duration in `tab["load_stats"]["folder"]` for **every** tab, including background ones that never became active |
| `on_load_stats_changed` callback (`gui.py: _on_load_stats_changed`) | `ThumbnailList` reports `{"folder", "thumb"}` whenever a timer resets or freezes; `gui.py` writes it into `_get_active_tab()` |
| `_apply_tab_state()` | Calls `show_load_stats(folder, thumb)` **before** `update_items()`, then `begin_thumb_timing()` if the tab has no saved thumb time yet |

Ordering matters: `show_load_stats()` deactivates the load cycle before any widget rebuild runs. If `update_items()` ran first, a still-running cycle from the previous tab would restart its thumb timer and emit that stale folder time into the newly active tab.

`show_load_stats()` restores frozen values only — it never starts a timer and never emits, so re-rendering a tab's thumbnails (`_ctk_img_cache` is cleared on every path-set change, so tab switches re-decode) cannot overwrite the totals it just restored.

### Tab Switching While a Tab Is Loading

Measured with a Tk latency probe (10 ms heartbeat, worst gap = longest freeze) against the
three real ARW folders in this workspace, 105 / 194 / 203 photos.

| Scenario | Before | After |
|---|---|---|
| Switch to a fully loaded tab (203 photos) | 33 ms | **16 ms** |
| Switch away and back mid-scan | 177 ms | 192 ms |
| Rapid switching (8 switches) during a load | 134-474 ms | **119 ms** |
| Switch while thumbnails decode | 95 ms | **16 ms** |
| Soft refresh of the same tab | 108 ms first, then 121 ms | **4-5 ms** |

The dominant problem was not slow rendering but an **exception inside a Tk callback**:

- `_batch_raw_requests` / `_batch_other_requests` were created as *local* variables in
  `_process_next_batch`, yet the soft-refresh branch in `update_items` called
  `self._batch_raw_requests.clear()`. Every soft refresh raised `AttributeError`, which
  Tk swallowed, leaving the row batch chain dead and the UI unresponsive to further
  updates. Both are now instance attributes initialised in `__init__`.
- `update_single_item_status` re-imported `CullingSession` per row and re-configured every
  label and indicator unconditionally. A `_row_render_cache` of what each row already
  renders (seeded when the row is created) makes a no-op refresh free: 108 ms -> 4 ms.

Other fixes on this path:

- **One drain instead of one task per photo.** Workers push results onto a deque and a
  single 1 ms tick applies the whole batch; previously every thumbnail scheduled its own
  `after(0)`, so a folder load queued one Tk task per photo.
- **Time-budgeted row batches.** `BATCH_SIZE` rows cost ~10 ms each, so a fixed 20-row
  batch blocked the UI for ~200 ms per tick. `ROW_BUILD_BUDGET_MS` (30 ms) bounds every tick
  regardless of row cost, at the price of more, shorter ticks.
- **In-flight de-duplication keyed by path.** Switching away and back mid-load re-queued
  every decode already running. Decoded thumbnails are path-keyed and reusable across load
  ids, so `_is_thumb_pending` is path-based and the registry only resets on a true rebuild.
- **Batch chains are cancelled in both paths.** The soft path did not cancel a pending chain,
  so repeated switches left several chains building rows for item sets no longer displayed.

Remaining: switching away and back mid-scan (~190 ms) is GIL contention with the scan
thread's Python-side passes, and rapid switching (~120 ms) is the rebuild itself. Both are
addressed by P1-16 (differential refresh) rather than by more UI tuning.

### Automatic Folder Reload

`culler/folder_watcher.py` polls each loaded tab's folder in a background thread (`FolderWatcher`, one daemon thread, 1.5s interval, 0.5s settle, 2s write grace) and reloads the owning tab when photos are added, removed, or modified on disk.

- **Polling, not watchdog.** No third-party dependency. One `os.scandir` per watched folder per interval, restricted to `ImageLoader.is_supported` files in the root.
- **`os.stat`, not `DirEntry.stat()`.** On Windows `os.scandir` reports a stale size for a file that is still being written, so a growing RAW looked "stable" and got reported mid-copy. Verified: `entry.stat().st_size` lagged one write behind `os.stat()`.
- **Two guards against double reporting.** A change must be identical across a full poll cycle *and* have a settled mtime. Windows refreshes mtime only when the last handle closes, so mid-write detection comes from `st_size` growth while the grace absorbs the close-time bump.
- **Explicit lifecycle.** `FolderWatcher.start()` launches the thread, `stop()` joins it (`gui.py: _on_close`). `watch()` alone never starts it, which keeps tests deterministic via `poll_now()`.
- **Thread marshalling.** Callbacks run on the watcher thread; `_watch_tab_directory()` wraps them in `self.after(0, ...)` so Tk is only touched from the GUI thread.
- **Watches are per folder, not per tab.** `_close_tab` unwatches only when no remaining tab uses the same directory; `_unwatch_tab_directory()` drops the previous folder when a tab is re-pointed elsewhere.
- **Self-inflicted changes are suppressed.** The app mutates the scanned folder in five places: move picked/rejected, batch move, trash, EXIF rating write-back (`-overwrite_original`), and JPG conversion. Each calls `_suppress_folder_watch()`, which adopts on-disk changes silently instead of reloading on top of the reload the app already performs. `suppress()` can only extend a window; `resync()` adopts the current state *and* clears it.
- **Ignore non-image churn.** `culling_manifest.json`, `.txt` files, and the app's `_SELECTED` / `_REJECTED` / `_Trash` subfolders never affect the non-recursive scan, so they never trigger a reload.

### Per-Tab Progress Isolation

`_sync_loading_progress()` reads from the **active tab only**:

```python
# gui.py: _sync_loading_progress()
def _sync_loading_progress(self):
    tab = self._get_active_tab()
    if not tab or not tab.get("loading"):
        return
    total = tab.get("load_total", 0)
    current = tab.get("load_current", 0)
    if total > 0:
        pct = current / total
        self.thumb_list.progress_bar.set(pct)
        self.thumb_list.lbl_progress_text.configure(text=f"Loading {current}/{total}")
```

When the user switches tabs:
- Progress bar instantly reflects the new active tab's state
- If the new tab isn't loading, the bar stays empty
- The previous tab continues loading in the background with its own `load_current`/`load_total` counters

### Tab Loading Indicator

`_update_tab_loading_indicator()` appends/removes `⟳` from the tab label:

```python
# gui.py: _update_tab_loading_indicator()
def _update_tab_loading_indicator(self, tab):
    idx = self.tabs.index(tab) if tab in self.tabs else -1
    if idx < 0:
        return
    base_label = tab.get("tab_label", "")
    if tab.get("loading") and not base_label.endswith(" ⟳"):
        tab["tab_label"] = base_label + " ⟳"
    elif not tab.get("loading") and base_label.endswith(" ⟳"):
        tab["tab_label"] = base_label[:-2]
    self.tab_bar.set_label(idx, tab["tab_label"])
```

### Placeholder Preloading During Directory Scan

To show thumbnails immediately while scanning:

1. `culler_engine.py` fires `progress_callback(0, len(self.items), "Found N photos")` right after sorting items, before the slow DB/EXIF overlay loop.
2. `gui.py`'s `on_progress` detects `current == 0 and total > 0` and calls `_preload_placeholder_items(tab)`.
3. `_preload_placeholder_items()` creates lightweight `ImageItem` copies from `session.items` and calls `thumb_list.update_items()`.

This triggers the **soft refresh path** because the paths are the same as what `_on_tab_scan_complete` will later use.

### Soft Refresh (No Widget Rebuild)

`ThumbnailList.update_items()` detects when the same rows are passed again:

```python
# culler/gui/thumbnail_list.py: update_items()
new_signature = self._row_signature(items)
if hasattr(self, "_current_item_signature") and self._current_item_signature == new_signature:
    # Soft refresh: keep widgets, just submit new thumbnail loads
    self._batch_raw_requests.clear()
    self._batch_other_requests.clear()
    self._total_thumbs = 0
    self._loaded_thumbs = 0
    # ... update selection borders ...
    # ... submit async thumbnail loads ...
    return
```

This avoids the expensive `widget.destroy()` + re-create cycle. The flow is:

1. Placeholder preload → widgets created with placeholder images
2. Scan completes → `_on_filter_changed()` → `update_items(real_items)` 
3. Row signature matches → soft refresh: placeholders stay in place, real thumbnails load async and replace them

**The comparison must include stack composition, not just the primary path.** A rescan
after deleting the JPG of an ARW+JPG pair keeps the same primary path (`ALPHA.ARW`) but
the item is no longer stacked, so the row height, the `ALPHA [Stacked: 1 ARW, 1 JPG]`
label, and the second thumbnail all have to change. `_row_signature()` returns
`(primary path, stacked paths, filename)` per item; `filename` already encodes the stack
label, so a changed stack forces a full rebuild. Flag/rating edits deliberately stay out
of the signature because the soft path already refreshes those via
`update_single_item_status()`.

### Stale Update Prevention (`_load_id`)

Each `update_items()` call increments `self._load_id`. Background thumbnail workers receive the current `load_id` and only apply results if they match:

```python
# culler/gui/thumbnail_list.py: _load_single_thumb_async()
def _load_single_thumb_async(self, file_path, max_size, white_balance, load_id):
    def worker():
        pil_thumb = self.image_loader.get_thumbnail(...)
        if pil_thumb:
            self._queue_thumb_result(path_str, pil_thumb, load_id)

# Workers never touch Tk directly: results land on a deque and one 1 ms tick paints
# them all, instead of one after(0) per photo.
def _queue_thumb_result(self, path_str, pil_thumb, load_id):
    self._thumb_result_queue.append((path_str, pil_thumb, load_id))
    if self._thumb_result_after_id is None:
        self._thumb_result_after_id = self.after(1, self._drain_thumb_results)
```

`_inflight_thumbs` maps `path -> load_id`, so switching tabs mid-load does not re-queue a
decode that is already running. Decoded thumbnails are path-keyed, so a result started for
an earlier load is still painted when a row for that path exists; only the progress counter
is load-scoped. Completion is therefore decided by outstanding work
(`_is_thumb_load_complete()`: rows built, nothing in flight, queue empty) rather than by
counters - counters cannot be satisfied once de-duplication is in play, and deriving
completion from them left the duration timer running forever.

Row batches are additionally capped by `ROW_BUILD_BUDGET_MS` (30 ms) instead of a fixed row
count, so no single tick blocks the UI for longer than the budget.

### Tab Switch & State Restoration

```python
# gui.py: _switch_tab()
def _switch_tab(self, index):
    self._save_active_tab_state()        # Snapshot old tab's UI state
    self.active_tab_index = index
    self.tab_bar.set_active(index)
    target = self.tabs[index]

    if not target["is_loaded"]:
        self._apply_tab_state(target)    # Set image_loader, clear viewer
        self._load_tab_directory(target) # Start async scan
    else:
        self._apply_tab_state(target)    # Restore items, thumbnails, viewer

    self._sync_loading_progress()        # Show correct tab's progress
    self._persist_tabs_state()
```

### Filter Isolation

Each tab stores its own `filter_values`. `_apply_tab_filter_values(tab)` applies the tab's specific filters to its session's items:

```python
# gui.py: _apply_tab_filter_values()
def _apply_tab_filter_values(self, tab):
    session = tab["session"]
    filter_vals = tab.get("filter_values", {})
    tab["current_items"] = session.get_filtered_items(
        flag_filter=filter_vals.get("flag", "All"),
        rating_filter=rating_filter_set,
        format_filter=fmt_val,
        tag_filter=tag_filter
    )
    tab["current_index"] = 0 if tab["current_items"] else -1
```

---

## 💻 Code Reference

```python
# culler/image_loader.py - ImageLoader class
class ImageLoader:
    MAX_THUMB_CACHE = 600
    MAX_THUMB_CACHE_BYTES = 192 * 1024 * 1024
    MAX_FULL_CACHE = 30
    MAX_FULL_CACHE_BYTES = 512 * 1024 * 1024
    MAX_PREVIEW_CACHE = 8

    def get_cached_thumbnail(file_path) -> Optional[Image.Image]:  # O(1) lookup
    def get_cached_full_image(file_path, raw_scale, wb) -> Optional[Image.Image]:  # O(1) lookup
    def load_full_image(file_path, raw_scale, wb) -> Optional[Image.Image]:  # LRU cache + load
    def get_thumbnail(file_path, max_size, raw_scale, wb) -> Optional[Image.Image]:  # Canonical 400×400 cache
    def clear_cache()  # Purge all RAM caches

# culler/gui/thumbnail_list.py
class ThumbnailList:
    _ctk_img_cache: Dict[str, ctk.CTkImage]
    _load_id: int                          # Incremented per update_items() to prevent stale updates
    _current_item_signature            # Row identity for soft refresh (path + stack + filename)
    def update_items()                      # Soft refresh when paths match, full rebuild otherwise
    def _load_single_thumb_async(load_id)   # ThreadPoolExecutor(max_workers=4) with stale-guard
    def _update_btn_image(load_id)          # PIL → CTkImage conversion, guarded by load_id

# culler/gui/canvas_viewer.py
class ImageCanvasViewer:
    def set_image(pil_img)  # Store preview + reset zoom
    def redraw(fast_mode=False)  # Resize + render with NEAREST/BILINEAR

# culler/exif_wrapper.py
class ExifToolWrapper:
    def get_orientation(path) -> int  # 3-tier: TIFF header → PIL → ExifTool
    def extract_preview_bytes(path) -> bytes  # Binary scan → ExifTool subprocess

# gui.py - Tab & Async Loading
def _load_tab_directory(tab)              # Async scan in daemon thread, no blocking modal
def _on_tab_scan_complete(tab)            # Applies filters, restores UI for active tab only
def _sync_loading_progress()              # Shows active tab's X/Y progress in thumbnail list bar
def _update_tab_loading_indicator(tab)    # Appends ⟳ to tab label during loading
def _preload_placeholder_items(tab)       # Creates placeholder rows immediately on scan start
def _apply_tab_filter_values(tab)         # Per-tab filter application
def _switch_tab(index)                    # Saves/restores tab state, starts async load if needed
```
