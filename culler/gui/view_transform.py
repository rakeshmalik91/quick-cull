"""Image-to-canvas geometry for the viewer, in one place.

The viewer used to recompute the contain-fit transform inline in five different
places (``redraw``, ``_draw_detection_rect``, ``_on_confirm_crop``,
``get_crop_box_percentages``, ``_on_confirm_anno``). Only ``redraw`` applied the
3500 px render cap, so the detection boxes were drawn against an *uncapped* scale
while the image on screen was capped: at high zoom the boxes drifted away from the
subject they were supposed to mark.

Everything here is pure geometry over sizes, so it is testable without a Tk window.
"""

from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional, Tuple

from PIL import Image

#: Longest edge of a rendered frame. Bounds the per-redraw resize cost and the size
#: of the Tk image, at the price of showing a slightly soft frame past this zoom.
MAX_RENDER_DIM = 3500


@dataclass(frozen=True)
class FitResult:
    """Where an image lands on the canvas, and how to map between the two spaces.

    ``width``/``height`` are the dimensions actually rendered (after :data:`MAX_RENDER_DIM`),
    and ``scale`` is the effective image-pixel-to-canvas-pixel factor that produced
    them, so every consumer of this object agrees on the same geometry.
    """

    width: int
    height: int
    scale: float
    center_x: float
    center_y: float

    @property
    def left(self) -> float:
        return self.center_x - (self.width / 2.0)

    @property
    def top(self) -> float:
        return self.center_y - (self.height / 2.0)

    def canvas_from_normalized(self, nx: float, ny: float) -> Tuple[float, float]:
        """Map a 0..1 box coordinate in image space onto the canvas."""
        return (self.left + nx * self.width, self.top + ny * self.height)

    def normalized_from_canvas(self, x: float, y: float) -> Tuple[float, float]:
        """Map a canvas point back to a 0..1 box coordinate in image space."""
        if self.width <= 0 or self.height <= 0:
            return (0.0, 0.0)
        return ((x - self.left) / self.width, (y - self.top) / self.height)

    def image_from_canvas(self, x: float, y: float, img_w: int, img_h: int) -> Tuple[float, float]:
        """Map a canvas point to a pixel coordinate in the *source* image."""
        if self.scale <= 0:
            return (0.0, 0.0)
        return ((x - self.left) / self.scale, (y - self.top) / self.scale)


def contain_fit(
    img_w: int,
    img_h: int,
    canvas_w: int,
    canvas_h: int,
    zoom: float = 1.0,
    pan_x: float = 0.0,
    pan_y: float = 0.0,
    max_dim: int = MAX_RENDER_DIM,
) -> Optional[FitResult]:
    """Aspect-fit ``img`` into the canvas at ``zoom``, applying the render cap.

    Returns ``None`` when either surface has no usable size yet (a widget that has not
    been laid out), which is the caller's cue to skip drawing rather than divide by ~0.
    """
    if img_w <= 0 or img_h <= 0 or canvas_w <= 10 or canvas_h <= 10:
        return None

    scale = min(canvas_w / img_w, canvas_h / img_h) * zoom
    if scale <= 0:
        return None

    width = max(1, int(img_w * scale))
    height = max(1, int(img_h * scale))

    if max_dim > 0 and (width > max_dim or height > max_dim):
        shrink = min(max_dim / width, max_dim / height)
        width = max(1, int(width * shrink))
        height = max(1, int(height * shrink))
        # Derive the scale from what is really on screen, so overlays land on it.
        scale = width / float(img_w)

    return FitResult(
        width=width,
        height=height,
        scale=scale,
        center_x=(canvas_w / 2.0) + pan_x,
        center_y=(canvas_h / 2.0) + pan_y,
    )


class RenderPyramid:
    """Bounded cache of resized renders of one image, reused across redraws.

    Every redraw used to resize straight from the source image, so zooming in and out
    re-ran a full-resolution resize per event even though the sizes repeat. This keeps
    the sizes that were already rendered and derives a new size from the smallest
    cached render that is still at least as large as the target, which is roughly an
    order of magnitude less pixel work than resampling the full-resolution source.

    Entries are shared, not copied: callers must treat the returned image as
    read-only. Only :class:`~culler.gui.canvas_viewer.ImageCanvasViewer` uses it, and
    it hands the image straight to ``ImageTk.PhotoImage``.
    """

    #: A wheel gesture from fit to max zoom produces ~13 distinct NEAREST sizes plus a
    #: final BILINEAR one, so the level cap has to cover a whole gesture or a
    #: zoom-in-then-out would evict everything it is about to reuse.
    MAX_LEVELS = 12
    MAX_BYTES = 96 * 1024 * 1024

    def __init__(self, max_levels: int = MAX_LEVELS, max_bytes: int = MAX_BYTES):
        self.max_levels = max_levels
        self.max_bytes = max_bytes
        self._source: Optional[Image.Image] = None
        self._levels: "OrderedDict[Tuple[int, int, int], Image.Image]" = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0

    def set_source(self, image: Optional[Image.Image]) -> None:
        """Point the pyramid at a new image; every cached render is dropped."""
        self._source = image
        self.clear()

    def clear(self) -> None:
        self._levels.clear()
        self._bytes = 0

    @property
    def source(self) -> Optional[Image.Image]:
        return self._source

    def level_count(self) -> int:
        return len(self._levels)

    @staticmethod
    def _bytes_of(img: Image.Image) -> int:
        width, height = img.size
        return int(width) * int(height) * (len(img.getbands()) or 3)

    def _best_source(self, key: Tuple[int, int, int]) -> Image.Image:
        """Smallest already-rendered image that can be downscaled into ``key``."""
        best: Optional[Image.Image] = None
        best_area = None
        for cached_key, img in self._levels.items():
            if cached_key[2] != key[2]:
                continue
            if img.size[0] >= key[0] and img.size[1] >= key[1]:
                area = img.size[0] * img.size[1]
                if best_area is None or area < best_area:
                    best, best_area = img, area
        if best is not None:
            return best
        if self._source is None:
            raise ValueError("RenderPyramid has no source image")
        return self._source

    def render(
        self,
        size: Tuple[int, int],
        resample: int = Image.Resampling.BILINEAR,
    ) -> Image.Image:
        """Return the render at exactly ``size``, computing it only if needed."""
        if self._source is None:
            raise ValueError("RenderPyramid has no source image")

        width = max(1, int(size[0]))
        height = max(1, int(size[1]))
        key = (width, height, int(resample))

        cached = self._levels.get(key)
        if cached is not None:
            self._levels.move_to_end(key)
            self.hits += 1
            return cached

        self.misses += 1
        source = self._best_source(key)
        if source.size == (width, height):
            rendered = source
        else:
            rendered = source.resize((width, height), resample)
            try:
                rendered.load()
            except Exception:
                pass

        self._levels[key] = rendered
        self._levels.move_to_end(key)
        self._bytes += self._bytes_of(rendered)
        self._evict()
        return rendered

    def _evict(self) -> None:
        while self._levels and (self._bytes > self.max_bytes or len(self._levels) > self.max_levels):
            _, evicted = self._levels.popitem(last=False)
            self._bytes -= self._bytes_of(evicted)
        self._bytes = max(0, self._bytes)