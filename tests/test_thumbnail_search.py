import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tkinter as tk
import customtkinter as ctk

from culler.culler_engine import ImageItem, FlagState
from culler.gui.thumbnail_list import ThumbnailList

_ROOT = None


def setUpModule():
    global _ROOT
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    try:
        _ROOT = ctk.CTk()
        _ROOT.geometry("900x700+30+30")
        _ROOT.update()
    except Exception:
        _ROOT = None


def tearDownModule():
    global _ROOT
    if _ROOT is not None:
        try:
            _ROOT.destroy()
        except Exception:
            pass
        _ROOT = None


class TestThumbnailSearch(unittest.TestCase):
    def setUp(self):
        if _ROOT is None:
            self.skipTest("Tkinter display not available")
        self.root = _ROOT
        self.mock_select = MagicMock()
        self.thumb_list = ThumbnailList(
            self.root,
            on_select_image=self.mock_select
        )
        self.thumb_list.pack()
        self.root.update()

        # Build mock items
        self.items = [
            ImageItem(Path("D:/Photos/DSC00101.JPG")),
            ImageItem(Path("D:/Photos/DSC00102.JPG")),
            ImageItem(Path("D:/Photos/DSC00201.JPG")),
            ImageItem(Path("D:/Photos/FAMILY_PORTRAIT.JPG")),
            ImageItem(Path("D:/Photos/SUNSET_DSC00300.JPG")),
        ]
        self.items[0].flag = FlagState.PICK
        self.items[1].flag = FlagState.REJECT
        self.items[2].rating = 4
        self.thumb_list.update_items(self.items, selected_idx=0)
        self.root.update()

    def tearDown(self):
        try:
            self.thumb_list.shutdown()
        except Exception:
            pass
        try:
            self.thumb_list.destroy()
        except Exception:
            pass
        if self.root:
            self.root.update()

    def test_search_ui_widgets_exist(self):
        self.assertTrue(hasattr(self.thumb_list, "search_frame"))
        self.assertTrue(hasattr(self.thumb_list, "search_entry"))
        self.assertTrue(hasattr(self.thumb_list, "btn_clear_search"))
        self.assertTrue(hasattr(self.thumb_list, "suggestion_container"))
        self.assertFalse(self.thumb_list.suggestion_container.winfo_ismapped())

    def test_continuous_suggestions_as_typed(self):
        # Type "DSC001"
        self.thumb_list.search_var.set("DSC001")
        self.root.update()

        # Should match DSC00101.JPG and DSC00102.JPG
        self.assertTrue(self.thumb_list.suggestion_container.winfo_ismapped())
        self.assertEqual(len(self.thumb_list._suggestion_items), 2)
        filenames = [item.filename for _, item in self.thumb_list._suggestion_items]
        self.assertIn("DSC00101.JPG", filenames)
        self.assertIn("DSC00102.JPG", filenames)

    def test_prefix_matches_ranked_before_substring(self):
        # "DSC" matches DSC00101, DSC00102, DSC00201 (as prefix), and SUNSET_DSC00300 (substring)
        self.thumb_list.search_var.set("DSC")
        self.root.update()

        items = self.thumb_list._suggestion_items
        self.assertEqual(len(items), 4)
        # Prefix matches should come first
        self.assertTrue(items[0][1].filename.startswith("DSC"))
        self.assertTrue(items[1][1].filename.startswith("DSC"))
        self.assertTrue(items[2][1].filename.startswith("DSC"))
        self.assertEqual(items[3][1].filename, "SUNSET_DSC00300.JPG")

    def test_no_matches_displays_message(self):
        self.thumb_list.search_var.set("NON_EXISTENT_FILE")
        self.root.update()

        self.assertTrue(self.thumb_list.suggestion_container.winfo_ismapped())
        self.assertEqual(len(self.thumb_list._suggestion_items), 0)
        # Should have a label indicating no matches
        labels = [w for w in self.thumb_list.suggestion_container.winfo_children() if isinstance(w, ctk.CTkLabel)]
        self.assertTrue(len(labels) >= 1)
        self.assertIn("No matches", labels[0].cget("text"))

    def test_arrow_keys_navigate_suggestions(self):
        self.thumb_list.search_var.set("DSC")
        self.root.update()

        # Initial highlight is index 0
        self.assertEqual(self.thumb_list._highlighted_suggestion_idx, 0)

        # Press Down
        self.thumb_list._on_search_down()
        self.assertEqual(self.thumb_list._highlighted_suggestion_idx, 1)

        # Press Up
        self.thumb_list._on_search_up()
        self.assertEqual(self.thumb_list._highlighted_suggestion_idx, 0)

        # Wrap around with Up
        self.thumb_list._on_search_up()
        self.assertEqual(self.thumb_list._highlighted_suggestion_idx, len(self.thumb_list._suggestion_items) - 1)

    def test_return_key_jumps_to_highlighted_suggestion(self):
        self.thumb_list.search_var.set("PORTRAIT")
        self.root.update()

        self.assertEqual(len(self.thumb_list._suggestion_items), 1)
        matched_idx, matched_item = self.thumb_list._suggestion_items[0]
        self.assertEqual(matched_item.filename, "FAMILY_PORTRAIT.JPG")

        # Press Return
        self.thumb_list._on_search_return()

        self.mock_select.assert_called_with(
            matched_idx,
            matched_item.path,
            is_continuous=False,
            is_ctrl=False,
            is_shift=False,
            from_click=False
        )
        self.assertFalse(self.thumb_list.suggestion_container.winfo_ismapped())

    def test_jump_to_index_direct_call(self):
        self.thumb_list.jump_to_index(2)
        self.mock_select.assert_called_with(
            2,
            self.items[2].path,
            is_continuous=False,
            is_ctrl=False,
            is_shift=False,
            from_click=False
        )

    def test_clear_search_clears_text_and_hides_suggestions(self):
        self.thumb_list.search_var.set("DSC")
        self.root.update()
        self.assertTrue(self.thumb_list.suggestion_container.winfo_ismapped())

        self.thumb_list.clear_search(keep_focus=False)
        self.assertEqual(self.thumb_list.search_var.get(), "")
        self.assertFalse(self.thumb_list.suggestion_container.winfo_ismapped())

    def test_escape_hides_suggestions(self):
        self.thumb_list.search_var.set("DSC")
        self.root.update()
        self.assertTrue(self.thumb_list.suggestion_container.winfo_ismapped())

        self.thumb_list._on_search_escape()
        self.assertFalse(self.thumb_list.suggestion_container.winfo_ismapped())

    def test_focus_search_sets_focus(self):
        self.thumb_list.search_var.set("Sample")
        self.thumb_list.focus_search()
        self.root.update()
        focused = self.root.focus_get()
        self.assertIsNotNone(focused)

    def test_click_suggestion_button_triggers_jump(self):
        self.thumb_list.search_var.set("PORTRAIT")
        self.root.update()

        self.assertEqual(len(self.thumb_list._suggestion_buttons), 1)
        matched_idx, matched_item = self.thumb_list._suggestion_items[0]
        btn = self.thumb_list._suggestion_buttons[0]

        # Trigger button command
        btn.invoke()

        self.mock_select.assert_called_with(
            matched_idx,
            matched_item.path,
            is_continuous=False,
            is_ctrl=False,
            is_shift=False,
            from_click=False
        )
        self.assertFalse(self.thumb_list.suggestion_container.winfo_ismapped())

    def test_hover_suggestion_updates_highlight(self):
        self.thumb_list.search_var.set("DSC")
        self.root.update()

        self.assertEqual(self.thumb_list._highlighted_suggestion_idx, 0)
        self.thumb_list._on_suggestion_hover(2)
        self.assertEqual(self.thumb_list._highlighted_suggestion_idx, 2)
        self.assertEqual(self.thumb_list._suggestion_buttons[2].cget("fg_color"), "#1f538d")
        self.assertEqual(self.thumb_list._suggestion_buttons[0].cget("fg_color"), "transparent")

    def test_partial_string_substring_matching(self):
        # Substring "300" within SUNSET_DSC00300.JPG
        self.thumb_list.search_var.set("300")
        self.root.update()

        self.assertEqual(len(self.thumb_list._all_matches), 1)
        self.assertEqual(self.thumb_list._all_matches[0][1].filename, "SUNSET_DSC00300.JPG")

        # Substring "SET" within SUNSET_DSC00300.JPG
        self.thumb_list.search_var.set("SET")
        self.root.update()
        self.assertEqual(len(self.thumb_list._all_matches), 1)
        self.assertEqual(self.thumb_list._all_matches[0][1].filename, "SUNSET_DSC00300.JPG")

        # Substring "PORT" within FAMILY_PORTRAIT.JPG
        self.thumb_list.search_var.set("PORT")
        self.root.update()
        self.assertEqual(len(self.thumb_list._all_matches), 1)
        self.assertEqual(self.thumb_list._all_matches[0][1].filename, "FAMILY_PORTRAIT.JPG")

    def test_multi_token_contains_matching(self):
        # Space-separated tokens "sunset 300"
        self.thumb_list.search_var.set("sunset 300")
        self.root.update()
        self.assertEqual(len(self.thumb_list._all_matches), 1)
        self.assertEqual(self.thumb_list._all_matches[0][1].filename, "SUNSET_DSC00300.JPG")

        # Space-separated tokens "family portrait"
        self.thumb_list.search_var.set("family portrait")
        self.root.update()
        self.assertEqual(len(self.thumb_list._all_matches), 1)
        self.assertEqual(self.thumb_list._all_matches[0][1].filename, "FAMILY_PORTRAIT.JPG")

    def test_enter_cycles_through_all_matches(self):
        self.thumb_list.search_var.set("DSC")
        self.root.update()

        # Should match 4 items: DSC00101, DSC00102, DSC00201, SUNSET_DSC00300
        self.assertEqual(len(self.thumb_list._all_matches), 4)
        self.assertEqual(self.thumb_list.lbl_search_count.cget("text"), "1/4")

        # Press Return -> jumps to match 0
        self.thumb_list._on_search_return()
        self.assertEqual(self.mock_select.call_args[0][0], self.thumb_list._all_matches[0][0])
        self.assertEqual(self.thumb_list.lbl_search_count.cget("text"), "2/4")

        # Press Return -> jumps to match 1
        self.thumb_list._on_search_return()
        self.assertEqual(self.mock_select.call_args[0][0], self.thumb_list._all_matches[1][0])
        self.assertEqual(self.thumb_list.lbl_search_count.cget("text"), "3/4")

        # Press Return -> jumps to match 2
        self.thumb_list._on_search_return()
        self.assertEqual(self.mock_select.call_args[0][0], self.thumb_list._all_matches[2][0])
        self.assertEqual(self.thumb_list.lbl_search_count.cget("text"), "4/4")

        # Press Return -> jumps to match 3
        self.thumb_list._on_search_return()
        self.assertEqual(self.mock_select.call_args[0][0], self.thumb_list._all_matches[3][0])
        self.assertEqual(self.thumb_list.lbl_search_count.cget("text"), "1/4")

    def test_shift_enter_cycles_backward(self):
        self.thumb_list.search_var.set("DSC")
        self.root.update()

        # Shift+Return from initial position -> jumps to last match (wrap around)
        self.thumb_list._on_search_shift_return()
        self.assertEqual(self.mock_select.call_args[0][0], self.thumb_list._all_matches[3][0])
        self.assertEqual(self.thumb_list.lbl_search_count.cget("text"), "4/4")

        # Shift+Return again -> jumps to match 2
        self.thumb_list._on_search_shift_return()
        self.assertEqual(self.mock_select.call_args[0][0], self.thumb_list._all_matches[2][0])
        self.assertEqual(self.thumb_list.lbl_search_count.cget("text"), "3/4")

    def test_search_count_indicator(self):
        # Empty
        self.assertEqual(self.thumb_list.lbl_search_count.cget("text"), "")

        # Matched
        self.thumb_list.search_var.set("PORTRAIT")
        self.root.update()
        self.assertEqual(self.thumb_list.lbl_search_count.cget("text"), "1/1")

        # Non-matching
        self.thumb_list.search_var.set("NON_EXISTING")
        self.root.update()
        self.assertEqual(self.thumb_list.lbl_search_count.cget("text"), "0/0")


