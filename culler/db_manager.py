import json
import sqlite3
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

from culler.paths import DB_PATH, resolve_workspace_path, get_dataset_dir_for_workspace


def _parse_box(value):
    """Decode a stored box column into a 4-tuple of floats, or None."""
    if not value:
        return None
    try:
        parsed = json.loads(value)
        if isinstance(parsed, (list, tuple)) and len(parsed) == 4:
            return (float(parsed[0]), float(parsed[1]), float(parsed[2]), float(parsed[3]))
    except Exception:
        return None
    return None


def _record_from_row(row) -> Dict[str, Any]:
    """Shared row -> record mapping, so both query paths produce the same shape."""
    keys = set(row.keys())
    return {
        "filename": row["filename"],
        "flag": row["flag"],
        "rating": row["rating"],
        "sharpness": row["sharpness"],
        "tags": row["tags"] if "tags" in keys else "",
        "detection_box": _parse_box(row["detection_box"]) if "detection_box" in keys else None,
        "eye_box": _parse_box(row["eye_box"]) if "eye_box" in keys else None,
    }


#: SQLite caps the number of bound parameters per statement (999 on older builds), so a
#: folder must not be sent as one long ``IN`` list.
_MAX_SQL_VARIABLES = 500


class DatabaseManager:
    """
    SQLite Database Manager for persisting application settings,
    window geometry (size & position), image culling history/ratings, and image tags.
    """

    def __init__(self, db_path: Optional[Any] = None):
        self.db_path = resolve_workspace_path(db_path)
        self._init_db()

    @property
    def dataset_dir(self) -> Path:
        """Returns the _DATASET directory associated with this workspace database."""
        return get_dataset_dir_for_workspace(self.db_path)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """
        Initialize SQLite database tables if they do not exist.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Key-value app settings table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

            # Image culling history table (persist ratings, flags, sharpness, tags, & detection boxes)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS image_records (
                    file_path TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    flag TEXT NOT NULL,
                    rating INTEGER NOT NULL DEFAULT 0,
                    sharpness REAL DEFAULT 0.0,
                    tags TEXT DEFAULT '',
                    detection_box TEXT DEFAULT '',
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Migration check: Ensure tags, detection_box, and eye_box columns exist
            try:
                cursor.execute("ALTER TABLE image_records ADD COLUMN tags TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass

            try:
                cursor.execute("ALTER TABLE image_records ADD COLUMN detection_box TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass

            try:
                cursor.execute("ALTER TABLE image_records ADD COLUMN eye_box TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass

            conn.commit()

    # --- App Settings & Window State API ---

    def set_setting(self, key: str, value: Any):
        str_val = json.dumps(value) if not isinstance(value, str) else value
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                (key, str_val, str_val)
            )
            conn.commit()

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._get_connection() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            if row:
                val = row["value"]
                try:
                    return json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    return val
            return default

    def get_window_geometry(self) -> Tuple[int, int, Optional[int], Optional[int], bool]:
        width = self.get_setting("window_width", default=1280)
        height = self.get_setting("window_height", default=800)
        pos_x = self.get_setting("window_x", default=None)
        pos_y = self.get_setting("window_y", default=None)
        is_max = self.get_setting("window_maximized", default=False)
        return int(width), int(height), pos_x, pos_y, bool(is_max)

    def save_window_geometry(self, width: int, height: int, x: Optional[int] = None, y: Optional[int] = None, is_maximized: bool = False):
        self.set_setting("window_width", width)
        self.set_setting("window_height", height)
        if x is not None:
            self.set_setting("window_x", x)
        if y is not None:
            self.set_setting("window_y", y)
        self.set_setting("window_maximized", is_maximized)

    def get_raw_scale(self) -> float:
        val = self.get_setting("raw_scale", default=0.25)
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.25

    def set_raw_scale(self, scale: float):
        self.set_setting("raw_scale", float(scale))

    def get_white_balance(self) -> str:
        return self.get_setting("white_balance", default="camera")

    def set_white_balance(self, wb: str):
        self.set_setting("white_balance", wb)

    def get_jpg_save_folder(self) -> str:
        val = self.get_setting("jpg_save_folder", default="<source>")
        if val in ("source", "<source>", ""):
            return "<source>"
        return val

    def set_jpg_save_folder(self, folder_path: str):
        val = (folder_path or "").strip()
        if val.lower() in ("source", "<source>", ""):
            val = "<source>"
        self.set_setting("jpg_save_folder", val)

    def get_stack_raw_jpg(self) -> bool:
        return self.get_setting("stack_raw_jpg", default=True)

    def set_stack_raw_jpg(self, enabled: bool):
        self.set_setting("stack_raw_jpg", enabled)

    def get_show_bounding_boxes(self) -> bool:
        val = self.get_setting("show_bounding_boxes", default=True)
        return str(val).lower() in ("true", "1", "yes")

    def set_show_bounding_boxes(self, show: bool):
        self.set_setting("show_bounding_boxes", str(show).lower())

    def get_custom_tags(self) -> list:
        """Get user-defined custom tags from settings."""
        val = self.get_setting("custom_tags", default="")
        if not val or not str(val).strip():
            return []
        return [t.strip() for t in str(val).split(",") if t.strip()]

    def set_custom_tags(self, tags: list):
        """Save user-defined custom tags to settings."""
        self.set_setting("custom_tags", ",".join(t.strip() for t in tags if t.strip()))

    def get_picked_folder(self) -> str:
        return self.get_setting("picked_folder", default="_SELECTED")

    def set_picked_folder(self, folder_name: str):
        self.set_setting("picked_folder", folder_name)

    def get_rejected_folder(self) -> str:
        return self.get_setting("rejected_folder", default="_REJECTED")

    def set_rejected_folder(self, folder_name: str):
        self.set_setting("rejected_folder", folder_name)

    def get_blur_method(self) -> str:
        return self.get_setting("blur_method", default="laplacian")

    def set_blur_method(self, method: str):
        self.set_setting("blur_method", method)

    def get_blur_percentile(self) -> float:
        try:
            return float(self.get_setting("blur_percentile", default=15.0))
        except Exception:
            return 15.0

    def set_blur_percentile(self, percentile: float):
        self.set_setting("blur_percentile", percentile)

    def get_duplicate_method(self) -> str:
        return self.get_setting("duplicate_method", default="dhash")

    def set_duplicate_method(self, method: str):
        self.set_setting("duplicate_method", method)

    def get_duplicate_threshold(self) -> float:
        try:
            return float(self.get_setting("duplicate_threshold", default=6.0))
        except Exception:
            return 6.0

    def set_duplicate_threshold(self, threshold: float):
        self.set_setting("duplicate_threshold", threshold)

    # Blur Action Preferences
    def get_blur_flag_action(self) -> str:
        return self.get_setting("blur_flag_action", default="Reject")

    def set_blur_flag_action(self, action: str):
        self.set_setting("blur_flag_action", action)

    def get_blur_tag_action(self) -> str:
        return self.get_setting("blur_tag_action", default="Blur")

    def set_blur_tag_action(self, tag: str):
        self.set_setting("blur_tag_action", tag)

    def get_blur_rating_action(self) -> str:
        return self.get_setting("blur_rating_action", default="None")

    def set_blur_rating_action(self, rating_str: str):
        self.set_setting("blur_rating_action", rating_str)

    def get_blur_subject_detect(self) -> bool:
        val = self.get_setting("blur_subject_detect", default="False")
        return str(val).lower() in ("true", "1", "yes")

    def set_blur_subject_detect(self, enabled: bool):
        self.set_setting("blur_subject_detect", str(enabled).lower())

    def get_eye_detection_method(self) -> str:
        val = self.get_setting("eye_detection_method", default="yolo").lower()
        if val in ("yolo", "auto"):
            return "yolo"
        return "simple"

    def set_eye_detection_method(self, method: str):
        val = method.lower() if method else "yolo"
        norm = "yolo" if val in ("yolo", "auto") else "simple"
        self.set_setting("eye_detection_method", norm)

    def get_clear_before_scan(self) -> bool:
        return self.get_setting("clear_before_scan", default="True")

    def set_clear_before_scan(self, enabled: bool):
        self.set_setting("clear_before_scan", str(enabled).lower())

    def get_safe_blur_scan(self) -> bool:
        val = self.get_setting("safe_blur_scan", default="True")
        return str(val).lower() in ("true", "1", "yes")

    def set_safe_blur_scan(self, enabled: bool):
        self.set_setting("safe_blur_scan", str(enabled).lower())

    # Duplicate Action Preferences
    def get_duplicate_flag_action(self) -> str:
        return self.get_setting("duplicate_flag_action", default="Reject")

    def set_duplicate_flag_action(self, action: str):
        self.set_setting("duplicate_flag_action", action)

    def get_duplicate_tag_action(self) -> str:
        return self.get_setting("duplicate_tag_action", default="Duplicate")

    def set_duplicate_tag_action(self, tag: str):
        self.set_setting("duplicate_tag_action", tag)

    def get_duplicate_rating_action(self) -> str:
        return self.get_setting("duplicate_rating_action", default="None")

    def set_duplicate_rating_action(self, rating_str: str):
        self.set_setting("duplicate_rating_action", rating_str)

    # --- UI State Persistence API ---

    def get_ui_panels_visible(self) -> Tuple[bool, bool]:
        thumbs = self.get_setting("ui_thumbnail_panel_visible", default=True)
        tools = self.get_setting("ui_tool_panel_visible", default=True)
        return bool(thumbs), bool(tools)

    def set_ui_panels_visible(self, thumbs: bool, tools: bool):
        self.set_setting("ui_thumbnail_panel_visible", bool(thumbs))
        self.set_setting("ui_tool_panel_visible", bool(tools))

    def get_ui_panels_width(self) -> Tuple[int, int]:
        thumbs_w = self.get_setting("ui_thumbnail_panel_width", default=340)
        tools_w = self.get_setting("ui_tool_panel_width", default=290)
        try:
            return int(thumbs_w), int(tools_w)
        except (ValueError, TypeError):
            return 340, 290

    def set_ui_panels_width(self, thumbs_width: int, tools_width: int):
        self.set_setting("ui_thumbnail_panel_width", int(thumbs_width))
        self.set_setting("ui_tool_panel_width", int(tools_width))

    def get_ui_menubar_visible(self) -> bool:
        return bool(self.get_setting("ui_menubar_visible", default=True))

    def set_ui_menubar_visible(self, visible: bool):
        self.set_setting("ui_menubar_visible", bool(visible))

    def get_meta_panel_collapsed(self) -> Dict[str, bool]:
        val = self.get_setting("meta_panel_bag_collapsed", default={})
        return val if isinstance(val, dict) else {}

    def set_meta_panel_collapsed(self, states: Dict[str, bool]):
        self.set_setting("meta_panel_bag_collapsed", states)

    # --- Image Record Persistence & Cleanup API ---

    def save_image_record(
        self,
        file_path: str,
        filename: str,
        flag: str,
        rating: int = 0,
        sharpness: float = 0.0,
        tags: str = "",
        detection_box: Optional[Tuple[float, float, float, float]] = None,
        eye_box: Optional[Tuple[float, float, float, float]] = None
    ):
        self.save_image_records([{
            "file_path": file_path,
            "filename": filename,
            "flag": flag,
            "rating": rating,
            "sharpness": sharpness,
            "tags": tags,
            "detection_box": detection_box,
            "eye_box": eye_box,
        }])

    def save_image_records(self, records: List[Dict[str, Any]]):
        """Upsert many records in one connection and one transaction.

        A folder scan calls this once per photo; one connection + commit per row made
        scanning cost thousands of transactions.
        """
        if not records:
            return

        rows = []
        for rec in records:
            box = rec.get("detection_box")
            eye = rec.get("eye_box")
            rows.append((
                rec["file_path"],
                rec.get("filename", ""),
                rec.get("flag", "UNFLAGGED"),
                rec.get("rating", 0),
                rec.get("sharpness", 0.0),
                rec.get("tags", ""),
                json.dumps(list(box)) if box else "",
                json.dumps(list(eye)) if eye else "",
            ))

        with self._get_connection() as conn:
            conn.executemany("""
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
            """, rows)
            conn.commit()

    def get_all_records_for_dir(self, dir_path: str) -> Dict[str, Dict[str, Any]]:
        clean_dir = str(Path(dir_path).resolve())
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM image_records WHERE file_path LIKE ?", (clean_dir + "%",)).fetchall()
            records = {}
            for r in rows:
                records[str(Path(r["file_path"]).resolve())] = _record_from_row(r)
            return records

    def get_records_for_paths(self, file_paths: List[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch records for specific files only.

        A differential refresh rebuilds a handful of rows and needs the saved state for
        exactly those; ``get_all_records_for_dir`` would read and parse every row of the
        folder on every refresh.
        """
        if not file_paths:
            return {}
        wanted = set()
        for fp in file_paths:
            try:
                wanted.add(str(Path(fp).resolve()))
            except (OSError, ValueError):
                wanted.add(str(fp))
        if not wanted:
            return {}

        records: Dict[str, Dict[str, Any]] = {}
        keys = list(wanted)
        with self._get_connection() as conn:
            for start in range(0, len(keys), _MAX_SQL_VARIABLES):
                chunk = keys[start:start + _MAX_SQL_VARIABLES]
                rows = conn.execute(
                    "SELECT * FROM image_records WHERE file_path IN (%s)" % ",".join("?" * len(chunk)),
                    chunk,
                ).fetchall()
                for r in rows:
                    records[str(Path(r["file_path"]).resolve())] = _record_from_row(r)
        return records

    def delete_image_records(self, file_paths: List[str]) -> int:
        """Delete the records of specific files. Returns rows removed."""
        if not file_paths:
            return 0
        keys = []
        for fp in file_paths:
            try:
                keys.append(str(Path(fp).resolve()))
            except (OSError, ValueError):
                keys.append(str(fp))
        if not keys:
            return 0
        removed = 0
        with self._get_connection() as conn:
            for start in range(0, len(keys), _MAX_SQL_VARIABLES):
                chunk = keys[start:start + _MAX_SQL_VARIABLES]
                cursor = conn.execute(
                    "DELETE FROM image_records WHERE file_path IN (%s)" % ",".join("?" * len(chunk)),
                    chunk,
                )
                removed += max(0, cursor.rowcount)
            conn.commit()
        return removed

    def cleanup_folder_metadata(self, folder_path: str) -> int:
        """
        Delete SQLite metadata records for a folder hierarchy and all its subfolders.
        """
        clean_prefix = str(Path(folder_path).resolve())
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM image_records WHERE file_path LIKE ?", (clean_prefix + "%",))
            deleted_count = cursor.rowcount
            conn.commit()
            return deleted_count

    def get_stored_folders_summary(self) -> Dict[str, Dict[str, int]]:
        """
        Return a hierarchical summary of all folders and their record counts stored in SQLite database.
        """
        with self._get_connection() as conn:
            rows = conn.execute("SELECT file_path, flag FROM image_records").fetchall()
            summary: Dict[str, Dict[str, int]] = {}

            for r in rows:
                p = Path(r["file_path"])
                folder_str = str(p.parent)

                if folder_str not in summary:
                    summary[folder_str] = {"total": 0, "pick": 0, "reject": 0, "unflagged": 0}

                summary[folder_str]["total"] += 1
                flag_val = r["flag"].upper()
                if flag_val == "PICK":
                    summary[folder_str]["pick"] += 1
                elif flag_val == "REJECT":
                    summary[folder_str]["reject"] += 1
                else:
                    summary[folder_str]["unflagged"] += 1

            return summary

    def cleanup_multiple_folders(self, folder_paths: List[str]) -> int:
        """
        Delete SQLite metadata records for a list of specified folder paths.
        """
        if not folder_paths:
            return 0

        total_deleted = 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for fp in folder_paths:
                clean_prefix = str(Path(fp).resolve())
                cursor.execute("DELETE FROM image_records WHERE file_path LIKE ?", (clean_prefix + "%",))
                total_deleted += cursor.rowcount
            conn.commit()

        return total_deleted

    def cleanup_entire_database(self) -> int:
        """
        Delete ALL stored metadata records in image_records.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM image_records")
            deleted_count = cursor.rowcount
            conn.commit()
            return deleted_count

    def cleanup_missing_files_metadata(self) -> int:
        """
        Clean up metadata records for files that no longer exist on disk.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            rows = cursor.execute("SELECT file_path FROM image_records").fetchall()
            missing = [r["file_path"] for r in rows if not Path(r["file_path"]).exists()]
            for p in missing:
                cursor.execute("DELETE FROM image_records WHERE file_path = ?", (p,))
            conn.commit()
            return len(missing)

    def get_open_tabs(self) -> Dict[str, Any]:
        val = self.get_setting("open_tabs", default=None)
        if val is None:
            return {"tabs": [], "active_index": 0}
        if isinstance(val, dict):
            return val
        if isinstance(val, list):
            return {"tabs": val, "active_index": 0}
        return {"tabs": [], "active_index": 0}

    def save_open_tabs(self, tabs_data: List[Dict[str, Any]], active_index: int = 0):
        payload = {
            "tabs": tabs_data,
            "active_index": active_index
        }
        self.set_setting("open_tabs", payload)

    def get_active_tab_index(self) -> int:
        val = self.get_open_tabs()
        return val.get("active_index", 0)
