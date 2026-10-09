# Database Architecture & Workspace Persistence Model

This document details the database architecture, SQLite schema, thread safety, batch optimizations, path canonicalization, and settings persistence mechanisms used in Quick Cull.

---

## 🏗️ Architecture & Storage Model

Quick Cull uses an embedded, zero-configuration, single-file SQLite database to persist image culling decisions, ratings, quality metrics, tags, bounding boxes, application preferences, and multi-tab workspace sessions.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                             Quick Cull Storage                              │
├──────────────────────────────────────┬──────────────────────────────────────┤
│    Persistent Workspace Database     │      Filesystem Dataset Storage      │
│     (.fpc-workspace / SQLite)        │      (_DATASET/annotations.json)     │
├──────────────────────────────────────┼──────────────────────────────────────┤
│ • image_records (flags, ratings,     │ • Manual YOLO bounding box edits     │
│   sharpness, tags, detection boxes)  │ • Active learning training exports   │
│ • app_settings (window geometry,     │ • Multi-class object annotations     │
│   panel states, multi-tab sessions)  │                                      │
└──────────────────────────────────────┴──────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      In-Memory Runtime RAM Layers                           │
├──────────────────────────────────────┬──────────────────────────────────────┤
│     ImageLoader LRU RAM Caches       │      CullingSession & Tab State      │
├──────────────────────────────────────┼──────────────────────────────────────┤
│ • _thumb_cache (192 MB byte budget)  │ • session.items (ImageItem objects)  │
│ • _full_cache  (512 MB byte budget)  │ • tab["current_items"] (filtered)    │
│ • _preview_cache (8 raw embedded)    │ • tab["_user_actions"] (in-flight)   │
└──────────────────────────────────────┴──────────────────────────────────────┘
```

### Workspace Concept & Database File Resolution

1. **Workspace Databases**:
   - The workspace file uses the extension `.fpc-workspace` (e.g., `default.fpc-workspace`), located at the project root or specified via workspace options.
   - Legacy migration: If `default.fpc-workspace` is missing but legacy `culler.db` exists at project root, `culler.paths.resolve_workspace_path()` automatically renames `culler.db` to `default.fpc-workspace` without data loss.
2. **Decoupled Dataset Directory**:
   - Every workspace database resolves its associated active learning folder (`_DATASET`) via `get_dataset_dir_for_workspace()`.
3. **RAM Separation**:
   - Database persistence is strictly decoupled from `ImageLoader` decoded pixel caches. Evicting decoded images from RAM never alters database records, and database writes do not invalidate resident decoded thumbnails.

---

## 🗄️ Database Schema & Table Specifications

The database schema consists of two primary tables: `image_records` and `app_settings`.

```
┌──────────────────────────────────────────────┐
│                image_records                 │
├──────────────────────────────────────────────┤
│ PK file_path        : TEXT                   │
│    filename         : TEXT                   │
│    flag             : TEXT                   │
│    rating           : INTEGER (0..5)         │
│    sharpness        : REAL                   │
│    tags             : TEXT                   │
│    detection_box    : TEXT (JSON 4-tuple)    │
│    eye_box          : TEXT (JSON 4-tuple)    │
│    last_updated     : TIMESTAMP              │
└──────────────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│                 app_settings                 │
├──────────────────────────────────────────────┤
│ PK key              : TEXT                   │
│    value            : TEXT                   │
└──────────────────────────────────────────────┘
```

### 1. `image_records` — Culling Decision & Metric Store

Persists photo decisions, quality evaluation scores, tags, and AI subject detections.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `file_path` | `TEXT` | `PRIMARY KEY` | Normalized, absolute path to the primary image file. |
| `filename` | `TEXT` | `NOT NULL` | Base filename (e.g. `DSC01042.ARW`). |
| `flag` | `TEXT` | `NOT NULL` | Culling state: `'PICK'`, `'REJECT'`, or `'UNFLAGGED'`. |
| `rating` | `INTEGER` | `NOT NULL DEFAULT 0` | Star rating integer ($0$ to $5$). |
| `sharpness` | `REAL` | `DEFAULT 0.0` | Computed Tenengrad or Laplacian sharpness score. |
| `tags` | `TEXT` | `DEFAULT ''` | Comma-separated tags (e.g. `'Blur,Favorite'`). |
| `detection_box` | `TEXT` | `DEFAULT ''` | Normalized $[x_{\text{min}}, y_{\text{min}}, x_{\text{max}}, y_{\text{max}}]$ YOLO subject box encoded as JSON string. |
| `eye_box` | `TEXT` | `DEFAULT ''` | Normalized $[x_{\text{min}}, y_{\text{min}}, x_{\text{max}}, y_{\text{max}}]$ eye crop box encoded as JSON string. |
| `last_updated` | `TIMESTAMP` | `DEFAULT CURRENT_TIMESTAMP` | Last modification time. |

#### Schema Migrations
On database initialization (`_init_db`), progressive column additions are executed conditionally within `try/except sqlite3.OperationalError` blocks:
- `ALTER TABLE image_records ADD COLUMN tags TEXT DEFAULT ''`
- `ALTER TABLE image_records ADD COLUMN detection_box TEXT DEFAULT ''`
- `ALTER TABLE image_records ADD COLUMN eye_box TEXT DEFAULT ''`

### 2. `app_settings` — Key-Value Application Store

Stores application configuration, window geometry, UI drawer widths, and open tab configurations as JSON strings or raw strings.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `key` | `TEXT` | `PRIMARY KEY` | Configuration parameter identifier. |
| `value` | `TEXT` | `NOT NULL` | JSON-encoded or string value. |

---

## ⚡ Concurrency & Thread-Safety Model

Quick Cull operates across multiple threads:
- **GUI Main Thread**: Tkinter event loop, canvas rendering, thumbnail list virtual scrolling, toolbar interactions.
- **Folder Scan Worker**: Background thread traversing directories, parsing EXIF metadata, and matching stacks.
- **Detection & Metric Workers**: Background threads running YOLO subject detection, Laplacian variance, and perceptual hashing.

### Connection-Per-Operation Pattern
SQLite connections cannot be safely shared across operating threads without complex locking. `DatabaseManager` enforces a connection-per-operation pattern:

```python
def _get_connection(self) -> sqlite3.Connection:
    conn = sqlite3.connect(str(self.db_path))
    conn.row_factory = sqlite3.Row
    return conn
