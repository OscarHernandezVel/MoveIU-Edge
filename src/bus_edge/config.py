"""Settings of the on-board agent, from ``EDGE_*`` environment variables (or an ``edge.env`` file).

Secrets (the ingest key) can come from ``EDGE_INGEST_KEY_FILE``. The server URL must use HTTPS, except for
loopback and private-network addresses (a model on the same Wi-Fi as the laptop running the service).
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class EdgeSettings:
    bus_id: str = "2101"
    server_url: str = "http://127.0.0.1:8080"
    ingest_key: str = ""
    #: ``auto`` (find the Arduino by USB id), a device path, or ``sim`` (simulated doors).
    serial_port: str = "auto"
    baud: int = 115_200
    #: ``front=0,rear=1`` (indexes, video files or /dev/v4l/by-path/... devices), ``sim`` (the model rendered in 3D,
    #: :mod:`bus_edge.vision.scene`), or ``off``. On a laptop the built-in camera is usually 0, so
    #: ``deploy/edge.env.example`` uses ``front=1,rear=2``.
    cameras: str = "front=0,rear=1"
    #: ``lego`` (yellow minifigure heads, by colour), ``yolo`` (the ONNX person model) or ``none``.
    detector: str = "lego"
    #: Yellow thresholds of the ``lego`` detector: hue_min,hue_max,saturation_min,value_min (OpenCV HSV).
    lego_hsv: str = "20,35,150,90"
    model_path: str = "models/yolov8n.onnx"
    model_input: int = 320
    vision_fps: float = 5.0
    conf_threshold: float = 0.4
    #: Class ids of the model that count as a passenger: ``0`` (COCO person) or the id of your own
    #: ``lego_minifigure`` class when the detector was trained on the model bus (docs/lego.md).
    detector_classes: str = "0"
    layout_path: str = "maqueta/bus_2101_layout.json"
    #: ``replay`` (route trace), ``fixed:lat,lng`` or the serial port of a NEO-6M (``COM5``, ``/dev/serial0``).
    gps: str = "replay"
    route_package: str = "../backend/demo_data/pasto_c1_c4.json"
    route_id: str = "C1"
    replay_speed_kmh: float = 25.0
    send_interval_s: float = 3.0
    outbox_path: str = "data/outbox.sqlite3"
    verify_tls: bool = True
    model_version: str = "occ-maqueta-0.1"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> EdgeSettings:
        source = dict(os.environ if env is None else env)
        key = source.get("EDGE_INGEST_KEY", "")
        key_file = source.get("EDGE_INGEST_KEY_FILE", "")
        if key_file:
            key = Path(key_file).read_text(encoding="utf-8").strip()
        defaults = cls()
        settings = cls(
            bus_id=source.get("EDGE_BUS_ID", defaults.bus_id),
            server_url=source.get("EDGE_SERVER_URL", defaults.server_url),
            ingest_key=key,
            serial_port=source.get("EDGE_SERIAL_PORT", defaults.serial_port),
            baud=int(source.get("EDGE_BAUD", defaults.baud)),
            cameras=source.get("EDGE_CAMERAS", defaults.cameras),
            detector=source.get("EDGE_DETECTOR", defaults.detector).strip().lower(),
            lego_hsv=source.get("EDGE_LEGO_HSV", defaults.lego_hsv),
            model_path=source.get("EDGE_MODEL_PATH", defaults.model_path),
            model_input=int(source.get("EDGE_MODEL_INPUT", defaults.model_input)),
            vision_fps=float(source.get("EDGE_VISION_FPS", defaults.vision_fps)),
            conf_threshold=float(source.get("EDGE_CONF_THRESHOLD", defaults.conf_threshold)),
            detector_classes=source.get("EDGE_DETECTOR_CLASSES", defaults.detector_classes),
            layout_path=source.get("EDGE_LAYOUT", defaults.layout_path),
            gps=source.get("EDGE_GPS", defaults.gps),
            route_package=source.get("EDGE_ROUTE_PACKAGE", defaults.route_package),
            route_id=source.get("EDGE_ROUTE_ID", defaults.route_id),
            replay_speed_kmh=float(source.get("EDGE_REPLAY_SPEED_KMH", defaults.replay_speed_kmh)),
            send_interval_s=float(source.get("EDGE_SEND_INTERVAL", defaults.send_interval_s)),
            outbox_path=source.get("EDGE_OUTBOX", defaults.outbox_path),
            verify_tls=source.get("EDGE_VERIFY_TLS", "true").strip().lower() not in ("0", "false", "no"),
            model_version=source.get("EDGE_MODEL_VERSION", defaults.model_version),
        )
        settings.validate()
        return settings

    def camera_devices(self) -> dict[str, int | str]:
        if self.cameras.strip().lower() in ("", "off", "none"):
            return {}
        devices: dict[str, int | str] = {}
        for part in self.cameras.split(","):
            name, _, device = part.partition("=")
            devices[name.strip()] = int(device) if device.strip().isdigit() else device.strip()
        return devices

    def simulated_cameras(self) -> bool:
        devices = self.camera_devices()
        return bool(devices) and all(device == "sim" for device in devices.values())

    def detector_class_ids(self) -> tuple[int, ...]:
        return tuple(int(part) for part in self.detector_classes.split(",") if part.strip().isdigit())

    def validate(self) -> None:
        problems: list[str] = []
        if not self.bus_id or len(self.bus_id) > 20 or not self.bus_id.replace("-", "").isalnum():
            problems.append("EDGE_BUS_ID must be 1-20 letters, digits or dashes")
        if not self.ingest_key:
            problems.append(
                "EDGE_INGEST_KEY (or EDGE_INGEST_KEY_FILE) is required: it is CORE_INGEST_KEY of the server"
            )
        parsed = urlparse(self.server_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            problems.append("EDGE_SERVER_URL must be http(s)://host[:port]")
        elif parsed.scheme == "http" and not _is_local(parsed.hostname):
            problems.append("EDGE_SERVER_URL must use https:// outside the local network (RNF-5.1)")
        if not 1.0 <= self.send_interval_s <= 60.0:
            problems.append("EDGE_SEND_INTERVAL must be between 1 and 60 seconds (v7: every 3 s)")
        if not 0.5 <= self.vision_fps <= 30:
            problems.append("EDGE_VISION_FPS must be between 0.5 and 30")
        if not 0.05 <= self.conf_threshold <= 0.95:
            problems.append("EDGE_CONF_THRESHOLD must be between 0.05 and 0.95")
        if not self.detector_class_ids() or len(self.detector_class_ids()) != len(
            [part for part in self.detector_classes.split(",") if part.strip()]
        ):
            problems.append("EDGE_DETECTOR_CLASSES must be class ids separated by commas, e.g. 0 or 0,1")
        if set(self.camera_devices()) - {"front", "rear"}:
            problems.append("EDGE_CAMERAS names must be front and/or rear, e.g. front=0,rear=1")
        if "sim" in self.camera_devices().values() and not self.simulated_cameras():
            problems.append("EDGE_CAMERAS: sim goes for both cameras or for none (front=sim,rear=sim)")
        if self.detector not in ("lego", "yolo", "none"):
            problems.append("EDGE_DETECTOR must be lego, yolo or none")
        try:
            from bus_edge.vision.lego import parse_hsv

            parse_hsv(self.lego_hsv)
        except ValueError as error:
            problems.append(str(error))
        if problems:
            raise ConfigError(problems)


def _is_local(host: str) -> bool:
    if host in ("localhost",) or host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def load_env_file(path: str | Path) -> dict[str, str]:
    """``KEY=value`` lines (``#`` comments), as in ``deploy/edge.env.example``."""
    values: dict[str, str] = {}
    file = Path(path)
    if not file.exists():
        return values
    for line in file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values
