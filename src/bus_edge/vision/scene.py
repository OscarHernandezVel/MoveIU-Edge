"""The LEGO-scale model bus in 3D, seen from its two cameras: synthetic camera frames and the layout they imply.

Two uses, with the same geometry and the same camera poses:

* :class:`SimulatedCamera` renders what the front or rear webcam would see (MDF body, windows, doors, 41 seats and
  yellow LEGO minifigures in the occupied positions). With ``EDGE_CAMERAS=front=sim,rear=sim`` the whole chain
  (detector, positions, counts, upload) runs without cameras; the frames never leave the process, like real ones.
* :func:`projected_layout` projects every seat, standing position, door zone and the cabin onto each camera image:
  a calibrated layout without clicking (exact for the simulated cameras; a starting point for real ones mounted
  as in docs/maqueta.md).

Geometry: the numbers of ``maqueta/make_cut_files.py`` (a test keeps both in step). Millimetres; x along the bus
from the outer face of the front, y across from the outer face of the right wall (the doors), z up from the
underside of the floor. Cameras: pinhole, 640x480, 110 degrees diagonal (the recommended 100-120 degree webcam),
lens 160 mm high on arches raised over the walls, tilted 45 degrees down; each one sees its five or six rows.

Rendering is a painter's algorithm: the inside of the body is convex, so walls and floor are drawn first and the
seats and figures after them, farthest first. Detections are matched to positions at the height of a minifigure's
head (``HEAD_Z``), which is about the same seated and standing, so a head's centre falls inside its position.
"""

from __future__ import annotations

import functools
import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from bus_edge.vision.layout import BusLayout, Kind, Position, maqueta_layout

# ------------------------------------------------------------------ geometry (maqueta/make_cut_files.py)
T, LENGTH, WIDTH, HEIGHT = 3.0, 720.0, 166.0, 130.0
INNER_W = WIDTH - 2 * T
SEAT, DOUBLE_W, AISLE = 30.0, 61.0, 38.0
ROW_STARTS = (54.0, *(108.0 + 54.0 * i for i in range(8)), 540.0)  # rows 1-10, inner coordinates
ROW_PITCH = 54.0
SEAT_H, BACK_H = 14.0, 34.0
BENCH_X = 616.0
WINDOW_Z = (50.0, 105.0)
FRONT_DOOR, REAR_DOOR = (14.0, 84.0), (548.0, 610.0)  # along the right wall, outer coordinates
DOOR_HEIGHT = 70.0
HEAD_Z = 39.0  # centre of a minifigure's head above the underside of the floor, seated (39.8) or standing (38.3)

Box = tuple[float, float, float, float, float, float]  # x0, x1, y0, y1, z0, z1
BGR = tuple[int, int, int]

COLOURS: dict[str, BGR] = {
    "room": (205, 196, 186),  # what is seen through the windows and over the walls
    "mdf": (120, 168, 204),
    "floor": (108, 152, 188),
    "mark": (90, 128, 160),
    "seat": (69, 102, 140),
    "arch": (60, 70, 80),
    "camera": (30, 30, 30),
    "yellow": (13, 204, 250),  # LEGO bright yellow: heads and hands
    "legs": (153, 51, 26),
}
TORSOS: tuple[BGR, ...] = ((38, 38, 200), (160, 90, 20), (51, 140, 26), (235, 235, 235), (40, 40, 40))


