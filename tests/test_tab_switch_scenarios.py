import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk
from PIL import Image

from culler.culler_engine import FlagState
from culler.gui.thumbnail_list import ThumbnailList


def _make_item(name, root="D:/Photos", is_stacked=False, flag="UNFLAGGED", rating=0):
    from culler.culler_engine import ImageItem

    path = Path(root) / name
    item = ImageItem.__new__(ImageItem)
    item.path = path
    item.stacked_paths = [path]
    item.is_stacked = is_stacked
    item.filename = name
    item.format_name = "JPEG Image"
    item.flag = flag
    item.rating = rating
    item.sharpness_score = 0.0
    item.detection_box = None
    item.eye_box = None
    return item


class TestTabSwitchScenarios(unittest.TestCase):
    """
    Switching tabs while a tab is loading must never raise, stall the batch chain, or
    queue duplicate work. Each test maps to a scenario reported from the running app.
    """

    @classmethod
    def setUpClass(cls):
        cls.root = ctk.CTk()
        cls.root.geometry("900x700+30+30")
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        self.loader = MagicMock()
        self.thumb = Image.new("RGB", (80, 80), "blue")
        self.loader.get_thumbnail.return_value = self.thumb
        self.list_widget = ThumbnailList(self.root, on_select_image=MagicMock(),
                                        image_loader=self.loader)
        self.list_widget.pack(side="left", fill="both", expand=True)
        self.root.update()
        self.addCleanup(self._teardown)

    def _teardown(self):
        try:
            self.list_widget.destroy()
        except Exception:
            pass
        self.root.update()

    def _drain_build(self, timeout_s=10.0):
        """Pump the event loop until the row-batch chain finishes."""
        import time as _time

        deadline = _time.monotonic() + timeout_s
        while self.list_widget._batch_after_id is not None and _time.monotonic() < deadline:
            self.root.update()
        self.root.update()

    def test_soft_refresh_repeatedly_does_not_raise(self):
        """
        Scenario: refresh / filter applied repeatedly to the same tab.

        This is the AttributeError regression: the soft-refresh path touched
        _batch_raw_requests, which existed only as a local variable, so every soft
        refresh raised inside a Tk callback and left the batch chain dead.
        """
        items = [_make_item(f"IMG_{i:03d}.JPG") for i in range(12)]
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()

        for idx in range(5):
            self.list_widget.update_items(items, selected_idx=idx)
            self.root.update()

        self.assertEqual(len(self.list_widget._row_frame_map), len(items))

    def test_switch_away_and_back_mid_load_keeps_chain_alive(self):
        """Scenario: switch away and back while thumbnails are still decoding."""
        tab_a = [_make_item(f"A_{i:03d}.JPG") for i in range(10)]
        tab_b = [_make_item(f"B_{i:03d}.JPG") for i in range(10)]

        self.list_widget.update_items(tab_a, selected_idx=0)
        self.root.update()
        self.list_widget.update_items(tab_b, selected_idx=0)
        self.root.update()
        self.list_widget.update_items(tab_a, selected_idx=0)
        self.root.update()

        self.assertEqual(self.list_widget._current_item_signature,
                         ThumbnailList._row_signature(tab_a))
        self.assertEqual(len(self.list_widget._row_frame_map), len(tab_a))

    def test_stale_results_from_an_older_load_are_dropped(self):
        """Scenario: a decode finishes after the user switched to another tab."""
        self.list_widget.update_items([_make_item("OLD.JPG")], selected_idx=0)
        self.root.update()
        old_load_id = self.list_widget._current_load_id

        self.list_widget.update_items([_make_item("NEW.JPG")], selected_idx=0)
        self.root.update()

        self.list_widget._queue_thumb_result("D:/Photos/OLD.JPG", self.thumb, old_load_id)
        self.list_widget._drain_thumb_results()

        self.assertNotIn("D:/Photos/OLD.JPG", self.list_widget._ctk_img_cache,
                         "a thumbnail from a superseded load must not be applied")

    def test_new_load_cancels_inflight_requests(self):
        """Scenario: switching tabs abandons the previous tab's pending decodes."""
        self.list_widget._load_single_thumb_async = MagicMock()
        items = [_make_item(f"A_{i:03d}.JPG") for i in range(6)]
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()
        self.list_widget._inflight_thumbs["D:/Photos/A_000.JPG"] = self.list_widget._current_load_id

        self.list_widget.update_items([_make_item("B_000.JPG")], selected_idx=0)

        self.assertEqual(self.list_widget._inflight_thumbs, {},
                         "outstanding requests for the previous tab must be dropped")

    def test_inflight_path_is_not_submitted_twice(self):
        """Scenario: repeated refreshes while a decode is running re-queue the same work."""
        # No thumbnail is ever produced, so every request stays in flight and any
        # repeat submission is pure duplicated work.
        self.loader.get_thumbnail.return_value = None
        submitted = []
        real_submit = self.list_widget._load_single_thumb_async

        def counting_submit(path, size, wb, load_id):
            submitted.append(str(path))
            return real_submit(path, size, wb, load_id)

        self.list_widget._load_single_thumb_async = counting_submit

        items = [_make_item(f"IMG_{i:03d}.JPG") for i in range(8)]
        self.list_widget.update_items(items, selected_idx=0)
        self._drain_build()
        first = len(submitted)
        self.assertGreater(first, 0, "the first load must submit work")

        self.list_widget.update_items(items, selected_idx=0)
        self.list_widget.update_items(items, selected_idx=1)

        self.assertEqual(len(submitted), first,
                         "a decode already in flight must not be queued again")
        self.assertEqual(len(set(submitted)), first, "no duplicate paths submitted")

    def test_thumbnail_results_are_coalesced_into_one_drain(self):
        """
        One UI tick must apply a whole batch of finished thumbnails, not one Tk task
        per photo.
        """
        scheduled = []
        self.list_widget.after = lambda ms, func=None, *a: scheduled.append(func) or "after#x"

        self.list_widget.update_items([_make_item("A.JPG")], selected_idx=0)
        for i in range(20):
            self.list_widget._queue_thumb_result(f"D:/Photos/{i}.JPG", self.thumb, 1)

        self.assertEqual(len(scheduled), 1,
                         "20 finished thumbnails must schedule a single drain")

    def test_drain_applies_every_queued_result(self):
        self.list_widget.update_items([_make_item(f"S_{i:03d}.JPG") for i in range(5)], selected_idx=0)
        self.root.update()
        load_id = self.list_widget._current_load_id

        self.list_widget._ctk_img_cache.clear()
        for i in range(5):
            self.list_widget._queue_thumb_result(f"D:/Photos/S_{i:03d}.JPG", self.thumb, load_id)
        self.list_widget._drain_thumb_results()

        self.assertEqual(len(self.list_widget._ctk_img_cache), 5)
        self.assertEqual(self.list_widget._thumb_result_queue, deque_len_zero())

    def test_row_build_respects_time_budget(self):
        """
        Scenario: switching to a 200-photo tab must not build every row in one go.
        """
        items = [_make_item(f"BULK_{i:03d}.JPG") for i in range(80)]
        original = self.list_widget.ROW_BUILD_BUDGET_MS
        self.list_widget.ROW_BUILD_BUDGET_MS = 0
        try:
            self.list_widget.update_items(items, selected_idx=0)
        finally:
            self.list_widget.ROW_BUILD_BUDGET_MS = original

        self.assertLess(len(self.list_widget._row_frame_map), len(items))
        self.assertIsNotNone(self.list_widget._batch_after_id)

    def test_status_refresh_skips_unchanged_rows(self):
        """A no-op refresh must not re-configure every row's widgets."""
        items = [_make_item(f"IMG_{i:03d}.JPG") for i in range(30)]
        self.list_widget.update_items(items, selected_idx=0)
        self._drain_build()

        for idx, item in enumerate(items):
            self.list_widget.update_single_item_status(idx, item)
        self.assertEqual(len(self.list_widget._row_render_cache), len(items))

        touched = []

        for idx, item in enumerate(items):
            widget = self.list_widget._label_map.get(idx)
            if widget is None:
                continue
            original = widget.configure

            def spy(*args, _o=original, _i=idx, **kwargs):
                touched.append(_i)
                return _o(*args, **kwargs)

            widget.configure = spy
            try:
                self.list_widget.update_single_item_status(idx, item)
            finally:
                widget.configure = original

        self.assertEqual(touched, [], "unchanged rows must not re-configure their widgets")

    def test_status_refresh_applies_changed_flag(self):
        items = [_make_item(f"IMG_{i:03d}.JPG") for i in range(5)]
        self.list_widget.update_items(items, selected_idx=0)
        self._drain_build()

        items[2].flag = FlagState.PICK
        self.list_widget.update_single_item_status(2, items[2])

        cached = self.list_widget._row_render_cache[2]
        self.assertEqual(cached[0], "#2b9348")

    def test_destroy_cancels_pending_timers(self):
        self.list_widget.update_items([_make_item(f"D_{i:03d}.JPG") for i in range(40)], selected_idx=0)
        self.root.update()
        self.list_widget.destroy()
        self.root.update()


def deque_len_zero():
    from collections import deque
    return deque()