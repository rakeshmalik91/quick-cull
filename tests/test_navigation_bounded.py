"""Navigation and prefetch must not spawn unbounded work (defects D10 and D19).

D10: one thread per navigation event and one per prefetch, with no cap.
D19: prefetch decoded every stacked path of every adjacent item.
"""

import inspect
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from culler.culler_engine import ImageItem
from gui import ImageCullerApp


def _item(path):
    it = ImageItem.__new__(ImageItem)
    p = Path(path)
    it.path = p
    it.stacked_paths = [p]
    it.is_stacked = False
    it.filename = p.name
    it.format_name = "JPEG Image"
    it.flag = "UNFLAGGED"
    it.rating = 0
    return it


class _RecordingPool:
    """Captures submitted work without running it, and records submission order."""

    def __init__(self):
        self.submitted = []
        self.lock = threading.Lock()

    def submit(self, fn, *args, **kwargs):
        with self.lock:
            self.submitted.append(fn)
        return MagicMock()

    def run_all(self):
        with self.lock:
            pending, self.submitted = self.submitted, []
        for fn in pending:
            fn()

    def shutdown(self, wait=False, cancel_futures=False):
        pass


def _make_app(count=6, stacked=False):
    app = MagicMock()
    app._load_request_id = 1
    app._load_pool = _RecordingPool()
    app._prefetch_pool = _RecordingPool()
    app.current_items = [_item(f"D:/Photos/P{i:03d}.JPG") for i in range(count)]
    if stacked:
        for it in app.current_items:
            second = Path(str(it.path).replace(".JPG", "-edit.JPG"))
            it.stacked_paths = [it.path, second]
            it.is_stacked = True
    app.toolbar.get_raw_scale.return_value = 0.25
    app.toolbar.get_white_balance.return_value = "camera"
    app._get_active_session.return_value = MagicMock()
    # The methods under test are invoked unbound, so the worker must reach the real
    # prefetch implementation rather than a mock attribute.
    app._prefetch_surrounding_images = (
        lambda center_idx, is_continuous=False:
        ImageCullerApp._prefetch_surrounding_images(app, center_idx, is_continuous)
    )
    app._on_image_loaded = lambda *a, **k: None
    app.after = lambda ms, fn=None, *a, **k: MagicMock()
    return app


class TestPrefetchTargets(unittest.TestCase):
    def test_prefetch_touches_only_the_immediate_neighbours(self):
        app = _make_app(count=10)
        ImageCullerApp._prefetch_surrounding_images(app, 5)

        self.assertEqual(len(app._prefetch_pool.submitted), 1,
                         "one task, not one thread per neighbour")

    def test_prefetch_never_decodes_a_stacked_variant(self):
        """D19: it used to load every path of every adjacent item."""
        app = _make_app(count=6, stacked=True)
        ImageCullerApp._prefetch_surrounding_images(app, 2)
        app._prefetch_pool.run_all()

        decoded = [Path(call.args[0])
                   for call in app._get_active_session.return_value.image_loader.load_full_image.call_args_list]
        self.assertTrue(decoded)
        for path in decoded:
            self.assertFalse(path.name.endswith("-edit.JPG"),
                             "stacked variants are reachable by clicking, not by prefetch")

    def test_prefetch_stops_when_navigation_moves_on(self):
        app = _make_app(count=8)
        ImageCullerApp._prefetch_surrounding_images(app, 2)
        app._load_request_id = 99

        app._prefetch_pool.run_all()

        app._get_active_session.return_value.image_loader.load_full_image.assert_not_called()

    def test_continuous_navigation_still_defers_the_prefetch(self):
        app = _make_app(count=6)
        ImageCullerApp._prefetch_surrounding_images(app, 2, is_continuous=True)

        self.assertEqual(app._prefetch_pool.submitted, [],
                         "a continuous scroll must not queue work per keystroke")

    def test_prefetch_at_the_edges_stays_in_range(self):
        app = _make_app(count=3)
        ImageCullerApp._prefetch_surrounding_images(app, 0)
        app._prefetch_pool.run_all()

        decoded = [Path(call.args[0])
                   for call in app._get_active_session.return_value.image_loader.load_full_image.call_args_list]
        self.assertTrue(all(0 <= int(p.stem[1:]) < 3 for p in decoded))


class TestNavigationUsesThePool(unittest.TestCase):
    def _cold_app(self, count=4):
        app = _make_app(count)
        loader = app._get_active_session.return_value.image_loader
        loader.get_cached_full_image.return_value = None
        loader.get_cached_thumbnail.return_value = None
        return app

    def test_cold_navigation_submits_one_task_to_the_load_pool(self):
        app = self._cold_app()

        ImageCullerApp._select_image(app, 0)

        self.assertEqual(len(app._load_pool.submitted), 1)
        names = app._load_pool.submitted[0].__code__.co_names
        self.assertIn("load_full_image", names)

    def test_uncached_navigation_runs_the_worker_and_then_prefetches(self):
        app = self._cold_app()

        ImageCullerApp._select_image(app, 0)
        app.after = lambda ms, fn=None, *a, **k: MagicMock()
        app._load_pool.run_all()

        self.assertEqual(len(app._prefetch_pool.submitted), 1)

    def test_source_no_longer_starts_a_thread_per_navigation(self):
        source = inspect.getsource(ImageCullerApp._select_image)
        self.assertNotIn("threading.Thread", source,
                         "navigation must submit to the bounded load pool")

    def test_source_no_longer_starts_a_thread_per_prefetch(self):
        source = inspect.getsource(ImageCullerApp._prefetch_surrounding_images)
        self.assertNotIn("threading.Thread", source,
                         "prefetch must submit to the single-worker prefetch pool")

    def test_pools_are_bounded(self):
        init_src = inspect.getsource(ImageCullerApp.__init__)
        self.assertIn("ThreadPoolExecutor(max_workers=2", init_src)
        self.assertIn("ThreadPoolExecutor(max_workers=1", init_src)

    def test_close_shuts_both_pools_down(self):
        source = inspect.getsource(ImageCullerApp._on_close)
        self.assertIn("_load_pool", source)
        self.assertIn("_prefetch_pool", source)
        self.assertIn("cancel_futures", source)


if __name__ == "__main__":
    unittest.main()