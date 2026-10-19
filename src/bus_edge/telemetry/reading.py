"""The telemetry payload: exactly the body of ``POST /api/v1/internal/buses/{bus_id}/telemetry``.

Only numbers leave the bus (PB-N07): position, speed, counts, confidence and the model version, plus the ids
of the occupied positions (``S02-L1``, ``P04``...) so the 3D twin can draw the cabin without any image.
``doors_total`` is the on-board count of the door sensors; the core service compares it with the cameras
and falls back to it when they disagree for more than 30 s or the confidence is low (RF-6.5, RF-13.4).
"""

from __future__ import annotations

from typing import Any

from bus_edge.doors.counter import DoorSnapshot
from bus_edge.gps.nmea import Fix
from bus_edge.vision.occupancy import VisionResult

MAX_CABIN_IDS = 80


def _bounded(value: int, upper: int) -> int:
    return max(0, min(upper, int(value)))


def build_payload(ts: float, fix: Fix, doors: DoorSnapshot, vision: VisionResult, model_version: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ts": round(ts, 3),
        "lat": round(fix.lat, 7),
        "lng": round(fix.lng, 7),
        "seated": _bounded(vision.seated, 500),
        "standing": _bounded(vision.standing, 500),
        "standing_out_of_position": _bounded(vision.standing_out_of_position, 500),
        "wheelchair_occupied": _bounded(vision.wheelchair_occupied, 20),
        "doors_total": _bounded(doors.on_board, 1000),
        "confidence": round(max(0.0, min(1.0, vision.confidence)), 3),
        "approximate_count": bool(vision.approximate_count),
        "model_version": model_version[:40],
    }
    if fix.speed_kmh is not None:
        payload["speed_kmh"] = round(max(0.0, min(200.0, fix.speed_kmh)), 1)
    if vision.visible_share > 0:
        # Only when a camera really saw the cabin: without cameras an empty list would claim "nobody is seated".
        payload["cabin"] = {"occupied": sorted(vision.occupied)[:MAX_CABIN_IDS]}
    return payload
