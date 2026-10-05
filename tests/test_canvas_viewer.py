"""Viewer widget behaviour: overlay placement, render reuse, resize coalescing.

These need a real Tk canvas, so they follow the pattern of the other widget tests and
skip cleanly when the environment has no Tk.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

import customtkinter as ctk

from culler.gui.canvas_viewer import ImageCanvasViewer
from culler.gui.view_transform import MAX_RENDER_DIM, contain_fit


def _tk_error(exc):
    return "TclError" in type(exc).__name__ or "tk" in str(exc).lower()


class TestImageCanvasViewer(unittest.TestCase):
    # One root for the whole module: creating and destroying a CTk root per test
    # intermittently fails with "can't find a usable tk.tcl", which would silently skip
    # the regression guard for the box drift - the defect this file exists for.
    @classmethod
    def setUpClass(cls):
        try:
            ctk.set_appearance_mode("dark")
            cls.root = ctk.CTk()
            cls.root.geometry("900x700")
            cls.root.update()
        except Exception as exc:
            raise unittest.SkipTest(f"Tk unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        try:
            self.viewer = ImageCanvasViewer(self.root)
            self.viewer.pack(fill="both", expand=True)
            self.root.update()
        except Exception as exc:
            if _tk_error(exc):
                self.skipTest(f"Tk unavailable: {exc}")
            raise

    def tearDown(self):
        try:
            self.viewer.destroy()
        except Exception:
            pass
        self.root.update()

    def _rect_coords(self, rect_id):
        return [float(v) for v in self.viewer.canvas.coords(rect_id)]

    def test_detection_box_tracks_the_rendered_image_at_high_zoom(self):
        """D20: the box math ignored the render cap, so it drifted off the subject."""
        self.viewer.set_image(Image.new("RGB", (6000, 4000), "blue"), preserve_zoom=False)
        self.viewer.zoom_level = 5.0
        self.viewer.redraw(force_resize=True)
        self.root.update()

        self.viewer.set_detection_box((0.25, 0.25, 0.75, 0.75))
        fit = self.viewer.current_fit()
        self.assertIsNotNone(fit)
        self.assertLessEqual(max(fit.width, fit.height), MAX_RENDER_DIM)

        coords = self._rect_coords(self.viewer._ai_rect_id)
        expected_left, expected_top = fit.canvas_from_normalized(0.25, 0.25)
        expected_right, expected_bottom = fit.canvas_from_normalized(0.75, 0.75)

        self.assertAlmostEqual(coords[0], expected_left, places=3)
        self.assertAlmostEqual(coords[1], expected_top, places=3)
        self.assertAlmostEqual(coords[2], expected_right, places=3)
        self.assertAlmostEqual(coords[3], expected_bottom, places=3)

    def test_detection_box_spans_the_whole_image_for_a_full_frame_box(self):
        self.viewer.set_image(Image.new("RGB", (3000, 2000), "blue"), preserve_zoom=False)
        self.viewer.redraw(force_resize=True)
        self.root.update()

        self.viewer.set_detection_box((0.0, 0.0, 1.0, 1.0))
        coords = self._rect_coords(self.viewer._ai_rect_id)
        fit = self.viewer.current_fit()

        self.assertAlmostEqual(coords[0], fit.left, places=2)
        self.assertAlmostEqual(coords[2], fit.left + fit.width, places=2)

    def test_boxes_follow_a_pan(self):
        self.viewer.set_image(Image.new("RGB", (2000, 1500), "blue"), preserve_zoom=False)
        self.viewer.redraw(force_resize=True)
        self.root.update()
        self.viewer.set_detection_box((0.1, 0.1, 0.4, 0.4))
        before = self._rect_coords(self.viewer._ai_rect_id)

        self.viewer.pan_x = 120.0
        self.viewer.pan_y = -40.0
        self.viewer.redraw(force_resize=False)
        after = self._rect_coords(self.viewer._ai_rect_id)

        self.assertAlmostEqual(after[0] - before[0], 120.0, places=3)
        self.assertAlmostEqual(after[1] - before[1], -40.0, places=3)

    def test_manual_box_uses_the_same_geometry_as_the_ai_box(self):
        self.viewer.set_image(Image.new("RGB", (4000, 3000), "blue"), preserve_zoom=False)
        self.viewer.zoom_level = 3.0
        self.viewer.redraw(force_resize=True)
        self.root.update()

        self.viewer.set_detection_box((0.2, 0.2, 0.6, 0.6), manual_box=(0.2, 0.2, 0.6, 0.6))
        ai = self._rect_coords(self.viewer._ai_rect_id)
        manual = self._rect_coords(self.viewer._manual_rect_id)

        for a, b in zip(ai, manual):
            self.assertAlmostEqual(a, b, places=6)

    def test_eye_box_uses_the_same_geometry_as_the_subject_box(self):
        self.viewer.set_image(Image.new("RGB", (2400, 1800), "blue"), preserve_zoom=False)
        self.viewer.zoom_level = 4.0
        self.viewer.redraw(force_resize=True)
        self.root.update()

        self.viewer.set_detection_box((0.3, 0.3, 0.7, 0.7), ai_eye_box=(0.4, 0.4, 0.5, 0.5))
        ai = self._rect_coords(self.viewer._ai_rect_id)
        eye = self._rect_coords(self.viewer._ai_eye_rect_id)
        fit = self.viewer.current_fit()

        # The eye box sits inside the subject box, in the same frame.
        self.assertGreater(eye[0], ai[0])
        self.assertLess(eye[2], ai[2])
        self.assertGreater(eye[0], fit.left)

    def test_repeat_redraw_reuses_the_cached_render(self):
        """D14: every redraw used to resize from the full-resolution source."""
        self.viewer.set_image(Image.new("RGB", (3000, 2000), "blue"), preserve_zoom=False)
        self.viewer.redraw(force_resize=True)
        hits_after_first = self.viewer._pyramid.hits
        misses_after_first = self.viewer._pyramid.misses

        for _ in range(5):
            self.viewer.redraw(force_resize=True)

        self.assertEqual(self.viewer._pyramid.misses, misses_after_first,
                         "the same render size must not be recomputed")
        self.assertGreater(self.viewer._pyramid.hits, hits_after_first)

    def test_zoom_out_and_back_reuses_the_render(self):
        self.viewer.set_image(Image.new("RGB", (3000, 2000), "blue"), preserve_zoom=False)
        self.viewer.zoom_level = 1.0
        self.viewer.redraw(force_resize=True)
        first_size = (self.viewer._pyramid_hits_and_size()
                      if hasattr(self.viewer, "_pyramid_hits_and_size") else None)

        self.viewer.zoom_level = 2.0
        self.viewer.redraw(force_resize=True)
        misses_before = self.viewer._pyramid.misses

        self.viewer.zoom_level = 1.0
        self.viewer.redraw(force_resize=True)

        self.assertEqual(self.viewer._pyramid.misses, misses_before,
                         "returning to a previously rendered size must be a cache hit")

    def test_changing_the_image_drops_the_pyramid(self):
        first = Image.new("RGB", (1000, 800), "blue")
        self.viewer.set_image(first, preserve_zoom=False)
        self.viewer.redraw(force_resize=True)
        self.assertGreaterEqual(self.viewer._pyramid.level_count(), 1)
        key_before = next(iter(self.viewer._pyramid._levels))

        second = Image.new("RGB", (1000, 800), "red")
        self.viewer.set_image(second, preserve_zoom=False)

        self.assertIsNot(self.viewer._pyramid.source, first)
        self.assertIs(self.viewer._pyramid.source, second)
        for key in self.viewer._pyramid._levels:
            self.assertIsNot(key, key_before,
                             "levels cached for the previous photo must not serve the new one")

    def test_resize_is_coalesced(self):
        self.viewer.set_image(Image.new("RGB", (2000, 1500), "blue"), preserve_zoom=False)
        self.viewer.redraw(force_resize=True)

        misses_before = self.viewer._pyramid.misses
        for _ in range(20):
            self.viewer._on_geometry_change()

        self.assertIsNotNone(self.viewer._configure_after_id,
                             "a burst of Configure events must schedule one redraw")
        self.assertEqual(self.viewer._pyramid.misses, misses_before)
        self.root.update()
        self.assertIsNone(self.viewer._configure_after_id)

    def test_crop_percentages_round_trip_through_the_same_geometry(self):
        self.viewer.set_image(Image.new("RGB", (2000, 1000), "blue"), preserve_zoom=False)
        self.viewer.zoom_level = 1.0
        self.viewer.redraw(force_resize=True)
        self.root.update()

        self.viewer.enter_crop_mode()
        fit = self.viewer.current_fit()
        self.viewer.crop_box = (
            fit.canvas_from_normalized(0.2, 0.4)[0],
            fit.canvas_from_normalized(0.2, 0.4)[1],
            fit.canvas_from_normalized(0.6, 0.8)[0],
            fit.canvas_from_normalized(0.6, 0.8)[1],
        )
        percentages = self.viewer.get_crop_box_percentages()

        for actual, expected in zip(percentages, (0.2, 0.4, 0.6, 0.8)):
            self.assertAlmostEqual(actual, expected, places=6)

    def test_crop_confirmation_goes_through_the_same_helper(self):
        self.viewer.set_image(Image.new("RGB", (2000, 1000), "blue"), preserve_zoom=False)
        self.viewer.redraw(force_resize=True)
        self.root.update()

        captured = []
        self.viewer.enter_crop_mode(on_confirm_callback=lambda *p: captured.append(p))
        fit = self.viewer.current_fit()
        x1, y1 = fit.canvas_from_normalized(0.1, 0.1)
        x2, y2 = fit.canvas_from_normalized(0.9, 0.9)
        self.viewer.crop_box = (x1, y1, x2, y2)

        self.viewer._on_confirm_crop()

        self.assertEqual(len(captured), 1)
        for actual, expected in zip(captured[0], (0.1, 0.1, 0.9, 0.9)):
            self.assertAlmostEqual(actual, expected, places=6)

    def test_annotation_box_round_trips_to_source_pixels(self):
        img_w, img_h = 2000, 1000
        self.viewer.set_image(Image.new("RGB", (img_w, img_h), "blue"), preserve_zoom=False)
        self.viewer.zoom_level = 1.0
        self.viewer.redraw(force_resize=True)
        self.root.update()

        captured = []
        self.viewer.enter_anno_mode(
            "D:/Photos/A.JPG",
            on_save_callback=lambda path, w, h, s, e, img: captured.append((w, h, s, e)),
        )
        fit = self.viewer.current_fit()
        # A box over source pixels x 500..1500, y 0..400, expressed on the canvas.
        self.viewer.anno_subject_box_px = (
            fit.left + 500 * fit.scale,
            fit.top + 0 * fit.scale,
            fit.left + 1500 * fit.scale,
            fit.top + 400 * fit.scale,
        )

        self.viewer._on_confirm_anno()

        self.assertEqual(len(captured), 1)
        width, height, subject, _eye = captured[0]
        self.assertEqual((width, height), (img_w, img_h))
        self.assertIsNotNone(subject)
        self.assertAlmostEqual(subject[0], 500, delta=2)
        self.assertAlmostEqual(subject[1], 0, delta=2)
        self.assertAlmostEqual(subject[2], 1500, delta=2)
        self.assertAlmostEqual(subject[3], 400, delta=2)

    def test_current_fit_is_none_before_an_image_is_set(self):
        self.assertIsNone(self.viewer.current_fit())

    def test_current_fit_agrees_with_contain_fit(self):
        self.viewer.set_image(Image.new("RGB", (1600, 1200), "blue"), preserve_zoom=False)
        self.viewer.zoom_level = 1.7
        self.viewer.pan_x = 11.0
        self.root.update()

        expected = contain_fit(
            1600, 1200,
            self.viewer.canvas.winfo_width(), self.viewer.canvas.winfo_height(),
            zoom=self.viewer.zoom_level, pan_x=self.viewer.pan_x, pan_y=self.viewer.pan_y,
        )
        actual = self.viewer.current_fit()

        self.assertEqual((actual.width, actual.height), (expected.width, expected.height))
        self.assertAlmostEqual(actual.scale, expected.scale)
        self.assertAlmostEqual(actual.center_x, expected.center_x)


if __name__ == "__main__":
    unittest.main()