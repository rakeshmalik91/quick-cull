import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import importlib.util
from unittest.mock import MagicMock, patch

from culler.culler_engine import CullingSession, FlagState, ImageItem
from gui import ImageCullerApp

culler_cli_path = Path(__file__).resolve().parent.parent / "culler.py"
spec = importlib.util.spec_from_file_location("culler_cli", culler_cli_path)
culler_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(culler_cli)

build_parser = culler_cli.build_parser
cmd_trash_rejected = culler_cli.cmd_trash_rejected


class TestCullingSessionTrashRejected(unittest.TestCase):
    def setUp(self):
        self.session = CullingSession()
        self.item_pick = ImageItem(Path("photo_pick.jpg"))
        self.item_pick.flag = FlagState.PICK
        self.item_reject1 = ImageItem(Path("photo_rej1.jpg"))
        self.item_reject1.flag = FlagState.REJECT
        self.item_reject2 = ImageItem(Path("photo_rej2.arw"))
        self.item_reject2.flag = FlagState.REJECT
        self.item_unflagged = ImageItem(Path("photo_unf.jpg"))
        self.item_unflagged.flag = FlagState.UNFLAGGED
        self.session.items = [self.item_pick, self.item_reject1, self.item_reject2, self.item_unflagged]

    @patch("send2trash.send2trash")
    def test_trash_rejected_items_all_formats(self, mock_send2trash):
        with patch.object(Path, "exists", return_value=True):
            moved_count = self.session.trash_rejected_items()
            self.assertEqual(moved_count, 2)
            self.assertEqual(len(self.session.items), 2)
            self.assertIn(self.item_pick, self.session.items)
            self.assertIn(self.item_unflagged, self.session.items)
            self.assertNotIn(self.item_reject1, self.session.items)
            self.assertNotIn(self.item_reject2, self.session.items)

    @patch("send2trash.send2trash")
    def test_trash_rejected_items_with_format_filter(self, mock_send2trash):
        with patch.object(Path, "exists", return_value=True):
            moved_count = self.session.trash_rejected_items(format_filter="JPG")
            self.assertEqual(moved_count, 1)
            self.assertNotIn(self.item_reject1, self.session.items)
            self.assertIn(self.item_reject2, self.session.items)


class TestCLICommands(unittest.TestCase):
    def test_build_parser_has_trash_rejected(self):
        parser = build_parser()
        args = parser.parse_args(["trash-rejected", "some/folder", "--format", "JPG"])
        self.assertEqual(args.command, "trash-rejected")
        self.assertEqual(args.path, "some/folder")
        self.assertEqual(args.format, "JPG")

    def test_cmd_trash_rejected(self):
        with patch.object(culler_cli, "resolve_input_path", return_value=(Path("dummy"), None)):
            session = MagicMock(spec=CullingSession)
            session.trash_rejected_items.return_value = 5
            args = MagicMock(path="dummy", format=None)
            cmd_trash_rejected(session, args)
            session.scan_directory.assert_called_once_with(Path("dummy"))
            session.trash_rejected_items.assert_called_once_with(format_filter=None)


class TestGUIUnrejectAndUnpick(unittest.TestCase):
    def setUp(self):
        self.mock_session = MagicMock(spec=CullingSession)
        self.item1 = ImageItem(Path("test1.jpg"))
        self.item1.flag = FlagState.REJECT
        self.item2 = ImageItem(Path("test2.jpg"))
        self.item2.flag = FlagState.PICK
        self.item3 = ImageItem(Path("test3.jpg"))
        self.item3.flag = FlagState.UNFLAGGED
        self.items = [self.item1, self.item2, self.item3]

    def test_unreject_single_selected(self):
        app = MagicMock()
        app.current_index = 0
        app.selected_indices = {0}
        app.current_items = list(self.items)
        app._get_active_session.return_value = self.mock_session

        ImageCullerApp._on_unreject_current(app)

        self.assertEqual(self.item1.flag, FlagState.UNFLAGGED)
        self.mock_session.save_item_record.assert_called_with(self.item1)
        app.thumb_list.update_single_item_status.assert_called_with(0, self.item1)

    def test_unreject_leaves_pick_untouched(self):
        app = MagicMock()
        app.current_index = 1
        app.selected_indices = {1}
        app.current_items = list(self.items)
        app._get_active_session.return_value = self.mock_session

        ImageCullerApp._on_unreject_current(app)

        self.assertEqual(self.item2.flag, FlagState.PICK)
        self.mock_session.save_item_record.assert_not_called()

    def test_unpick_single_selected(self):
        app = MagicMock()
        app.current_index = 1
        app.selected_indices = {1}
        app.current_items = list(self.items)
        app._get_active_session.return_value = self.mock_session

        ImageCullerApp._on_unpick_current(app)

        self.assertEqual(self.item2.flag, FlagState.UNFLAGGED)
        self.mock_session.save_item_record.assert_called_with(self.item2)
        app.thumb_list.update_single_item_status.assert_called_with(1, self.item2)

    def test_unpick_leaves_reject_untouched(self):
        app = MagicMock()
        app.current_index = 0
        app.selected_indices = {0}
        app.current_items = list(self.items)
        app._get_active_session.return_value = self.mock_session

        ImageCullerApp._on_unpick_current(app)

        self.assertEqual(self.item1.flag, FlagState.REJECT)
        self.mock_session.save_item_record.assert_not_called()

    def test_unreject_multi_select(self):
        app = MagicMock()
        app.current_index = 0
        app.selected_indices = {0, 1, 2}
        app.current_items = list(self.items)
        app._get_active_session.return_value = self.mock_session

        ImageCullerApp._on_unreject_current(app)

        self.assertEqual(self.item1.flag, FlagState.UNFLAGGED)
        self.assertEqual(self.item2.flag, FlagState.PICK)
        self.assertEqual(self.item3.flag, FlagState.UNFLAGGED)


