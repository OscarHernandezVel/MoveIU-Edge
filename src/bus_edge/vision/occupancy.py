"""From detected people to the counts the core service expects (RF-6.2, RF-9.2, RF-9.5, RF-13.3, RF-13.4).

For each camera, every detection is reduced to its *anchor* point and assigned to the position (seat,
standing position, wheelchair space or door zone) whose polygon contains it; when polygons overlap, the
smallest one wins (the most specific). Then:

* a seat, standing position or wheelchair space holds **one** person: a second person in it is excess
  (``standing_out_of_position``, RF-9.2);
* a person inside the bus but in no position (floor, steps, aisle ends) is out of position (RF-13.3);
* a door zone is a passage (RF-9.5): people there are ignored while it is in use, but one who **stays
  more than 10 s with the bus moving** is counted out of position (the zone must be kept clear);
* detections outside the cabin polygon of the camera (people seen through a window) are ignored.

Each position belongs to exactly one camera (``camera`` column of ``bus_positions``), so nobody is counted
twice by the front and the rear camera.

``confidence`` is the mean detection score (0.95 for an empty, well-lit bus) scaled by the share of positions
that are visible; ``approximate_count`` is set when less than 70 % of the positions are visible (RF-13.4),
e.g. a camera is unplugged or its image is too dark. The core service then relies on the door count.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bus_edge.vision.detector import Detection
from bus_edge.vision.layout import CAPACITY_KINDS, BusLayout, Kind, Position, point_in_polygon

DOOR_DWELL_S = 10.0
MIN_VISIBLE_SHARE = 0.7
EMPTY_BUS_CONFIDENCE = 0.95


@dataclass(frozen=True)
class CameraObservation:
    """What one camera saw in its newest usable frame (no image, only boxes)."""

    ok: bool
    detections: tuple[Detection, ...] = ()
    at: float = 0.0


@dataclass(frozen=True)
class VisionResult:
    seated: int
    standing: int
    standing_out_of_position: int
    wheelchair_occupied: int
    confidence: float
    approximate_count: bool
    visible_share: float
    occupied: frozenset[str] = field(default_factory=frozenset)  # ids of occupied positions (for the display)

    @property
    def total(self) -> int:
        return self.seated + self.standing + self.standing_out_of_position + self.wheelchair_occupied


class OccupancyEstimator:
    def __init__(self, layout: BusLayout, anchor_height: float = 0.8, max_age_s: float = 3.0) -> None:
        self._layout = layout
        self._anchor = anchor_height
        self._max_age = max_age_s
        self._door_since: dict[str, float] = {}
        # Smallest polygon first, so the most specific position wins where polygons overlap.
        self._by_camera: dict[str, list[Position]] = {
            camera: sorted((p for p in layout.for_camera(camera) if p.calibrated), key=lambda p: p.area())
            for camera in {p.camera for p in layout.positions}
        }

    def estimate(self, observations: dict[str, CameraObservation], now: float, moving: bool) -> VisionResult:
        counts = {kind: 0 for kind in Kind}
        excess = 0
        occupied: set[str] = set()
        scores: list[float] = []
        visible_cameras = {camera for camera, obs in observations.items() if obs.ok and now - obs.at <= self._max_age}
        doors_seen: set[str] = set()
        for camera in visible_cameras:
            cabin = self._layout.cabin.get(camera)
            for detection in observations[camera].detections:
                anchor = detection.anchor(self._anchor)
                if cabin and not point_in_polygon(anchor, cabin):
                    continue
                scores.append(detection.score)
                position = next((p for p in self._by_camera.get(camera, []) if p.contains(anchor)), None)
                if position is None:
                    excess += 1  # in the bus but in no position: floor, steps, aisle ends (RF-13.3)
                elif position.kind is Kind.DOOR:
                    doors_seen.add(position.id)
                elif position.id in occupied:
                    excess += 1  # two people in one position (RF-9.2)
                else:
                    occupied.add(position.id)
                    counts[position.kind] += 1
        excess += self._door_dwellers(doors_seen, now, moving)
        share = self._visible_share(visible_cameras)
        base = sum(scores) / len(scores) if scores else EMPTY_BUS_CONFIDENCE
        return VisionResult(
            seated=counts[Kind.SEAT],
            standing=counts[Kind.STANDING],
            standing_out_of_position=excess,
            wheelchair_occupied=counts[Kind.WHEELCHAIR],
            confidence=round(max(0.0, min(1.0, base * share)), 3),
            approximate_count=share < MIN_VISIBLE_SHARE,
            visible_share=round(share, 3),
            occupied=frozenset(occupied),
        )

    def _door_dwellers(self, doors_seen: set[str], now: float, moving: bool) -> int:
        for door_id in list(self._door_since):
            if door_id not in doors_seen:
                del self._door_since[door_id]  # the zone was cleared: restart its timer
        for door_id in doors_seen:
            self._door_since.setdefault(door_id, now)
        if not moving:
            return 0  # doors in use at a stop: people there are boarding or alighting
        return sum(1 for since in self._door_since.values() if now - since > DOOR_DWELL_S)

    def _visible_share(self, visible_cameras: set[str]) -> float:
        capacity = [p for p in self._layout.positions if p.kind in CAPACITY_KINDS]
        if not capacity:
            return 0.0
        visible = sum(1 for p in capacity if p.camera in visible_cameras and p.calibrated)
        return visible / len(capacity)
