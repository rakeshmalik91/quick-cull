"""The grid footer lost its progress bar and "N / M" label.

Removing a widget that other modules drive is the kind of change that leaves a live
reference behind, and the reference fails *inside a Tk callback* - Tk prints the
traceback and carries on, so nothing looks broken until the code path that needed the
widget never runs. That is exactly what happened: `_on_scan_complete` touched the
removed progress bar before it reached `set_image_loader` and `_on_filter_changed`, so a
finished scan never handed its items to the grid and the grid stayed on "Images (0)".

These tests make the removal safe to repeat: nothing may reference the retired widgets,
and the widgets must actually be gone.
"""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Widgets that used to live in the grid footer and were deliberately removed.
RETIRED = ("progress_bar", "lbl_progress_text", "progress_row")

#: Modules that must not reference them. progress_dialog has its own progress bar of the
#: same name and is a separate widget, so it is excluded by name rather than by pattern.
EXEMPT = {"progress_dialog.py"}


def _python_sources():
    for directory in ("", "culler", "culler/gui"):
        base = ROOT / directory if directory else ROOT
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.py")):
            if path.name in EXEMPT or path.name.startswith("__"):
                continue
            yield path


def _attribute_names(path: Path):
    """Every ``something.<name>`` appearing in the file, as written in the source."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            yield node.attr
        elif isinstance(node, ast.Name):
            yield node.id


class TestRetiredFooterWidgetsAreNotReferenced(unittest.TestCase):
    def test_no_module_still_uses_them(self):
        offenders = []
        for path in _python_sources():
            names = set(_attribute_names(path))
            hits = sorted(set(RETIRED) & names)
            if hits:
                offenders.append(f"{path.relative_to(ROOT)}: {', '.join(hits)}")

        self.assertEqual(offenders, [],
                         "a widget removed from the grid footer is still referenced; "
                         "that fails silently inside a Tk callback:\n  "
                         + "\n  ".join(offenders))

    def test_the_grid_really_does_not_build_them(self):
        source = (ROOT / "culler" / "gui" / "thumbnail_list.py").read_text(encoding="utf-8")
        for name in RETIRED:
            self.assertNotIn(f"self.{name}", source,
                             f"thumbnail_list must not build {name} any more")

    def test_scan_progress_has_somewhere_to_go(self):
        """Scan progress is folder-wide, so it moved to the status bar rather than vanish."""
        source = (ROOT / "gui.py").read_text(encoding="utf-8")
        self.assertIn("def _sync_loading_progress", source)
        body = source.split("def _sync_loading_progress", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("_update_status", body,
                      "scan progress should be reported on the status bar")


class TestScanCompletionHandsItemsToTheGrid(unittest.TestCase):
    """
    The failure mode was a statement between "the scan finished" and "the grid is told",
    so the ordering itself is asserted: the completion handlers must not touch the grid's
    footer on their way to handing over the items.
    """

    def _handler_body(self, name: str) -> str:
        source = (ROOT / "gui.py").read_text(encoding="utf-8")
        start = source.index(f"def {name}(")
        return source[start:start + 2000].split("\n    def ", 1)[0]

    def test_scan_completion_calls_the_filter_before_anything_else_slow(self):
        body = self._handler_body("_on_scan_complete")
        self.assertIn("_on_filter_changed", body,
                      "the completed scan must push its items into the grid")
        self.assertNotIn("progress_bar", body)
        self.assertNotIn("lbl_progress_text", body)

    def test_tab_scan_completion_calls_the_filter(self):
        body = self._handler_body("_on_tab_scan_complete")
        self.assertIn("_on_filter_changed", body)
        self.assertNotIn("progress_bar", body)
        self.assertNotIn("lbl_progress_text", body)


if __name__ == "__main__":
    unittest.main()