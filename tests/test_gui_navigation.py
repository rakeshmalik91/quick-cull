import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from gui import ImageCullerApp


class TestFastPreviewGuard(unittest.TestCase):
    """
    The fast preview is decoded on a worker thread and applied on the UI thread, so the
    request id must be re-checked when the callback actually runs. Otherwise a preview of
    photo N lands after photo N+1 was displayed and stays there until N+1 finishes decoding.
    """

    def _make_app(self, request_id=7):
        app = MagicMock()
        app._load_request_id = request_id
        app.viewer = MagicMock()
        app._apply_fast_preview = lambda img, req_id: ImageCullerApp._apply_fast_preview(app, img, req_id)
        return app

    def test_applies_preview_for_current_request(self):
        app = self._make_app(request_id=7)
        img = Image.new("RGB", (8, 8))

        self.assertTrue(ImageCullerApp._apply_fast_preview(app, img, 7))
        app.viewer.set_image.assert_called_once_with(img, preserve_zoom=True)

    def test_drops_preview_from_superseded_request(self):
        app = self._make_app(request_id=8)
        img = Image.new("RGB", (8, 8))

        self.assertFalse(ImageCullerApp._apply_fast_preview(app, img, 7))
        app.viewer.set_image.assert_not_called()

    def test_older_preview_after_newer_image_is_dropped(self):
        """Regression for the exact race: req 7 preview queued, req 8 displayed first."""
        app = self._make_app(request_id=8)
        newer = Image.new("RGB", (16, 16), "blue")

        # req 8's full image is already on screen.
        ImageCullerApp._apply_fast_preview(app, newer, 8)
        # req 7's late fast preview arrives and must not overwrite it.
        ImageCullerApp._apply_fast_preview(app, Image.new("RGB", (8, 8), "red"), 7)

        app.viewer.set_image.assert_called_once_with(newer, preserve_zoom=True)


if __name__ == "__main__":
    unittest.main()