"""The action bags: no overlap, drag-to-reorder, and a persisted order."""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk

from culler.gui.metadata_panel import (
    BAG_KEYS,
    BAG_SETTINGS_KEY,
    PANEL_WIDTH,
    MetadataPanel,
)


_ROOT = None


def setUpModule():
    global _ROOT
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    _ROOT = ctk.CTk()
    _ROOT.geometry("420x760+30+30")
    _ROOT.update()


def tearDownModule():
    try:
        _ROOT.destroy()
    except Exception:
        pass


def _callbacks():
    return dict(
        on_set_flag=MagicMock(),
        on_set_rating=MagicMock(),
        on_toggle_tag=MagicMock(),
        on_unflag_all=MagicMock(),
        on_untag_all=MagicMock(),
        on_unrate_all=MagicMock(),
        on_clear_all=MagicMock(),
        on_crop=MagicMock(),
        on_annotate=MagicMock(),
        on_convert_jpg=MagicMock(),
        on_move_picked=MagicMock(),
        on_move_rejected=MagicMock(),
        on_config_output_folders=MagicMock(),
    )


class _PanelTestCase(unittest.TestCase):
    def make_panel(self, **kwargs):
        cbs = _callbacks()
        cbs.update(kwargs)
        panel = MetadataPanel(_ROOT, **cbs)
        panel.pack(side="top", fill="x")
        _ROOT.update()
        self.addCleanup(self._destroy, panel)
        return panel

    @staticmethod
    def _destroy(panel):
        try:
            panel.destroy()
        except Exception:
            pass
        _ROOT.update()


class TestBagLayout(_PanelTestCase):
    def test_all_bags_are_present_and_labelled(self):
        panel = self.make_panel()

        self.assertEqual(sorted(panel.bag_order()), sorted(BAG_KEYS))
        titles = {panel._bag_titles[k].cget("text") for k in panel.bag_order()}
        self.assertEqual(
            titles,
            {"CULLING ACTIONS", "MOVE & EXPORT", "IMAGE TAGS", "EXIF METADATA"},
        )

    def test_bags_do_not_overlap(self):
        """
        Packed straight into a fixed-height panel the bags overflowed and Tk placed the
        remainder on top of each other. This is the regression guard for that.
        """
        panel = self.make_panel()
        _ROOT.update()

        spans = []
        for key in panel.bag_order():
            frame = panel._bags[key]
            top = frame.winfo_rooty()
            spans.append((top, top + frame.winfo_height(), key))
        spans.sort()

        for i in range(1, len(spans)):
            self.assertGreaterEqual(
                spans[i][0], spans[i - 1][1],
                f"{spans[i][2]} overlaps {spans[i - 1][2]}",
            )

    def test_no_bag_is_wider_than_the_panel(self):
        """
        The old fixed 125 px tag buttons needed more inner width than the panel had, so
        the tags bag grew past its neighbours.
        """
        panel = self.make_panel()
        _ROOT.update()
        panel_width = panel.winfo_width()

        for key in panel.bag_order():
            req = panel._bags[key].winfo_reqwidth()
            self.assertLessEqual(req, panel_width,
                                 f"{key} needs {req}px but the panel is {panel_width}px")

    def test_tags_still_fit_after_adding_a_very_long_custom_tag(self):
        panel = self.make_panel()
        _ROOT.update()

        panel.refresh_tag_buttons(["AnExtremelyLongCustomTagName", "Second", "Third"])
        _ROOT.update()

        self.assertLessEqual(panel._bags["tags"].winfo_reqwidth(), panel.winfo_width())
        self.assertIn("AnExtremelyLongCustomTagName", panel._tag_buttons)

    def _shorten(self, panel, height=170):
        """Confine the panel to a fixed-height host, as a short window would."""
        panel.pack_forget()
        host = ctk.CTkFrame(_ROOT, width=PANEL_WIDTH, height=height, fg_color="#1a1a1a")
        host.pack(side="top", fill="x")
        host.pack_propagate(False)
        panel.pack(in_=host, side="top", fill="both", expand=True)
        self.addCleanup(host.destroy)
        _ROOT.update()
        return host

    def test_bags_scroll_when_the_window_is_short(self):
        """Overflow scrolls rather than overlapping."""
        panel = self.make_panel()
        self._shorten(panel)

        # A CTkScrollableFrame *is* its inner frame; the viewport is its canvas.
        area = panel._bags_area
        viewport = area._parent_canvas.winfo_height()
        content = area.winfo_reqheight()
        self.assertLess(viewport, 400, "the test must actually have shortened the panel")
        self.assertGreater(content, viewport,
                           "a short panel must have more content than viewport")

    def test_bags_do_not_overlap_when_the_panel_is_short(self):
        """The overlap guard must hold in exactly the case that used to break."""
        panel = self.make_panel()
        self._shorten(panel)

        spans = []
        for key in panel.bag_order():
            frame = panel._bags[key]
            top = frame.winfo_rooty()
            spans.append((top, top + frame.winfo_height(), key))
        spans.sort()

        for i in range(1, len(spans)):
            self.assertGreaterEqual(
                spans[i][0], spans[i - 1][1],
                f"{spans[i][2]} overlaps {spans[i - 1][2]} in a short panel",
            )

    def test_every_bag_uses_the_same_width(self):
        panel = self.make_panel()
        _ROOT.update()

        widths = {panel._bags[k].winfo_width() for k in panel.bag_order()}
        self.assertEqual(len(widths), 1, f"bags have differing widths: {widths}")


