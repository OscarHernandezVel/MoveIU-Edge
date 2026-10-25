"""Detector of the yellow LEGO minifigures of the model bus, by colour: no model, no training, a few ms per frame.

A person detector trained on photos (YOLOv8n) does not recognise a minifigure reliably (docs/lego.md). The model's
passengers, though, all have the same LEGO bright yellow head, a colour nothing else in the bus has: MDF and seats
are brown (hue below 20 on OpenCV's 0-180 scale) and torsos are not yellow (use other colours for them). So:

1. HSV threshold on yellow; closing fills the printed eyes and smile, opening removes specks;
2. each connected blob is a candidate; blobs much smaller than a nearby bigger one just above them are its hands;
3. a blob much wider than tall is two heads side by side and is split.

Each detection is the box of a **head**: :attr:`anchor_height` 0.5 puts the anchor at the head's centre, and the
layout must be drawn at head height (what :func:`bus_edge.vision.scene.projected_layout` does).

Lighting changes the thresholds: ``EDGE_LEGO_HSV=hue_min,hue_max,saturation_min,value_min`` (default 20,35,150,90).
Check them on the real model with ``python -m bus_edge check`` and adjust; the defaults were tuned on the simulation.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from bus_edge.vision.detector import Detection, Detector

DEFAULT_HSV = (20, 35, 150, 90)


def parse_hsv(text: str) -> tuple[int, int, int, int]:
    values = tuple(int(part) for part in text.split(","))
    if len(values) != 4 or not (0 <= values[0] < values[1] <= 180) or not all(0 <= v <= 255 for v in values[2:]):
        raise ValueError("EDGE_LEGO_HSV must be hue_min,hue_max,saturation_min,value_min, e.g. 20,35,150,90")
    return values  # type: ignore[return-value]


class YellowFigureDetector(Detector):
    version = "lego-yellow-1"
    anchor_height = 0.5

    def __init__(self, hsv: tuple[int, int, int, int] = DEFAULT_HSV, min_area_px: int = 10) -> None:
        self._low = np.array([hsv[0], hsv[2], hsv[3]], dtype=np.uint8)
        self._high = np.array([hsv[1], 255, 255], dtype=np.uint8)
        self._min_area = min_area_px

    def mask(self, frame: Any) -> np.ndarray:
        import cv2

        mask = cv2.inRange(cv2.cvtColor(frame, cv2.COLOR_BGR2HSV), self._low, self._high)
        # Opening only: a closing would also weld the heads of neighbouring rows, which in the distance are 1-2 px
        # apart. The printed face leaves holes inside a head, not a cut through it.
        return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))

    def detect(self, frame: Any) -> list[Detection]:
        import cv2

        mask = self.mask(frame)
        count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
        blobs = [tuple(int(v) for v in stats[i]) for i in range(1, count) if stats[i][4] >= self._min_area]
        heads = [b for b in blobs if not _is_hand(b, blobs)]
        height, width = mask.shape
        found: list[Detection] = []
        for x, y, w, h, area in heads:
            # A head (with its stud) is about as wide as tall. A blob much wider is heads side by side; much taller,
            # heads of consecutive rows stacked in the distance.
            across = max(1, round(w / h)) if w > 1.5 * h else 1
            stacked = max(1, round(h / (1.1 * w))) if h > 1.6 * w else 1
            score = min(0.99, 0.5 + 0.5 * area / max(1, w * h))
            for i in range(across):
                for j in range(stacked):
                    x0, x1 = x + w * i / across, x + w * (i + 1) / across
                    y0, y1 = y + h * j / stacked, y + h * (j + 1) / stacked
                    found.append(Detection(x0 / width, y0 / height, x1 / width, y1 / height, score))
        return found


def _is_hand(blob: tuple[int, ...], blobs: list[tuple[int, ...]]) -> bool:
    """A hand is a small blob just below and beside a head several times its area."""
    x, y, w, h, area = blob
    cx, cy = x + w / 2, y + h / 2
    for ox, oy, ow, oh, oarea in blobs:
        if oarea < 2.2 * area:
            continue
        ocx, ocy = ox + ow / 2, oy + oh / 2
        if 0 < cy - ocy <= 3.5 * oh and abs(cx - ocx) <= 2.5 * ow:
            return True
    return False
