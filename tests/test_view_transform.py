"""Viewer geometry and the render pyramid (defects D14 and D20).

D20: the contain-fit transform used to be recomputed inline in five places and only
``redraw`` applied the 3500 px render cap, so detection boxes drifted off the subject
they mark. D14: every redraw resized from the full-resolution source.

These are pure-geometry tests, so they do not need a Tk window.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from culler.gui.view_transform import MAX_RENDER_DIM, RenderPyramid, contain_fit


class TestContainFit(unittest.TestCase):
    def test_fits_inside_the_canvas_at_zoom_one(self):
        fit = contain_fit(4000, 3000, 800, 600)
        self.assertEqual((fit.width, fit.height), (800, 600))
        self.assertAlmostEqual(fit.scale, 0.2)

    def test_letterboxes_on_the_short_axis(self):
        fit = contain_fit(4000, 3000, 800, 600, zoom=1.0)
        self.assertEqual((fit.width, fit.height), (800, 600))
        self.assertAlmostEqual(fit.center_x, 400)
        self.assertAlmostEqual(fit.center_y, 300)

    def test_no_layout_yet_returns_none(self):
        self.assertIsNone(contain_fit(4000, 3000, 1, 1))
        self.assertIsNone(contain_fit(0, 0, 800, 600))

    def test_cap_is_applied_and_scale_follows_the_render(self):
        fit = contain_fit(6000, 4000, 800, 600, zoom=5.0)
        self.assertLessEqual(max(fit.width, fit.height), MAX_RENDER_DIM)
        # The reported scale must describe what is actually on screen, otherwise
        # overlays are placed against a transform that was never drawn.
        self.assertAlmostEqual(fit.scale, fit.width / 6000.0, places=6)

    def test_cap_preserves_aspect_ratio(self):
        fit = contain_fit(6000, 4000, 800, 600, zoom=5.0)
        self.assertAlmostEqual(fit.width / fit.height, 6000 / 4000.0, places=2)

    def test_pan_moves_the_centre_not_the_size(self):
        centred = contain_fit(4000, 3000, 800, 600)
        panned = contain_fit(4000, 3000, 800, 600, pan_x=120.0, pan_y=-40.0)
        self.assertEqual((panned.width, panned.height), (centred.width, centred.height))
        self.assertAlmostEqual(panned.center_x, centred.center_x + 120.0)
        self.assertAlmostEqual(panned.center_y, centred.center_y - 40.0)

    def test_normalized_round_trip(self):
        fit = contain_fit(6000, 4000, 800, 600, zoom=3.0, pan_x=25.0)
        nx, ny = fit.normalized_from_canvas(*fit.canvas_from_normalized(0.25, 0.75))
        self.assertAlmostEqual(nx, 0.25, places=6)
        self.assertAlmostEqual(ny, 0.75, places=6)

    def test_image_round_trip_uses_source_pixels(self):
        img_w, img_h = 6000, 4000
        fit = contain_fit(img_w, img_h, 800, 600, zoom=3.0, pan_x=25.0, pan_y=-10.0)
        # Top-left corner of the rendered image maps to source pixel (0, 0).
        x, y = fit.image_from_canvas(fit.left, fit.top, img_w, img_h)
        self.assertAlmostEqual(x, 0.0, places=3)
        self.assertAlmostEqual(y, 0.0, places=3)
        # And the centre to the centre of the source.
        x, y = fit.image_from_canvas(fit.center_x, fit.center_y, img_w, img_h)
        self.assertAlmostEqual(x, img_w / 2.0, places=2)
        self.assertAlmostEqual(y, img_h / 2.0, places=2)

    def test_left_and_top_match_the_centre(self):
        fit = contain_fit(1000, 800, 500, 400, pan_x=10.0)
        self.assertAlmostEqual(fit.left, fit.center_x - fit.width / 2.0)
        self.assertAlmostEqual(fit.top, fit.center_y - fit.height / 2.0)


class TestDetectionBoxesLandOnTheRender(unittest.TestCase):
    """The regression D20 describes, expressed as geometry."""

    def test_box_matches_the_image_when_the_cap_kicks_in(self):
        img_w, img_h = 6000, 4000
        fit = contain_fit(img_w, img_h, 800, 600, zoom=5.0)

        # Full width of the render is the full width of the image, whatever the cap did.
        left_x, _ = fit.canvas_from_normalized(0.0, 0.5)
        right_x, _ = fit.canvas_from_normalized(1.0, 0.5)
        self.assertAlmostEqual(right_x - left_x, fit.width, places=6)
        self.assertAlmostEqual(left_x, fit.left, places=6)
        self.assertAlmostEqual(right_x, fit.left + fit.width, places=6)

    def test_the_reported_scale_always_describes_the_reported_size(self):
        """The invariant every overlay relies on: scale and size are one geometry."""
        img_w, img_h = 6000, 4000
        for zoom in (0.2, 1.0, 2.5, 5.0):
            for max_dim in (0, MAX_RENDER_DIM):
                fit = contain_fit(img_w, img_h, 800, 600, zoom=zoom, max_dim=max_dim)
                self.assertAlmostEqual(fit.scale, fit.width / float(img_w), places=6)
                self.assertAlmostEqual(fit.scale * img_h, fit.height, delta=1.5)


class TestRenderPyramid(unittest.TestCase):
    def setUp(self):
        self.source = Image.new("RGB", (1200, 900), "blue")

    def test_renders_exactly_the_requested_size(self):
        pyramid = RenderPyramid()
        pyramid.set_source(self.source)
        out = pyramid.render((320, 240))
        self.assertEqual(out.size, (320, 240))

    def test_same_size_is_served_from_the_cache(self):
        pyramid = RenderPyramid()
        pyramid.set_source(self.source)
        first = pyramid.render((320, 240))
        second = pyramid.render((320, 240))

        self.assertIs(first, second)
        self.assertEqual(pyramid.hits, 1)
        self.assertEqual(pyramid.misses, 1)

    def test_zoom_out_and_back_in_reuses_the_earlier_render(self):
        """D14: repeated redraws must not resize from the full-resolution source."""
        pyramid = RenderPyramid()
        pyramid.set_source(self.source)
        pyramid.render((600, 450))
        misses_after_first = pyramid.misses
        pyramid.render((300, 225))
        pyramid.render((600, 450))

        self.assertEqual(pyramid.misses, misses_after_first + 1,
                         "the smaller render is new; the return to 600x450 is a hit")

    def test_downscaling_derives_from_the_smallest_sufficient_level(self):
        pyramid = RenderPyramid()
        pyramid.set_source(self.source)
        big = pyramid.render((1200, 900))          # identical to the source
        calls = []

        original = Image.Image.resize

        def spy(self, size, *args, **kwargs):
            calls.append((self.size, size))
            return original(self, size, *args, **kwargs)

        Image.Image.resize = spy
        try:
            pyramid.render((600, 450))
        finally:
            Image.Image.resize = original

        self.assertEqual(calls[-1][0], (1200, 900),
                         "the smallest cached level that covers the target is the source here")

    def test_filters_do_not_share_an_entry(self):
        pyramid = RenderPyramid()
        pyramid.set_source(self.source)
        smooth = pyramid.render((200, 150), Image.Resampling.BILINEAR)
        crisp = pyramid.render((200, 150), Image.Resampling.NEAREST)

        self.assertIsNot(smooth, crisp)
        self.assertEqual(pyramid.level_count(), 2)

    def test_level_count_is_bounded(self):
        pyramid = RenderPyramid(max_levels=3, max_bytes=1 << 30)
        pyramid.set_source(self.source)
        for size in (1000, 900, 800, 700, 600, 500):
            pyramid.render((size, int(size * 0.75)))

        self.assertLessEqual(pyramid.level_count(), 3)

    def test_byte_budget_is_bounded(self):
        pyramid = RenderPyramid(max_levels=64, max_bytes=2 * 1024 * 1024)
        pyramid.set_source(self.source)
        for size in (1000, 900, 800, 700, 600):
            pyramid.render((size, int(size * 0.75)))

        resident = sum(pyramid._bytes_of(pyramid._levels[k]) for k in pyramid._levels)
        self.assertLessEqual(resident, 2 * 1024 * 1024 + 1000 * 900 * 3)

    def test_set_source_drops_every_level(self):
        pyramid = RenderPyramid()
        pyramid.set_source(self.source)
        pyramid.render((200, 150))
        self.assertEqual(pyramid.level_count(), 1)

        pyramid.set_source(Image.new("RGB", (100, 100), "red"))

        self.assertEqual(pyramid.level_count(), 0)

    def test_render_without_a_source_is_an_error(self):
        pyramid = RenderPyramid()
        with self.assertRaises(ValueError):
            pyramid.render((10, 10))

    def test_a_level_identical_to_the_source_is_not_copied(self):
        pyramid = RenderPyramid()
        pyramid.set_source(self.source)
        out = pyramid.render(self.source.size)
        self.assertIs(out, self.source)


if __name__ == "__main__":
    unittest.main()