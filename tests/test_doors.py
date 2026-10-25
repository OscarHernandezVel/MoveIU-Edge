"""Door counter: serial protocol (also cross-checked against the C++ firmware), counting and the links."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bus_edge.doors.counter import PassengerCounter
from bus_edge.doors.link import SerialDoorLink, SimulatedDoorLink
from bus_edge.doors.protocol import (
    Ack,
    Boot,
    Door,
    DoorEvent,
    DoorEventKind,
    Heartbeat,
    LineError,
    frame,
    parse_line,
)

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware"


class TestProtocol:
    def test_every_line_kind_is_decoded(self) -> None:
        assert parse_line(frame("BOOT,door-counter-1.0,12")) == Boot("door-counter-1.0", 12)
        event = parse_line(frame("DOOR,7,F,IN,5,2,4000") + "\r\n")
        assert event == DoorEvent(7, Door.FRONT, DoorEventKind.IN, 5, 2, 4000)
        assert parse_line(frame("HB,8,5,2,0,1,3,5000")) == Heartbeat(8, 5, 2, False, True, 3, 5000)
        assert parse_line(frame("ACK,RESET,9")) == Ack("RESET", 9)
        aborted = parse_line(frame("DOOR,10,R,ABORTED,5,2,6000"))
        assert aborted == DoorEvent(10, Door.REAR, DoorEventKind.ABORTED, 5, 2, 6000)

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "HB,1,2,3",
            "$HB,1,2,3*00",  # wrong checksum
            frame("DOOR,1,X,IN,1,0,5"),  # unknown door
            frame("DOOR,1,F,JUMP,1,0,5"),  # unknown event
            frame("HB,1,-2,0,0,0,0,5"),  # negative total
            frame("NOPE,1"),
            "$" + "A" * 200 + "*00",
        ],
    )
    def test_corrupted_lines_are_rejected(self, line: str) -> None:
        with pytest.raises(LineError):
            parse_line(line)

    @pytest.mark.skipif(shutil.which("g++") is None, reason="g++ not installed")
    def test_python_and_firmware_compute_the_same_checksum(self, tmp_path: Path) -> None:
        source = tmp_path / "frame.cpp"
        source.write_text(
            '#include <cstdio>\n#include "protocol.h"\n'
            'int main(){char out[80];protocol::frame("DOOR,42,R,OUT,17,9,123456",out,sizeof(out));'
            'std::printf("%s",out);return 0;}\n'
        )
        binary = tmp_path / "frame"
        subprocess.run(  # noqa: S603
            ["g++", "-std=c++11", "-I", str(FIRMWARE / "door_counter"), str(source), "-o", str(binary)],  # noqa: S607
            check=True,
        )
        produced = subprocess.run([str(binary)], check=True, capture_output=True).stdout.decode("ascii")  # noqa: S603
        assert produced == frame("DOOR,42,R,OUT,17,9,123456") + "\r\n"
        assert parse_line(produced).alighted == 9  # type: ignore[union-attr]

    @pytest.mark.skipif(shutil.which("g++") is None, reason="g++ not installed")
    def test_the_firmware_logic_tests_pass(self, tmp_path: Path) -> None:
        binary = tmp_path / "logic"
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "g++",
                "-std=c++11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(FIRMWARE / "door_counter"),
                str(FIRMWARE / "tests" / "test_firmware_logic.cpp"),
                "-o",
                str(binary),
            ],
            check=True,
        )
        result = subprocess.run([str(binary)], capture_output=True, text=True)  # noqa: S603
        assert result.returncode == 0, result.stdout


def door(seq: int, kind: str, boarded: int, alighted: int, which: str = "F") -> Any:
    return parse_line(frame(f"DOOR,{seq},{which},{kind},{boarded},{alighted},{seq * 100}"))


def hb(seq: int, boarded: int, alighted: int, front: int = 0, rear: int = 0) -> Any:
    return parse_line(frame(f"HB,{seq},{boarded},{alighted},{front},{rear},0,{seq * 100}"))


class TestCounter:
    def test_boardings_minus_alightings(self) -> None:
        counter = PassengerCounter()
        counter.apply(Boot("fw", 0), 1.0)
        for seq, (b, a) in enumerate([(1, 0), (2, 0), (3, 0), (3, 1)], start=1):
            counter.apply(door(seq, "IN" if a == 0 else "OUT", b, a), 2.0)
        snap = counter.snapshot()
        assert (snap.boarded, snap.alighted, snap.on_board, snap.lost_lines) == (3, 1, 2, 0)
        assert snap.healthy(3.0) and not snap.healthy(20.0)

    def test_lost_lines_are_corrected_by_the_cumulative_totals(self) -> None:
        counter = PassengerCounter()
        counter.apply(hb(1, 0, 0), 0.0)
        counter.apply(hb(5, 4, 1), 1.0)  # lines 2-4 were lost
        snap = counter.snapshot()
        assert snap.on_board == 3 and snap.lost_lines == 3

    def test_sequence_wrap_is_not_a_loss(self) -> None:
        counter = PassengerCounter()
        counter.apply(hb(65_535, 1, 0), 0.0)
        counter.apply(hb(0, 1, 0), 1.0)
        assert counter.snapshot().lost_lines == 0

    def test_an_arduino_restart_keeps_the_people_on_board(self) -> None:
        counter = PassengerCounter()
        counter.apply(Boot("fw", 0), 0.0)
        counter.apply(hb(1, 10, 3), 1.0)
        counter.apply(Boot("fw", 0), 2.0)  # USB cable wiggled: the UNO rebooted
        counter.apply(hb(1, 2, 0), 3.0)
        snap = counter.snapshot()
        assert snap.on_board == 9 and snap.reboots == 1

    def test_a_restart_without_its_boot_line_is_also_detected(self) -> None:
        counter = PassengerCounter()
        counter.apply(hb(1, 10, 3), 1.0)
        counter.apply(hb(2, 1, 0), 2.0)  # totals went backwards
        assert counter.snapshot().on_board == 8

    def test_reset_at_the_terminal_and_never_negative(self) -> None:
        counter = PassengerCounter()
        counter.apply(hb(1, 5, 2), 0.0)
        counter.apply(parse_line(frame("ACK,RESET,2")), 1.0)
        assert counter.snapshot().on_board == 0
        counter.apply(hb(3, 0, 2), 2.0)  # two alightings nobody saw boarding
        assert counter.snapshot().on_board == 0

    def test_both_doors_count_both_ways_and_aborted_crossings_are_kept_apart(self) -> None:
        counter = PassengerCounter()
        lines = [("IN", 1, 0, "F"), ("IN", 2, 0, "R"), ("ABORTED", 2, 0, "F"), ("OUT", 2, 1, "F"), ("IN", 3, 1, "F")]
        for seq, (kind, b, a, which) in enumerate(lines, start=1):
            counter.apply(door(seq, kind, b, a, which), 1.0)
        snap = counter.snapshot()
        assert (snap.front_in, snap.front_out, snap.rear_in, snap.rear_out, snap.aborted) == (2, 1, 1, 0, 1)
        assert snap.on_board == 2  # the aborted crossing never changes the count
        counter.apply(parse_line(frame("ACK,RESET,6")), 2.0)
        snap = counter.snapshot()
        assert (snap.front_in, snap.rear_in, snap.aborted, snap.on_board) == (0, 0, 0, 0)

    def test_obstruction_flags(self) -> None:
        counter = PassengerCounter()
        counter.apply(door(1, "BLOCKED", 0, 0, "R"), 0.0)
        assert counter.snapshot().rear_obstructed
        counter.apply(door(2, "CLEARED", 0, 0, "R"), 1.0)
        assert not counter.snapshot().rear_obstructed
        counter.corrupted()
        assert counter.snapshot().corrupted_lines == 1


class FakeSerial:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = list(lines)
        self.written: list[bytes] = []
        self.closed = False

    def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        raise OSError("unplugged")

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def close(self) -> None:
        self.closed = True


class TestLinks:
    def test_the_serial_link_reads_asks_for_status_and_reconnects(self) -> None:
        opened: list[FakeSerial] = []

        def opener(port: str, baud: int) -> FakeSerial:
            assert (port, baud) == ("/dev/fake", 115_200)
            first = [frame("BOOT,fw,0").encode()] if not opened else []  # the same session, reconnected
            fake = FakeSerial([*first, frame("HB,1,3,1,0,0,0,1").encode(), b"garbage\n", b""])
            opened.append(fake)
            return fake

        link = SerialDoorLink("/dev/fake", opener=opener)
        link.start()
        try:
            import time

            deadline = time.monotonic() + 5
            while len(opened) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            link.stop()
        assert len(opened) >= 2 and opened[0].closed  # it reconnected after the port vanished
        assert opened[0].written[0] == b"STATUS\n"
        snap = link.snapshot()
        assert snap.on_board == 2 and snap.corrupted_lines >= 1

    def test_reset_is_sent_to_the_arduino(self) -> None:
        link = SerialDoorLink("/dev/fake")
        fake = FakeSerial([])
        link._serial = fake
        link.feed(frame("HB,1,4,0,0,0,0,1"))
        link.request_reset()
        assert fake.written == [b"RESET\n"] and link.snapshot().on_board == 0

    def test_the_simulator_never_goes_negative_or_over_capacity(self) -> None:
        link = SimulatedDoorLink(seed=3, capacity=10)
        link.start()
        link.stop()
        for _ in range(200):
            link.step()
            snap = link.snapshot()
            assert 0 <= snap.on_board <= 10
        snap = link.snapshot()
        assert snap.front_in + snap.rear_in == snap.boarded and snap.front_out + snap.rear_out == snap.alighted
        assert snap.rear_in > 0 and snap.front_out > 0 and snap.aborted > 0  # both doors, both ways
        link.request_reset()
        assert link.snapshot().on_board == 0