class TestBagReordering(_PanelTestCase):
    def _drag(self, panel, key, target_key):
        start = MagicMock()
        start.widget = panel._bag_titles[key]
        move = MagicMock()
        move.y_root = panel._bags[target_key].winfo_rooty() + 2
        end = MagicMock()
        panel._on_bag_drag_start(start)
        panel._on_bag_drag_motion(move)
        panel._on_bag_drag_end(end)
        _ROOT.update()

    def test_dragging_a_title_reorders_the_bags(self):
        panel = self.make_panel()

        self._drag(panel, "meta", "action")

        self.assertEqual(panel.bag_order()[0], "meta",
                         "the dragged bag must land where it was dropped")

    def test_visual_order_follows_the_stored_order(self):
        panel = self.make_panel()

        self._drag(panel, "meta", "action")

        by_position = sorted(
            ((panel._bags[k].winfo_rooty(), k) for k in panel.bag_order()))
        self.assertEqual([k for _, k in by_position], panel.bag_order(),
                         "what is drawn top to bottom must match the stored order")

    def test_drag_reports_the_new_order_once(self):
        saved = []
        panel = self.make_panel(on_bag_order_changed=lambda order: saved.append(list(order)))

        self._drag(panel, "tags", "move")

        self.assertEqual(len(saved), 1, "one drag must report one order")
        self.assertEqual(saved[0], panel.bag_order())

    def test_dragging_a_title_leaves_no_stale_state(self):
        panel = self.make_panel()

        self._drag(panel, "move", "action")

        self.assertIsNone(panel._drag_key)
        self.assertIsNone(panel._drag_target)

    def test_drag_restores_the_title_colour(self):
        panel = self.make_panel()
        before = panel._bag_titles["move"].cget("text_color")

        self._drag(panel, "move", "action")

        self.assertEqual(panel._bag_titles["move"].cget("text_color"), before,
                         "the drag highlight must be cleared, and to a valid colour")

    def test_motion_without_a_press_is_ignored(self):
        panel = self.make_panel()
        before = panel.bag_order()

        move = MagicMock()
        move.y_root = 10
        panel._on_bag_drag_motion(move)

        self.assertEqual(panel.bag_order(), before)

    def test_set_bag_order_applies_a_new_order(self):
        panel = self.make_panel()

        panel.set_bag_order(["meta", "action", "move", "tags"])
        _ROOT.update()

        by_position = sorted(
            ((panel._bags[k].winfo_rooty(), k) for k in panel.bag_order()))
        self.assertEqual([k for _, k in by_position],
                         ["meta", "action", "move", "tags"])


class TestBagOrderPersistence(_PanelTestCase):
    def test_unknown_keys_are_dropped_and_missing_ones_added(self):
        panel = self.make_panel(bag_order=["meta", "no-such-bag", "action"])

        self.assertEqual(set(panel.bag_order()), set(BAG_KEYS))
        self.assertNotIn("no-such-bag", panel.bag_order())
        self.assertEqual(panel.bag_order()[:2], ["meta", "action"],
                         "the persisted order must be respected where it is valid")

    def test_an_absent_order_falls_back_to_the_default(self):
        panel = self.make_panel(bag_order=None)

        self.assertEqual(panel.bag_order(), list(BAG_KEYS))

    def test_a_reordered_panel_comes_back_in_that_order(self):
        first = self.make_panel()
        self._drag_panel_to(first, "meta", "action")
        saved = first.bag_order()
        first.destroy()
        _ROOT.update()

        second = self.make_panel(bag_order=saved)
        self.assertEqual(second.bag_order(), saved)

    def _drag_panel_to(self, panel, key, target_key):
        start = MagicMock()
        start.widget = panel._bag_titles[key]
        move = MagicMock()
        move.y_root = panel._bags[target_key].winfo_rooty() + 2
        panel._on_bag_drag_start(start)
        panel._on_bag_drag_motion(move)
        panel._on_bag_drag_end(MagicMock())

    def test_the_settings_key_is_namespaced(self):
        self.assertEqual(BAG_SETTINGS_KEY, "meta_panel_bag_order")


