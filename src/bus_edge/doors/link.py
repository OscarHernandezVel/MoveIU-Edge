"""Connection to the door counter: the real Arduino over USB serial, or a simulator for a PC without hardware."""

from __future__ import annotations

import contextlib
import logging
import random
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from bus_edge.doors.counter import DoorSnapshot, PassengerCounter
from bus_edge.doors.protocol import LineError, frame, parse_line

log = logging.getLogger(__name__)

#: USB ids of the Arduino UNO R3 (official board, 16U2 bridge) and of common clones (CH340, FTDI).
ARDUINO_USB_IDS = {(0x2341, 0x0043), (0x2341, 0x0001), (0x2A03, 0x0043), (0x1A86, 0x7523), (0x0403, 0x6001)}


class DoorLink(ABC):
    def __init__(self) -> None:
        self.counter = PassengerCounter()

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def request_reset(self) -> None:
        """Terminal auto-correction (RNF-6.3): everybody got off, start counting from zero."""

    def snapshot(self) -> DoorSnapshot:
        return self.counter.snapshot()


def find_arduino_port() -> str | None:
    """First serial port whose USB id is an Arduino UNO (or a clone)."""
    from serial.tools import list_ports

    for port in list_ports.comports():
        if port.vid is not None and (port.vid, port.pid) in ARDUINO_USB_IDS:
            return str(port.device)
    return None


class SerialDoorLink(DoorLink):
    """Reads the Arduino in a background thread and reconnects by itself when the cable is unplugged."""

    def __init__(
        self,
        port: str = "auto",
        baud: int = 115_200,
        opener: Callable[[str, int], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._port = port
        self._baud = baud
        self._opener = opener or _open_serial
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._serial: Any = None
        self._write_lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="door-link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._close()

    def request_reset(self) -> None:
        self.counter.reset()
        self._write(b"RESET\n")

    def _write(self, data: bytes) -> None:
        with self._write_lock:
            if self._serial is not None:
                try:
                    self._serial.write(data)
                except Exception:  # noqa: BLE001 - the reader thread will reconnect
                    log.warning("Could not write to the door counter")

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            port = find_arduino_port() if self._port == "auto" else self._port
            if port is None:
                log.warning("No Arduino found on USB; retrying in %.0f s", backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            try:
                self._serial = self._opener(port, self._baud)
                log.info("Door counter connected on %s", port)
                backoff = 1.0
                self._write(b"STATUS\n")
                self._read_until_error()
            except Exception as error:  # noqa: BLE001 - unplugged cable, permission, ...
                log.warning("Door counter link lost (%s); reconnecting", type(error).__name__)
            finally:
                self._close()
            self._stop.wait(backoff)

    def _read_until_error(self) -> None:
        while not self._stop.is_set():
            raw = self._serial.readline()
            if not raw:
                continue  # read timeout: nothing this second
            self.feed(raw.decode("ascii", errors="replace"))

    def feed(self, text: str) -> None:
        """Process one received line (public so tests and the simulator can inject lines)."""
        try:
            self.counter.apply(parse_line(text), self._clock())
        except LineError:
            self.counter.corrupted()

    def _close(self) -> None:
        with self._write_lock:
            if self._serial is not None:
                with contextlib.suppress(Exception):  # closing a port that already vanished
                    self._serial.close()
                self._serial = None


def _open_serial(port: str, baud: int) -> Any:
    import serial

    return serial.Serial(port, baud, timeout=1.0)


class SimulatedDoorLink(DoorLink):
    """Produces the same lines as the firmware, with people boarding at the front and alighting at the rear.

    For developing on a PC without the Arduino (``EDGE_SERIAL_PORT=sim``). Deterministic with a seed."""

    def __init__(
        self, seed: int = 7, period_s: float = 8.0, capacity: int = 49, clock: Callable[[], float] = time.monotonic
    ) -> None:
        super().__init__()
        self._random = random.Random(seed)
        self._period = period_s
        self._capacity = capacity
        self._clock = clock
        self._boarded = 0
        self._alighted = 0
        self._seq = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._emit(f"BOOT,simulator,{self._ms()}")
        self._thread = threading.Thread(target=self._run, name="door-sim", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def request_reset(self) -> None:
        self._boarded = self._alighted = 0
        self._emit(f"ACK,RESET,{self._next()}")

    def step(self) -> None:
        """One simulated stop: a few people get off at the rear, a few get on at the front."""
        on_board = self._boarded - self._alighted
        for _ in range(self._random.randint(0, min(3, on_board))):
            self._alighted += 1
            self._emit(f"DOOR,{self._next()},R,OUT,{self._boarded},{self._alighted},{self._ms()}")
        for _ in range(self._random.randint(0, 4)):
            if self._boarded - self._alighted >= self._capacity:
                break
            self._boarded += 1
            self._emit(f"DOOR,{self._next()},F,IN,{self._boarded},{self._alighted},{self._ms()}")
        self._emit(f"HB,{self._next()},{self._boarded},{self._alighted},0,0,0,{self._ms()}")

    def _run(self) -> None:
        next_stop = self._clock() + self._period
        while not self._stop.wait(1.0):  # a heartbeat every second, like the firmware
            if self._clock() >= next_stop:
                next_stop += self._period
                self.step()
            else:
                self._emit(f"HB,{self._next()},{self._boarded},{self._alighted},0,0,0,{self._ms()}")

    def _emit(self, body: str) -> None:
        self.counter.apply(parse_line(frame(body)), self._clock())

    def _next(self) -> int:
        self._seq = (self._seq + 1) % 65_536
        return self._seq

    def _ms(self) -> int:
        return int(self._clock() * 1000) % 4_294_967_296
