import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tkinter as tk
from culler.gui.tooltip import ToolTip


class TestToolTip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
            cls.root.update()
        except Exception:
            cls.root = None

    @classmethod
    def tearDownClass(cls):
        if cls.root is not None:
            try:
                cls.root.destroy()
            except Exception:
                pass
            cls.root = None

    def setUp(self):
        if self.root is None:
            self.skipTest("Tkinter display not available")
        self.btn = tk.Button(self.root, text="Test Button")
        self.btn.pack()
        self.root.update()

    def tearDown(self):
        try:
            self.btn.destroy()
        except Exception:
            pass

    def test_tooltip_init_and_bindings(self):
        tip = ToolTip(self.btn, "Help text")
        self.assertEqual(tip.text, "Help text")
        self.assertIsNone(tip.tooltip_window)
        self.assertIsNone(tip._timer_id)

    def test_tooltip_schedule_and_cancel(self):
        tip = ToolTip(self.btn, "Help text", delay_ms=100)
        tip._schedule()
        self.assertIsNotNone(tip._timer_id)
        tip._cancel()
        self.assertIsNone(tip._timer_id)

    def test_tooltip_show_and_hide(self):
        tip = ToolTip(self.btn, "Help text", delay_ms=10)
        tip._show()
        self.assertIsNotNone(tip.tooltip_window)
        self.assertTrue(tip.tooltip_window.winfo_exists())

        tip._hide()
        self.assertIsNone(tip.tooltip_window)

    def test_tooltip_clamped_on_right_edge(self):
        # Mock widget to be located near the right edge of screen
        tip = ToolTip(self.btn, "Very Long Help Tooltip Text Here", delay_ms=10)

        with patch.object(self.btn, "winfo_rootx", return_value=1900), \
             patch.object(self.btn, "winfo_rooty", return_value=50), \
             patch.object(self.btn, "winfo_width", return_value=50), \
             patch.object(self.btn, "winfo_height", return_value=30):
            tip._show()
            self.assertIsNotNone(tip.tooltip_window)
            tw = tip.tooltip_window

            # Window rootx + reqwidth must not exceed screenwidth
            screen_w = tw.winfo_screenwidth()
            tip_w = tw.winfo_reqwidth()
            # Geometry format is +x+y
            geom = tw.wm_geometry()
            # Extract x from geometry e.g. "106x27+1750+86" or "+1750+86"
            parts = geom.split("+")
            x = int(parts[1])
            self.assertLessEqual(x + tip_w, screen_w)
            tip._hide()

    def test_tooltip_clamped_on_left_edge(self):
        tip = ToolTip(self.btn, "Tooltip Text", delay_ms=10)

        with patch.object(self.btn, "winfo_rootx", return_value=-50), \
             patch.object(self.btn, "winfo_rooty", return_value=50), \
             patch.object(self.btn, "winfo_width", return_value=20), \
             patch.object(self.btn, "winfo_height", return_value=30):
            tip._show()
            self.assertIsNotNone(tip.tooltip_window)
            tw = tip.tooltip_window
            geom = tw.wm_geometry()
            parts = geom.split("+")
            x = int(parts[1])
            self.assertGreaterEqual(x, 8)
            tip._hide()

    def test_tooltip_flips_above_when_near_bottom(self):
        tip = ToolTip(self.btn, "Tooltip Text", delay_ms=10)

        with patch.object(self.btn, "winfo_rootx", return_value=500), \
             patch.object(self.btn, "winfo_rooty", return_value=1050), \
             patch.object(self.btn, "winfo_width", return_value=50), \
             patch.object(self.btn, "winfo_height", return_value=30):
            tip._show()
            self.assertIsNotNone(tip.tooltip_window)
            tw = tip.tooltip_window
            geom = tw.wm_geometry()
            parts = geom.split("+")
            y = int(parts[2])
            # It should flip above widget_ry (1050)
            self.assertLess(y, 1050)
            tip._hide()


if __name__ == "__main__":
    unittest.main()