class TestPanelWidth(_PanelTestCase):
    def test_panel_is_the_narrow_right_sidebar(self):
        panel = self.make_panel()

        self.assertEqual(PANEL_WIDTH, 290,
                         "the sidebar keeps the width it has always had")
        self.assertGreater(PANEL_WIDTH, 240)
        self.assertLess(PANEL_WIDTH, 340, "it must leave room for the viewer")

    def test_the_panel_still_fits_its_longest_bag(self):
        """The right sidebar is narrower than the grid column, so this is tighter."""
        panel = self.make_panel()
        _ROOT.update()
        panel.refresh_tag_buttons(["AnExtremelyLongCustomTagName", "SecondOne"])
        _ROOT.update()

        for key in panel.bag_order():
            self.assertLessEqual(panel._bags[key].winfo_reqwidth(), panel.winfo_width(),
                                 f"{key} overflows the sidebar")


class TestCullingActionsControls(_PanelTestCase):
    def test_star_buttons_under_culling_actions(self):
        rating_clicked = []
        panel = self.make_panel(on_set_rating=lambda r: rating_clicked.append(r))
        self.assertEqual(len(panel.star_buttons), 5)
        # Verify star buttons are children inside action_content
        for btn in panel.star_buttons:
            self.assertEqual(btn.master, panel.star_btn_frame)
            self.assertEqual(panel.star_btn_frame.master, panel.action_content)

        # Trigger star 3
        panel.star_buttons[2].invoke()
        self.assertEqual(rating_clicked, [3])

    def test_clear_buttons_2_in_a_row_under_culling_actions(self):
        unflag_called = []
        untag_called = []
        unrate_called = []
        clear_called = []

        panel = self.make_panel(
            on_unflag_all=lambda: unflag_called.append(True),
            on_untag_all=lambda: untag_called.append(True),
            on_unrate_all=lambda: unrate_called.append(True),
            on_clear_all=lambda: clear_called.append(True),
        )

        # Verify buttons exist and are inside action_content
        self.assertIsNotNone(panel.btn_unflag_all)
        self.assertIsNotNone(panel.btn_untag_all)
        self.assertIsNotNone(panel.btn_unrate_all)
        self.assertIsNotNone(panel.btn_clear_all)

        self.assertEqual(panel.btn_unflag_all.master, panel.reset_row1)
        self.assertEqual(panel.btn_untag_all.master, panel.reset_row1)
        self.assertEqual(panel.btn_unrate_all.master, panel.reset_row2)
        self.assertEqual(panel.btn_clear_all.master, panel.reset_row2)

        self.assertEqual(panel.reset_row1.master, panel.action_content)
        self.assertEqual(panel.reset_row2.master, panel.action_content)

        # Check button styling (red for individual clear, darker red for clear all)
        self.assertEqual(panel.btn_unflag_all.cget("fg_color"), "#991b1b")
        self.assertEqual(panel.btn_untag_all.cget("fg_color"), "#991b1b")
        self.assertEqual(panel.btn_unrate_all.cget("fg_color"), "#991b1b")
        self.assertEqual(panel.btn_clear_all.cget("fg_color"), "#520713")

        # Check button invocations
        panel.btn_unflag_all.invoke()
        panel.btn_untag_all.invoke()
        panel.btn_unrate_all.invoke()
        panel.btn_clear_all.invoke()

        self.assertEqual(unflag_called, [True])
        self.assertEqual(untag_called, [True])
        self.assertEqual(unrate_called, [True])
        self.assertEqual(clear_called, [True])

    def test_tag_buttons_color_is_green(self):
        panel = self.make_panel()
        self.assertIn("Blur", panel._tag_buttons)
        self.assertEqual(panel._tag_buttons["Blur"].cget("fg_color"), "#1b4332")
        self.assertEqual(panel._tag_buttons["Duplicate"].cget("fg_color"), "#1b4332")
        self.assertEqual(panel._tag_buttons["Dark"].cget("fg_color"), "#1b4332")
        self.assertEqual(panel._tag_buttons["Over-exposed"].cget("fg_color"), "#1b4332")


if __name__ == "__main__":
    unittest.main()