class TestGuiSearchIntegration(unittest.TestCase):
    def test_is_entry_focused(self):
        from gui import ImageCullerApp
        mock_gui = MagicMock(spec=ImageCullerApp)

        # When no focus
        mock_gui.focus_get.return_value = None
        self.assertFalse(ImageCullerApp._is_entry_focused(mock_gui))

        # When standard tk.Entry has focus
        mock_entry = MagicMock(spec=tk.Entry)
        mock_entry.winfo_class.return_value = "Entry"
        mock_gui.focus_get.return_value = mock_entry
        self.assertTrue(ImageCullerApp._is_entry_focused(mock_gui))

        # When ctk.CTkEntry has focus
        mock_ctk_entry = MagicMock(spec=ctk.CTkEntry)
        mock_ctk_entry.winfo_class.return_value = "CTkEntry"
        mock_gui.focus_get.return_value = mock_ctk_entry
        self.assertTrue(ImageCullerApp._is_entry_focused(mock_gui))

        # When regular frame has focus
        mock_frame = MagicMock(spec=ctk.CTkFrame)
        mock_frame.winfo_class.return_value = "Frame"
        mock_frame._entry = None
        mock_gui.focus_get.return_value = mock_frame
        self.assertFalse(ImageCullerApp._is_entry_focused(mock_gui))

    def test_on_focus_search_unhides_panel_and_focuses(self):
        from gui import ImageCullerApp
        mock_gui = MagicMock(spec=ImageCullerApp)
        mock_gui._thumbnail_panel_visible = False
        mock_gui.thumb_list = MagicMock()

        res = ImageCullerApp._on_focus_search(mock_gui)
        self.assertEqual(res, "break")
        mock_gui.toggle_thumbnail_panel.assert_called_once_with(True)
        mock_gui.thumb_list.focus_search.assert_called_once()


if __name__ == "__main__":
    unittest.main()
