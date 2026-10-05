import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk

from culler.gui.tab_bar import TabBar, ABOUT_ICON, ADD_TAB_ICON


# One Tk root for the whole module. A second CTk() in this process fails with
# "invalid command name tcl_findLibrary", and creating a root per test intermittently
# fails with "can't find a usable tk.tcl" - which would skip, not fail, the regression
# guards below.
_ROOT = None


def setUpModule():
    global _ROOT
    ctk.set_appearance_mode("dark")
    _ROOT = ctk.CTk()
    _ROOT.geometry("1000x200+30+30")
    _ROOT.update()


def tearDownModule():
    try:
        _ROOT.destroy()
    except Exception:
        pass


class _TabBarTestCase(unittest.TestCase):
    """Shared root plus a bar that is destroyed after each test."""

    @property
    def root(self):
        return _ROOT

    def make_bar(self, **callbacks):
        self.bar = TabBar(_ROOT, **callbacks)
        self.bar.pack(side="top", fill="x")
        _ROOT.update()
        self.addCleanup(self._destroy_bar)
        return self.bar

    def _destroy_bar(self):
        try:
            self.bar.destroy()
        except Exception:
            pass
        _ROOT.update()


class TestTabBarLayout(_TabBarTestCase):
    """
    The '+' button lives inside the scrolling strip after the last tab, and About sits
    at the right edge of the bar.
    """

    def setUp(self):
        self.added = []
        self.about = []
        self.make_bar(
            on_new_tab=lambda: self.added.append(True),
            on_about=lambda: self.about.append(True),
        )

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


class TestTabBarCloseAll(_TabBarTestCase):
    """The Close All button, and the memory-release path it drives."""

    def setUp(self):
        self.closed_all = []
        self.make_bar(
            on_new_tab=lambda: None,
            on_close_all=lambda: self.closed_all.append(True),
            on_tab_closed=lambda idx: None,
        )

    def test_close_all_button_is_hidden_with_no_tabs(self):
        self.assertFalse(self.bar._btn_close_all.winfo_ismapped())

    def test_close_all_button_appears_with_tabs(self):
        self.bar.add_tab("A")
        self.root.update()
        self.assertTrue(self.bar._btn_close_all.winfo_ismapped())

    def test_close_all_button_triggers_callback(self):
        self.bar.add_tab("A")
        self.root.update()
        self.bar._btn_close_all.invoke()
        self.assertEqual(self.closed_all, [True])

    def test_close_all_button_is_hidden_again_after_removing_every_tab(self):
        for label in ("A", "B"):
            self.bar.add_tab(label)
        self.root.update()

        self.bar.remove_all_tabs()
        self.root.update()

        self.assertEqual(self.bar.get_tab_count(), 0)
        self.assertFalse(self.bar._btn_close_all.winfo_ismapped())

    def test_remove_all_tabs_destroys_every_button(self):
        for label in ("A", "B", "C"):
            self.bar.add_tab(label)
        self.root.update()
        buttons = list(self.bar._tab_buttons) + list(self.bar._close_buttons)

        self.bar.remove_all_tabs()

        for button in buttons:
            self.assertFalse(button.winfo_exists(),
                             "a removed tab must not leave a live widget behind")
        self.assertEqual(self.bar.get_labels(), [])

    def test_close_all_with_no_tabs_does_nothing(self):
        self.bar._handle_close_all()
        self.assertEqual(self.closed_all, [])


class TestTabBarHitTesting(_TabBarTestCase):
    """
    Regression for the drag-and-click glitches: hit-testing used to walk every tab's
    widget tree, once per mouse-motion event, and the drag target was derived from the
    binding widget rather than the cursor.
    """

    def setUp(self):
        self.reordered = []
        self.make_bar(on_tab_reordered=lambda a, b: self.reordered.append((a, b)))
        for label in ("A", "B", "C"):
            self.bar.add_tab(label)
        self.root.update()

    def test_index_lookup_needs_no_tree_walk(self):
        for index, button in enumerate(self.bar._tab_buttons):
            self.assertEqual(self.bar._get_index_for_widget(button), index)

    def test_close_button_resolves_to_its_own_tab(self):
        for index, button in enumerate(self.bar._close_buttons):
            self.assertEqual(self.bar._get_index_for_widget(button), index)

    def test_nested_label_of_a_close_button_resolves(self):
        """CustomTkinter nests a label and a canvas inside the button."""
        close_btn = self.bar._close_buttons[1]
        children = close_btn.winfo_children()
        if not children:
            self.skipTest("no nested widgets to test in this CustomTkinter version")
        self.assertEqual(self.bar._get_index_for_widget(children[0]), 1)

    def test_index_lookup_survives_removal(self):
        btn_b = self.bar._tab_buttons[1]
        self.bar.remove_tab(0)
        self.root.update()
        self.assertEqual(self.bar._get_index_for_widget(btn_b), 0)

    def test_index_lookup_survives_reorder(self):
        btn_a, btn_b, btn_c = self.bar._tab_buttons
        self.bar.reorder(0, 2)
        self.root.update()
        self.assertEqual(self.bar._get_index_for_widget(btn_a), 2)
        self.assertEqual(self.bar._get_index_for_widget(btn_b), 0)
        self.assertEqual(self.bar._get_index_for_widget(btn_c), 1)

    def test_unknown_widget_resolves_to_nothing(self):
        self.assertEqual(self.bar._get_index_for_widget(None), -1)
        self.assertEqual(self.bar._get_index_for_widget(self.bar._btn_add), -1)

    def test_drag_motion_coalesces_to_one_hit_test_per_idle(self):
        event = MagicMock()
        event.x_root = 400
        event.y_root = 50
        self.bar._drag_source_idx = 0
        self.bar._drag_over_idx = 0
        self.bar._drag_start_x = 10
        self.bar._drag_start_y = 50

        for _ in range(50):
            self.bar._on_drag_motion(event)

        self.assertIsNotNone(self.bar._drag_hit_after_id,
                             "a drag must schedule a hit test")
        self.root.update()
        self.assertIsNone(self.bar._drag_hit_after_id,
                          "it must be consumed exactly once, not once per motion event")

    def test_drag_motion_without_a_source_is_ignored(self):
        event = MagicMock()
        event.x_root = 400
        event.y_root = 50
        self.bar._drag_source_idx = None
        self.bar._on_drag_motion(event)
        self.assertIsNone(self.bar._drag_hit_after_id)

    def test_adding_a_tab_does_not_repack_the_existing_ones(self):
        """A full relayout per add is O(n²) over a session and visibly jumps."""
        before = {}
        for index, button in enumerate(self.bar._tab_buttons):
            before[button] = button.pack_info()

        self.bar.add_tab("D")
        self.root.update()

        for button, info in before.items():
            self.assertEqual(button.pack_info(), info,
                             "adding a tab must not disturb the existing buttons")


if __name__ == "__main__":
    unittest.main()