```

1. **Short-Lived Connections**: Each public API method opens a connection via `with self._get_connection() as conn:`.
2. **Automatic Context Cleanup**: The Python context manager automatically closes the connection and handles commit or rollback on exception.
3. **`sqlite3.Row` Factory**: Results allow case-insensitive and key-based dictionary access.
4. **No Cross-Thread Connection Leaks**: Background scan threads and the main GUI thread never share SQLite connection handles.

---

## 🚀 High-Performance Querying & Batch Operations

### 1. Single-Transaction Bulk Upserts (`save_image_records`)
Writing decisions one row at a time with individual transactions creates thousands of disk sync flushes. `DatabaseManager.save_image_records` batches all row updates into a single transaction:

```sql
INSERT INTO image_records (file_path, filename, flag, rating, sharpness, tags, detection_box, eye_box)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(file_path) DO UPDATE SET
    flag = excluded.flag,
    rating = excluded.rating,
    sharpness = excluded.sharpness,
    tags = excluded.tags,
    detection_box = excluded.detection_box,
    eye_box = excluded.eye_box,
    last_updated = CURRENT_TIMESTAMP
```

Executing this statement via `conn.executemany` reduces a 10,000-photo folder update from several seconds down to under **15 milliseconds**.

### 2. SQLite Variable Limit Chunking (`_MAX_SQL_VARIABLES = 500`)
Standard SQLite builds limit the maximum number of bound host variables (`?` parameters) to 999 (or 32,766 on newer versions). Querying an `IN (?, ?, ...)` clause with a 2,000-photo directory would crash SQLite with `too many SQL variables`.

`DatabaseManager` introduces `_MAX_SQL_VARIABLES = 500` chunking across all dynamic multi-key queries:

```python
_MAX_SQL_VARIABLES = 500

for start in range(0, len(keys), _MAX_SQL_VARIABLES):
    chunk = keys[start:start + _MAX_SQL_VARIABLES]
    rows = conn.execute(
        "SELECT * FROM image_records WHERE file_path IN (%s)" % ",".join("?" * len(chunk)),
        chunk,
    ).fetchall()
