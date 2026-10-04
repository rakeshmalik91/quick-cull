import sys
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk

from culler.gui.about_dialog import AboutDialog, CO_AUTHORS, COPYRIGHT, TITLE


def _widgets_in(widget):
    """Every widget under ``widget``, including itself."""
    found = [widget]
    for child in widget.winfo_children():
        found.extend(_widgets_in(child))
    return found


def _texts_in(widget):
    """Rendered text of every label and button under ``widget``."""
    texts = []
    for child in _widgets_in(widget):
        if isinstance(child, (ctk.CTkLabel, ctk.CTkButton)):
            try:
                texts.append(str(child.cget("text")))
            except Exception:
                continue
    return texts


class TestAboutDialogContent(unittest.TestCase):
    """
    The About window carries the authorship and co-author credits.
    """

    @classmethod
    def setUpClass(cls):
        # One Tk root for the class: a root per test intermittently fails with
        # "Can't find a usable tk.tcl" in this environment.
        cls.root = ctk.CTk()
        cls.root.geometry("500x400+30+30")
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def test_credit_constants(self):
        self.assertEqual(COPYRIGHT, "\u00a9 Rakesh Malik 2026")
        self.assertEqual(CO_AUTHORS, ["Gemini 3.8 Flash", "Spaace Bunny Alpha", "Claude Opus 4.6"])
        self.assertEqual(TITLE, "About")

    def test_dialog_renders_all_credits(self):
        dialog = AboutDialog(self.root, app_name="Quick Cull", version="1.0.0")
        self.root.update()

        self.assertEqual(dialog.title(), TITLE)
        rendered = "\n".join(_texts_in(dialog))

        self.assertIn("Quick Cull", rendered)
        self.assertIn("1.0.0", rendered)
        self.assertIn(COPYRIGHT, rendered)
        self.assertIn("Co authored by:", rendered)
        for name in CO_AUTHORS:
            self.assertIn(name, rendered)

        dialog.destroy()
        self.root.update()

    def test_version_is_optional(self):
        dialog = AboutDialog(self.root, app_name="Quick Cull")
        self.root.update()

        self.assertNotIn("version", "\n".join(_texts_in(dialog)))

        dialog.destroy()
        self.root.update()

    def test_close_button_destroys_window(self):
        dialog = AboutDialog(self.root, app_name="Quick Cull", version="1.0.0")
        self.root.update()

        buttons = [w for w in _widgets_in(dialog) if isinstance(w, ctk.CTkButton)]
        close_buttons = [b for b in buttons if str(b.cget("text")) == "Close"]
        self.assertEqual(len(close_buttons), 1)

        close_buttons[0].invoke()
        self.root.update()
        self.assertFalse(dialog.winfo_exists())


class TestAboutDialogWiring(unittest.TestCase):
    """
    gui._on_about_clicked focuses a live dialog instead of stacking copies.
    """

    def test_repeated_open_reuses_live_dialog(self):
        from gui import ImageCullerApp

        app = MagicMock()
        dialog = MagicMock()
        dialog.winfo_exists.return_value = True
        app._about_dialog = dialog

        ImageCullerApp._on_about_clicked(app)

        dialog.lift.assert_called_once()
        dialog.focus_force.assert_called_once()
        self.assertIs(app._about_dialog, dialog)

    def test_reopens_after_dialog_was_closed(self):
        from gui import ImageCullerApp
        import gui as gui_module

        app = MagicMock()
        stale = MagicMock()
        stale.winfo_exists.return_value = False
        app._about_dialog = stale

        created = {}

        class FakeDialog:
            def __init__(self, master, app_name="", version=""):
                created["app_name"] = app_name
                created["version"] = version

        original = gui_module.AboutDialog
        gui_module.AboutDialog = FakeDialog
        try:
            ImageCullerApp._on_about_clicked(app)
        finally:
            gui_module.AboutDialog = original

        self.assertIsInstance(app._about_dialog, FakeDialog)
        self.assertEqual(created["version"], "1.0.0")

    def test_stale_reference_is_cleared(self):
        """
        A reference to an already-destroyed window must not break reopening.
        """
        from gui import ImageCullerApp
        import gui as gui_module

        app = MagicMock()
        stale = MagicMock()
        stale.winfo_exists.side_effect = tk.TclError("invalid command name")
        app._about_dialog = stale

        created = {}

        class FakeDialog:
            def __init__(self, master, app_name="", version=""):
                created["opened"] = True

        original = gui_module.AboutDialog
        gui_module.AboutDialog = FakeDialog
        try:
            ImageCullerApp._on_about_clicked(app)
        finally:
            gui_module.AboutDialog = original

        self.assertTrue(created.get("opened"), "a fresh dialog must open")
        self.assertIsInstance(app._about_dialog, FakeDialog)


if __name__ == "__main__":
    unittest.main()