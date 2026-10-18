"""Minimal NMEA 0183 parser for the u-blox NEO-6M (only what the bus needs: position, speed, course).

Only ``RMC`` sentences are used (``$GPRMC`` / ``$GNRMC``): they carry validity, latitude, longitude, speed
over ground (knots) and course. Checksums are verified; a sentence with status ``V`` (no fix) yields ``None``.
No third-party dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

KNOTS_TO_KMH = 1.852


@dataclass(frozen=True)
class Fix:
    lat: float
    lng: float
    speed_kmh: float | None
    course_deg: float | None = None
    source: str = "gps"


def _checksum_ok(sentence: str) -> bool:
    if "*" not in sentence:
        return False
    body, given = sentence[1:].split("*", 1)
    value = 0
    for char in body.encode("ascii", errors="replace"):
        value ^= char
    try:
        return int(given[:2], 16) == value
    except ValueError:
        return False


def _coordinate(value: str, hemisphere: str, degree_digits: int) -> float:
    degrees = int(value[:degree_digits])
    minutes = float(value[degree_digits:])
    result = degrees + minutes / 60.0
    return -result if hemisphere in ("S", "W") else result


def parse_rmc(sentence: str) -> Fix | None:
    """``Fix`` from a valid RMC sentence; ``None`` for other sentences, no fix or a bad checksum."""
    text = sentence.strip()
    if not text.startswith("$") or text[3:6] != "RMC" or not _checksum_ok(text):
        return None
    fields = text[1:].split("*")[0].split(",")
    if len(fields) < 9 or fields[2] != "A" or not fields[3] or not fields[5]:
        return None
    try:
        lat = _coordinate(fields[3], fields[4], 2)
        lng = _coordinate(fields[5], fields[6], 3)
        speed = float(fields[7]) * KNOTS_TO_KMH if fields[7] else None
        course = float(fields[8]) if fields[8] else None
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return Fix(lat, lng, speed, course)