```

This chunking applies to:
- `get_records_for_paths(file_paths)`
- `delete_image_records(file_paths)`

### 3. Sub-5ms Placeholder Preloading
When opening a folder, waiting for full EXIF extraction (which takes seconds) before reading flags would freeze toolbar counts and delay visual badges.

Instead, `scan_directory` runs a two-stage scan:
1. **Discovery Stage (< 5ms)**: File names and sizes are gathered. `get_records_for_paths` queries SQLite for all discovered paths.
2. **Immediate Overlay**: Placeholder `ImageItem` instances are overlaid with `flag`, `rating`, and `tags` from SQLite and passed to the GUI via `on_discovered`.
3. **Instant UI Presentation**: The thumbnail list renders immediately with correct green Pick / red Reject borders and filter strip counts without blocking on EXIF.
4. **Non-Blocking Background Reconciliation**: The EXIF worker continues asynchronously without overwriting flags or ratings.

---

## 🔍 Path Canonicalization & Windows Case-Insensitivity

Windows file paths are case-insensitive and can exist with alternate representations (drive letter case `c:` vs `C:`, forward vs backward slashes, symlinks, relative vs absolute paths).

### Resolution Protocol
1. **Storage Canonicalization**: Paths are resolved using `str(Path(file_path).resolve())` before writing to `image_records`.
2. **Dual-Key Lookup Contract (`_lookup_record`)**:
   When looking up an item's record in memory, `_lookup_record` checks candidates in priority order:
   - `str(item.path)`
   - `str(item.path.resolve())`
   - Sibling stacked paths: For RAW+JPG stacks, all paths in `item.stacked_paths` are checked. This guarantees that if a JPG was flagged in a previous session and is now stacked with an ARW, the flag is preserved.
3. **Directory Hierarchy Matching**:
   Prefix queries for folder sweeps use `file_path LIKE ?` with `clean_dir + "%"` to match child files and subdirectories.

---

## 🛠️ Settings & UI State API Reference

`DatabaseManager` provides typed getters and setters backed by `app_settings`:

### Window Geometry & Layout State
- `get_window_geometry()` / `save_window_geometry(width, height, x, y, is_maximized)`: Preserves position and maximized state across sessions.
- `get_ui_panels_visible()` / `set_ui_panels_visible(thumbs, tools)`: Preserves sidebar visibility.
- `get_ui_panels_width()` / `set_ui_panels_width(thumbs_width, tools_width)`: Preserves split container widths.
- `get_ui_menubar_visible()` / `set_ui_menubar_visible(visible)`: Preserves menubar state.
- `get_meta_panel_collapsed()` / `set_meta_panel_collapsed(states)`: Preserves metadata accordion drawer collapse/expand states.

### Multi-Tab State
- `get_open_tabs()` -> `{"tabs": [...], "active_index": int}`: Restores directories, filter values, and active tab index on startup.
- `save_open_tabs(tabs_data, active_index)`: Serializes open tabs to `app_settings` key `"open_tabs"`.

### Culling Preferences & Algorithm Parameters
- `get_raw_scale()` / `set_raw_scale(scale)`: Half/Quarter/Eighth draft decode scale for libraw.
- `get_white_balance()` / `set_white_balance(wb)`: White balance mode (`"camera"`, `"auto"`).
- `get_stack_raw_jpg()` / `set_stack_raw_jpg(enabled)`: RAW+JPG pair grouping toggle.
- `get_picked_folder()` / `get_rejected_folder()`: Destination subfolder names (`"_SELECTED"`, `"_REJECTED"`).
- `get_blur_method()` / `set_blur_method(method)`: `"laplacian"`, `"patch_grid"`, or `"fft"`.
- `get_blur_percentile()` / `set_blur_percentile(p)`: Sensitivity cutoff percentile (default `15.0`).
- `get_blur_subject_detect()` / `set_blur_subject_detect(enabled)`: YOLO subject crop toggle.
- `get_eye_detection_method()` / `set_eye_detection_method(method)`: `"yolo"` vs `"simple"`.
- `get_duplicate_method()` / `set_duplicate_method(method)`: `"dhash"` vs `"phash"`.
- `get_duplicate_threshold()` / `set_duplicate_threshold(thresh)`: Hamming distance threshold (default `6.0`).
- Auto-action preferences for blur and duplicate scans (`blur_flag_action`, `blur_tag_action`, `duplicate_flag_action`, etc.).

---

## 🧹 Database Cleanup & Maintenance Operations

Quick Cull includes a dedicated Metadata Cleanup utility (`culler.gui.MetadataCleanupDialog`) backed by maintenance APIs in `DatabaseManager`:

```
┌────────────────────────────────────────────────────────────────────────┐
│                      Metadata Cleanup Dialog                           │
├────────────────────────────────────────────────────────────────────────┤
│ • Folder Hierarchy Summary : get_stored_folders_summary()              │
│ • Prune Missing Files      : cleanup_missing_files_metadata()          │
│ • Remove Selected Folders  : cleanup_multiple_folders(folder_paths)    │
│ • Clear Entire Database    : cleanup_entire_database()                 │
└────────────────────────────────────────────────────────────────────────┘
```

1. **`get_stored_folders_summary()`**:
   Aggregates stored photo records by parent directory, returning total, picked, rejected, and unflagged counts for reporting in the GUI treeview.
2. **`cleanup_folder_metadata(folder_path)`**:
   Deletes records matching `file_path LIKE ?` with `resolved_folder + "%"`.
3. **`cleanup_missing_files_metadata()`**:
   Scans `image_records`, checks `Path(p).exists()`, and deletes orphaned records for files that have been deleted or moved outside Quick Cull.
4. **`cleanup_entire_database()`**:
   Executes `DELETE FROM image_records` to wipe image history while preserving user settings.

---

## 🔒 Data Integrity & In-Flight State Protection

1. **In-Flight User Action Protection**:
   While background scanning or EXIF extraction is in flight, any user action (picking, rejecting, star rating, tagging) modifies the shared `ImageItem` in memory, saves to SQLite immediately via `save_item_records`, and tracks in `tab["_user_actions"]`.
   When background scan reconciliation completes, `_sync_tab_scan_items` ensures in-flight user decisions are never overwritten by completed scan results or stale DB records.
2. **Atomic Renames**:
   When files are renamed on disk, `culler_engine._persist_renames()` atomically deletes old paths and inserts new records with existing flags, ratings, and tags preserved.
3. **JSON Deserialization Safety**:
   `_parse_box` validates that bounding box JSON decodes strictly into a 4-tuple of floats. Corrupted or invalid JSON gracefully falls back to `None` without raising runtime exceptions.
