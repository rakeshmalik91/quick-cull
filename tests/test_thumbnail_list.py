import sys
import unittest
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk
from PIL import Image

from culler.gui.thumbnail_list import ThumbnailList
from culler.culler_engine import ImageItem, FlagState
from culler.image_loader import ImageLoader


class TestThumbnailList(unittest.TestCase):
    """
    Automated Unit Test Suite for ThumbnailList placeholder generation, 
    non-blocking batch loading, progress bar, and soft item refresh.
    """

    def setUp(self):
        try:
            ctk.set_appearance_mode("dark")
            ctk.set_default_color_theme("blue")
            self.root = ctk.CTk()
            self.root.withdraw()
        except Exception as e:
            if "TclError" in type(e).__name__ or "tk" in str(e).lower() or "sizegrip" in str(e).lower():
                self.skipTest(f"Skipping GUI test due to Tcl/Tk environment restriction: {e}")
            else:
                raise e

        self.loader = ImageLoader()
        try:
            self.list_widget = ThumbnailList(
                self.root,
                on_select_image=MagicMock(),
                image_loader=self.loader
            )
            self.list_widget.pack(fill="both", expand=True)
            self.list_widget.update()
        except Exception as e:
            if "TclError" in type(e).__name__ or "tk" in str(e).lower() or "sizegrip" in str(e).lower():
                self.skipTest(f"Skipping GUI widget test due to Tcl/Tk environment restriction: {e}")
            else:
                raise e

    def tearDown(self):
        try:
            self.list_widget.shutdown()
        except Exception:
            pass
        try:
            self.list_widget.destroy()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _make_item(self, name="TEST.JPG", is_stacked=False, flag=FlagState.UNFLAGGED, rating=0):
        p = Path(f"D:/Photos/{name}")
        item = ImageItem(p)
        item.filename = p.name
        item.flag = flag
        item.rating = rating
        if is_stacked:
            p2 = Path(f"D:/Photos/{name.replace('.JPG', '_ARW.ARW')}")
            item.stacked_paths = [p, p2]
            item.is_stacked = True
        return item

    def test_stack_change_on_same_primary_path_rebuilds_rows(self):
        """
        A rescan can keep the same primary path while the stack changes (the JPG of
        an ARW+JPG pair is deleted, or a RAW appears next to a lone JPG). The rows
        must be rebuilt so the stacked filename, height and extra thumbnails update.
        """
        stacked = self._make_item("ALPHA.ARW", is_stacked=True)
        stacked.filename = "ALPHA [Stacked: 1 ARW, 1 JPG]"
        self.list_widget.update_items([stacked], selected_idx=0)
        self.list_widget.update()
        self.assertTrue(stacked.is_stacked)

        unstacked = self._make_item("ALPHA.ARW")
        self.list_widget.update_items([unstacked], selected_idx=0)
        self.list_widget.update()

        self.assertEqual(len(self.list_widget._row_frame_map), 1)
        self.assertEqual(
            self.list_widget._row_frame_map[0].cget("height"),
            96,
            "row must fall back to the single-image height once the stack is gone",
        )

    def test_stack_growth_on_same_primary_path_rebuilds_rows(self):
        """
        Adding the RAW next to a lone JPG keeps the primary path, but must still
        rebuild the row so it becomes a stacked row.
        """
        lone = self._make_item("BRAVO.JPG")
        self.list_widget.update_items([lone], selected_idx=0)
        self.list_widget.update()
        self.assertEqual(self.list_widget._row_frame_map[0].cget("height"), 96)

        stacked = self._make_item("BRAVO.JPG", is_stacked=True)
        stacked.path = Path("D:/Photos/BRAVO.ARW")
        stacked.filename = "BRAVO [Stacked: 1 ARW, 1 JPG]"
        self.list_widget.update_items([stacked], selected_idx=0)
        self.list_widget.update()

        self.assertEqual(len(self.list_widget._row_frame_map), 1)
        self.assertEqual(self.list_widget._row_frame_map[0].cget("height"), 190)

    def test_row_signature_detects_stack_changes_only(self):
        """
        Verify the soft-refresh signature changes with stack composition, not with
        flag/rating edits that the status refresh already handles.
        """
        item = self._make_item("ALPHA.ARW")
        base = ThumbnailList._row_signature([item])

        self.assertEqual(base, ThumbnailList._row_signature([self._make_item("ALPHA.ARW")]))

        flagged = self._make_item("ALPHA.ARW", flag=FlagState.PICK, rating=3)
        self.assertEqual(base, ThumbnailList._row_signature([flagged]),
                         "flag/rating changes must not force a rebuild")

        stacked = self._make_item("ALPHA.ARW", is_stacked=True)
        self.assertNotEqual(base, ThumbnailList._row_signature([stacked]))

    def test_create_placeholder_image(self):
        """
        Verify _create_placeholder_image returns a correctly sized RGB image.
        """
        img = self.list_widget._create_placeholder_image((70, 70))
        self.assertIsInstance(img, Image.Image)
        self.assertEqual(img.size, (70, 70))
        self.assertEqual(img.mode, "RGB")

    def test_update_items_empty_list(self):
        """
        Verify update_items with empty list clears widgets and shows no progress.
        """
        self.list_widget.update_items([], selected_idx=0)
        self.list_widget.update()
        self.assertEqual(len(self.list_widget._row_frame_map), 0)
        self.assertEqual(self.list_widget.progress_bar.get(), 0.0)

    def test_update_items_starts_batch_load(self):
        """
        Verify update_items with items stores pending state and starts batch processing.
        """
        items = [self._make_item(f"IMG_{i:03d}.JPG") for i in range(5)]
        self.list_widget.update_items(items, selected_idx=0)
        self.assertEqual(len(self.list_widget._pending_items), 5)
        self.assertEqual(self.list_widget._pending_selected_idx, 0)

    def test_progress_bar_initialized_on_update(self):
        """
        Verify progress bar resets to 0.0 on update_items call.
        """
        items = [self._make_item(f"IMG_{i:03d}.JPG") for i in range(3)]
        self.list_widget.update_items(items, selected_idx=0)
        self.assertEqual(self.list_widget.progress_bar.get(), 0.0)

    def test_update_btn_image_tracks_progress(self):
        """
        Verify _update_btn_image increments loaded count and updates progress UI.
        """
        self.list_widget._total_thumbs = 2
        self.list_widget._loaded_thumbs = 0

        pil_thumb = Image.new("RGB", (80, 80), color="red")
        self.list_widget._current_load_id = 1
        self.list_widget._update_btn_image("D:/Photos/TEST.JPG", pil_thumb, load_id=1)

        self.assertEqual(self.list_widget._loaded_thumbs, 1)
        self.assertEqual(self.list_widget.progress_bar.get(), 0.5)

    def test_progress_stats_persist_after_completion(self):
        """
        Verify progress bar and count stay visible at completion instead of hiding.
        """
        self.list_widget._total_thumbs = 1
        self.list_widget._loaded_thumbs = 1
        self.list_widget._pending_items = []
        self.list_widget._batch_index = 0

        self.list_widget._update_progress_ui()
        self.assertEqual(self.list_widget.progress_bar.get(), 1.0)
        self.assertEqual(self.list_widget.lbl_progress_text.cget("text"), "1 / 1")

    def test_batch_cancel_on_new_update(self):
        """
        Verify that calling update_items with different paths cancels any pending batch after callbacks.
        """
        items1 = [self._make_item(f"IMG_{i:03d}.JPG") for i in range(3)]
        self.list_widget.update_items(items1, selected_idx=0)
        self.list_widget._batch_after_id = "after_id_123"
        items2 = [self._make_item(f"OTHER_{i:03d}.JPG") for i in range(3)]
        self.list_widget.update_items(items2, selected_idx=0)
        self.assertIsNone(self.list_widget._batch_after_id)

    def test_soft_update_skips_rebuild_when_paths_match(self):
        """
        Verify update_items does a soft refresh (no widget destroy/recreate) when paths match.
        """
        items = [self._make_item(f"IMG_{i:03d}.JPG") for i in range(5)]
        self.list_widget.update_items(items, selected_idx=0)
        self.assertEqual(len(self.list_widget._row_frame_map), 5)

        row_count_before = len(self.list_widget._row_frame_map)
        self.list_widget.update_items(items, selected_idx=0)
        self.assertEqual(len(self.list_widget._row_frame_map), row_count_before)

    def test_soft_update_submits_thumbnails(self):
        """
        Verify soft update submits thumbnail loads for items not yet cached.
        """
        items = [self._make_item(f"IMG_{i:03d}.JPG") for i in range(3)]
        self.list_widget.update_items(items, selected_idx=0)
        self.list_widget._total_thumbs = 0
        self.list_widget._loaded_thumbs = 0
        self.list_widget._batch_raw_requests.clear()
        self.list_widget._batch_other_requests.clear()
        self.list_widget.update_items(items, selected_idx=1)
        self.assertGreater(self.list_widget._total_thumbs, 0)

    def test_placeholder_ctk_images_created(self):
        """
        Verify placeholder CTkImages are created for all sizes.
        """
        items = [self._make_item(f"IMG_{i:03d}.JPG") for i in range(2)]
        try:
            self.list_widget.update_items(items, selected_idx=0)
            self.assertTrue(hasattr(self.list_widget, "_placeholder_ctk_70"))
            self.assertTrue(hasattr(self.list_widget, "_placeholder_ctk_80"))
            self.assertTrue(hasattr(self.list_widget, "_placeholder_ctk_90"))
        except Exception as e:
            if "TclError" in type(e).__name__ or "tk.tcl" in str(e).lower():
                self.skipTest(f"Skipping CTkImage test due to headless environment Tcl restriction: {e}")
            else:
                raise e

    def test_format_elapsed(self):
        """
        Verify elapsed durations render as seconds, mm:ss, or h:mm:ss.
        """
        self.assertEqual(ThumbnailList._format_elapsed(0.0), "0.0s")
        self.assertEqual(ThumbnailList._format_elapsed(12.34), "12.3s")
        self.assertEqual(ThumbnailList._format_elapsed(59.99), "60.0s")
        self.assertEqual(ThumbnailList._format_elapsed(75.0), "1:15")
        self.assertEqual(ThumbnailList._format_elapsed(3725.0), "1:02:05")

    def test_load_timing_starts_hidden_until_folder_load(self):
        """
        Verify the timing label stays empty and the thumbnail timer idle until a folder load starts.
        """
        self.assertEqual(self.list_widget.lbl_load_timing.cget("text"), "")

        items = [self._make_item(f"IMG_{i:03d}.JPG") for i in range(3)]
        self.list_widget.update_items(items, selected_idx=0)
        self.assertIsNone(self.list_widget._thumb_time_start)
        self.assertEqual(self.list_widget.lbl_load_timing.cget("text"), "")

    def test_load_timing_shows_folder_and_thumb_durations(self):
        """
        Verify folder and thumbnail load times are both shown above the progress bar.
        """
        started = time.monotonic() - 3.0
        self.list_widget.start_load_timing(started)
        self.assertIn("Folder:", self.list_widget.lbl_load_timing.cget("text"))

        self.list_widget.start_thumb_timing()
        self.assertIn("Thumbs:", self.list_widget.lbl_load_timing.cget("text"))

        self.list_widget.finish_folder_timing()
        self.assertAlmostEqual(self.list_widget._folder_time_final, 3.0, delta=0.5)

        before = self.list_widget.lbl_load_timing.cget("text")
        self.list_widget.finish_thumb_timing()
        self.assertIsNotNone(self.list_widget._thumb_time_final)
        time.sleep(0.05)
        self.list_widget._update_load_timing()
        self.assertEqual(self.list_widget.lbl_load_timing.cget("text"), before)

    def test_load_timing_stays_visible_after_completion(self):
        """
        Verify totals stay on screen after the thumbnails finish instead of being cleared.
        """
        self.list_widget.start_load_timing(time.monotonic() - 1.0)
        self.list_widget.finish_folder_timing()
        self.list_widget.start_thumb_timing()
        self.list_widget._total_thumbs = 1
        self.list_widget._loaded_thumbs = 1
        self.list_widget._pending_items = []
        self.list_widget._batch_index = 0

        self.list_widget._update_progress_ui()
        text = self.list_widget.lbl_load_timing.cget("text")
        self.assertIn("Folder:", text)
        self.assertIn("Thumbs:", text)
        self.assertEqual(self.list_widget.progress_bar.get(), 1.0)
        self.assertEqual(self.list_widget.lbl_progress_text.cget("text"), "1 / 1")

        time.sleep(0.05)
        self.list_widget._update_load_timing()
        self.assertEqual(self.list_widget.lbl_load_timing.cget("text"), text)
        self.assertIsNone(self.list_widget._timing_after_id)

    def test_finished_timing_survives_filter_refresh(self):
        """
        Verify a later filter refresh keeps the completed load's totals.
        """
        self.list_widget.start_load_timing(time.monotonic() - 1.0)
        self.list_widget.finish_folder_timing()
        self.list_widget.start_thumb_timing()
        self.list_widget._total_thumbs = 1
        self.list_widget._loaded_thumbs = 1
        self.list_widget._pending_items = []
        self.list_widget._batch_index = 0
        self.list_widget._update_progress_ui()
        text = self.list_widget.lbl_load_timing.cget("text")
        thumb_final = self.list_widget._thumb_time_final

        self.list_widget.update_items([self._make_item("FILTERED.JPG")], selected_idx=0)
        self.assertEqual(self.list_widget.lbl_load_timing.cget("text"), text)
        self.assertEqual(self.list_widget._thumb_time_final, thumb_final)

    def test_finish_load_timing_freezes_folder_and_thumb(self):
        """
        Verify finish_load_timing freezes both timers for a load that ended early.
        """
        self.list_widget.start_load_timing(time.monotonic() - 2.0)
        self.list_widget.start_thumb_timing()
        self.list_widget.finish_load_timing()

        self.assertIsNotNone(self.list_widget._folder_time_final)
        self.assertAlmostEqual(self.list_widget._folder_time_final, 2.0, delta=0.5)
        self.assertIsNotNone(self.list_widget._thumb_time_final)
        self.assertIsNone(self.list_widget._timing_after_id)
        self.assertIn("Folder:", self.list_widget.lbl_load_timing.cget("text"))

    def test_show_load_stats_restores_saved_totals(self):
        """
        Verify a saved tab's totals are displayed without restarting any timer.
        """
        self.list_widget.show_load_stats(4.5, 1.25)
        text = self.list_widget.lbl_load_timing.cget("text")
        self.assertEqual(text, "Folder: 4.5s   |   Thumbs: 1.2s")
        self.assertIsNone(self.list_widget._timing_after_id)
        self.assertFalse(self.list_widget._load_cycle_active)
        self.assertFalse(self.list_widget._folder_scan_active)

        time.sleep(0.05)
        self.list_widget._update_load_timing()
        self.assertEqual(self.list_widget.lbl_load_timing.cget("text"), text)

    def test_show_load_stats_clears_when_empty(self):
        """
        Verify restoring a tab without stats clears the label.
        """
        self.list_widget.show_load_stats(4.5, 1.25)
        self.list_widget.show_load_stats(None, None)
        self.assertEqual(self.list_widget.lbl_load_timing.cget("text"), "")

    def test_begin_thumb_timing_survives_cached_rerender(self):
        """
        Verify re-rendering a restored tab's thumbnails keeps its saved folder total.
        """
        self.list_widget.show_load_stats(4.5, 1.25)
        self.list_widget.begin_thumb_timing()
        self.assertIsNotNone(self.list_widget._thumb_time_start)
        self.assertIsNone(self.list_widget._thumb_time_final)

        self.list_widget.freeze_load_timing()
        self.assertIsNotNone(self.list_widget._thumb_time_final)
        self.assertEqual(self.list_widget._folder_time_final, 4.5)
        self.assertIn("Folder: 4.5s", self.list_widget.lbl_load_timing.cget("text"))

    def test_load_stats_emitted_on_freeze(self):
        """
        Verify frozen totals are reported so the active tab can persist them.
        """
        emitted = []
        self.list_widget.on_load_stats_changed = lambda stats: emitted.append(stats)

        self.list_widget.start_load_timing(time.monotonic() - 2.0)
        self.list_widget.finish_folder_timing()
        self.assertEqual(len(emitted), 2)
        self.assertAlmostEqual(emitted[-1]["folder"], 2.0, delta=0.5)
        self.assertIsNone(emitted[-1]["thumb"])

        self.list_widget.start_thumb_timing()
        self.list_widget.freeze_load_timing()
        self.assertIsNotNone(emitted[-1]["thumb"])

    def test_show_load_stats_does_not_emit(self):
        """
        Verify restoring a tab does not write back over the stats it just restored.
        """
        emitted = []
        self.list_widget.on_load_stats_changed = lambda stats: emitted.append(stats)
        self.list_widget.show_load_stats(4.5, 1.25)
        self.assertEqual(emitted, [])

    def test_load_timing_frozen_for_empty_item_list(self):
        """
        Verify an empty folder result freezes the timers and shows a 0 / 0 count.
        """
        self.list_widget.start_load_timing(time.monotonic() - 1.0)
        self.list_widget.update_items([], selected_idx=0)
        self.assertEqual(self.list_widget.lbl_progress_text.cget("text"), "0 / 0")
        self.assertIn("Folder:", self.list_widget.lbl_load_timing.cget("text"))
        self.assertIsNotNone(self.list_widget._folder_time_final)
        self.assertIsNone(self.list_widget._timing_after_id)


if __name__ == "__main__":
    unittest.main()
