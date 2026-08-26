"""Draw the demo video's frames: a plain lecture slide with a moving average.

The first cut of the demo used an FFmpeg gradient, which extracted correctly and
looked exactly like a broken image in the frame reviewer. The reviewer is the
screen that has to show a *picture* next to the words, so the picture has to be
worth looking at.

Deliberately restrained, and in the project's own palette. This is a stand-in
for somebody's lecture recording, not a thing to admire.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720
INK = (32, 30, 29)
PAPER = (243, 242, 242)
GRID = (215, 211, 211)
MUTED = (96, 93, 93)
ACCENT = (236, 48, 19)
SLOW = (96, 93, 93)

BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
REG = "/System/Library/Fonts/Supplemental/Arial.ttf"


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def series(n: int) -> list[float]:
    """A price-ish walk. Deterministic, so every render is identical."""
    return [50 + 18 * math.sin(i / 7.0) + 9 * math.sin(i / 2.3 + 1.1) + 0.22 * i for i in range(n)]


def moving_average(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        if i + 1 < window:
            out.append(None)
        else:
            out.append(sum(values[i + 1 - window : i + 1]) / window)
    return out


def draw_frame(index: int, total: int, out: Path) -> None:
    revealed = int(24 + (index / max(1, total - 1)) * 96)
    prices = series(120)
    fast = moving_average(prices, 6)
    slow = moving_average(prices, 20)

    image = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(image)

    d.text((72, 58), "Moving averages", font=font(BOLD, 46), fill=INK)
    d.text(
        (72, 118),
        "A summary of what already happened — not a forecast",
        font=font(REG, 26),
        fill=MUTED,
    )
    d.line([(72, 168), (W - 72, 168)], fill=INK, width=3)

    left, right, top, bottom = 96, W - 96, 214, H - 118
    lo, hi = min(prices) - 6, max(prices) + 6

    def xy(i: int, v: float) -> tuple[float, float]:
        x = left + (i / (len(prices) - 1)) * (right - left)
        y = bottom - ((v - lo) / (hi - lo)) * (bottom - top)
        return x, y

    for step in range(5):
        y = top + step * (bottom - top) / 4
        d.line([(left, y), (right, y)], fill=GRID, width=1)

    def polyline(values: list[float | None], colour, width: int) -> None:
        pts = [xy(i, v) for i, v in enumerate(values[:revealed]) if v is not None]
        if len(pts) > 1:
            d.line(pts, fill=colour, width=width, joint="curve")

    polyline([float(p) for p in prices], GRID, 2)
    polyline(slow, SLOW, 3)
    polyline(fast, ACCENT, 4)

    if revealed > 20 and fast[revealed - 1] is not None:
        x, y = xy(revealed - 1, float(fast[revealed - 1]))
        d.ellipse([x - 7, y - 7, x + 7, y + 7], fill=ACCENT)

    d.text((left, H - 92), "6-period", font=font(BOLD, 22), fill=ACCENT)
    d.text((left + 118, H - 92), "20-period", font=font(BOLD, 22), fill=SLOW)
    d.text(
        (right - 250, H - 92),
        f"bar {revealed:>3} of {len(prices)}",
        font=font(REG, 22),
        fill=MUTED,
    )

    image.save(out, quality=92)


def main() -> None:
    here = Path(__file__).parent / "frames"
    here.mkdir(exist_ok=True)
    total = 29 * 12  # 29 seconds at 12 fps
    for i in range(total):
        draw_frame(i, total, here / f"{i:05d}.jpg")
    print(f"  wrote {total} frames to {here}")


if __name__ == "__main__":
    main()
