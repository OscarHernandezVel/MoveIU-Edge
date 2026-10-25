"""Where the bus is: a real NEO-6M, or a replay along the real route (for indoor demonstrations).

v7 recommends replaying pre-recorded coordinates when the presentation room blocks the satellites; the
replay here follows the actual trace of the route from the catalogue package (``demo_data/*.json``), out
and back, at a configurable speed, pausing briefly at every stop.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from abc import ABC, abstractmethod
from bisect import bisect_right
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from bus_edge.gps.nmea import Fix, parse_rmc

log = logging.getLogger(__name__)
EARTH_RADIUS_M = 6_371_008.8


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


class GpsSource(ABC):
    def start(self) -> None:  # noqa: B027 - optional hook
        """Start background work, if any."""

    def stop(self) -> None:  # noqa: B027 - optional hook
        """Stop background work, if any."""

    @abstractmethod
    def current(self) -> Fix | None:
        """Newest fix, or ``None`` while there is no position."""

    def at_terminal(self) -> bool:
        return False


class FixedGps(GpsSource):
    def __init__(self, lat: float, lng: float) -> None:
        self._fix = Fix(lat, lng, 0.0, source="fixed")

    def current(self) -> Fix | None:
        return self._fix


class NmeaSerialGps(GpsSource):
    """NEO-6M at 9600 baud: through a USB-TTL adapter (``COM5`` on the Windows laptop, ``/dev/ttyUSB0`` on Linux) or
    on the UART of a Raspberry Pi (``/dev/serial0``). The laptop of the model bus uses the route replay instead."""

    def __init__(
        self,
        port: str,
        baud: int = 9600,
        max_age_s: float = 5.0,
        opener: Callable[[str, int], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._port, self._baud, self._max_age = port, baud, max_age_s
        self._opener = opener or _open_serial
        self._clock = clock
        self._fix: Fix | None = None
        self._fix_at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="gps", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def feed(self, sentence: str) -> None:
        fix = parse_rmc(sentence)
        if fix is not None:
            with self._lock:
                self._fix, self._fix_at = fix, self._clock()

    def current(self) -> Fix | None:
        with self._lock:
            if self._fix is None or self._clock() - self._fix_at > self._max_age:
                return None  # a stale position is worse than none
            return self._fix

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                link = self._opener(self._port, self._baud)
                try:
                    while not self._stop.is_set():
                        raw = link.readline()
                        if raw:
                            self.feed(raw.decode("ascii", errors="replace"))
                finally:
                    link.close()
            except Exception as error:  # noqa: BLE001 - unplugged module, permissions
                log.warning("GPS unavailable on %s (%s); retrying", self._port, type(error).__name__)
                self._stop.wait(5.0)


def _open_serial(port: str, baud: int) -> Any:
    import serial

    return serial.Serial(port, baud, timeout=1.0)


class Polyline:
    """A route trace with cumulative distances: position at a distance in O(log n)."""

    def __init__(self, points: Sequence[tuple[float, float]]) -> None:
        if len(points) < 2:
            raise ValueError("a route needs at least two points")
        self.points = list(points)
        self.cumulative = [0.0]
        for a, b in zip(self.points, self.points[1:], strict=False):
            self.cumulative.append(self.cumulative[-1] + haversine_m(a, b))
        self.length_m = self.cumulative[-1]
        if self.length_m <= 0:
            raise ValueError("the route has zero length")

    def at(self, distance_m: float) -> tuple[float, float]:
        target = min(max(distance_m, 0.0), self.length_m)
        index = min(max(bisect_right(self.cumulative, target) - 1, 0), len(self.points) - 2)
        span = self.cumulative[index + 1] - self.cumulative[index]
        t = 0.0 if span <= 0 else (target - self.cumulative[index]) / span
        (lat1, lng1), (lat2, lng2) = self.points[index], self.points[index + 1]
        return lat1 + (lat2 - lat1) * t, lng1 + (lng2 - lng1) * t


def load_route(package_path: str | Path, route_id: str) -> tuple[Polyline, Polyline, list[tuple[float, float]]]:
    """Outbound trace, return trace (its own, or the outbound reversed) and stop positions of a route."""
    data = json.loads(Path(package_path).read_text(encoding="utf-8"))
    route = next((r for r in data["routes"] if r["route_id"] == route_id), None)
    if route is None:
        raise ValueError(f"route {route_id} is not in {package_path}")
    outbound = [(float(lat), float(lng)) for lng, lat in route["path"]]
    back_raw = route.get("return_path")
    back = [(float(lat), float(lng)) for lng, lat in back_raw] if back_raw else list(reversed(outbound))
    stops_by_id = {s["stop_id"]: (float(s["lat"]), float(s["lng"])) for s in data["stops"]}
    stops = [stops_by_id[s] for s in route["stops"] if s in stops_by_id]
    return Polyline(outbound), Polyline(back), stops


class RouteReplayGps(GpsSource):
    """Moves along the route at ``speed_kmh``, out and back forever, dwelling ``dwell_s`` at the terminals."""

    def __init__(
        self,
        outbound: Polyline,
        back: Polyline,
        speed_kmh: float = 25.0,
        dwell_s: float = 20.0,
        start_fraction: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if speed_kmh <= 0:
            raise ValueError("speed must be positive")
        self._legs = (outbound, back)
        self._speed = speed_kmh / 3.6
        self._dwell = dwell_s
        self._clock = clock
        self._t0 = clock() - start_fraction * outbound.length_m / self._speed

    def _state(self) -> tuple[tuple[float, float], float, bool]:
        """(position, speed km/h, at terminal) right now."""
        cycle = [self._legs[0].length_m / self._speed, self._dwell, self._legs[1].length_m / self._speed, self._dwell]
        elapsed = (self._clock() - self._t0) % sum(cycle)
        if elapsed < cycle[0]:
            return self._legs[0].at(elapsed * self._speed), self._speed * 3.6, False
        elapsed -= cycle[0]
        if elapsed < cycle[1]:
            return self._legs[0].at(self._legs[0].length_m), 0.0, True
        elapsed -= cycle[1]
        if elapsed < cycle[2]:
            return self._legs[1].at(elapsed * self._speed), self._speed * 3.6, False
        return self._legs[1].at(self._legs[1].length_m), 0.0, True

    def current(self) -> Fix | None:
        (lat, lng), speed, _ = self._state()
        return Fix(round(lat, 7), round(lng, 7), speed, source="replay")

    def at_terminal(self) -> bool:
        return self._state()[2]


class TerminalDetector:
    """Arrival at a terminal from a real GPS: inside ``radius_m`` of a terminal and slower than ``max_speed``.
    Reports each arrival once (edge-triggered), so the door counter is reset once per trip."""

    def __init__(self, terminals: Sequence[tuple[float, float]], radius_m: float = 40.0, max_speed_kmh: float = 5.0):
        self._terminals = list(terminals)
        self._radius = radius_m
        self._max_speed = max_speed_kmh
        self._inside = False

    def arrived(self, fix: Fix | None, replay_says_terminal: bool = False) -> bool:
        here = replay_says_terminal or (
            fix is not None
            and (fix.speed_kmh or 0.0) <= self._max_speed
            and any(haversine_m((fix.lat, fix.lng), t) <= self._radius for t in self._terminals)
        )
        arrived = here and not self._inside
        self._inside = here
        return arrived
