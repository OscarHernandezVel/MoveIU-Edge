"""Interior map of the bus as seen by the cameras (RF-9.4): one polygon per seat, standing position and door.

The file has the same ``positions`` shape as the catalogue package of the core service (``kind``, ``row_no``,
``side``, ``camera``, ``roi``), so a calibrated map can be copied into the fleet file and loaded into the
``bus_positions`` table unchanged. Polygons use **normalised image coordinates** (0-1), so they survive a
change of camera resolution.

Point-in-polygon uses ray casting: O(k) for a polygon of k vertices; a camera has ~25 positions of 4 vertices.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

Point = tuple[float, float]


class Kind(StrEnum):
    SEAT = "seat"
    STANDING = "standing"
    WHEELCHAIR = "wheelchair"
    DOOR = "door"


CAPACITY_KINDS = (Kind.SEAT, Kind.STANDING, Kind.WHEELCHAIR)


@dataclass(frozen=True)
class Position:
    id: str
    kind: Kind
    row_no: int
    side: str
    camera: str
    polygon: tuple[Point, ...]

    @property
    def calibrated(self) -> bool:
        return len(self.polygon) >= 3

    def contains(self, point: Point) -> bool:
        return point_in_polygon(point, self.polygon)

    def area(self) -> float:
        pts = self.polygon
        return abs(sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1] for i in range(len(pts)))) / 2.0


@dataclass
class BusLayout:
    bus_id: str
    seats: int
    standing_positions: int
    wheelchair_spaces: int
    door_zones: int
    positions: list[Position]
    cameras: dict[str, dict[str, Any]] = field(default_factory=dict)
    cabin: dict[str, tuple[Point, ...]] = field(default_factory=dict)

    def for_camera(self, camera: str) -> list[Position]:
        return [p for p in self.positions if p.camera == camera]

    def capacity_positions(self) -> list[Position]:
        return [p for p in self.positions if p.kind in CAPACITY_KINDS]

    def problems(self) -> list[str]:
        """Consistency rules of v7: positions of each kind match the capacity of the bus."""
        found: list[str] = []
        counts = {kind: sum(1 for p in self.positions if p.kind is kind) for kind in Kind}
        expected = {
            Kind.SEAT: self.seats,
            Kind.STANDING: self.standing_positions,
            Kind.WHEELCHAIR: self.wheelchair_spaces,
            Kind.DOOR: self.door_zones,
        }
        for kind, wanted in expected.items():
            if counts[kind] != wanted:
                found.append(f"{kind.value}: {counts[kind]} positions, capacity says {wanted}")
        ids = [p.id for p in self.positions]
        if len(ids) != len(set(ids)):
            found.append("position ids must be unique")
        return found

    def calibrated_share(self) -> float:
        capacity = self.capacity_positions()
        return sum(1 for p in capacity if p.calibrated) / len(capacity) if capacity else 0.0

    # ------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        return {
            "bus_id": self.bus_id,
            "capacity": {
                "seats": self.seats,
                "standing_positions": self.standing_positions,
                "wheelchair_spaces": self.wheelchair_spaces,
                "door_zones": self.door_zones,
            },
            "cameras": self.cameras,
            "cabin": {camera: [list(p) for p in polygon] for camera, polygon in self.cabin.items()},
            "positions": [
                {
                    "id": p.id,
                    "kind": p.kind.value,
                    "row_no": p.row_no,
                    "side": p.side,
                    "camera": p.camera,
                    "roi": {"calibrated": p.calibrated, "polygon": [[round(x, 4), round(y, 4)] for x, y in p.polygon]},
                }
                for p in self.positions
            ],
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def _polygon(raw: Any) -> tuple[Point, ...]:
    if not raw:
        return ()
    points = tuple((float(x), float(y)) for x, y in raw)
    if len(points) < 3 or not all(0.0 <= v <= 1.0 for point in points for v in point):
        raise ValueError("a polygon needs 3+ points with normalised coordinates between 0 and 1")
    return points


def load_layout(path: str | Path) -> BusLayout:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    capacity = data["capacity"]
    positions = [
        Position(
            str(item.get("id") or f"P{index:02d}"),
            Kind(item["kind"]),
            int(item["row_no"]),
            str(item["side"]),
            str(item["camera"]),
            _polygon((item.get("roi") or {}).get("polygon")),
        )
        for index, item in enumerate(data["positions"], start=1)
    ]
    return BusLayout(
        str(data["bus_id"]),
        int(capacity["seats"]),
        int(capacity["standing_positions"]),
        int(capacity.get("wheelchair_spaces", 0)),
        int(capacity.get("door_zones", 0)),
        positions,
        dict(data.get("cameras", {})),
        {camera: _polygon(polygon) for camera, polygon in (data.get("cabin") or {}).items() if polygon},
    )


def point_in_polygon(point: Point, polygon: tuple[Point, ...]) -> bool:
    """Ray casting (even-odd rule); points exactly on an edge may fall either way."""
    if len(polygon) < 3:
        return False
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def maqueta_layout(bus_id: str = "2101") -> BusLayout:
    """Interior of the LEGO-scale model of a 12 m bus: 41 seats, 8 standing positions, 2 doors (``docs/maqueta.md``).

    As in RF-6.4 of the design document: row 1 (front door) and row 10 (rear door) only have 2 seats on the
    left, across from the door; rows 2-9 have 2 + 2 seats and one standing position in the aisle; row 11 is
    the rear bench of 5, right behind the rear door. The front camera watches rows 1-5 and the front door;
    the rear camera rows 6-11 and the rear door. Polygons are empty until the calibration tool draws them."""
    positions: list[Position] = []
    for row in range(1, 11):
        camera = "front" if row <= 5 else "rear"
        door_row = row in (1, 10)
        for side in ("left",) if door_row else ("left", "right"):
            for seat in (1, 2):
                positions.append(Position(f"S{row:02d}-{side[0].upper()}{seat}", Kind.SEAT, row, side, camera, ()))
        if not door_row:
            positions.append(Position(f"P{row:02d}", Kind.STANDING, row, "aisle", camera, ()))
    for seat in range(1, 6):
        positions.append(Position(f"S11-T{seat}", Kind.SEAT, 11, "rear", "rear", ()))
    positions.append(Position("D-FRONT", Kind.DOOR, 1, "right", "front", ()))
    positions.append(Position("D-REAR", Kind.DOOR, 10, "right", "rear", ()))
    return BusLayout(bus_id, 41, 8, 0, 2, positions, {"front": {"device": 0}, "rear": {"device": 1}}, {})
