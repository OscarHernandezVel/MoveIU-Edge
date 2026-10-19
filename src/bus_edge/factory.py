"""Builds the agent from the settings (composition root of the edge)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from bus_edge.agent import EdgeAgent
from bus_edge.config import EdgeSettings
from bus_edge.doors.link import DoorLink, SerialDoorLink, SimulatedDoorLink
from bus_edge.gps.sources import FixedGps, GpsSource, NmeaSerialGps, RouteReplayGps, TerminalDetector, load_route
from bus_edge.telemetry.outbox import Outbox
from bus_edge.telemetry.uploader import IngestClient
from bus_edge.vision.cameras import CameraWorker, OpenCvCamera
from bus_edge.vision.detector import Detector, NullDetector, YoloOnnxDetector
from bus_edge.vision.layout import load_layout, maqueta_layout
from bus_edge.vision.occupancy import OccupancyEstimator

log = logging.getLogger(__name__)


def build_doors(settings: EdgeSettings) -> DoorLink:
    if settings.serial_port == "sim":
        return SimulatedDoorLink()
    return SerialDoorLink(settings.serial_port, settings.baud)


def build_gps(settings: EdgeSettings) -> tuple[GpsSource, TerminalDetector]:
    spec = settings.gps
    if spec.startswith("fixed:"):
        lat, lng = (float(v) for v in spec[6:].split(","))
        return FixedGps(lat, lng), TerminalDetector([])
    outbound, back, _ = load_route(settings.route_package, settings.route_id)
    terminals = [outbound.points[0], outbound.points[-1]]
    if spec == "replay":
        return RouteReplayGps(outbound, back, settings.replay_speed_kmh), TerminalDetector(terminals)
    return NmeaSerialGps(spec), TerminalDetector(terminals)


def build_detector(settings: EdgeSettings) -> Detector:
    model = Path(settings.model_path)
    if not model.exists():
        log.warning("Model %s not found: cameras will run without detections (see edge/README.md)", model)
        return NullDetector()
    return YoloOnnxDetector(model, settings.model_input, settings.conf_threshold, classes=settings.detector_class_ids())


def _camera_opener(device: int | str) -> Callable[[], OpenCvCamera]:
    return lambda: OpenCvCamera(device)


def build_agent(settings: EdgeSettings) -> EdgeAgent:
    layout_path = Path(settings.layout_path)
    layout = load_layout(layout_path) if layout_path.exists() else maqueta_layout(settings.bus_id)
    for problem in layout.problems():
        log.warning("Layout: %s", problem)
    if layout.calibrated_share() < 1.0:
        log.warning(
            "Only %.0f %% of the positions are calibrated: run 'python -m bus_edge calibrate'",
            layout.calibrated_share() * 100,
        )
    detector = build_detector(settings)
    workers = {
        name: CameraWorker(name, _camera_opener(device), detector, settings.vision_fps)
        for name, device in settings.camera_devices().items()
    }
    gps, terminal = build_gps(settings)
    version = settings.model_version if detector.version != "no-model" else f"{settings.model_version}-doors"
    return EdgeAgent(
        settings,
        build_doors(settings),
        gps,
        workers,
        OccupancyEstimator(layout),
        Outbox(settings.outbox_path),
        IngestClient(settings.server_url, settings.ingest_key, verify_tls=settings.verify_tls),
        terminal,
        version,
    )
