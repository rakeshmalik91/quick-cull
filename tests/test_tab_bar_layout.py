import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk

from culler.gui.tab_bar import TabBar, ABOUT_ICON, ADD_TAB_ICON


class TestTabBarLayout(unittest.TestCase):
    """
    The '+' button lives inside the scrolling strip after the last tab, and About sits
    at the right edge of the bar.
    """

    @classmethod
    def setUpClass(cls):
        # One Tk root for the whole class: creating a root per test intermittently
        # fails with "Can't find a usable tk.tcl" in this environment.
        cls.root = ctk.CTk()
        cls.root.geometry("1000x200+30+30")
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        self.added = []
        self.about = []
        self.bar = TabBar(
            self.root,
            on_new_tab=lambda: self.added.append(True),
            on_about=lambda: self.about.append(True),
        )
        self.bar.pack(side="top", fill="x")
        self.root.update()

    def tearDown(self):
        try:
            self.bar.destroy()
        except Exception:
            pass
        self.root.update()

    def _add(self, *labels):
        for label in labels:
            self.bar.add_tab(label)
        self.root.update()

    def test_add_button_is_plus(self):
        self.assertEqual(ADD_TAB_ICON, "+")
        self.assertEqual(self.bar._btn_add.cget("text"), ADD_TAB_ICON)

    def test_about_button_is_an_info_icon(self):
        """The About button carries an icon glyph, not the word 'About'."""
        self.assertEqual(self.bar._btn_about.cget("text"), ABOUT_ICON)
        self.assertEqual(self.bar._btn_about.cget("text"), "\u24d8")
        self.assertNotEqual(self.bar._btn_about.cget("text"), "About")

    def test_about_glyph_renders_in_the_default_font(self):
        """
        A missing glyph would render as a narrow fallback box; assert a sane width.
        """
        font = self.bar._btn_about.cget("font")
        try:
            width = font.measure(ABOUT_ICON)
        except Exception:
            self.skipTest("font measurement unavailable in this environment")
        self.assertGreaterEqual(width, 10,
                                "the info glyph must exist in the button font")

    def test_add_button_lives_in_the_scrolling_strip(self):
        """It must scroll with the tabs, not be pinned to the bar edge."""
        self.assertEqual(self.bar._btn_add.master, self.bar._inner_frame)

    def test_add_button_sits_after_the_last_tab(self):
        self._add("A", "B", "C")
        last_close_right = (self.bar._close_buttons[-1].winfo_x()
                            + self.bar._close_buttons[-1].winfo_width())
        add_left = self.bar._btn_add.winfo_x()
        self.assertGreaterEqual(add_left, last_close_right,
                                "'+' must follow the last tab, not precede or overlap it")

    def test_add_button_stays_last_with_many_tabs(self):
        self._add(*[f"Tab {i}" for i in range(8)])
        last_close_right = (self.bar._close_buttons[-1].winfo_x()
                            + self.bar._close_buttons[-1].winfo_width())
        self.assertGreaterEqual(self.bar._btn_add.winfo_x(), last_close_right)

    def test_about_button_is_on_the_right_edge(self):
        self._add("A", "B")
        about = self.bar._btn_about
        about_right = about.winfo_x() + about.winfo_width()
        bar_width = self.bar.winfo_width()
        self.assertGreaterEqual(bar_width - about_right, -2,
                                "About must hug the right edge of the bar")
        self.assertGreaterEqual(about_right, bar_width - about.winfo_width() - 12)

    def test_about_button_triggers_callback(self):
        self.bar._btn_about.invoke()
        self.assertEqual(self.about, [True])

    def test_add_button_triggers_callback(self):
        self.bar._btn_add.invoke()
        self.assertEqual(self.added, [True])

    def test_about_callback_is_optional(self):
        bar = TabBar(self.root)
        bar.pack(side="top", fill="x")
        self.root.update()
        try:
            bar._btn_about.invoke()  # must not raise
        finally:
            bar.destroy()
            self.root.update()

    def test_relayout_keeps_plus_last_after_removal(self):
        self._add("A", "B", "C")
        self.bar.remove_tab(2)
        self.root.update()
        last_close_right = (self.bar._close_buttons[-1].winfo_x()
                            + self.bar._close_buttons[-1].winfo_width())
        self.assertGreaterEqual(self.bar._btn_add.winfo_x(), last_close_right)

    def test_relayout_keeps_plus_last_after_removing_every_tab(self):
        self._add("A", "B")
        self.bar.remove_tab(0)
        self.bar.remove_tab(0)
        self.root.update()
        self.assertEqual(self.bar.get_tab_count(), 0)
        self.assertTrue(self.bar._btn_add.winfo_exists())

    def test_relayout_keeps_plus_last_after_reorder(self):
        self._add("A", "B", "C")
        self.bar.reorder(0, 2)
        self.root.update()
        last_close_right = (self.bar._close_buttons[-1].winfo_x()
                            + self.bar._close_buttons[-1].winfo_width())
        self.assertGreaterEqual(self.bar._btn_add.winfo_x(), last_close_right)

    def test_tab_order_preserved_after_relayout(self):
        self._add("A", "B", "C")
        self._add("D")
        self.assertEqual(self.bar.get_labels(), ["A", "B", "C", "D"])


if __name__ == "__main__":
    unittest.main()