class TestAutoFilterSwitch(unittest.TestCase):
    def test_operation_emptying_filter_switches_to_all(self):
        app = MagicMock()
        session = MagicMock(spec=CullingSession)
        remaining_item = ImageItem(Path("stay.jpg"))
        session.items = [remaining_item]

        def mock_filtered(flag_filter="All", **kwargs):
            if flag_filter == "Reject":
                return []
            return [remaining_item]

        session.get_filtered_items.side_effect = mock_filtered

        tab = {
            "session": session,
            "filter_values": {"flag": "Reject", "rating": [], "format": "All", "tag": []},
            "current_items": [],
            "pending_target_image": None,
        }
        app._get_active_tab.return_value = tab
        app._get_active_session.return_value = session
        app.toolbar.get_filter_values.return_value = {
            "flag": "Reject", "rating": [], "format": "All", "tag": []
        }
        app.toolbar.seg_filter.get.return_value = "Reject"
        app.toolbar.get_white_balance.return_value = "camera"
        app.current_items = []
        app.current_index = 0

        ImageCullerApp._on_filter_changed(app, trigger_source="delete")

        app.toolbar.seg_filter.set.assert_called_with("All")
        self.assertEqual(tab["filter_values"]["flag"], "All")
        self.assertEqual(app.current_items, [remaining_item])

    def test_user_filter_click_does_not_auto_switch(self):
        app = MagicMock()
        session = MagicMock(spec=CullingSession)
        remaining_item = ImageItem(Path("stay.jpg"))
        session.items = [remaining_item]
        session.get_filtered_items.return_value = []

        tab = {
            "session": session,
            "filter_values": {"flag": "Reject", "rating": [], "format": "All", "tag": []},
            "current_items": [],
            "pending_target_image": None,
        }
        app._get_active_tab.return_value = tab
        app._get_active_session.return_value = session
        app.toolbar.get_filter_values.return_value = {
            "flag": "Reject", "rating": [], "format": "All", "tag": []
        }
        app.toolbar.seg_filter.get.return_value = "Reject"
        app.toolbar.get_white_balance.return_value = "camera"
        app.current_items = []
        app.current_index = -1

        ImageCullerApp._on_filter_changed(app, trigger_source="filter")

        app.toolbar.seg_filter.set.assert_not_called()
        self.assertEqual(app.current_items, [])


class TestDeleteAllRejectedGUI(unittest.TestCase):
    @patch("tkinter.messagebox.showinfo")
    def test_delete_all_rejected_empty(self, mock_showinfo):
        app = MagicMock()
        session = MagicMock(spec=CullingSession)
        session.directory = Path("dummy_dir")
        session.items = [ImageItem(Path("p.jpg"))]
        app._get_active_tab.return_value = {"session": session}
        app._get_active_session.return_value = session

        ImageCullerApp._on_delete_all_rejected_to_trash(app)

        mock_showinfo.assert_called_once()
        app._confirm_and_delete_files.assert_not_called()

    @patch("tkinter.messagebox.askyesno", return_value=True)
    def test_delete_all_rejected_invokes_confirm(self, mock_askyesno):
        app = MagicMock()
        session = MagicMock(spec=CullingSession)
        session.directory = Path("dummy_dir")
        rej_item = ImageItem(Path("rej.jpg"))
        rej_item.flag = FlagState.REJECT
        session.items = [rej_item]
        app._get_active_tab.return_value = {"session": session}
        app._get_active_session.return_value = session
        app.toolbar.get_format_filter.return_value = "All Formats"

        ImageCullerApp._on_delete_all_rejected_to_trash(app)

        app._confirm_and_delete_files.assert_called_once_with(
            [rej_item], [rej_item.path], is_batch_rejected=True
        )


class TestMetadataPanelTrashButton(unittest.TestCase):
    def test_trash_rejected_button_exists_and_calls_callback(self):
        import customtkinter as ctk
        from culler.gui.metadata_panel import MetadataPanel

        root = ctk.CTk()
        try:
            on_trash_cb = MagicMock()
            panel = MetadataPanel(
                root,
                on_set_flag=MagicMock(),
                on_set_rating=MagicMock(),
                on_toggle_tag=MagicMock(),
                on_trash_rejected=on_trash_cb
            )
            self.assertTrue(hasattr(panel, "btn_trash_rejected"))
            self.assertIn("Move Rejected to Trash", panel.btn_trash_rejected.cget("text"))

            # Invoke button command
            panel.btn_trash_rejected.invoke()
            on_trash_cb.assert_called_once()
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
