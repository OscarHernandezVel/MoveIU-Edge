"""Payload, SQLite outbox, HTTP client, settings and the agent cycle (with fakes for the hardware)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from bus_edge.agent import EdgeAgent
from bus_edge.config import ConfigError, EdgeSettings, load_env_file
from bus_edge.doors.link import SimulatedDoorLink
from bus_edge.gps.nmea import Fix
from bus_edge.gps.sources import FixedGps, TerminalDetector
from bus_edge.telemetry.outbox import Outbox
from bus_edge.telemetry.reading import build_payload
from bus_edge.telemetry.uploader import IngestClient, SendResult
from bus_edge.vision.layout import maqueta_layout
from bus_edge.vision.occupancy import OccupancyEstimator, VisionResult

KEY = "clave-de-prueba-0123456789"


def payload(ts: float, seated: int = 1) -> dict[str, Any]:
    return {"ts": ts, "lat": 1.2, "lng": -77.3, "seated": seated, "standing": 0, "doors_total": 1,
            "confidence": 0.9, "model_version": "t"}  # fmt: skip


# ------------------------------------------------------------------- payload
class TestPayload:
    def test_only_numbers_bounded_to_what_the_server_accepts(self) -> None:
        doors = SimulatedDoorLink().snapshot()
        vision = VisionResult(3, 2, 9_999, 50, 1.7, True, 0.5)
        body = build_payload(1_790_000_000.123456, Fix(1.21, -77.28, 250.0), doors, vision, "m" * 60)
        assert body["standing_out_of_position"] == 500 and body["wheelchair_occupied"] == 20
        assert body["confidence"] == 1.0 and body["speed_kmh"] == 200.0 and len(body["model_version"]) == 40
        assert body["approximate_count"] is True and body["ts"] == 1_790_000_000.123
        assert set(body) <= {
            "ts", "lat", "lng", "seated", "standing", "standing_out_of_position", "wheelchair_occupied",
            "doors_total", "confidence", "approximate_count", "model_version", "speed_kmh", "cabin",
        }  # fmt: skip
        no_speed = build_payload(1.0, Fix(1.0, 1.0, None), doors, vision, "m")
        assert "speed_kmh" not in no_speed

    def test_the_cabin_travels_as_position_ids_only_when_a_camera_saw_it(self) -> None:
        doors = SimulatedDoorLink().snapshot()
        seen = VisionResult(2, 1, 0, 0, 0.9, False, 1.0, frozenset({"S05-L1", "P04", "S02-R2"}))
        assert build_payload(1.0, Fix(1.0, 1.0, None), doors, seen, "m")["cabin"] == {
            "occupied": ["P04", "S02-R2", "S05-L1"]
        }
        blind = VisionResult(0, 0, 0, 0, 0.0, True, 0.0)  # cameras off or unplugged: unknown, not "empty"
        assert "cabin" not in build_payload(1.0, Fix(1.0, 1.0, None), doors, blind, "m")
        empty = VisionResult(0, 0, 0, 0, 0.95, False, 1.0)  # seen and really empty
        assert build_payload(1.0, Fix(1.0, 1.0, None), doors, empty, "m")["cabin"] == {"occupied": []}


# -------------------------------------------------------------------- outbox
class TestOutbox:
    def test_oldest_first_and_survives_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "outbox.sqlite3"
        box = Outbox(path)
        for ts in (30.0, 10.0, 20.0):
            box.put("2101", payload(ts))
        box.close()
        box = Outbox(path)  # power cut and reboot
        pending = box.oldest(10)
        assert [p.ts for p in pending] == [10.0, 20.0, 30.0] and len(box) == 3
        box.failed([pending[0].id])
        assert box.oldest(1)[0].attempts == 1
        box.remove([p.id for p in pending[:2]])
        assert [p.ts for p in box.oldest(10)] == [30.0]

    def test_it_is_bounded_by_rows_and_by_age(self) -> None:
        box = Outbox(":memory:", max_rows=3, max_age_s=100.0)
        for ts in range(1, 6):
            box.put("2101", payload(float(ts)))
        assert [p.ts for p in box.oldest(10)] == [3.0, 4.0, 5.0]  # the oldest dropped first
        box.put("2101", payload(1000.0))
        assert [p.ts for p in box.oldest(10)] == [1000.0]


# -------------------------------------------------------------- HTTP client
class Recorder(BaseHTTPRequestHandler):
    responses: ClassVar[list[tuple[int, Any]]] = []
    received: ClassVar[list[tuple[str, dict[str, str], Any]]] = []

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Recorder.received.append((self.path, dict(self.headers), body))
        status, answer = Recorder.responses.pop(0) if Recorder.responses else (200, {"status": "applied"})
        raw = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    Recorder.responses, Recorder.received = [], []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


class TestClient:
    def test_one_reading_and_the_key_header(self, server: str) -> None:
        result = IngestClient(server, KEY).send_one("2101", payload(1.0))
        path, headers, body = Recorder.received[0]
        assert result.ok and path == "/api/v1/internal/buses/2101/telemetry"
        assert headers["X-Ingest-Key"] == KEY and body["seated"] == 1

    def test_a_batch_reports_rejected_items(self, server: str) -> None:
        Recorder.responses = [(200, {"items": [{"index": 0, "status": "applied"}, {"index": 1, "status": "rejected"}]})]
        result = IngestClient(server, KEY).send_batch([("2101", payload(1.0)), ("2101", payload(2.0))])
        _, _, body = Recorder.received[0]
        assert result.ok and result.rejected_indexes == (1,) and body["readings"][1]["bus_id"] == "2101"

    @pytest.mark.parametrize(
        ("status", "retryable"), [(500, True), (503, True), (429, True), (401, True), (422, False)]
    )
    def test_errors_are_classified(self, server: str, status: int, retryable: bool) -> None:
        Recorder.responses = [(status, {"error": {}})]
        result = IngestClient(server, KEY).send_one("2101", payload(1.0))
        assert not result.ok and result.retryable is retryable and result.status == status

    def test_an_unreachable_server_is_retryable(self) -> None:
        result = IngestClient("http://127.0.0.1:9", KEY, timeout_s=1).send_one("2101", payload(1.0))
        assert result == SendResult(False, True, None)


# ------------------------------------------------------------------ settings
class TestSettings:
    def test_defaults_need_only_the_key(self) -> None:
        settings = EdgeSettings.from_env({"EDGE_INGEST_KEY": KEY})
        assert settings.bus_id == "2101" and settings.camera_devices() == {"front": 0, "rear": 1}
        assert EdgeSettings.from_env({"EDGE_INGEST_KEY": KEY, "EDGE_CAMERAS": "off"}).camera_devices() == {}
        assert settings.detector_class_ids() == (0,)
        lego = EdgeSettings.from_env({"EDGE_INGEST_KEY": KEY, "EDGE_DETECTOR_CLASSES": "1, 2"})
        assert lego.detector_class_ids() == (1, 2)

    @pytest.mark.parametrize(
        ("env", "fragment"),
        [
            ({}, "EDGE_INGEST_KEY"),
            ({"EDGE_INGEST_KEY": KEY, "EDGE_SERVER_URL": "http://bus.example.com"}, "https"),
            ({"EDGE_INGEST_KEY": KEY, "EDGE_SEND_INTERVAL": "0.2"}, "EDGE_SEND_INTERVAL"),
            ({"EDGE_INGEST_KEY": KEY, "EDGE_CAMERAS": "roof=0"}, "EDGE_CAMERAS"),
            ({"EDGE_INGEST_KEY": KEY, "EDGE_BUS_ID": "bus 1"}, "EDGE_BUS_ID"),
            ({"EDGE_INGEST_KEY": KEY, "EDGE_DETECTOR_CLASSES": "lego"}, "EDGE_DETECTOR_CLASSES"),
        ],
    )
    def test_invalid_settings_are_explained(self, env: dict[str, str], fragment: str) -> None:
        with pytest.raises(ConfigError) as caught:
            EdgeSettings.from_env(env)
        assert fragment in str(caught.value)

    def test_private_addresses_may_use_http_and_the_key_can_come_from_a_file(self, tmp_path: Path) -> None:
        secret = tmp_path / "key"
        secret.write_text(KEY + "\n")
        for url in ("http://192.168.1.20:8080", "http://localhost:8080", "http://portatil.local:8080"):
            assert (
                EdgeSettings.from_env({"EDGE_INGEST_KEY_FILE": str(secret), "EDGE_SERVER_URL": url}).ingest_key == KEY
            )
        env_file = tmp_path / "edge.env"
        env_file.write_text("# comentario\nEDGE_BUS_ID=2102\nEDGE_ROUTE_ID='C2'\nbasura\n")
        assert load_env_file(env_file) == {"EDGE_BUS_ID": "2102", "EDGE_ROUTE_ID": "C2"}
        assert load_env_file(tmp_path / "missing.env") == {}


# --------------------------------------------------------------------- agent
class FakeClient:
    def __init__(self) -> None:
        self.up = True
        self.sent: list[list[float]] = []

    def send_one(self, bus_id: str, body: dict[str, Any]) -> SendResult:
        return self._send([body])

    def send_batch(self, items: list[tuple[str, dict[str, Any]]]) -> SendResult:
        return self._send([body for _, body in items])

    def _send(self, bodies: list[dict[str, Any]]) -> SendResult:
        if not self.up:
            return SendResult(False, True, None)
        self.sent.append([b["ts"] for b in bodies])
        return SendResult(True, False, 200)


class Clock:
    def __init__(self) -> None:
        self.t = 1_790_000_000.0

    def __call__(self) -> float:
        return self.t


class TerminalGps(FixedGps):
    def __init__(self) -> None:
        super().__init__(1.2, -77.3)
        self.terminal = False

    def at_terminal(self) -> bool:
        return self.terminal


def make_agent(client: FakeClient, clock: Clock, gps: Any = None) -> tuple[EdgeAgent, SimulatedDoorLink]:
    settings = EdgeSettings.from_env({"EDGE_INGEST_KEY": KEY, "EDGE_CAMERAS": "off"})
    doors = SimulatedDoorLink(seed=1, clock=clock)
    agent = EdgeAgent(
        settings,
        doors,
        gps or FixedGps(1.2, -77.3),
        {},
        OccupancyEstimator(maqueta_layout()),
        Outbox(":memory:"),
        client,  # type: ignore[arg-type]
        TerminalDetector([]),
        "occ-test",
        wall_clock=clock,
        monotonic=clock,
    )
    return agent, doors


class TestAgent:
    def test_each_cycle_queues_and_sends_one_reading(self) -> None:
        client, clock = FakeClient(), Clock()
        agent, doors = make_agent(client, clock)
        doors.step()
        report = agent.cycle()
        assert report.queued and report.sent == 1 and report.backlog == 0
        assert report.vision is not None and report.vision.confidence == 0 and report.vision.approximate_count
        assert "puertas" in report.line("C1")

    def test_readings_wait_offline_and_go_out_oldest_first_in_one_batch(self) -> None:
        client, clock = FakeClient(), Clock()
        agent, _ = make_agent(client, clock)
        client.up = False
        for _ in range(4):
            clock.t += 3
            report = agent.cycle()
        assert report.backlog == 4 and client.sent == []  # nothing lost, nothing sent
        client.up = True
        clock.t += 120  # past the back-off
        report = agent.cycle()
        assert report.sent == 5 and report.backlog == 0
        assert client.sent[-1] == sorted(client.sent[-1])  # chronological (RNF-8.2)

    def test_arriving_at_the_terminal_resets_the_door_counter(self) -> None:
        client, clock = FakeClient(), Clock()
        gps = TerminalGps()
        agent, doors = make_agent(client, clock, gps)
        for _ in range(5):
            doors.step()
        agent.cycle()
        gps.terminal = True
        agent.cycle()
        assert doors.snapshot().on_board == 0

    def test_without_a_position_no_reading_is_made(self) -> None:
        class NoFix(FixedGps):
            def current(self) -> Fix | None:
                return None

        client, clock = FakeClient(), Clock()
        agent, _ = make_agent(client, clock, NoFix(0, 0))
        report = agent.cycle()
        assert not report.queued and report.backlog == 0 and "GPS" in report.note