@dataclass(frozen=True)
class Spot:
    """Footprint of a position on the floor (outer coordinates, mm)."""

    id: str
    kind: Kind
    x0: float
    x1: float
    y0: float
    y1: float
    camera: str

    @property
    def centre(self) -> tuple[float, float]:
        return (self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2


@functools.cache
def spots() -> dict[str, Spot]:
    """Every seat, standing position and door zone of the model, by id (the ids of :func:`maqueta_layout`)."""
    found: dict[str, Spot] = {}
    for row, start in enumerate(ROW_STARTS, start=1):
        camera = "front" if row <= 5 else "rear"
        door_row = row in (1, 10)
        x0 = T + start + (2.0 if row == 1 else 20.0 if row == 10 else 10.0)
        for side in ("L",) if door_row else ("R", "L"):
            for seat in (1, 2):
                offset = (seat - 1) * (SEAT + 1)
                y0 = T + (offset if side == "R" else INNER_W - SEAT - offset)
                sid = f"S{row:02d}-{side}{seat}"
                found[sid] = Spot(sid, Kind.SEAT, x0, x0 + SEAT, y0, y0 + SEAT, camera)
        if not door_row:
            px, py = T + start + 8, T + DOUBLE_W + 4
            found[f"P{row:02d}"] = Spot(f"P{row:02d}", Kind.STANDING, px, px + ROW_PITCH - 12, py, py + AISLE - 8, camera)
    for seat in range(1, 6):
        y0 = T + 2 + (seat - 1) * (INNER_W - 4) / 5
        found[f"S11-T{seat}"] = Spot(f"S11-T{seat}", Kind.SEAT, T + BENCH_X, T + BENCH_X + SEAT, y0, y0 + 31.2, "rear")
    found["D-FRONT"] = Spot("D-FRONT", Kind.DOOR, *FRONT_DOOR, T, T + 40, "front")
    found["D-REAR"] = Spot("D-REAR", Kind.DOOR, *REAR_DOOR, T, T + 40, "rear")
    return found


def windows() -> dict[str, list[tuple[float, float]]]:
    """x-ranges of the window panes of each side wall (60 mm panes every 68 mm, none over a door)."""
    panes = [(x, x + 60.0) for x in np.arange(100.0, LENGTH - 30 - 60 + 1e-6, 68.0)]
    doors = (FRONT_DOOR, REAR_DOOR)
    right = [p for p in panes if all(p[1] <= a - 4 or p[0] >= b + 4 for a, b in doors)]
    return {"right": right, "left": panes}


# ------------------------------------------------------------------ camera
@dataclass(frozen=True)
class CameraPose:
    name: str
    position: tuple[float, float, float]
    forward: tuple[float, float, float]
    right: tuple[float, float, float]
    down: tuple[float, float, float]
    width: int = 640
    height: int = 480
    fov_diagonal_deg: float = 110.0

    @property
    def focal(self) -> float:
        return math.hypot(self.width, self.height) / 2 / math.tan(math.radians(self.fov_diagonal_deg) / 2)

    def to_camera(self, points: np.ndarray) -> np.ndarray:
        """World (n, 3) -> camera (n, 3): right, down, forward."""
        rel = np.asarray(points, dtype=float) - np.array(self.position)
        return rel @ np.array([self.right, self.down, self.forward]).T

    def to_pixels(self, cam: np.ndarray) -> np.ndarray:
        """Camera coordinates (forward > 0) -> pixels (n, 2)."""
        f = self.focal
        return np.stack(
            [self.width / 2 + f * cam[:, 0] / cam[:, 2], self.height / 2 + f * cam[:, 1] / cam[:, 2]], axis=1
        )


#: Lens 160 mm above the underside of the floor (the arch raised 45 mm over the walls on two spacers), tilted 45 degrees
#: down. Measured on the simulation: no recognition errors from 0 to 49 passengers; with the arch on the walls
#: (112 mm, 38 degrees) the heads of the farthest rows overlap and 96 of 417 positions were wrong.
CAMERA_HEIGHT_MM, CAMERA_PITCH_DEG = 160.0, 45.0
ARCH_SPACER_MM = 45.0


def camera_pose(name: str, pitch_deg: float = CAMERA_PITCH_DEG, height: float = CAMERA_HEIGHT_MM) -> CameraPose:
    """The webcam on its arch: the front one looks back (rows 1-5), the rear one looks forward (rows 6-11).
    Facing the rear, the door side (y = 0) is on the left of the image; facing the front, on the right."""
    p = math.radians(pitch_deg)
    if name == "front":
        return CameraPose(name, (T + 15, WIDTH / 2, height), (math.cos(p), 0, -math.sin(p)), (0, 1, 0),
                          (-math.sin(p), 0, -math.cos(p)))  # fmt: skip
    if name == "rear":
        return CameraPose(name, (LENGTH - T - 15, WIDTH / 2, height), (-math.cos(p), 0, -math.sin(p)), (0, -1, 0),
                          (math.sin(p), 0, -math.cos(p)))  # fmt: skip
    raise ValueError(f"unknown camera {name!r}: front or rear")


NEAR_MM = 1.0


def _clip_near(cam: np.ndarray) -> np.ndarray:
    """Sutherland-Hodgman against the plane forward = NEAR_MM (parts behind the lens are cut away)."""
    out: list[np.ndarray] = []
    for i in range(len(cam)):
        a, b = cam[i - 1], cam[i]
        a_in, b_in = a[2] >= NEAR_MM, b[2] >= NEAR_MM
        if a_in != b_in:
            t = (NEAR_MM - a[2]) / (b[2] - a[2])
            out.append(a + t * (b - a))
        if b_in:
            out.append(b)
    return np.array(out) if out else np.empty((0, 3))


def clip_to_image(points: np.ndarray, width: int, height: int) -> np.ndarray:
    """Sutherland-Hodgman of a 2D polygon against the image rectangle."""
    poly = [tuple(p) for p in points]
    for axis, limit, keep_less in ((0, 0.0, False), (0, float(width), True), (1, 0.0, False), (1, float(height), True)):
        out: list[tuple[float, float]] = []
        for i in range(len(poly)):
            a, b = poly[i - 1], poly[i]
            a_in = a[axis] <= limit if keep_less else a[axis] >= limit
            b_in = b[axis] <= limit if keep_less else b[axis] >= limit
            if a_in != b_in:
                t = (limit - a[axis]) / (b[axis] - a[axis])
                out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
            if b_in:
                out.append(b)
        poly = out
        if not poly:
            break
    return np.array(poly, dtype=float).reshape(-1, 2)


# ------------------------------------------------------------------ scene content
def _panel(u0: float, u1: float, v0: float, v1: float, holes: list[tuple[float, float, float, float]]) -> list[Box]:
    """Rectangles covering a wall panel (u along it, v up) except its holes (u0, u1, v0, v1)."""
    us = sorted({u0, u1, *(u for h in holes for u in h[:2])})
    vs = sorted({v0, v1, *(v for h in holes for v in h[2:])})
    cells = []
    for a, b in zip(vs, vs[1:]):
        for c, d in zip(us, us[1:]):
            cu, cv = (c + d) / 2, (a + b) / 2
            if not any(h[0] < cu < h[1] and h[2] < cv < h[3] for h in holes):
                cells.append((c, d, a, b, 0.0, 0.0))
    return cells


def wall_faces() -> list[tuple[np.ndarray, str]]:
    """Inner faces of the four walls and of the floor, with the window, door and windscreen openings."""
    faces: list[tuple[np.ndarray, str]] = []
    win = windows()
    doors = [(a, b, T, T + DOOR_HEIGHT) for a, b in (FRONT_DOOR, REAR_DOOR)]
    for side, y in (("right", T), ("left", WIDTH - T)):
        holes = [(a, b, *WINDOW_Z) for a, b in win[side]] + (doors if side == "right" else [])
        for u0, u1, v0, v1, *_ in _panel(0.0, LENGTH, 0.0, HEIGHT, holes):
            faces.append((np.array([[u0, y, v0], [u1, y, v0], [u1, y, v1], [u0, y, v1]]), "mdf"))
    for x, holes in ((T, [(T + 10, WIDTH - T - 10, 60.0, 115.0)]), (LENGTH - T, [])):
        for u0, u1, v0, v1, *_ in _panel(T, WIDTH - T, 0.0, HEIGHT, holes):
            faces.append((np.array([[x, u0, v0], [x, u1, v0], [x, u1, v1], [x, u0, v1]]), "mdf"))
    faces.append((np.array([[T, T, T], [LENGTH - T, T, T], [LENGTH - T, WIDTH - T, T], [T, WIDTH - T, T]]), "floor"))
    for spot in spots().values():  # engraved outlines of the positions
        if spot.kind is not Kind.DOOR:
            x0, x1, y0, y1 = spot.x0 + 1, spot.x1 - 1, spot.y0 + 1, spot.y1 - 1
            faces.append((np.array([[x0, y0, T], [x1, y0, T], [x1, y1, T], [x0, y1, T]]), "mark"))
    return faces


@functools.cache
def seat_boxes() -> list[tuple[Box, str]]:
    """Double seats (plate, backrest towards the rear, two legs) and the rear bench."""
    boxes: list[tuple[Box, str]] = []
    top = T + SEAT_H
    groups: dict[tuple[str, str], list[Spot]] = {}
    for spot in spots().values():
        if spot.kind is Kind.SEAT:
            groups.setdefault((spot.id[:3], spot.id[4] if spot.id[3] == "-" else ""), []).append(spot)
    for members in groups.values():
        x0, x1 = members[0].x0, members[0].x1
        y0, y1 = min(s.y0 for s in members), max(s.y1 for s in members)
        boxes.append(((x0, x1, y0, y1, top, top + T), "seat"))
        boxes.append(((x1, x1 + T, y0, y1, T, T + BACK_H), "seat"))
        boxes += [((x0 + 2, x1 - 2, y, y + T, T, top), "seat") for y in (y0, y1 - T)]
    return boxes


def far_arch(pose: CameraPose) -> list[tuple[Box, str]]:
    """The arch at the other end of the bus, on its two spacers, with its webcam (visible in the distance)."""
    x0 = LENGTH - T - 30 if pose.name == "front" else T
    top = HEIGHT + ARCH_SPACER_MM
    lens = pose.position[2]
    return [
        ((x0, x0 + 30, T, WIDTH - T, top - T, top), "arch"),
        ((x0 + 5, x0 + 25, T, T + 6, HEIGHT, top - T), "arch"),
        ((x0 + 5, x0 + 25, WIDTH - T - 6, WIDTH - T, HEIGHT, top - T), "arch"),
        ((x0 + 3, x0 + 27, WIDTH / 2 - 19, WIDTH / 2 + 19, lens - 15, lens + 15), "camera"),
    ]


def figure_boxes(spot: Spot, torso: BGR) -> list[tuple[Box, BGR | str]]:
    """A LEGO minifigure facing the front (-x): yellow head and hands, coloured torso, blue legs. Seated on a seat
    (hips on the plate, against the backrest), standing elsewhere (feet on the floor, in the middle of the spot)."""
    cx, cy = spot.centre
    if spot.kind is Kind.SEAT:
        back = spot.x1 - 2  # the backrest is right behind the seat
        z = T + SEAT_H + T
        hx = back - 6
        parts: list[tuple[Box, BGR | str]] = [
            ((hx - 4, hx + 4, cy - 8, cy + 8, z, z + 4), "legs"),
            ((hx - 19, hx - 4, cy - 7.5, cy - 0.5, z, z + 7), "legs"),
            ((hx - 19, hx - 4, cy + 0.5, cy + 7.5, z, z + 7), "legs"),
        ]
        z += 4
    else:
        hx, z = cx, T
        parts = [
            ((hx - 3.5, hx + 3.5, cy - 7.5, cy - 0.5, z, z + 15.5), "legs"),
            ((hx - 3.5, hx + 3.5, cy + 0.5, cy + 7.5, z, z + 15.5), "legs"),
            ((hx - 4, hx + 4, cy - 8, cy + 8, z + 15.5, z + 19.5), "legs"),
        ]
        z += 19.5
    parts += [
        ((hx - 4, hx + 4, cy - 8, cy + 8, z, z + 12), torso),
        ((hx - 2, hx + 2, cy - 12, cy - 8, z + 2, z + 11), torso),
        ((hx - 2, hx + 2, cy + 8, cy + 12, z + 2, z + 11), torso),
        ((hx - 5, hx - 2, cy - 11.5, cy - 8.5, z - 1, z + 2.5), "yellow"),  # hands
        ((hx - 5, hx - 2, cy + 8.5, cy + 11.5, z - 1, z + 2.5), "yellow"),
        ((hx - 4.8, hx + 4.8, cy - 4.8, cy + 4.8, z + 12, z + 19.5), "yellow"),  # head
        ((hx - 1.9, hx + 1.9, cy - 1.9, cy + 1.9, z + 19.5, z + 21.2), "yellow"),  # stud
    ]
    return parts


# ------------------------------------------------------------------ rendering
# The six faces of a box as indices into its 8 corners (corner i has bits x = i & 1, y = i >> 1 & 1, z = i >> 2 & 1),
# with the axis and sign of their outward normal and how much light they get.
_FACES = np.array([[0, 2, 6, 4], [1, 3, 7, 5], [0, 1, 5, 4], [2, 3, 7, 6], [0, 1, 3, 2], [4, 5, 7, 6]])
_NORMALS = ((0, -1), (0, 1), (1, -1), (1, 1), (2, -1), (2, 1))
_SHADE = np.array([0.82, 0.82, 0.72, 0.72, 0.5, 1.0])


def _colour(name: BGR | str) -> np.ndarray:
    return np.array(COLOURS[name] if isinstance(name, str) else name, dtype=float)


def _fill(image: np.ndarray, pose: CameraPose, cam: np.ndarray, colour: Any) -> None:
    import cv2

    cam = cam if cam[:, 2].min() >= NEAR_MM else _clip_near(cam)
    if len(cam) >= 3:
        pixels = np.round(pose.to_pixels(cam) * 16).astype(np.int32)
        cv2.fillPoly(image, [pixels], tuple(int(c) for c in colour), cv2.LINE_8, shift=4)


_BACKGROUNDS: dict[CameraPose, np.ndarray] = {}


def _background(pose: CameraPose) -> np.ndarray:
    """Room, walls (with the window and door openings) and the engraved floor: the same in every frame of a camera,
    rendered once. They bound a convex room, so they never hide each other and need no sorting."""
    if pose not in _BACKGROUNDS:
        image = np.empty((pose.height, pose.width, 3), dtype=np.uint8)
        image[:] = COLOURS["room"]
        for corners, colour in wall_faces():
            _fill(image, pose, pose.to_camera(corners), _colour(colour))
        _BACKGROUNDS[pose] = image
    return _BACKGROUNDS[pose]


def render(pose: CameraPose, occupied: dict[str, BGR] | None = None) -> np.ndarray:
    """What the camera sees (BGR, ``height x width``), with a minifigure in each occupied position
    (``{position id: torso colour}``). Seats and figures are painted farthest face first."""
    import cv2

    image = _background(pose).copy()
    solids: list[tuple[Box, BGR | str]] = [*seat_boxes(), *far_arch(pose)]
    places = spots()
    for index, (pid, torso) in enumerate(sorted((occupied or {}).items())):
        solids += figure_boxes(places[pid], torso or TORSOS[index % len(TORSOS)])
    boxes = np.array([box for box, _ in solids])  # (b, 6)
    colours = np.array([_colour(colour) for _, colour in solids])  # (b, 3)
    bits = np.arange(8)
    corners = np.stack([boxes[:, (bits & 1)], boxes[:, 2 + (bits >> 1 & 1)], boxes[:, 4 + (bits >> 2 & 1)]], axis=2)
    faces = corners[:, _FACES]  # (b, 6, 4, 3)
    position = np.array(pose.position)
    centres = faces.mean(axis=2) - position  # (b, 6, 3)
    facing = np.stack([-centres[:, k, axis] * sign > 0 for k, (axis, sign) in enumerate(_NORMALS)], axis=1)
    box_index, face_index = np.nonzero(facing)  # back faces culled
    depth = np.linalg.norm(centres[box_index, face_index], axis=1)
    order = np.argsort(-depth, kind="stable")  # farthest first
    cam = pose.to_camera(faces[box_index, face_index].reshape(-1, 3)).reshape(-1, 4, 3)
    shades = (colours[box_index] * _SHADE[face_index][:, None]).astype(int).tolist()
    in_front = cam[:, :, 2].min(axis=1) >= NEAR_MM
    pixels = np.zeros((len(cam), 4, 2), dtype=np.int32)  # all projected at once: the loop below only fills
    safe = cam[in_front]
    pixels[in_front] = np.round(pose.to_pixels(safe.reshape(-1, 3)).reshape(-1, 4, 2) * 16).astype(np.int32)
    for k in order:
        if in_front[k]:
            cv2.fillPoly(image, [pixels[k]], shades[k], cv2.LINE_8, shift=4)
        else:
            _fill(image, pose, cam[k], shades[k])  # crosses the lens plane: clip it first
    return cv2.GaussianBlur(image, (3, 3), 0)  # a webcam is never pixel-sharp


# ------------------------------------------------------------------ layout from the camera poses
def projected_polygon(pose: CameraPose, spot: Spot, z: float = HEAD_Z) -> tuple[tuple[float, float], ...]:
    corners = np.array([[spot.x0, spot.y0, z], [spot.x1, spot.y0, z], [spot.x1, spot.y1, z], [spot.x0, spot.y1, z]])
    return projected_quad(pose, corners)


def openings() -> list[tuple[str, np.ndarray]]:
    """Windows and doors of the side walls as 3D quads on the inner face (for drawing them over an image)."""
    quads: list[tuple[str, np.ndarray]] = []
    for side, y in (("right", T), ("left", WIDTH - T)):
        for a, b in windows()[side]:
            quads.append(("ventana", np.array([[a, y, WINDOW_Z[0]], [b, y, WINDOW_Z[0]], [b, y, WINDOW_Z[1]], [a, y, WINDOW_Z[1]]])))
    for name, (a, b) in (("D-FRONT", FRONT_DOOR), ("D-REAR", REAR_DOOR)):
        quads.append((name, np.array([[a, T, T], [b, T, T], [b, T, T + DOOR_HEIGHT], [a, T, T + DOOR_HEIGHT]])))
    return quads


def projected_quad(pose: CameraPose, corners: np.ndarray) -> tuple[tuple[float, float], ...]:
    """A 3D polygon on the image, clipped to it, in normalised coordinates (empty when not seen)."""
    cam = _clip_near(pose.to_camera(corners))
    if len(cam) < 3:
        return ()
    pixels = clip_to_image(pose.to_pixels(cam), pose.width, pose.height)
    if len(pixels) < 3:
        return ()
    return tuple((round(float(u) / pose.width, 4), round(float(v) / pose.height, 4)) for u, v in pixels)


def projected_layout(bus_id: str = "2101") -> BusLayout:
    """The model's layout with every polygon projected onto its camera at head height, and each camera's cabin (the
    inside of the body at that height: heads seen through a window or a door fall outside it)."""
    layout = maqueta_layout(bus_id)
    places = spots()
    poses = {name: camera_pose(name) for name in ("front", "rear")}
    positions = [
        Position(p.id, p.kind, p.row_no, p.side, p.camera, projected_polygon(poses[p.camera], places[p.id]))
        for p in layout.positions
    ]
    halves = {"front": (T, T + ROW_STARTS[5]), "rear": (T + ROW_STARTS[5], LENGTH - T)}
    cabin = {
        name: projected_polygon(pose, Spot("cabin", Kind.SEAT, *halves[name], T, WIDTH - T, name))
        for name, pose in poses.items()
    }
    cameras = {
        name: {"device": "sim", "pose": {"position": list(p.position), "pitch_deg": CAMERA_PITCH_DEG}}
        for name, p in poses.items()
    }
    return BusLayout(layout.bus_id, layout.seats, layout.standing_positions, layout.wheelchair_spaces,
                     layout.door_zones, positions, cameras, cabin)  # fmt: skip


# ------------------------------------------------------------------ simulated camera
class SimulatedPassengers:
    """Who sits where in the simulation: the first ``n`` positions of a fixed shuffled order (seats first, as people
    do), ``n`` coming from the door counter so the cameras and the doors tell the same story."""

    def __init__(self, on_board: Callable[[], int], seed: int = 7) -> None:
        self._on_board = on_board
        rng = random.Random(seed)
        places = [s for s in spots().values() if s.kind is not Kind.DOOR]
        seats = [s.id for s in places if s.kind is Kind.SEAT]
        standing = [s.id for s in places if s.kind is Kind.STANDING]
        rng.shuffle(seats)
        rng.shuffle(standing)
        self.order = seats + standing
        self.torsos = {pid: TORSOS[rng.randrange(len(TORSOS))] for pid in self.order}

    def occupied(self) -> dict[str, BGR]:
        count = max(0, min(len(self.order), self._on_board()))
        return {pid: self.torsos[pid] for pid in self.order[:count]}


class SimulatedCamera:
    """A camera of :class:`~bus_edge.vision.cameras.CameraWorker` that renders the model instead of filming it.
    A frame is rendered again only when the occupancy changes (a few ms otherwise)."""

    def __init__(self, name: str, passengers: SimulatedPassengers) -> None:
        self._pose = camera_pose(name)
        self._passengers = passengers
        self._cache: tuple[frozenset[str], Any] | None = None

    def read(self) -> Any:
        occupied = self._passengers.occupied()
        key = frozenset(occupied)
        if self._cache is None or self._cache[0] != key:
            self._cache = (key, render(self._pose, occupied))
        return self._cache[1].copy()  # the worker deletes its frame: hand out a copy

    def close(self) -> None:
        self._cache = None
