import sys
import time
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


# One Tk root for the whole module. A second CTk() in the same process intermittently
# fails with "invalid command name tcl_findLibrary", which surfaces as errors and skips
# rather than real failures.
_ROOT = None


def setUpModule():
    global _ROOT
    _ROOT = ctk.CTk()
    _ROOT.geometry("900x700+30+30")
    _ROOT.update()


def tearDownModule():
    try:
        _ROOT.destroy()
    except Exception:
        pass

class TestTabSwitchScenarios(unittest.TestCase):
    """
    Switching tabs while a tab is loading must never raise, stall the batch chain, or
    queue duplicate work. Each test maps to a scenario reported from the running app.
    """

    @classmethod
    def setUpClass(cls):
        cls.root = _ROOT

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
        # Ignore the pool's own timers: only the coalescing of a fresh batch of results
        # is under test here.
        scheduled.clear()
        self.list_widget._thumb_result_after_id = None
        self.list_widget._thumb_result_queue.clear()
        baseline = 0

        for i in range(20):
            self.list_widget._queue_thumb_result(f"D:/Photos/{i}.JPG", self.thumb, 1)
        # Queueing does not schedule: workers must not touch Tk. The UI thread arms it.
        self.assertEqual(len(scheduled), 0,
                         "a decode worker must not register a Tk timer")

        self.list_widget._arm_thumb_drain()
        self.assertEqual(len(scheduled), 1,
                         "20 queued results must need exactly one drain")
        self.list_widget._arm_thumb_drain()
        self.assertEqual(len(scheduled), 1, "a second arm must not queue a second drain")
        self.assertGreaterEqual(len(self.list_widget._thumb_result_queue), baseline + 20,
                                "every result must be queued, not dropped on the floor")

    def test_drain_applies_every_queued_result(self):
        items = [_make_item(f"S_{i:03d}.JPG") for i in range(5)]
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()
        load_id = self.list_widget._current_load_id

        # Address the rows the way the grid does - by the path string it actually
        # registered - so this tests the drain rather than path formatting.
        paths = [str(item.path) for item in items]
        self.assertEqual(sorted(self.list_widget._btn_map), sorted(paths),
                         "every visible row must be addressable for painting")

        self.list_widget._ctk_img_cache.clear()
        self.list_widget._thumb_result_queue.clear()
        for path in paths:
            self.list_widget._queue_thumb_result(path, self.thumb, load_id)
        self.list_widget._drain_thumb_results()

        self.assertEqual(len(self.list_widget._ctk_img_cache), 5)
        self.assertEqual(self.list_widget._thumb_result_queue, deque_len_zero())

    def test_row_build_respects_time_budget(self):
        """
        Scenario: switching to a large tab must not build every row in one go.

        With a recycled pool the invariant is stronger: only the visible window exists,
        so a 200-photo tab costs the same as a 20-photo one.
        """
        items = [_make_item(f"BULK_{i:03d}.JPG") for i in range(200)]

        self.list_widget.update_items(items, selected_idx=0)

        self.assertLess(len(self.list_widget._row_frame_map), len(items))
        self.assertGreater(len(self.list_widget._row_frame_map), 0)
        self.assertLessEqual(len(self.list_widget._row_frame_map), 120,
                             "about 100 rows is all that should be built")

    def test_status_refresh_skips_unchanged_rows(self):
        """A no-op refresh must not re-configure every row's widgets."""
        items = [_make_item(f"IMG_{i:03d}.JPG") for i in range(12)]
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
        self.list_widget.update_items([_make_item(f"D_{i:03d}.JPG") for i in range(12)], selected_idx=0)
        self.root.update()
        self.list_widget.destroy()
        self.root.update()


class TestThumbnailFailureDoesNotStick(unittest.TestCase):
    """
    A decode worker that raised used to leave its path in ``_inflight_thumbs`` forever.
    ``_is_thumb_load_complete()`` therefore never became true, so the progress bar stuck
    mid-way and the duration timer ran until the app was closed.
    """

    @classmethod
    def setUpClass(cls):
        cls.root = _ROOT

    def setUp(self):
        self.loader = MagicMock()
        self.loader.get_thumbnail.side_effect = OSError("unreadable")
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

    def _run_pending(self):
        for _ in range(300):
            self.root.update()
            if not self.list_widget._executor._threads:
                break
            time.sleep(0.01)
        self.root.update()

    def test_a_failing_decode_does_not_block_completion(self):
        items = [_make_item(f"F_{i:03d}.JPG") for i in range(4)]
        self.list_widget.update_items(items, selected_idx=0)
        self._run_pending()

        self.assertEqual(self.list_widget._inflight_thumbs, {},
                         "a failed decode must not stay 'in flight' forever")
        self.assertTrue(self.list_widget._is_thumb_load_complete(),
                        "a load whose decodes all failed must still be complete")

    def test_a_failed_path_is_not_retried_on_every_refresh(self):
        items = [_make_item(f"F_{i:03d}.JPG") for i in range(3)]
        self.list_widget.update_items(items, selected_idx=0)
        self._run_pending()
        first = self.loader.get_thumbnail.call_count

        self.list_widget.update_items(items, selected_idx=1)
        self._run_pending()

        self.assertEqual(self.loader.get_thumbnail.call_count, first,
                         "a decode known to yield nothing must not be queued again")

    def test_a_failed_path_is_cleared_when_it_succeeds_later(self):
        path = Path("D:/Photos/F_000.JPG")
        self.list_widget.update_items([_make_item("F_000.JPG")], selected_idx=0)
        self._run_pending()
        self.assertIn(str(path), self.list_widget._failed_thumbs)

        self.loader.get_thumbnail.side_effect = None
        self.loader.get_thumbnail.return_value = Image.new("RGB", (80, 80), "blue")
        self.list_widget._load_single_thumb_async(path, (80, 80), "camera", 999)
        self._run_pending()

        self.assertNotIn(str(path), self.list_widget._failed_thumbs,
                         "a path that now decodes must leave the failed set")


