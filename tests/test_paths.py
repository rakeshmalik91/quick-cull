import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from culler.paths import (
    get_project_root,
    get_lib_dir,
    get_models_dir,
    get_exiftool_dir,
    get_default_workspace_path,
    get_dataset_dir_for_workspace,
    resolve_workspace_path,
    PROJECT_ROOT,
    LIB_DIR,
    MODELS_DIR,
    EXIFTOOL_DIR,
    DEFAULT_WORKSPACE_PATH,
    DEFAULT_DATASET_DIR
)
from culler.db_manager import DatabaseManager


class TestPathsAndWorkspaces(unittest.TestCase):
    """
    Automated Unit Test Suite for project-relative paths and workspace file routing.
    """

    def test_project_relative_paths(self):
        """
        Verify that lib, models, exiftools, and default workspace are rooted in PROJECT_ROOT.
        """
        root = get_project_root()
        self.assertEqual(PROJECT_ROOT, root)
        self.assertEqual(LIB_DIR, root / "lib")
        self.assertEqual(MODELS_DIR, root / "lib" / "models")
        self.assertEqual(EXIFTOOL_DIR, root / "lib" / "exif-tools")
        self.assertEqual(DEFAULT_WORKSPACE_PATH, root / "default.qc-workspace")
        self.assertEqual(DEFAULT_DATASET_DIR, root / "_DATASET")

    def test_dataset_dir_for_custom_workspace(self):
        """
        Verify that custom workspace files look for _DATASET in that workspace file's directory.
        """
        custom_ws = Path("D:/MyCustomProject/project.qc-workspace")
        ds_dir = get_dataset_dir_for_workspace(custom_ws)
        self.assertEqual(ds_dir, Path("D:/MyCustomProject/_DATASET"))

        custom_folder = Path("D:/AnotherProject")
        ds_dir2 = get_dataset_dir_for_workspace(custom_folder)
        self.assertEqual(ds_dir2, Path("D:/AnotherProject/_DATASET"))

        # None falls back to default
        ds_default = get_dataset_dir_for_workspace(None)
        self.assertEqual(ds_default, DEFAULT_DATASET_DIR)

    def test_database_manager_dynamic_dataset_dir(self):
        """
        Verify that DatabaseManager dynamically provides the correct dataset_dir property.
        """
        temp_dir = Path(tempfile.mkdtemp())
        ws_file = temp_dir / "custom.qc-workspace"

        try:
            db = DatabaseManager(db_path=ws_file)
            self.assertEqual(db.db_path, ws_file.resolve())
            self.assertEqual(db.dataset_dir, temp_dir.resolve() / "_DATASET")
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_resolve_workspace_path_defaults(self):
        """
        Verify resolve_workspace_path resolves default.qc-workspace when None is passed.
        """
        resolved = resolve_workspace_path(None)
        self.assertEqual(resolved, DEFAULT_WORKSPACE_PATH)


if __name__ == "__main__":
    unittest.main()
