"""Interactive camera calibration (RF-9.4): draw the polygon of every seat, standing position and door.

    python -m bus_edge calibrate --camera front --device 0

Controls (window "Calibracion"):
  left click   add a corner of the current position (4 corners -> next position automatically)
  u            undo the last corner            n / b    next / previous position
  x            clear the current polygon        c        draw the cabin outline (Enter closes it)
  f            freeze / unfreeze the image      s        save the layout file     q  quit (asks to save)

The image is only shown on this screen; it is never written to disk (PB-N07). Coordinates are saved
normalised (0-1) in the layout file, in the same format as ``bus_positions`` of the core service.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from bus_edge.vision.layout import BusLayout, Point, Position, load_layout, maqueta_layout

WINDOW = "Calibracion"
CORNERS = 4
COLORS = {"seat": (80, 200, 80), "standing": (60, 160, 255), "wheelchair": (255, 160, 40), "door": (60, 60, 230)}


class Calibrator:
    """State of a calibration session, independent of OpenCV so it can be tested."""

    def __init__(self, layout: BusLayout, camera: str) -> None:
        self.layout = layout
        self.camera = camera
        self.indexes = [i for i, p in enumerate(layout.positions) if p.camera == camera]
        if not self.indexes:
            raise ValueError(f"the layout has no positions for the {camera} camera")
        self.cursor = 0
        self.points: list[Point] = []
        self.cabin_mode = False
        self.cabin: list[Point] = list(layout.cabin.get(camera, ()))

    @property
    def current(self) -> Position:
        return self.layout.positions[self.indexes[self.cursor]]

    def click(self, x: float, y: float) -> None:
        point = (min(1.0, max(0.0, x)), min(1.0, max(0.0, y)))
        if self.cabin_mode:
            self.cabin.append(point)
            return
        self.points.append(point)
        if len(self.points) == CORNERS:
            self._store(tuple(self.points))
            self.points = []
            self.move(1)

    def undo(self) -> None:
        if self.cabin_mode and self.cabin:
            self.cabin.pop()
        elif self.points:
            self.points.pop()

    def clear(self) -> None:
        self.points = []
        self._store(())

    def move(self, step: int) -> None:
        self.points = []
        self.cursor = max(0, min(len(self.indexes) - 1, self.cursor + step))

    def toggle_cabin(self) -> None:
        self.cabin_mode = not self.cabin_mode
        if self.cabin_mode:
            self.cabin = []

    def close_cabin(self) -> None:
        if len(self.cabin) >= 3:
            self.layout.cabin[self.camera] = tuple(self.cabin)
        self.cabin_mode = False

    def done(self) -> int:
        return sum(1 for i in self.indexes if self.layout.positions[i].calibrated)

    def _store(self, polygon: tuple[Point, ...]) -> None:
        index = self.indexes[self.cursor]
        self.layout.positions[index] = replace(self.layout.positions[index], polygon=polygon)


def _draw(cv2: Any, frame: Any, session: Calibrator) -> Any:
    height, width = frame.shape[:2]
    canvas = frame.copy()

    def pixels(points: Any) -> Any:
        import numpy as np

        return np.array([[int(x * width), int(y * height)] for x, y in points], dtype=np.int32)

    for index in session.indexes:
        position = session.layout.positions[index]
        if position.calibrated:
            color = COLORS.get(position.kind.value, (200, 200, 200))
            thickness = 3 if position is session.current else 1
            cv2.polylines(canvas, [pixels(position.polygon)], True, color, thickness)
            x, y = pixels(position.polygon).mean(axis=0)
            cv2.putText(canvas, position.id, (int(x) - 20, int(y)), cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1)
    if session.cabin:
        cv2.polylines(canvas, [pixels(session.cabin)], not session.cabin_mode, (255, 255, 255), 1)
    for x, y in session.points:
        cv2.circle(canvas, (int(x * width), int(y * height)), 4, (0, 255, 255), -1)
    current = session.current
    title = (
        "CABINA: clic en el contorno, Enter para cerrar"
        if session.cabin_mode
        else f"{current.id} ({current.kind.value}, fila {current.row_no}, {current.side}) "
        f"- {len(session.points)}/{CORNERS} esquinas - listas {session.done()}/{len(session.indexes)}"
    )
    cv2.rectangle(canvas, (0, 0), (width, 22), (0, 0, 0), -1)
    cv2.putText(canvas, title, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
    return canvas


def run(layout_path: str, camera: str, device: int | str, bus_id: str = "2101") -> int:
    import cv2

    path = Path(layout_path)
    layout = load_layout(path) if path.exists() else maqueta_layout(bus_id)
    session = Calibrator(layout, camera)
    capture = cv2.VideoCapture(device)
    if not capture.isOpened():
        print(f"No se pudo abrir la cámara {device}.")
        return 1
    size: dict[str, int] = {"w": 640, "h": 480}

    def on_mouse(event: int, x: int, y: int, *_: Any) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            session.click(x / size["w"], y / size["h"])

    cv2.namedWindow(WINDOW)
    cv2.setMouseCallback(WINDOW, on_mouse)
    frozen: Any = None
    try:
        while True:
            if frozen is None:
                ok, frame = capture.read()
                if not ok:
                    continue
            else:
                frame = frozen
            size["h"], size["w"] = frame.shape[:2]
            cv2.imshow(WINDOW, _draw(cv2, frame, session))
            key = cv2.waitKey(30) & 0xFF
            if key == ord("q"):
                break
            actions = {
                ord("u"): session.undo,
                ord("n"): lambda: session.move(1),
                ord("b"): lambda: session.move(-1),
                ord("x"): session.clear,
                ord("c"): session.toggle_cabin,
                13: session.close_cabin,
                10: session.close_cabin,
            }
            if key in actions:
                actions[key]()
            elif key == ord("f"):
                frozen = None if frozen is not None else frame.copy()
            elif key == ord("s"):
                layout.save(path)
                print(f"Guardado {path} ({session.done()}/{len(session.indexes)} posiciones de la cámara {camera}).")
    finally:
        del frozen
        capture.release()
        cv2.destroyAllWindows()
    layout.save(path)
    print(f"Guardado {path}.")
    return 0
