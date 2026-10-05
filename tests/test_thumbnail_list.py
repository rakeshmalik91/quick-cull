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


# One Tk root for the whole module. Creating a CTk root per test intermittently fails
# with "invalid command name tcl_findLibrary" or "can't find a usable tk.tcl", which
# turns into errors, skips and wildly variable runtimes rather than a real failure.
_ROOT = None


def setUpModule():
    global _ROOT
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    _ROOT = ctk.CTk()
    _ROOT.geometry("900x700+30+30")
    _ROOT.update()


def tearDownModule():
    try:
        _ROOT.destroy()
    except Exception:
        pass


class TestThumbnailList(unittest.TestCase):
    """
    Automated Unit Test Suite for ThumbnailList placeholder generation,
    non-blocking batch loading, progress bar, and soft item refresh.
    """

    def setUp(self):
        self.root = _ROOT
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
        self.root.update()

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
        Verify update_items with empty list clears widgets and settles immediately.
        """
        self.list_widget.update_items([], selected_idx=0)
        self.list_widget.update()
        self.assertEqual(len(self.list_widget._row_frame_map), 0)
        self.assertTrue(self.list_widget._is_thumb_load_complete())

    def test_update_items_starts_batch_load(self):
        """
        Verify update_items with items stores pending state and starts batch processing.
        """
        items = [self._make_item(f"IMG_{i:03d}.JPG") for i in range(5)]
        self.list_widget.update_items(items, selected_idx=0)
        self.assertEqual(len(self.list_widget._pending_items), 5)
        self.assertEqual(self.list_widget._pending_selected_idx, 0)

    def test_completion_reflects_pending_work_not_row_count(self):
        """
        With a recycled pool, "incomplete" means a rebind is still pending or a request
        is outstanding - not that rows remain to build, because rows are never all built.
        There is no progress bar any more; what matters is that the duration settles.
        """
        items = [self._make_item(f"IMG_{i:03d}.JPG") for i in range(60)]

        self.list_widget.start_load_timing(time.monotonic() - 1.0)
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()

        # A rebind still outstanding means the load is not finished.
        self.list_widget.row_pool._rebind_after_id = "pending"
        self.assertFalse(self.list_widget._is_thumb_load_complete())

        # Once the pool has settled and nothing is queued or decoding, it is complete -
        # even for a folder whose decodes all failed.
        self.list_widget.row_pool._rebind_after_id = None
        self.list_widget._inflight_thumbs.clear()
        self.list_widget._thumb_result_queue.clear()
        self.list_widget._update_progress_ui()

        self.assertTrue(self.list_widget._is_thumb_load_complete())
        self.assertIsNone(self.list_widget._timing_after_id,
                          "the duration must stop once the load settles")

    def test_there_is_no_batch_count_in_the_grid_footer(self):
        """
        The footer used to carry a progress bar and an "N / M" counter. With a
        virtualised grid that count was the number of *bound* rows decoded - a batch
        count that changed as you scrolled and said nothing about the folder.
        """
        for name in ("progress_bar", "lbl_progress_text", "progress_row"):
            self.assertFalse(hasattr(self.list_widget, name),
                             f"{name} should have been removed")

    def test_progress_complete_when_nothing_outstanding(self):
        """With no rows to build and no requests in flight, the load is complete."""
        self.list_widget._pending_items = []
        self.list_widget._batch_index = 0
        self.list_widget._inflight_thumbs.clear()
        self.list_widget._thumb_result_queue.clear()
        self.list_widget._total_thumbs = 0

        self.list_widget._update_progress_ui()

        self.assertTrue(self.list_widget._is_thumb_load_complete())

    def test_thumb_timer_freezes_once_everything_loaded(self):
        """
        Regression: the Thumbs duration timer ran forever because completion was
        derived from counters that de-duplicated requests could never satisfy.
        """
        self.list_widget.start_load_timing()
        self.list_widget.start_thumb_timing()
        self.list_widget._pending_items = []
        self.list_widget._batch_index = 0
        self.list_widget._inflight_thumbs.clear()
        self.list_widget._thumb_result_queue.clear()
        self.list_widget._total_thumbs = 0

        self.list_widget._update_progress_ui()

        self.assertFalse(self.list_widget._load_cycle_active,
                         "the load cycle must end once no work is outstanding")
        self.assertIsNone(self.list_widget._timing_after_id,
                          "the refresh timer must be cancelled, not left running")
        self.assertIsNotNone(self.list_widget._thumb_time_final)

    def test_update_btn_image_counts_a_painted_thumbnail(self):
        """
        A painted thumbnail counts as loaded and clears the path's in-flight mark.

        With a recycled pool, "still incomplete" means a rebind is pending or a request
        is outstanding - not "rows remain to build", since rows are never all built.
        """
        items = [self._make_item("TEST.JPG")]
        self.list_widget.update_items(items, selected_idx=0)
        self.list_widget.row_pool._rebind_after_id = "pending"
        self.list_widget._total_thumbs = 2
        self.list_widget._loaded_thumbs = 0

        pil_thumb = Image.new("RGB", (80, 80), color="red")
        self.list_widget._current_load_id = 1
        self.list_widget._update_btn_image(str(items[0].path), pil_thumb, load_id=1)

        self.assertEqual(self.list_widget._loaded_thumbs, 1)
        self.assertNotIn(str(items[0].path), self.list_widget._inflight_thumbs)
        self.assertFalse(self.list_widget._is_thumb_load_complete())

        self.list_widget.row_pool._rebind_after_id = None

    def test_duration_stats_survive_completion(self):
        """The footer keeps the Folder/Thumbs timings after the load settles."""
        self.list_widget._total_thumbs = 1
        self.list_widget._loaded_thumbs = 1
        self.list_widget._pending_items = []
        self.list_widget._batch_index = 0

        self.list_widget._update_progress_ui()

        self.assertTrue(self.list_widget._is_thumb_load_complete())
        self.assertFalse(self.list_widget._load_cycle_active)

    def test_batch_cancel_on_new_update(self):
        """
        Verify that loading a different item set cancels a pending pool rebind.
        """
        items1 = [self._make_item(f"IMG_{i:03d}.JPG") for i in range(3)]
        self.list_widget.update_items(items1, selected_idx=0)

        cancelled = []
        real_cancel = self.list_widget.after_cancel

        def recording_cancel(ident):
            cancelled.append(ident)
            return real_cancel(ident)

        self.list_widget.row_pool._rebind_after_id = "after_id_stale"
        self.list_widget.after_cancel = recording_cancel
        try:
            items2 = [self._make_item(f"OTHER_{i:03d}.JPG") for i in range(3)]
            self.list_widget.update_items(items2, selected_idx=0)
        finally:
            self.list_widget.after_cancel = real_cancel

        self.assertIn("after_id_stale", cancelled,
                      "a pending rebind must be cancelled on a new item set")
        # Settle explicitly rather than relying on the event loop: the invariant under
        # test is that no stale chain survives, not that Tk dispatched an idle callback.
        pool = self.list_widget.row_pool
        pool.sync(budget=len(items2))
        self.assertIsNone(self.list_widget.row_pool._rebind_after_id,
                          "the pool must settle once the new item set is bound")
        self.assertEqual(
            {pool.items[i].filename for i in pool.visible_item_indices()},
            {it.filename for it in items2},
            "the grid must show the item set that was loaded last")

    def test_soft_refresh_does_not_leave_a_stale_rebind(self):
        """
        Switching away and back (same items, soft refresh) must not leave two rebinds
        racing for the same slots.
        """
        items = [self._make_item(f"IMG_{i:03d}.JPG") for i in range(3)]
        self.list_widget.update_items(items, selected_idx=0)
        self.list_widget.row_pool.request_sync(reason="test")

        self.list_widget.update_items(items, selected_idx=0)
        pool = self.list_widget.row_pool
        pool.sync(budget=len(items))

        self.assertIsNone(pool._rebind_after_id,
                          "a soft refresh must settle the pool, not queue more work")
        self.assertEqual(len(self.list_widget._row_frame_map), len(items))

    def test_a_large_folder_only_binds_the_visible_window(self):
        """
        A folder far bigger than the viewport must not build a row per photo: only the
        rows the viewport can show exist, and the rest are bound as the user scrolls.
        """
        items = [self._make_item(f"VIRT_{i:04d}.JPG") for i in range(2000)]
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()

        bound = len(self.list_widget._row_frame_map)
        self.assertGreater(bound, 0, "the first screen must be painted")
        self.assertLess(bound, len(items),
                        "the whole folder must not be built")
        self.assertLessEqual(bound, 120, "about 100 rows is all that should exist in memory")

    def test_pool_is_reused_rather_than_recreated(self):
        """
        Recycled means the widgets survive a scroll: only their bindings change.
        """
        items = [self._make_item(f"POOL_{i:03d}.JPG") for i in range(200)]
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()
        frames_before = dict(self.list_widget._row_frame_map)
        slots_before = len(self.list_widget.row_pool._slots)

        self.list_widget.row_pool.scroll_to_index(3)
        self.list_widget._drain_pool_now()
        self.root.update()

        self.assertEqual(len(self.list_widget.row_pool._slots), slots_before,
                         "scrolling must not create new widgets")
        reused = [f for idx, f in frames_before.items()
                  if self.list_widget._row_frame_map.get(idx) is f]
        self.assertGreater(len(reused), 0, "surviving rows must keep their frame widgets")

    def test_scrolling_binds_the_rows_it_moves_to(self):
        items = [self._make_item(f"SCROLL_{i:03d}.JPG") for i in range(300)]
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()
        before = set(self.list_widget._row_frame_map)

        self.list_widget.row_pool.scroll_to_index(200, align="top")
        self.list_widget._drain_pool_now()
        self.root.update()

        after = set(self.list_widget._row_frame_map)
        self.assertTrue(before != after, "the bound window must have moved")
        self.assertTrue(any(idx >= 150 for idx in after),
                        "rows near the new scroll position must be bound")

    def test_rebind_is_bounded_per_tick(self):
        """
        A jump to a far position rebinds a few rows and resumes on the next tick, so
        the longest UI tick stays bounded instead of covering the whole distance.
        """
        from culler.gui.row_pool import REBIND_ROWS_PER_TICK

        items = [self._make_item(f"JUMP_{i:04d}.JPG") for i in range(3000)]
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()

        pool = self.list_widget.row_pool
        bound_before = len([i for i in pool._slot_items if i is not None])

        pool.scroll_to_index(2900, align="top")
        pool.sync(budget=REBIND_ROWS_PER_TICK)

        bound_after = len([i for i in pool._slot_items if i is not None])
        self.assertLessEqual(bound_after - bound_before, REBIND_ROWS_PER_TICK,
                             "one tick must not rebind the whole distance")
        self.assertIsNotNone(pool._rebind_after_id,
                             "an unfinished rebind must resume on the next tick")

    def test_draining_the_pool_binds_every_visible_row(self):
        items = [self._make_item(f"DRAIN_{i:03d}.JPG") for i in range(400)]
        self.list_widget.update_items(items, selected_idx=0)
        pool = self.list_widget.row_pool
        for _ in range(200):
            if pool._rebind_after_id is None:
                break
            pool.sync(budget=len(items))

        self.assertIsNone(pool._rebind_after_id)
        visible = set(pool.visible_item_indices())
        self.assertEqual(set(self.list_widget._row_frame_map), visible,
                         "exactly the visible window must be bound once settled")

    def test_bound_rows_have_a_label_widget(self):
        """
        Regression: registering a bound row popped the label entry the body binder had
        just created, so no row ever had a label - filenames, star ratings and flag
        colours silently stopped rendering, and rows flickered as they were rebuilt.
        """
        items = [self._make_item(f"LBL_{i:03d}.JPG") for i in range(30)]
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()
        pool = self.list_widget.row_pool

        for index in pool.visible_item_indices():
            self.assertIn(index, self.list_widget._label_map,
                          f"row {index} is bound but has no label widget")
            widget = self.list_widget._label_map[index]
            self.assertTrue(widget.cget("text").startswith(items[index].filename),
                            f"row {index} must show its filename, got "
                            f"{widget.cget('text')!r}")

    def test_a_single_rebind_tick_is_bounded(self):
        """
        Regression: set_selected_indices drained the pool with an unbounded budget, so a
        single turn re-bound *every* slot rather than one tick's worth. That is what
        made navigation feel like a freeze even on a small folder.

        The bound is per tick, not per press: pressing a row near the edge legitimately
        scrolls the view, and the rest of the screen then catches up over later ticks.
        """
        from culler.gui.row_pool import REBIND_ROWS_PER_TICK

        items = [self._make_item(f"NAV_{i:03d}.JPG") for i in range(200)]
        self.list_widget.update_items(items, selected_idx=0)
        pool = self.list_widget.row_pool
        previous = None
        for _ in range(50):
            pool.sync(budget=len(items))
            self.root.update()
            current = list(pool._slot_items)
            if current == previous:
                break
            previous = current

        # Jump the view somewhere else entirely, then run exactly one bounded tick.
        pool.scroll_to_index(150, align="top")
        pool._last_scroll_offset = -1

        rebinds = []
        real_bind = pool._bind_slot
        pool._bind_slot = lambda s, i: (rebinds.append(i), real_bind(s, i))[1]
        try:
            pool.sync(budget=REBIND_ROWS_PER_TICK)
        finally:
            pool._bind_slot = real_bind

        self.assertLessEqual(
            len(rebinds), REBIND_ROWS_PER_TICK,
            f"one rebind tick rebound {len(rebinds)} rows; the whole screen must never "
            f"be re-bound in one turn")
        self.assertIsNotNone(pool._rebind_after_id,
                             "the rest of the window must resume on a later tick")

    def test_pressing_a_row_already_mid_window_rebinds_nothing(self):
        items = [self._make_item(f"MID_{i:03d}.JPG") for i in range(30)]
        self.list_widget.update_items(items, selected_idx=0)
        pool = self.list_widget.row_pool
        previous = None
        for _ in range(50):
            pool.sync(budget=len(items))
            self.root.update()
            current = list(pool._slot_items)
            if current == previous:
                break
            previous = current

        visible = pool.visible_item_indices()
        middle = visible[len(visible) // 2]
        rebinds = []
        real_bind = pool._bind_slot
        pool._bind_slot = lambda s, i: (rebinds.append(i), real_bind(s, i))[1]
        try:
            for _ in range(5):
                self.list_widget.set_selected_indices({middle}, middle)
        finally:
            pool._bind_slot = real_bind

        self.assertEqual(rebinds, [],
                         "re-selecting a row already on screen must rebind nothing")
        self.assertEqual(self.list_widget._row_frame_map[middle],
                         pool.slot_for_index(middle)["frame"])

    def test_a_selection_beyond_the_window_eventually_binds(self):
        """Navigating past the pool must still bring the row into view."""
        items = [self._make_item(f"FAR_{i:03d}.JPG") for i in range(200)]
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()
        pool = self.list_widget.row_pool

        self.list_widget.set_selected_indices({150}, 150)
        for _ in range(50):
            if pool.is_visible(150):
                break
            pool.sync(budget=len(items))

        self.assertTrue(pool.is_visible(150),
                        "the selected row must become visible after scrolling to it")
        self.assertEqual(self.list_widget._row_frame_map[150],
                         pool.slot_for_index(150)["frame"])

    def test_thumbnail_results_are_painted_without_a_worker_touching_tk(self):
        """
        Regression: decode workers registered the drain with after(). When that failed
        nothing retried it, so results sat in the queue for ever - the row kept its
        placeholder and the path stayed 'in flight', so the grid never finished loading.
        """
        items = [self._make_item(f"ARM_{i:03d}.JPG") for i in range(10)]
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()

        paths = [str(item.path) for item in items]
        load_id = self.list_widget._current_load_id
        self.list_widget._ctk_img_cache.clear()
        self.list_widget._thumb_result_queue.clear()
        self.list_widget._thumb_result_after_id = None

        real_after = self.list_widget.after
        armed = []

        def failing_after(ms, func=None, *a, **k):
            if func == self.list_widget._drain_thumb_results:
                armed.append(ms)
                raise RuntimeError("no Tk loop available in this worker")
            return real_after(ms, func, *a, **k)

        self.list_widget.after = failing_after
        try:
            for path in paths:
                self.list_widget._queue_thumb_result(
                    path, Image.new("RGB", (80, 80), "blue"), load_id)
        finally:
            self.list_widget.after = real_after

        self.assertEqual(armed, [],
                         "a decode worker must not try to register Tk work at all")
        self.assertEqual(len(self.list_widget._thumb_result_queue), len(items),
                         "results must be queued even though no timer was registered")

        # The UI thread picks them up on its next visit, exactly once.
        self.list_widget._arm_thumb_drain()
        self.assertIsNotNone(self.list_widget._thumb_result_after_id)
        self.list_widget._arm_thumb_drain()
        for _ in range(20):
            self.root.update()
            time.sleep(0.002)
        self.assertEqual(len(self.list_widget._thumb_result_queue), 0,
                         "the UI thread must drain what the workers queued")
        self.assertEqual(len(self.list_widget._ctk_img_cache), len(items))
        self.assertEqual(len(self.list_widget._inflight_thumbs), 0,
                         "no path may stay 'in flight' once its thumbnail is painted")

    def test_a_small_folder_is_fully_bound_immediately(self):
        """
        A folder that fits on screen is bound in the same turn as the update, so a tab
        switch paints it without waiting for a tick.
        """
        items = [self._make_item(f"LONG_{i:03d}.JPG") for i in range(4)]

        self.list_widget.update_items(items, selected_idx=0)

        self.assertEqual(len(self.list_widget._row_frame_map), len(items))

    def test_visible_rows_each_get_exactly_one_thumbnail_request(self):
        """
        Requests follow the viewport: one per bound row, none for rows nobody can see.
        """
        items = [self._make_item(f"THUMB_{i:04d}.JPG") for i in range(600)]
        requested = []
        original = self.list_widget._load_single_thumb_async

        def record(path, size, wb, load_id):
            requested.append(Path(path))
            return original(path, size, wb, load_id)

        self.list_widget._load_single_thumb_async = record
        try:
            self.list_widget.update_items(items, selected_idx=0)
            self.root.update()
        finally:
            self.list_widget._load_single_thumb_async = original

        pool = self.list_widget.row_pool
        visible = {pool.items[i].path for i in pool.visible_item_indices()}
        self.assertTrue(visible, "there must be a visible window")
        self.assertEqual(set(requested), visible,
                         "exactly the visible rows are requested, once each")
        self.assertLess(len(requested), len(items) / 4,
                        "a 600-row folder must not queue every thumbnail up front")

    def test_soft_update_skips_rebuild_when_paths_match(self):
        """
        Verify update_items does a soft refresh (no widget destroy/recreate) when paths match.
        """
        items = [self._make_item(f"IMG_{i:03d}.JPG") for i in range(5)]
        self.list_widget.update_items(items, selected_idx=0)
        self.list_widget.update()
        self.assertEqual(len(self.list_widget._row_frame_map), 5)

        row_count_before = len(self.list_widget._row_frame_map)
        self.list_widget.update_items(items, selected_idx=0)
        self.assertEqual(len(self.list_widget._row_frame_map), row_count_before)

    def test_soft_update_submits_thumbnails(self):
        """
        Verify soft update submits thumbnail loads for items not yet cached, and that
        the totals still cover every item.
        """
        items = [self._make_item(f"IMG_{i:03d}.JPG") for i in range(3)]
        self.list_widget.update_items(items, selected_idx=0)

        # Pretend the thumbnails decoded and were cached, then clear the registry so the
        # soft refresh has to reason about cached vs outstanding work again.
        for item in items:
            self.list_widget._ctk_img_cache[str(item.path)] = object()
            self.list_widget._inflight_thumbs.pop(str(item.path), None)

        submitted = []
        self.list_widget._load_single_thumb_async = (
            lambda path, size, wb, load_id: submitted.append(str(path)))

        self.list_widget.update_items(items, selected_idx=1)

        self.assertEqual(submitted, [], "cached thumbnails must not be re-submitted")
        self.assertEqual(self.list_widget._total_thumbs, len(items),
                         "totals must cover cached items too")
        self.assertEqual(self.list_widget._loaded_thumbs, len(items))

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
        self.assertIn("Image 1-3 of 3 loaded", self.list_widget.lbl_load_timing.cget("text"))

    def test_load_timing_shows_folder_and_thumb_durations(self):
        """
        Verify folder and thumbnail load times are both shown above the progress bar.
        """
        started = time.monotonic() - 3.0
        self.list_widget.start_load_timing(started)
        self.assertIn("Memory:", self.list_widget.lbl_load_timing.cget("text"))

        self.list_widget.start_thumb_timing()
        self.assertIn("Memory:", self.list_widget.lbl_load_timing.cget("text"))

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
        self.assertIn("loaded", text)
        self.assertIn("Memory:", text)
        self.assertFalse(self.list_widget._load_cycle_active,
                         "the duration must stop once the load settles")

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
        self.assertIn("Image 1-1 of 1 loaded", self.list_widget.lbl_load_timing.cget("text"))
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
        self.assertIn("Memory:", self.list_widget.lbl_load_timing.cget("text"))

    def test_show_load_stats_restores_saved_totals(self):
        """
        Verify a saved tab's totals are displayed without restarting any timer.
        """
        self.list_widget.show_load_stats(4.5, 1.25)
        text = self.list_widget.lbl_load_timing.cget("text")
        self.assertIn("Memory:", text)
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
        self.assertIn("Memory:", self.list_widget.lbl_load_timing.cget("text"))

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
        Verify an empty folder result freezes the timers.
        """
        self.list_widget.start_load_timing(time.monotonic() - 1.0)
        self.list_widget.update_items([], selected_idx=0)
        self.assertIn("Memory:", self.list_widget.lbl_load_timing.cget("text"))
        self.assertIsNotNone(self.list_widget._folder_time_final)
        self.assertIsNone(self.list_widget._timing_after_id)

    def test_pool_stats_shows_loaded_pool_size_and_memory(self):
        items = [self._make_item(f"IMG_{i:03d}.JPG") for i in range(15)]
        self.list_widget.start_load_timing()
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()
        text = self.list_widget.lbl_pool_stats.cget("text")
        self.assertIn("Image 1-15 of 15 loaded", text)
        self.assertIn("Memory:", text)
        self.assertIn("MB", text)
        self.assertAlmostEqual(self.list_widget.pool_progress_bar.get(), 1.0, places=2)

    def test_differential_sliding_window_navigation(self):
        """
        Navigating within the window safe zone must not rebind or slide slots.
        Approaching within SLIDE_MARGIN of the window edge shifts by SLIDE_CHUNK.
        """
        items = [self._make_item(f"DIFF_{i:04d}.JPG") for i in range(300)]
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()

        pool = self.list_widget.row_pool
        # Initial window is 0 to 99
        self.assertEqual((pool.window_start, pool.window_end), (0, 99))
        self.assertIn("Image 1-100 of 300 loaded", self.list_widget.lbl_pool_stats.cget("text"))
        self.assertAlmostEqual(self.list_widget.pool_progress_bar.get(), 100 / 300, places=2)
        r_start, r_end = self.list_widget.pool_progress_bar.get_range()
        self.assertAlmostEqual(r_start, 0.0, places=2)
        self.assertAlmostEqual(r_end, 100 / 300, places=2)

        # Navigating to index 50 (well inside safe zone [20, 79]) does NOT slide or rebind
        slot_50_before = pool.slot_for_index(50)
        self.list_widget.set_selected_indices({50}, 50)
        self.assertEqual((pool.window_start, pool.window_end), (0, 99))
        self.assertIs(pool.slot_for_index(50), slot_50_before)

        # Navigating to index 80 (approaching window_end 99 within margin 20) triggers slide
        self.list_widget.set_selected_indices({80}, 80)
        self.assertEqual((pool.window_start, pool.window_end), (30, 129))
        self.assertIn("Image 31-130 of 300 loaded", self.list_widget.lbl_pool_stats.cget("text"))
        self.assertAlmostEqual(self.list_widget.pool_progress_bar.get(), 130 / 300, places=2)
        r_start, r_end = self.list_widget.pool_progress_bar.get_range()
        self.assertAlmostEqual(r_start, 30 / 300, places=2)
        self.assertAlmostEqual(r_end, 130 / 300, places=2)
        # Row 50 is still retained in the middle and was not recreated
        self.assertIsNotNone(pool.slot_for_index(50))


    def test_scrolling_repositions_container_window_in_canvas(self):
        """
        Verify that scrolling moves the inner frame's canvas coordinates so that bound
        rows are physically aligned with the canvas viewport rather than stranded at (0, 0).
        """
        items = [self._make_item(f"WIN_POS_{i:04d}.JPG") for i in range(200)]
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()

        pool = self.list_widget.row_pool
        canvas = self.list_widget.scroll_frame._parent_canvas
        win_id = self.list_widget.scroll_frame._create_window_id

        # At top: window must be at y = 0
        coords_top = canvas.coords(win_id)
        self.assertEqual(coords_top[1], 0.0)

        # Scroll to index 100
        pool.scroll_to_index(100, align="top")
        pool.sync(budget=len(items))
        self.root.update()

        first_bound = pool._slot_items[0]
        self.assertIsNotNone(first_bound)
        expected_y = float(pool.offset_of(first_bound))
        coords_scrolled = canvas.coords(win_id)
        self.assertEqual(coords_scrolled[1], expected_y,
                         f"window y must match offset_of({first_bound}) = {expected_y}")

        # Slot 0 should be mapped and viewable at the scrolled position
        slot0_frame = pool._slots[0]["frame"]
        self.assertEqual(slot0_frame.winfo_viewable(), 1,
                         "bound slot must be viewable on screen at scrolled position")

    def test_fast_navigation_leaves_only_one_blue_highlight(self):
        """
        Fast navigation must never leave multiple images highlighted in blue (#1f538d).
        Both the row frame border and thumbnail button must be uniquely highlighted.
        """
        items = [self._make_item(f"FAST_NAV_{i:03d}.JPG") for i in range(50)]
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()
        pool = self.list_widget.row_pool

        for target_idx in range(1, 25):
            target_path = items[target_idx].path
            self.list_widget.set_selected_indices({target_idx}, target_idx, active_path=target_path)

            # Check all bound slots in pool
            blue_frames = []
            for slot in pool._slots:
                idx = slot.get("item_index")
                if idx is not None:
                    border = slot.get("applied", {}).get("border")
                    if border and border[0] == "#1f538d":
                        blue_frames.append(idx)

            self.assertEqual(blue_frames, [target_idx],
                             f"At step {target_idx}, exactly one row frame must be blue, found {blue_frames}")

            # Check all buttons in _btn_map
            blue_buttons = [p for p, btn in self.list_widget._btn_map.items()
                            if btn.cget("fg_color") == "#1f538d"]
            self.assertEqual(blue_buttons, [str(target_path)],
                             f"At step {target_idx}, exactly one button must be blue, found {blue_buttons}")

    def test_differential_slide_never_leaks_blue_highlights_to_new_items(self):
        """
        When pool slots are rotated and rebound during differential sliding,
        the newly bound items must never inherit blue highlights from recycled slots.
        """
        items = [self._make_item(f"LEAK_CHECK_{i:04d}.JPG") for i in range(200)]
        self.list_widget.update_items(items, selected_idx=0)
        self.root.update()
        pool = self.list_widget.row_pool

        # Start at index 0 (active and blue)
        self.list_widget.set_selected_indices({0}, 0, active_path=items[0].path)

        # Jump/navigate to index 85 which triggers sliding window (shifts window to 30..129)
        self.list_widget.set_selected_indices({85}, 85, active_path=items[85].path)
        self.root.update()

        blue_frames = []
        for slot in pool._slots:
            idx = slot.get("item_index")
            if idx is not None:
                border = slot.get("applied", {}).get("border")
                if border and border[0] == "#1f538d":
                    blue_frames.append(idx)

        self.assertEqual(blue_frames, [85],
                         f"Only index 85 should have blue border, found {blue_frames}")

        blue_buttons = [p for p, btn in self.list_widget._btn_map.items()
                        if btn.cget("fg_color") == "#1f538d"]
        self.assertEqual(blue_buttons, [str(items[85].path)],
                         f"Only index 85 button should be blue, found {blue_buttons}")


if __name__ == "__main__":
    unittest.main()
