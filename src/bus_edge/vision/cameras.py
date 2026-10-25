"""Camera workers: grab a frame, run the detector, keep only the boxes, discard the frame (PB-N07).

Privacy by design (RNF-7.1, RNF-7.2): a frame lives only inside :meth:`CameraWorker._cycle`; it is never
written to disk, logged, queued or sent. What leaves the worker is a :class:`CameraObservation` with boxes
and scores. The calibration tool is the only code that shows images, on the local screen, on demand.

A frame is *usable* only if it exists and is not too dark or washed out (mean brightness between 20 and
235): a covered lens or a camera facing a lamp must lower the confidence, not report an empty bus.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from bus_edge.vision.detector import Detector
from bus_edge.vision.occupancy import CameraObservation

log = logging.getLogger(__name__)
MIN_BRIGHTNESS, MAX_BRIGHTNESS = 20.0, 235.0


def usable(frame: Any) -> bool:
    if frame is None or getattr(frame, "size", 0) == 0:
        return False
    mean = float(frame.mean())
    return MIN_BRIGHTNESS <= mean <= MAX_BRIGHTNESS


def open_capture(device: int | str, width: int = 640, height: int = 480) -> Any:
    """A webcam (its index, or ``/dev/videoN`` on Linux) or a recorded video, opened the same way by the agent, the
    hardware check and the calibration tool: the polygons are drawn on exactly the image the detector sees.

    Low resolution on purpose: 640x480 is plenty for a 72 cm model. MJPEG keeps two webcams within the bandwidth of
    the USB ports. On Windows DirectShow is used: the default backend (Media Foundation) takes seconds to open a
    camera and ignores the buffer size, and the indices it gives can differ from the ones ``check`` printed."""
    import cv2

    if not isinstance(device, int):
        return cv2.VideoCapture(device)  # a video file (or a device path): it keeps its own format
    capture = cv2.VideoCapture(device, cv2.CAP_DSHOW) if sys.platform == "win32" else cv2.VideoCapture(device)
    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # before the size: DirectShow needs it first
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # always the newest frame, never a stale queue
    return capture


class OpenCvCamera:
    """A USB webcam, or a recorded video played in a loop (to rehearse without the cameras)."""

    def __init__(self, device: int | str, width: int = 640, height: int = 480) -> None:
        self._capture = open_capture(device, width, height)
        self._is_file = not isinstance(device, int)

    def read(self) -> Any:
        ok, frame = self._capture.read()
        if not ok and self._is_file:  # end of the video: start over
            import cv2

            self._capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._capture.read()
        return frame if ok else None

    def close(self) -> None:
        self._capture.release()


class CameraWorker:
    def __init__(
        self,
        name: str,
        open_camera: Callable[[], Any],
        detector: Detector,
        fps: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._open = open_camera
        self._detector = detector
        self._period = 1.0 / max(0.5, fps)
        self._clock = clock
        self._lock = threading.Lock()
        self._latest = CameraObservation(False)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._camera: Any = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"camera-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def latest(self) -> CameraObservation:
        with self._lock:
            return self._latest

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._camera = self._open()
                while not self._stop.is_set():
                    started = self._clock()
                    self._cycle()
                    self._stop.wait(max(0.0, self._period - (self._clock() - started)))
            except Exception as error:  # noqa: BLE001 - unplugged camera, driver hiccup
                log.warning("Camera %s failed (%s); retrying", self.name, type(error).__name__)
                with self._lock:
                    self._latest = CameraObservation(False, (), self._clock())
                self._stop.wait(3.0)
            finally:
                if self._camera is not None:
                    self._camera.close()
                    self._camera = None

    def _cycle(self) -> None:
        frame = self._camera.read()
        try:
            if not usable(frame):
                observation = CameraObservation(False, (), self._clock())
            else:
                observation = CameraObservation(True, tuple(self._detector.detect(frame)), self._clock())
        finally:
            del frame  # RNF-7.2: the image is gone right after inference
        with self._lock:
            self._latest = observation
