"""
Generate the Quick Cull application icon.

Draws the mark procedurally (so the source of truth is code, not a binary blob)
and writes:

    media/quick_cull.ico              multi-resolution Windows icon (16..256)
    media/quick_cull_icon.png         256px master (docs and other surfaces)
    media/quick_cull_splash_icon.png  96px variant drawn by the splash screen

Usage (from project root):
    python build_scripts/make_icon.py
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
MEDIA_DIR = ROOT / "media"
ICO_PATH = MEDIA_DIR / "quick_cull.ico"
PNG_PATH = MEDIA_DIR / "quick_cull_icon.png"
SPLASH_PNG_PATH = MEDIA_DIR / "quick_cull_splash_icon.png"

SIZE = 1024
SS = 2  # supersampling factor for smooth edges
SPLASH_SIZE = 96

BG = (18, 18, 18, 255)
BLADE = (47, 128, 237, 255)
BLADE_DARK = (31, 83, 141, 255)
CHECK = (255, 255, 255, 255)

ICO_SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]


def rounded_rect_mask(size, radius, supersample):
    """Return an 'L' mask of a rounded square at supersampled resolution."""
    mask = Image.new("L", (size * supersample, size * supersample), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size * supersample - 1, size * supersample - 1],
        radius=int(radius * supersample),
        fill=255,
    )
    return mask


def draw_aperture(canvas, center, radius, blades=6):
    """Draw a camera iris: `blades` rounded blades around a hexagonal opening."""
    blade_len = radius * 1.02
    blade_thick = radius * 0.44
    ring = radius * 0.60  # blade centerline distance from the middle

    for i in range(blades):
        angle = i * (360.0 / blades)
        color = BLADE if i % 2 == 0 else BLADE_DARK

        blade = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(blade)
        cx, cy = center
        d.rounded_rectangle(
            [
                cx - blade_len / 2,
                cy - ring - blade_thick / 2,
                cx + blade_len / 2,
                cy - ring + blade_thick / 2,
            ],
            radius=blade_thick * 0.30,
            fill=color,
        )
        blade = blade.rotate(-angle, resample=Image.BICUBIC, center=center)
        canvas.alpha_composite(blade)

    # Hexagonal opening in the middle of the iris.
    hole = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    hd = ImageDraw.Draw(hole)
    pts = []
    for i in range(blades):
        a = math.radians(i * (360.0 / blades) - 90)
        pts.append((center[0] + radius * 0.40 * math.cos(a),
                    center[1] + radius * 0.40 * math.sin(a)))
    hd.polygon(pts, fill=BG)
    canvas.alpha_composite(hole)


def draw_check(canvas, center, size, width):
    """Draw a rounded white checkmark centered on `center`."""
    stroke = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(stroke)
    x, y = center
    d.line(
        [(x - size * 0.42, y + size * 0.02),
         (x - size * 0.12, y + size * 0.32),
         (x + size * 0.44, y - size * 0.34)],
        fill=CHECK,
        width=int(width),
        joint="curve",
    )
    r = width / 2.0
    for px, py in ((x - size * 0.42, y + size * 0.02),
                   (x - size * 0.12, y + size * 0.32),
                   (x + size * 0.44, y - size * 0.34)):
        d.ellipse([px - r, py - r, px + r, py + r], fill=CHECK)
    canvas.alpha_composite(stroke)


def build_master() -> Image.Image:
    big = SIZE * SS
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))

    card = Image.new("RGBA", (big, big), BG)
    card.putalpha(rounded_rect_mask(SIZE, SIZE * 0.20, SS))
    canvas.alpha_composite(card)

    center = (big / 2, big / 2)
    draw_aperture(canvas, center, big * 0.31)
    draw_check(canvas, center, big * 0.27, big * 0.056)

    return canvas.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    master = build_master()

    master.save(ICO_PATH, format="ICO", sizes=ICO_SIZES)
    master.resize((256, 256), Image.LANCZOS).save(PNG_PATH, format="PNG")
    # The splash renders at native size, so it needs a matching asset.
    master.resize((SPLASH_SIZE, SPLASH_SIZE), Image.LANCZOS).save(SPLASH_PNG_PATH, format="PNG")

    print(f"Wrote {ICO_PATH}")
    print(f"Wrote {PNG_PATH}")
    print(f"Wrote {SPLASH_PNG_PATH}")


if __name__ == "__main__":
    main()
