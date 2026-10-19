"""The edge agent against the real core service (FastAPI + PostgreSQL), over real HTTP with uvicorn.

Skipped unless the core service and its test database are available (``CORE_TEST_DATABASE_URL``).
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2] / "backend"
pytestmark = pytest.mark.skipif(
    not os.environ.get("CORE_TEST_DATABASE_URL"), reason="needs CORE_TEST_DATABASE_URL (core service tests)"
)


@pytest.fixture
def core() -> Any:
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
    uvicorn = pytest.importorskip("uvicorn")
    from core_service.api import create_app
    from system_harness import build_system

    system = build_system(ai_enabled=False)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(system.context), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield system, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_payload_matches_the_ingestion_schema() -> None:
    sys.path.insert(0, str(ROOT / "src"))
    schemas = pytest.importorskip("core_service.api.schemas")
    from bus_edge.doors.link import SimulatedDoorLink
    from bus_edge.gps.nmea import Fix
    from bus_edge.telemetry.reading import build_payload
    from bus_edge.vision.occupancy import VisionResult

    body = build_payload(
        1.0, Fix(1.2, -77.3, 20.0), SimulatedDoorLink().snapshot(), VisionResult(1, 2, 0, 0, 0.9, False, 1.0), "m"
    )
    schemas.TelemetryIn.model_validate(body)  # extra="forbid": any unknown field would fail here
    seen = VisionResult(1, 1, 0, 0, 0.9, False, 1.0, frozenset({"S02-L1", "P04"}))
    with_cabin = build_payload(1.0, Fix(1.2, -77.3, 20.0), SimulatedDoorLink().snapshot(), seen, "m")
    assert schemas.TelemetryIn.model_validate(with_cabin).cabin.occupied == ["P04", "S02-L1"]


def test_the_agent_feeds_the_live_state_and_recovers_after_an_outage(core: Any) -> None:
    from factories import demo_track

    from bus_edge.agent import EdgeAgent
    from bus_edge.config import EdgeSettings
    from bus_edge.doors.link import SimulatedDoorLink
    from bus_edge.gps.sources import FixedGps, TerminalDetector
    from bus_edge.telemetry.outbox import Outbox
    from bus_edge.telemetry.uploader import IngestClient
    from bus_edge.vision.layout import maqueta_layout
    from bus_edge.vision.occupancy import OccupancyEstimator

    system, url = core
    key = system.context.settings.security.ingest_key
    point = demo_track().path.point_at(0.3)
    wall = system.clock.now
    doors = SimulatedDoorLink(seed=5)
    for _ in range(4):
        doors.step()
    settings = EdgeSettings.from_env({"EDGE_INGEST_KEY": key, "EDGE_BUS_ID": "1045", "EDGE_SERVER_URL": url})

    def agent_for(target: str) -> EdgeAgent:
        return EdgeAgent(
            settings,
            doors,
            FixedGps(point.lat, point.lng),
            {},
            OccupancyEstimator(maqueta_layout("1045")),
            outbox,
            IngestClient(target, key, timeout_s=2),
            TerminalDetector([]),
            "occ-maqueta-test",
            wall_clock=wall,
        )

    outbox = Outbox(":memory:")
    offline = agent_for("http://127.0.0.1:9")  # the link is down: readings wait in SQLite
    for _ in range(3):
        offline.cycle()
        system.clock.advance(3)
    assert len(outbox) == 3

    online = agent_for(url)
    report = online.cycle()  # backlog + new reading, one batch, oldest first
    assert report.sent == 4 and report.backlog == 0
    state = system.context.live_states.get("1045")
    assert state is not None and state.model_version == "occ-maqueta-test"
    assert state.people_on_board == doors.snapshot().on_board  # no cameras: the door count is used
