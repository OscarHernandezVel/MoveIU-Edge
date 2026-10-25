"""People on board according to the door sensors ("Total de Pasajeros a Bordo", RF-6.1).

The Arduino reports cumulative totals (``boarded``, ``alighted``). This class turns them into the on-board
count and survives the three things that go wrong on a real bus:

* **lost lines**: totals are cumulative, so the next line corrects the count; gaps in the 16-bit sequence
  number are counted for diagnostics;
* **the Arduino resets** (loose USB cable, brown-out): its totals restart at zero. The people already on
  board are kept in ``base`` so the count does not drop to zero in the middle of a trip;
* **more alightings than boardings** (a missed boarding): the count is clamped at zero, never negative.

``reset()`` is the terminal auto-correction (RNF-6.3): everyone gets off at the terminal.

With two sensors per door both doors count boardings and alightings. The per-door counts and the aborted
crossings come from the individual events, so a lost line can make them miss one: they are for traceability
and diagnostics; the on-board count always comes from the cumulative totals.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from bus_edge.doors.protocol import Ack, Boot, Door, DoorEvent, DoorEventKind, Heartbeat, Message

SEQ_MODULO = 1 << 16


@dataclass(frozen=True)
class DoorSnapshot:
    on_board: int
    boarded: int
    alighted: int
    front_obstructed: bool
    rear_obstructed: bool
    lost_lines: int
    corrupted_lines: int
    reboots: int
    last_seen: float | None  # monotonic seconds of the newest valid line
    front_in: int = 0
    front_out: int = 0
    rear_in: int = 0
    rear_out: int = 0
    aborted: int = 0

    def healthy(self, now: float, max_silence: float = 5.0) -> bool:
        return self.last_seen is not None and now - self.last_seen <= max_silence


class PassengerCounter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._base = 0
        self._boarded = 0
        self._alighted = 0
        self._front_obstructed = False
        self._rear_obstructed = False
        self._last_seq: int | None = None
        self._lost = 0
        self._corrupted = 0
        self._reboots = 0
        self._last_seen: float | None = None
        self._booted_once = False
        self._by_door = {(door, kind): 0 for door in Door for kind in (DoorEventKind.IN, DoorEventKind.OUT)}
        self._aborted = 0

    # ------------------------------------------------------------------ input
    def apply(self, message: Message, now: float) -> None:
        with self._lock:
            self._last_seen = now
            if isinstance(message, Boot):
                if self._booted_once or self._boarded or self._alighted:
                    self._absorb_restart()
                self._booted_once = True
                self._last_seq = None
                return
            self._track_sequence(message.seq)
            if isinstance(message, Ack):
                if message.what in ("RESET", "BUTTON"):
                    self._zero()
                return
            if isinstance(message, DoorEvent):
                if (message.door, message.kind) in self._by_door:
                    self._by_door[message.door, message.kind] += 1
                elif message.kind is DoorEventKind.ABORTED:
                    self._aborted += 1
                elif message.kind is DoorEventKind.BLOCKED:
                    self._set_obstructed(message.door.value, True)
                elif message.kind is DoorEventKind.CLEARED:
                    self._set_obstructed(message.door.value, False)
            elif isinstance(message, Heartbeat):
                self._front_obstructed, self._rear_obstructed = message.front_blocked, message.rear_blocked
            self._update_totals(message.boarded, message.alighted)

    def corrupted(self) -> None:
        with self._lock:
            self._corrupted += 1

    def reset(self) -> None:
        """Local reset (the Arduino confirms it with ``ACK,RESET``)."""
        with self._lock:
            self._zero()

    # ---------------------------------------------------------------- output
    def snapshot(self) -> DoorSnapshot:
        with self._lock:
            return DoorSnapshot(
                on_board=self._on_board(),
                boarded=self._boarded,
                alighted=self._alighted,
                front_obstructed=self._front_obstructed,
                rear_obstructed=self._rear_obstructed,
                lost_lines=self._lost,
                corrupted_lines=self._corrupted,
                reboots=self._reboots,
                last_seen=self._last_seen,
                front_in=self._by_door[Door.FRONT, DoorEventKind.IN],
                front_out=self._by_door[Door.FRONT, DoorEventKind.OUT],
                rear_in=self._by_door[Door.REAR, DoorEventKind.IN],
                rear_out=self._by_door[Door.REAR, DoorEventKind.OUT],
                aborted=self._aborted,
            )

    # ------------------------------------------------------------- internals
    def _on_board(self) -> int:
        return max(0, self._base + self._boarded - self._alighted)

    def _zero(self) -> None:
        self._base = self._boarded = self._alighted = self._aborted = 0
        self._by_door = dict.fromkeys(self._by_door, 0)

    def _absorb_restart(self) -> None:
        self._base = self._on_board()
        self._boarded = self._alighted = 0
        self._reboots += 1

    def _update_totals(self, boarded: int, alighted: int) -> None:
        if boarded < self._boarded or alighted < self._alighted:
            # Totals went backwards without a BOOT line: the Arduino restarted and its BOOT line was lost.
            self._absorb_restart()
        self._boarded, self._alighted = boarded, alighted

    def _track_sequence(self, seq: int) -> None:
        if self._last_seq is not None:
            gap = (seq - self._last_seq - 1) % SEQ_MODULO
            if 0 < gap < SEQ_MODULO // 2:
                self._lost += gap
        self._last_seq = seq

    def _set_obstructed(self, door: str, value: bool) -> None:
        if door == "front":
            self._front_obstructed = value
        else:
            self._rear_obstructed = value
