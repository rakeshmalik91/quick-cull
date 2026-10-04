import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap
from bootstrap import MIN_SPLASH_MS, SplashScreen, launch_gui
from culler.db_manager import DatabaseManager


class TestSplashScreen(unittest.TestCase):
    """
    Unit tests for SplashScreen component.
    """

    def _make_splash(self):
        """Build a splash around a stub window so no display is required."""
        with patch.object(SplashScreen, "_build"), \
             patch.object(SplashScreen, "_center"), \
             patch.object(SplashScreen, "_start_animation"):
            return SplashScreen(window=MagicMock())

    def test_splash_screen_initialization_and_status(self):
        splash = self._make_splash()
        splash.lbl_status = MagicMock()

        # Test set_status
        splash.set_status("Loading test folder...")
        splash.lbl_status.configure.assert_called_with(text="Loading test folder...")

        # Test close
        splash.close()
        splash.window.destroy.assert_called_once()

    def test_close_is_idempotent_and_ignores_later_status_updates(self):
        splash = self._make_splash()
        splash.lbl_status = MagicMock()

        splash.close()
        splash.close()

        splash.set_status("too late")
        splash.lbl_status.configure.assert_not_called()

    def test_cancel_marks_splash_as_cancelled(self):
        splash = self._make_splash()
        self.assertFalse(splash.cancelled)

        splash.cancel()
        self.assertTrue(splash.cancelled)

    def test_spin_until_returns_when_predicate_is_satisfied(self):
        splash = self._make_splash()
        # Run the poll callback synchronously: the stub window has no event loop.
        splash.window.after.side_effect = lambda delay_ms, callback: callback()

        self.assertTrue(splash.spin_until(lambda: True))
        splash.window.quit.assert_called()

    def test_spin_until_gives_up_when_cancelled(self):
        splash = self._make_splash()
        splash.cancel()
        splash.window.quit.reset_mock()
        splash.window.after.side_effect = lambda delay_ms, callback: callback()

        self.assertFalse(splash.spin_until(lambda: False))
        splash.window.quit.assert_not_called()

    def test_launch_gui_paints_splash_before_loading_the_app(self):
        order = []

        app_class = MagicMock()
        splash = MagicMock()
        splash.cancelled = False
        splash.elapsed_ms = MIN_SPLASH_MS * 2
        splash.spin_until.side_effect = lambda predicate: (
            order.append("spin"),
            predicate() or True,
        )[1]

        def fake_import():
            order.append("import")
            return app_class

        with patch("bootstrap.SplashScreen", return_value=splash) as mock_splash_cls, \
             patch("bootstrap._import_app", side_effect=fake_import):
            app = launch_gui(initial_path="D:/Photos/Trip", workspace_path=None)

        mock_splash_cls.assert_called_once()
        self.assertEqual(order, ["import", "spin"])
        app_class.assert_called_once_with(
            initial_path="D:/Photos/Trip",
            workspace_path=None,
            show_splash=False,
            splash_screen=splash,
        )
        splash.close.assert_called()
        app.mainloop.assert_called_once()
        self.assertIs(app, app_class.return_value)

    def test_launch_gui_returns_none_when_splash_is_cancelled(self):
        splash = MagicMock()
        splash.cancelled = True
        splash.spin_until.return_value = False

        with patch("bootstrap.SplashScreen", return_value=splash), \
             patch("bootstrap._import_app", return_value=MagicMock()):
            self.assertIsNone(launch_gui())

        splash.close.assert_called_once()

    def test_launch_gui_closes_splash_before_reraising_import_error(self):
        splash = MagicMock()
        splash.cancelled = False
        splash.elapsed_ms = MIN_SPLASH_MS * 2
        splash.spin_until.side_effect = lambda predicate: predicate()

        with patch("bootstrap.SplashScreen", return_value=splash), \
             patch("bootstrap._import_app", side_effect=ImportError("no numpy")):
            with self.assertRaises(ImportError):
                launch_gui()

        splash.close.assert_called_once()


class TestImageCullerAppSplash(unittest.TestCase):
    """
    The main window drives the splash it is handed and closes it when ready.
    """

    def _bare_app(self):
        from gui import ImageCullerApp

        temp_db_fd, temp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(temp_db_fd)
        app = ImageCullerApp.__new__(ImageCullerApp)
        app.db = DatabaseManager(db_path=temp_db_path)
        app.tabs = []
        app.active_tab_index = -1
        app.current_items = []
        app.current_index = -1
        app.selected_indices = set()
        app.selection_anchor_idx = 0
        app._create_components = MagicMock()
        app._bind_events = MagicMock()
        app._restore_tabs_state = MagicMock()
        app.deiconify = MagicMock()
        app.withdraw = MagicMock()
        app.lift = MagicMock()
        app.focus_force = MagicMock()
        app.update_idletasks = MagicMock()
        app.update = MagicMock()
        app.title = MagicMock()
        app.geometry = MagicMock()
        app.after = MagicMock()
        return app, temp_db_path

    @staticmethod
    def _cleanup(temp_db_path):
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass

    def test_image_culler_app_splash_screen_lifecycle(self):
        app, temp_db_path = self._bare_app()
        splash = MagicMock()

        with patch("customtkinter.CTk.__init__", return_value=None):
            from gui import ImageCullerApp
            ImageCullerApp.__init__(app, initial_path=None, show_splash=False, splash_screen=splash)

        app.withdraw.assert_called_once()
        self.assertTrue(splash.set_status.called)
        app.deiconify.assert_called_once()
        splash.close.assert_called_once()
        self._cleanup(temp_db_path)

    def test_image_culler_app_creates_splash_when_enabled(self):
        app, temp_db_path = self._bare_app()

        with patch("customtkinter.CTk.__init__", return_value=None), \
             patch("gui.SplashScreen") as mock_splash_cls:
            mock_splash_inst = MagicMock()
            mock_splash_cls.return_value = mock_splash_inst

            from gui import ImageCullerApp
            ImageCullerApp.__init__(app, initial_path="D:/Photos/Trip", show_splash=True)

            mock_splash_cls.assert_called_once_with()
            self.assertTrue(mock_splash_inst.set_status.called)
            mock_splash_inst.close.assert_called_once()

        self._cleanup(temp_db_path)


class TestAppIdentity(unittest.TestCase):
    """
    Branding + icon assets used by the window and the splash.
    """

    def test_app_name(self):
        self.assertEqual(bootstrap.APP_NAME, "Quick Cull")
        self.assertEqual(bootstrap.APP_TITLE, "QUICK CULL")

    def test_icon_assets_exist(self):
        self.assertTrue(bootstrap.ICON_PATH.exists(), "missing media/quick_cull.ico")
        self.assertTrue(bootstrap.ICON_PNG_PATH.exists(), "missing media/quick_cull_icon.png")
        self.assertTrue(
            bootstrap.SPLASH_ICON_PNG_PATH.exists(), "missing media/quick_cull_splash_icon.png"
        )

    def test_bootstrap_stays_dependency_free(self):
        """The splash must not pull the slow GUI stack into the launcher."""
        source = Path(bootstrap.__file__).read_text(encoding="utf-8")
        for banned in ("customtkinter", "PIL", "numpy", "torch", "culler"):
            self.assertNotIn(f"import {banned}", source)

    def test_min_splash_duration_is_sane(self):
        self.assertGreater(MIN_SPLASH_MS, 0)
        self.assertLessEqual(MIN_SPLASH_MS, 1500)


if __name__ == "__main__":
    unittest.main()