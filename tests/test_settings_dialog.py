import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk
from culler.db_manager import DatabaseManager
from culler.gui.settings_dialog import SettingsDialog


class TestSettingsDialog(unittest.TestCase):
    """
    Automated Unit Test Suite for SettingsDialog layout, Workspace tab & configuration persistence.
    """

    def setUp(self):
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".qc-workspace")
        os.close(self.temp_db_fd)
        self.db = DatabaseManager(db_path=self.temp_db_path)
        try:
            self.root = ctk.CTk()
            self.root.withdraw()
            self.gui_available = True
        except Exception:
            self.gui_available = False

    def tearDown(self):
        if hasattr(self, "root") and self.root:
            try:
                self.root.destroy()
            except Exception:
                pass
        if os.path.exists(self.temp_db_path):
            try:
                os.remove(self.temp_db_path)
            except Exception:
                pass

    def test_settings_dialog_initialization(self):
        """Verify that SettingsDialog initializes widgets and tabs without error."""
        if not self.gui_available:
            self.skipTest("GUI environment not available")

        saved = False

        def on_save():
            nonlocal saved
            saved = True

        dialog = SettingsDialog(master=self.root, db=self.db, on_save=on_save)
        self.assertEqual(dialog.title(), "⚙️ Application Settings")
        self.assertTrue(dialog.resizable()[0])
        self.assertTrue(dialog.resizable()[1])
        self.assertIsNotNone(dialog.tabview)

        # Verify tabs exist
        self.assertEqual(dialog.tabview.get(), "General")

        # Trigger save settings logic directly
        dialog._save_settings()
        self.assertTrue(saved)

    def test_settings_dialog_workspace_tab(self):
        """Verify that SettingsDialog opens directly to Workspace tab and displays workspace info."""
        if not self.gui_available:
            self.skipTest("GUI environment not available")

        # Save some sample data into the db
        self.db.save_image_record(
            file_path="D:/Photos/Trip/DSC001.JPG",
            filename="DSC001.JPG",
            flag="PICK",
            rating=5,
            sharpness=100.0,
            tags="[]"
        )

        dialog = SettingsDialog(master=self.root, db=self.db, initial_tab="Workspace")
        self.assertEqual(dialog.tabview.get(), "Workspace")
        self.assertIsNotNone(dialog.ws_tree_scroll)
        self.assertIn(str(Path("D:/Photos/Trip")), dialog._folder_summary)

        # Test select/deselect all
        dialog._select_all_folders()
        self.assertTrue(all(v.get() for v in dialog._check_vars.values()))

        dialog._deselect_all_folders()
        self.assertTrue(not any(v.get() for v in dialog._check_vars.values()))

        dialog.destroy()


if __name__ == "__main__":
    unittest.main()
