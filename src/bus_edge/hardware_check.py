"""``python -m bus_edge check``: what is connected and does it work? (run it after every wiring change)."""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping
from pathlib import Path

from bus_edge.doors.link import ARDUINO_USB_IDS


def _serial_ports() -> str | None:
    try:
        from serial.tools import list_ports
    except ImportError:
        print("  pyserial no está instalado: pip install -r requirements.txt")
        return None
    arduino = None
    ports = list(list_ports.comports())
    if not ports:
        print("  No hay puertos serie. ¿Está conectado el cable USB del Arduino?")
    for port in ports:
        is_arduino = port.vid is not None and (port.vid, port.pid) in ARDUINO_USB_IDS
        ids = f"{port.vid:04x}:{port.pid:04x}" if port.vid is not None else "----:----"
        print(f"  {port.device:<16} {ids}  {port.description}{'  <- Arduino' if is_arduino else ''}")
        if is_arduino and arduino is None:
            arduino = str(port.device)
    return arduino


def _arduino_lines(port: str, seconds: float = 4.0) -> None:
    import serial

    from bus_edge.doors.protocol import LineError, parse_line

    print(f"  Escuchando {port} durante {seconds:g} s (pase la mano por los sensores)...")
    good = bad = 0
    with serial.Serial(port, 115_200, timeout=0.5) as link:
        time.sleep(2.0)  # opening the port resets the UNO; its bootloader needs ~1.5 s
        link.write(b"STATUS\n")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            raw = link.readline().decode("ascii", errors="replace").strip()
            if not raw:
                continue
            try:
                print(f"    {parse_line(raw)}")
                good += 1
            except LineError:
                print(f"    (línea inválida) {raw!r}")
                bad += 1
    print(f"  {good} líneas válidas, {bad} inválidas.")


def _cameras() -> None:
    try:
        import cv2
    except ImportError:
        print("  OpenCV no está instalado: pip install -r requirements.txt")
        return
    from bus_edge.vision.cameras import open_capture

    found = 0
    for index in range(5):
        capture = open_capture(index)  # as the agent opens them, so the indices printed here are the ones to use
        if not capture.isOpened():
            continue
        ok, frame = capture.read()
        if ok:
            found += 1
            height, width = frame.shape[:2]
            codec = int(capture.get(cv2.CAP_PROP_FOURCC)).to_bytes(4, "little").decode("ascii", "replace")
            print(
                f"  cámara {index}: {width}x{height} {codec}, brillo medio {frame.mean():.0f} (útil entre 20 y 235)"
                + ("  <- integrada del portátil?" if index == 0 and sys.platform == "win32" else "")
            )
        del frame
        capture.release()
    if not found:
        print("  No se encontró ninguna cámara. Revise el cable y la alimentación USB.")
    if sys.platform == "win32":
        print("  Windows: las webcams USB suelen ser 1 y 2 (EDGE_CAMERAS=front=1,rear=2). Si falta alguna, revise")
        print("  Configuración > Privacidad y seguridad > Cámara > 'Permitir que las aplicaciones de escritorio...'.")
    elif not found:
        print("  Linux: ls /dev/video*")


def _model(path: str) -> None:
    if not Path(path).exists():
        print(f"  {path} no existe: exporte YOLOv8n a ONNX (ver edge/README.md). Sin modelo solo cuentan las puertas.")
        return
    import numpy as np

    from bus_edge.vision.detector import YoloOnnxDetector

    detector = YoloOnnxDetector(path)
    frame = np.full((480, 640, 3), 128, dtype=np.uint8)
    detector.detect(frame)
    started = time.perf_counter()
    for _ in range(5):
        detector.detect(frame)
    per_frame = (time.perf_counter() - started) / 5
    print(f"  {detector.version}: {per_frame * 1000:.0f} ms por imagen (~{1 / per_frame:.1f} fps por cámara)")


def run_check(values: Mapping[str, str]) -> int:
    print("1. Puertos serie")
    arduino = _serial_ports()
    print("2. Arduino (contador de puertas)")
    configured = values.get("EDGE_SERIAL_PORT", "auto")
    port = arduino if configured == "auto" else configured
    if port and port != "sim":
        try:
            _arduino_lines(port)
        except Exception as error:  # noqa: BLE001 - report and continue with the other checks
            hint = (
                "Windows: cierre el Monitor serie del Arduino IDE, que ocupa el puerto"
                if sys.platform == "win32"
                else "Linux: ¿el usuario está en el grupo dialout?"
            )
            print(f"  No se pudo abrir {port}: {error} ({hint})")
    else:
        print("  Sin Arduino.")
    print("3. Cámaras")
    _cameras()
    print("4. Modelo de visión")
    _model(values.get("EDGE_MODEL_PATH", "models/yolov8n.onnx"))
    return 0