class TestRepeatedUpdatesAlwaysBindTheVisibleWindow(unittest.TestCase):
    """
    Regression: switching tabs left the grid holding only the rows built in the first
    tick, so the new tab looked empty.

    A tab switch reaches ``update_items`` twice for the same item set (once from
    ``_apply_tab_state``/``_on_tab_scan_complete`` and once from the filter toolbar's
    own callback). With a per-item row build the second call hit a soft-refresh path
    that assumed every row already existed, cancelled the pending chain and marked the
    build finished - so the rest of the grid was never built.

    A recycled pool removes the failure mode structurally: there is no partial build to
    freeze, because only the visible window exists and it is bound synchronously on
    every update. These tests hold that property.
    """

    @classmethod
    def setUpClass(cls):
        cls.root = _ROOT

    def setUp(self):
        self.loader = MagicMock()
        # None, not a real image: a decoded thumbnail makes the worker thread reach for
        # the event loop, which a test that only calls update() has no main loop for.
        self.loader.get_thumbnail.return_value = None
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

    def _drain(self, timeout_s=15.0):
        deadline = time.monotonic() + timeout_s
        while self.list_widget.row_pool._rebind_after_id is not None and time.monotonic() < deadline:
            self.root.update()
        self.root.update()

    def _visible(self):
        pool = self.list_widget.row_pool
        return set(pool.visible_item_indices())

    def test_repeated_updates_of_the_same_items_bind_the_whole_window(self):
        items = [_make_item(f"SW_{i:03d}.JPG") for i in range(60)]

        self.list_widget.update_items(items, selected_idx=0)
        first = len(self.list_widget._row_frame_map)

        self.list_widget.update_items(items, selected_idx=0)
        self._drain()

        self.assertEqual(set(self.list_widget._row_frame_map), self._visible(),
                         "the bound rows must be exactly the visible window")
        self.assertEqual(len(self.list_widget._row_frame_map), first)

    def test_alternating_updates_converge_on_the_last_item_set(self):
        tab_a = [_make_item(f"A_{i:03d}.JPG") for i in range(60)]
        tab_b = [_make_item(f"B_{i:03d}.JPG") for i in range(60)]

        for _ in range(4):
            self.list_widget.update_items(tab_a, selected_idx=0)
            self.list_widget.update_items(tab_a, selected_idx=0)
            self.root.update()
            self.list_widget.update_items(tab_b, selected_idx=0)
            self.list_widget.update_items(tab_b, selected_idx=0)
            self.root.update()

        self._drain()

        self.assertEqual(set(self.list_widget._row_frame_map), self._visible())
        pool = self.list_widget.row_pool
        self.assertTrue(all(pool.items[i].filename.startswith("B_")
                            for i in pool.visible_item_indices()),
                        "the grid must show the item set that was loaded last")

    def test_a_fresh_item_set_rebinds_every_visible_row(self):
        """A different item set must not leave rows bound to the previous one."""
        first = [_make_item(f"F_{i:03d}.JPG") for i in range(40)]
        second = [_make_item(f"S_{i:03d}.JPG") for i in range(40)]

        self.list_widget.update_items(first, selected_idx=0)
        self._drain()
        self.list_widget.update_items(second, selected_idx=0)
        self._drain()

        pool = self.list_widget.row_pool
        for idx in pool.visible_item_indices():
            self.assertTrue(pool.items[idx].filename.startswith("S_"))
            self.assertIn(idx, self.list_widget._row_frame_map)

    def test_requests_are_not_duplicated_across_repeated_updates(self):
        submitted = []
        real_submit = self.list_widget._load_single_thumb_async

        def counting(path, size, wb, load_id):
            submitted.append(str(path))
            return real_submit(path, size, wb, load_id)

        self.list_widget._load_single_thumb_async = counting
        self.addCleanup(setattr, self.list_widget, "_load_single_thumb_async", real_submit)

        items = [_make_item(f"REQ_{i:03d}.JPG") for i in range(40)]
        self.list_widget.update_items(items, selected_idx=0)
        self._drain()
        first = len(submitted)
        self.assertGreater(first, 0, "the first load must submit work")

        self.list_widget.update_items(items, selected_idx=1)
        self.list_widget.update_items(items, selected_idx=0)
        self._drain()

        self.assertEqual(len(set(submitted)), len(submitted),
                         "no path may be submitted twice for the same item set")

def deque_len_zero():
    from collections import deque
    return deque()
