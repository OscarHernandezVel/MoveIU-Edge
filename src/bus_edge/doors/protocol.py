"""Lines sent by the Arduino door counter (``firmware/door_counter/protocol.h``).

Every line is ``$BODY*CS`` where CS is the XOR of the body characters in hexadecimal (NMEA style). A line
with a bad checksum or shape raises :class:`LineError` and is discarded: a corrupted line must never become
a wrong passenger count.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LineError(ValueError):
    pass


class Door(StrEnum):
    FRONT = "front"
    REAR = "rear"


class DoorEventKind(StrEnum):
    IN = "IN"
    OUT = "OUT"
    BLOCKED = "BLOCKED"
    CLEARED = "CLEARED"


@dataclass(frozen=True)
class Boot:
    firmware: str
    ms: int


@dataclass(frozen=True)
class DoorEvent:
    seq: int
    door: Door
    kind: DoorEventKind
    boarded: int
    alighted: int
    ms: int


@dataclass(frozen=True)
class Heartbeat:
    seq: int
    boarded: int
    alighted: int
    front_blocked: bool
    rear_blocked: bool
    noise_edges: int
    ms: int


@dataclass(frozen=True)
class Ack:
    what: str
    seq: int


Message = Boot | DoorEvent | Heartbeat | Ack
MAX_LINE = 96


def checksum(body: str) -> int:
    value = 0
    for char in body.encode("ascii"):
        value ^= char
    return value


def frame(body: str) -> str:
    """Build a line exactly as the firmware does (used by the simulator and the tests)."""
    return f"${body}*{checksum(body):02X}"


def parse_line(raw: str) -> Message:
    text = raw.strip()
    if len(text) > MAX_LINE or not text.startswith("$") or len(text) < 5 or text[-3] != "*":
        raise LineError("malformed line")
    body, given = text[1:-3], text[-2:]
    try:
        if int(given, 16) != checksum(body):
            raise LineError("bad checksum")
    except (ValueError, UnicodeEncodeError) as error:
        raise LineError("bad checksum") from error
    fields = body.split(",")
    try:
        return _decode(fields)
    except (IndexError, ValueError) as error:
        raise LineError(f"bad {fields[0]} line") from error


def _decode(fields: list[str]) -> Message:
    kind = fields[0]
    if kind == "BOOT" and len(fields) == 3:
        return Boot(fields[1], int(fields[2]))
    if kind == "DOOR" and len(fields) == 7:
        door = {"F": Door.FRONT, "R": Door.REAR}[fields[2]] if fields[2] in ("F", "R") else None
        if door is None:
            raise ValueError("door")
        return DoorEvent(
            int(fields[1]), door, DoorEventKind(fields[3]), _count(fields[4]), _count(fields[5]), int(fields[6])
        )
    if kind == "HB" and len(fields) == 8:
        return Heartbeat(
            int(fields[1]),
            _count(fields[2]),
            _count(fields[3]),
            fields[4] == "1",
            fields[5] == "1",
            _count(fields[6]),
            int(fields[7]),
        )
    if kind == "ACK" and len(fields) == 3:
        return Ack(fields[1], int(fields[2]))
    raise ValueError("unknown")


def _count(text: str) -> int:
    value = int(text)
    if value < 0:
        raise ValueError("negative")
    return value
