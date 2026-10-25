"""Person detector for the edge (RF-12.2): YOLOv8n exported to ONNX, run with ONNX Runtime on the CPU of the on-board
computer (the Windows laptop of the model bus, or a Raspberry Pi).

Pre-processing letterboxes the frame to ``input_size`` (640 px on the laptop; 320 px keeps a Raspberry Pi 4 near
5 fps); the YOLOv8 output ``(1, 4 + classes, N)`` is decoded, filtered by score and reduced with non-maximum
suppression written in NumPy (no extra dependency). Only the ``person`` class (COCO id 0) is kept.

Export the model once on a PC: ``pip install ultralytics`` then
``yolo export model=yolov8n.pt format=onnx imgsz=640`` and copy ``yolov8n.onnx`` to ``edge/models/``. An exported
model has a fixed input size: when it differs from ``EDGE_MODEL_INPUT`` the model's own size is used (with a warning).
Licence: Ultralytics YOLOv8 is AGPL-3.0; a commercial product needs their enterprise licence or another
detector (the :class:`Detector` interface makes the swap a one-class change).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

PERSON = 0
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Detection:
    """A box in normalised image coordinates (0-1) with its score."""

    x1: float
    y1: float
    x2: float
    y2: float
    score: float
    label: int = PERSON

    def anchor(self, height_fraction: float = 0.8) -> tuple[float, float]:
        """Point that decides which position the person occupies: horizontally centred, near the bottom
        of the box (the hips of a seated person, the feet area of a standing one, seen from above)."""
        return ((self.x1 + self.x2) / 2.0, self.y1 + (self.y2 - self.y1) * height_fraction)


class Detector(ABC):
    version = "none"
    #: Where the point that picks a position sits in a box: near the bottom of a person's box (0.8); at the centre of
    #: a head's box for detectors that only box heads (0.5). The layout is drawn at that height.
    anchor_height = 0.8

    @abstractmethod
    def detect(self, frame: Any) -> list[Detection]:
        """People in ``frame`` (a BGR ``numpy`` image). The frame is not kept."""


class NullDetector(Detector):
    """No model yet: nobody is detected (the door count still works)."""

    version = "no-model"

    def detect(self, frame: Any) -> list[Detection]:
        return []


def letterbox(frame: np.ndarray, size: int) -> tuple[np.ndarray, float, int, int]:
    """Resize keeping the aspect ratio and pad to ``size`` x ``size`` (grey 114, as in YOLO training)."""
    import cv2

    height, width = frame.shape[:2]
    scale = min(size / width, size / height)
    new_w, new_h = round(width * scale), round(height * scale)
    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    pad_x, pad_y = (size - new_w) // 2, (size - new_h) // 2
    canvas[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = resized
    return canvas, scale, pad_x, pad_y


def nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    """Greedy non-maximum suppression on ``(n, 4)`` xyxy boxes; returns kept indices, best first."""
    order = scores.argsort()[::-1]
    keep: list[int] = []
    areas = (boxes[:, 2] - boxes[:, 0]).clip(0) * (boxes[:, 3] - boxes[:, 1]).clip(0)
    while order.size > 0:
        best = int(order[0])
        keep.append(best)
        rest = order[1:]
        xx1 = np.maximum(boxes[best, 0], boxes[rest, 0])
        yy1 = np.maximum(boxes[best, 1], boxes[rest, 1])
        xx2 = np.minimum(boxes[best, 2], boxes[rest, 2])
        yy2 = np.minimum(boxes[best, 3], boxes[rest, 3])
        inter = (xx2 - xx1).clip(0) * (yy2 - yy1).clip(0)
        iou = inter / (areas[best] + areas[rest] - inter + 1e-9)
        order = rest[iou <= iou_threshold]
    return keep


def decode_yolov8(
    output: np.ndarray,
    scale: float,
    pad_x: int,
    pad_y: int,
    width: int,
    height: int,
    conf_threshold: float,
    iou_threshold: float,
    classes: tuple[int, ...] = (PERSON,),
) -> list[Detection]:
    """YOLOv8 head ``(1, 4 + C, N)`` -> detections in normalised coordinates of the original frame."""
    predictions = output[0].T  # (N, 4 + C)
    class_scores = predictions[:, 4:]
    labels = class_scores.argmax(axis=1)
    scores = class_scores[np.arange(len(labels)), labels]
    mask = (scores >= conf_threshold) & np.isin(labels, classes)
    if not mask.any():
        return []
    cx, cy, w, h = (predictions[mask, i] for i in range(4))
    boxes = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)
    boxes[:, [0, 2]] = (boxes[:, [0, 2]] - pad_x) / scale / width
    boxes[:, [1, 3]] = (boxes[:, [1, 3]] - pad_y) / scale / height
    boxes = boxes.clip(0.0, 1.0)
    kept_scores, kept_labels = scores[mask], labels[mask]
    return [
        Detection(
            float(boxes[i, 0]),
            float(boxes[i, 1]),
            float(boxes[i, 2]),
            float(boxes[i, 3]),
            float(kept_scores[i]),
            int(kept_labels[i]),
        )
        for i in nms(boxes, kept_scores, iou_threshold)
    ]


def model_input_size(shape: Any, configured: int) -> int:
    """The square input of an exported model (``[1, 3, 640, 640]``), or ``configured`` when it is dynamic (names
    instead of numbers). A model exported at 320 cannot run at 640, nor the other way round."""
    height, width = (shape[2], shape[3]) if len(shape) == 4 else (None, None)
    if isinstance(height, int) and height == width:
        if height != configured:
            log.warning("The model was exported at %s px: using %s instead of EDGE_MODEL_INPUT=%s", height, height,
                        configured)  # fmt: skip
        return height
    return configured


class YoloOnnxDetector(Detector):
    def __init__(
        self,
        model_path: str | Path,
        input_size: int = 320,
        conf_threshold: float = 0.4,
        iou_threshold: float = 0.45,
        threads: int = 3,
        classes: tuple[int, ...] = (PERSON,),
    ) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        self._session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        model_input = self._session.get_inputs()[0]
        self._input = model_input.name
        self._size = model_input_size(model_input.shape, input_size)
        self._conf = conf_threshold
        self._iou = iou_threshold
        self._classes = classes
        self.version = f"yolov8n-onnx-{self._size}"

    def detect(self, frame: Any) -> list[Detection]:
        height, width = frame.shape[:2]
        canvas, scale, pad_x, pad_y = letterbox(frame, self._size)
        tensor = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0  # BGR -> RGB, NCHW
        del canvas
        output = self._session.run(None, {self._input: tensor})[0]
        return decode_yolov8(output, scale, pad_x, pad_y, width, height, self._conf, self._iou, self._classes)
