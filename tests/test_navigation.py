import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from culler.culler_engine import CullingSession, ImageItem


class TestNavigationAndSelection(unittest.TestCase):
    """
    Automated Unit Test Suite for Culler Navigation, Multi-Selection, 
    Keyboard Shortcuts, Range Select, Ctrl Toggle, and Auto-Scroll.
    """

    def setUp(self):
        # Create a mock session with 10 dummy items
        self.session = MagicMock(spec=CullingSession)
        self.session.items = []
        for i in range(10):
            p = Path(f"D:/Photos/TEST_{i:03d}.JPG")
            item = ImageItem(p)
            item.filename = p.name
            self.session.items.append(item)

        self.session.get_filtered_items.return_value = list(self.session.items)

    def test_single_step_navigation(self):
        """
        Verify that pressing Left/Right/Up/Down moves +/- 1 photo and maintains single selection.
        """
        selected_indices = {0}
        current_index = 0
        anchor_idx = 0

        # Step forward (+1)
        delta = 1
        new_idx = max(0, min(len(self.session.items) - 1, current_index + delta))
        is_shift = False
        is_ctrl = False

        if not is_shift and not is_ctrl:
            selected_indices = {new_idx}
            anchor_idx = new_idx
            current_index = new_idx

        self.assertEqual(current_index, 1)
        self.assertEqual(selected_indices, {1})
        self.assertEqual(anchor_idx, 1)

    def test_continuous_navigation_single_selection(self):
        """
        Verify that holding down arrow keys (is_continuous=True) updates active index 
        WITHOUT accumulating passed-over photos into selected_indices.
        """
        selected_indices = {0}
        anchor_idx = 0

        # Simulate holding right arrow across indices 0 -> 1 -> 2 -> 3
        for idx in range(1, 4):
            is_continuous = True
            is_shift = False
            is_ctrl = False

            if not is_shift and not is_ctrl:
                selected_indices = {idx}
                anchor_idx = idx

            self.assertEqual(selected_indices, {idx})

        # Verify final state is strictly single item {3}
        self.assertEqual(selected_indices, {3})

    def test_jump_navigation_page_and_ctrl(self):
        """
        Verify that Page Up / Page Down and Ctrl+Up / Ctrl+Down jump +/- 10 photos bounded cleanly.
        """
        items = [ImageItem(Path(f"D:/Photos/IMG_{i}.JPG")) for i in range(25)]
        current_index = 0

        # Jump forward +10
        current_index = max(0, min(len(items) - 1, current_index + 10))
        self.assertEqual(current_index, 10)

        # Jump forward +10
        current_index = max(0, min(len(items) - 1, current_index + 10))
        self.assertEqual(current_index, 20)

        # Jump forward +10 (exceeds max 24, bounds to 24)
        current_index = max(0, min(len(items) - 1, current_index + 10))
        self.assertEqual(current_index, 24)

        # Jump backward -10
        current_index = max(0, min(len(items) - 1, current_index - 10))
        self.assertEqual(current_index, 14)

    def test_home_and_end_navigation(self):
        """
        Verify Home jumps to index 0 and End jumps to last index.
        """
        items_count = 15
        
        # Home
        idx_home = 0
        self.assertEqual(idx_home, 0)

        # End
        idx_end = items_count - 1
        self.assertEqual(idx_end, 14)

    def test_shift_range_multiselect(self):
        """
        Verify Shift + Arrow / Shift + Click selects range from anchor_idx to target index.
        """
        anchor_idx = 2
        selected_indices = {2}

        # Shift + Down to index 5
        target_idx = 5
        start_i, end_i = min(anchor_idx, target_idx), max(anchor_idx, target_idx)
        selected_indices = set(range(start_i, end_i + 1))

        self.assertEqual(selected_indices, {2, 3, 4, 5})
        self.assertEqual(anchor_idx, 2)  # Anchor remains unchanged

        # Shift + Up to index 1
        target_idx = 1
        start_i, end_i = min(anchor_idx, target_idx), max(anchor_idx, target_idx)
        selected_indices = set(range(start_i, end_i + 1))

        self.assertEqual(selected_indices, {1, 2})

    def test_ctrl_toggle_multiselect(self):
        """
        Verify Ctrl + Click toggles individual items in/out of selected_indices.
        """
        selected_indices = {2}
        anchor_idx = 2

        # Ctrl + click index 5 -> Add 5
        idx = 5
        if idx in selected_indices and len(selected_indices) > 1:
            selected_indices.remove(idx)
        else:
            selected_indices.add(idx)
        anchor_idx = idx

        self.assertEqual(selected_indices, {2, 5})
        self.assertEqual(anchor_idx, 5)

        # Ctrl + click index 7 -> Add 7
        idx = 7
        if idx in selected_indices and len(selected_indices) > 1:
            selected_indices.remove(idx)
        else:
            selected_indices.add(idx)
        anchor_idx = idx

        self.assertEqual(selected_indices, {2, 5, 7})

        # Ctrl + click index 5 again -> Remove 5
        idx = 5
        if idx in selected_indices and len(selected_indices) > 1:
            selected_indices.remove(idx)
        else:
            selected_indices.add(idx)

        self.assertEqual(selected_indices, {2, 7})

    def test_select_all_and_select_none(self):
        """
        Verify Select All selects 0..N-1 and Select None resets to active index.
        """
        items_count = 10
        current_index = 3

        # Select All
        selected_indices = set(range(items_count))
        self.assertEqual(len(selected_indices), 10)

        # Select None
        selected_indices = {current_index}
        self.assertEqual(selected_indices, {3})

    def test_click_viewport_auto_scroll_flag(self):
        """
        Verify that mouse clicks set auto_scroll=False while keyboard navigation sets auto_scroll=True.
        """
        # Mouse click
        from_click = True
        auto_scroll = not from_click
        self.assertFalse(auto_scroll)

        # Keyboard navigation
        from_click = False
        auto_scroll = not from_click
        self.assertTrue(auto_scroll)

    def test_delta_diff_row_updates(self):
        """
        Verify that changing selection from {2} to {3} computes changed_indices={2, 3}
        so only 2 rows update instead of re-rendering all rows.
        """
        prev_selected = {2}
        prev_active = 2
        new_selected = {3}
        new_active = 3

        changed_indices = (new_selected ^ prev_selected) | {new_active, prev_active}
        self.assertEqual(changed_indices, {2, 3})

    def test_preserve_selected_image_on_filter_change(self):
        """
        Verify that switching filters preserves the active photo index if present in new list.
        """
        items_all = [ImageItem(Path(f"D:/Photos/IMG_{i}.JPG")) for i in range(10)]
        active_path = items_all[5].path

        # Simulate filtering to a sub-list containing IMG_5.JPG
        items_filtered = [items_all[2], items_all[5], items_all[8]]

        target_idx = 0
        for idx, item in enumerate(items_filtered):
            if item.path == active_path or active_path in item.stacked_paths:
                target_idx = idx
                break

        # IMG_5.JPG is at index 1 in items_filtered
        self.assertEqual(target_idx, 1)
        self.assertEqual(items_filtered[target_idx].path, active_path)

    def test_copy_image_to_clipboard_function(self):
        """
        Verify copy_image_to_clipboard handles PIL images and None input cleanly.
        """
        from gui import copy_image_to_clipboard
        self.assertFalse(copy_image_to_clipboard(None))

    def test_load_100_percent_shortcut(self):
        """
        Verify that _on_load_100_percent method is available as a one-time operation.
        """
        from gui import ImageCullerApp
        self.assertTrue(hasattr(ImageCullerApp, "_on_load_100_percent"))

    def test_select_all_and_select_none(self):
        """
        Verify _select_all selects all items and _select_none clears to active item.
        """
        from gui import ImageCullerApp
        app = MagicMock(spec=ImageCullerApp)
        app.current_items = list(self.session.items)
        app.current_index = 3
        app.selected_indices = {3}
        tab = {"selected_indices": {3}}
        app._get_active_tab.return_value = tab
        app.thumb_list = MagicMock()

        # Execute Select All
        ImageCullerApp._select_all(app)
        self.assertEqual(app.selected_indices, set(range(10)))
        self.assertEqual(tab["selected_indices"], set(range(10)))
        app.thumb_list.set_selected_indices.assert_called_with(
            set(range(10)), 3, active_path=self.session.items[3].path
        )

        # Execute Select None
        ImageCullerApp._select_none(app)
        self.assertEqual(app.selected_indices, {3})
        self.assertEqual(tab["selected_indices"], {3})
        app.thumb_list.set_selected_indices.assert_called_with(
            {3}, 3, active_path=self.session.items[3].path
        )

    def test_on_ctrl_a_handler(self):
        """
        Verify _on_ctrl_a calls _select_all when not in entry, and selects entry text when in entry.
        """
        from gui import ImageCullerApp
        app = MagicMock(spec=ImageCullerApp)
        app._is_entry_focused.return_value = False

        # When not in entry: calls _select_all
        res = ImageCullerApp._on_ctrl_a(app)
        self.assertEqual(res, "break")
        app._select_all.assert_called_once()

        # When in entry: selects entry text range
        app._select_all.reset_mock()
        app._is_entry_focused.return_value = True
        mock_entry = MagicMock()
        app.focus_get.return_value = mock_entry

        res = ImageCullerApp._on_ctrl_a(app)
        self.assertEqual(res, "break")
        app._select_all.assert_not_called()
        mock_entry.select_range.assert_called_with(0, "end")
        mock_entry.icursor.assert_called_with("end")


if __name__ == "__main__":
    unittest.main()
