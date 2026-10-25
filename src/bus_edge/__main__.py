"""Command line of the on-board agent (messages in Spanish for the operator).

    python -m bus_edge run          agent: doors + cameras + GPS -> core service, every 3 s
    python -m bus_edge doors        live door counter (to test the wiring of the FC-51)
    python -m bus_edge check        hardware check: serial ports, cameras, model, Arduino
    python -m bus_edge calibrate --camera front     draw the positions (RF-9.4); device from EDGE_CAMERAS
    python -m bus_edge layout       write the layout of the LEGO-scale model (41 seats, 8 standing, 2 doors)
    python -m bus_edge preview      simulated camera view with what the detector recognises (PNG)

Settings come from ``EDGE_*`` variables or from the file given with ``--env`` (default ``edge.env``).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from bus_edge.config import ConfigError, EdgeSettings, load_env_file


def _settings(env_file: str) -> EdgeSettings:
    values = {**load_env_file(env_file), **os.environ}
    return EdgeSettings.from_env(values)


def _keep_awake(on: bool) -> None:
    """Windows: the laptop must not go to sleep while the agent runs (the screen may still turn off). Without this
    a presentation on battery or a long pause suspends the cameras, the Arduino link and the uploads."""
    if sys.platform != "win32":
        return
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | (es_system_required if on else 0))


def cmd_run(args: argparse.Namespace) -> int:
    from bus_edge.factory import build_agent

    settings = _settings(args.env)
    agent = build_agent(settings)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    agent.start()
    every = f"{settings.send_interval_s:g}"
    print(f"Bus {settings.bus_id} enviando a {settings.server_url} cada {every} s (Ctrl+C para salir)")
    _keep_awake(True)
    try:
        agent.run(stop, on_report=lambda report: print(report.line(settings.route_id), flush=True))
    finally:
        _keep_awake(False)
        agent.stop()
    return 0


def cmd_doors(args: argparse.Namespace) -> int:
    from bus_edge.factory import build_doors

    values = {**load_env_file(args.env), **os.environ}
    values.setdefault("EDGE_INGEST_KEY", "not-needed-here")
    link = build_doors(EdgeSettings.from_env(values))
    link.start()
    print("Cruce cada puerta con la mano: exterior -> interior = sube, interior -> exterior = baja. Ctrl+C para salir.")
    try:
        while True:
            s = link.snapshot()
            health = "OK" if s.healthy(time.monotonic()) else "SIN SEÑAL"
            print(
                f"\rArduino {health} | subieron {s.boarded:3d} | bajaron {s.alighted:3d} | a bordo {s.on_board:3d} "
                f"| del +{s.front_in}/-{s.front_out} tra +{s.rear_in}/-{s.rear_out} | abortados {s.aborted} "
                f"| obstruida del/tra {int(s.front_obstructed)}/{int(s.rear_obstructed)} "
                f"| líneas perdidas {s.lost_lines} corruptas {s.corrupted_lines}   ",
                end="",
                flush=True,
            )
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
    finally:
        link.stop()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from bus_edge.hardware_check import run_check

    values = {**load_env_file(args.env), **os.environ}
    return run_check(values)


def calibration_device(camera: str, given: str | None, values: dict[str, str]) -> int | str:
    """``--device`` if given; otherwise the device of that camera in ``EDGE_CAMERAS``, so the polygons are drawn on
    the camera the agent will read (on a laptop the built-in camera is 0 and the webcams 1 and 2)."""
    if given is None:
        values = {**values}
        values.setdefault("EDGE_INGEST_KEY", "not-needed-here")
        devices = EdgeSettings.from_env(values).camera_devices()
        return devices.get(camera, 0 if camera == "front" else 1)
    return int(given) if given.isdigit() else given


def cmd_calibrate(args: argparse.Namespace) -> int:
    from bus_edge.calibrate import run

    device = calibration_device(args.camera, args.device, {**load_env_file(args.env), **os.environ})
    print(f"Calibrando la cámara {args.camera} (dispositivo {device}).")
    return run(args.layout, args.camera, device, args.bus)


def cmd_preview(args: argparse.Namespace) -> int:
    """What a simulated camera sees and what the detector recognises in it, drawn over the frame. Only the simulation:
    a frame of a real camera is never written to disk (RNF-7.2)."""
    import cv2
    import numpy as np

    from bus_edge.vision import scene
    from bus_edge.vision.lego import YellowFigureDetector, parse_hsv
    from bus_edge.vision.occupancy import CameraObservation, OccupancyEstimator

    values = {**load_env_file(args.env), **os.environ}
    detector = YellowFigureDetector(parse_hsv(values.get("EDGE_LEGO_HSV", "20,35,150,90")))
    occupied = scene.SimulatedPassengers(lambda: args.people, seed=args.seed).occupied()
    layout = scene.projected_layout(args.bus)
    estimator = OccupancyEstimator(layout, anchor_height=detector.anchor_height)
    frames = {name: scene.render(scene.camera_pose(name), occupied) for name in ("front", "rear")}
    seen = {name: CameraObservation(True, tuple(detector.detect(frame)), 0.0) for name, frame in frames.items()}
    result = estimator.estimate(seen, 0.0, moving=False)
    pose = scene.camera_pose(args.camera)
    image = frames[args.camera].copy()

    def outline(polygon: tuple[tuple[float, float], ...], colour: tuple[int, int, int], fill: bool = False) -> None:
        if len(polygon) < 3:
            return
        points = (np.array(polygon) * [pose.width, pose.height]).astype(np.int32)
        if fill:
            overlay = image.copy()
            cv2.fillPoly(overlay, [points], colour)
            cv2.addWeighted(overlay, 0.35, image, 0.65, 0, dst=image)
        cv2.polylines(image, [points], True, colour, 1, cv2.LINE_AA)

    for name, quad in scene.openings():
        outline(scene.projected_quad(pose, quad), (0, 0, 220) if name.startswith("D-") else (220, 160, 0))
    for position in layout.for_camera(args.camera):
        taken = position.id in result.occupied
        outline(position.polygon, (40, 200, 40) if taken else (150, 150, 150), fill=taken)
    for d in seen[args.camera].detections:
        cv2.rectangle(image, (int(d.x1 * pose.width), int(d.y1 * pose.height)),
                      (int(d.x2 * pose.width), int(d.y2 * pose.height)), (255, 0, 255), 1)  # fmt: skip
    out = Path(args.out or f"vista_{args.camera}.png")
    cv2.imwrite(str(out), image)
    truth = set(occupied)
    print(f"{out}: cámara {args.camera}, {len(seen[args.camera].detections)} cabezas amarillas detectadas.")
    print(
        f"Bus completo: {len(truth)} figuras puestas, {len(result.occupied)} posiciones reconocidas "
        f"({len(result.occupied & truth)} correctas), {result.seated} sentadas, {result.standing} de pie, "
        f"{result.standing_out_of_position} fuera de posición."
    )
    print("Verde: posición ocupada · gris: libre · rojo: puertas · azul: ventanas · magenta: cabezas detectadas.")
    return 0 if result.occupied == truth else 1


def cmd_layout(args: argparse.Namespace) -> int:
    from bus_edge.vision.layout import maqueta_layout

    path = Path(args.output)
    if path.exists() and not args.force:
        print(f"{path} ya existe (use --force para reemplazarlo; perdería la calibración).")
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    if args.projected:
        from bus_edge.vision.scene import projected_layout

        projected_layout(args.bus).save(path)
        print(f"Escrito {path}: 41 sillas, 8 de pie, 2 puertas, proyectadas desde la pose de las cámaras (docs/maqueta.md).")
        print("Con cámaras reales es un punto de partida: compruébelo con 'calibrate' y corrija lo que no coincida.")
        return 0
    maqueta_layout(args.bus).save(path)
    print(f"Escrito {path}: 41 sillas, 8 posiciones de pie, 2 puertas (sin calibrar).")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("EDGE_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="bus_edge", description="Agente a bordo del bus (maqueta)")
    parser.add_argument("--env", default="edge.env", help="archivo de configuración (EDGE_*)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("doors").set_defaults(func=cmd_doors)
    sub.add_parser("check").set_defaults(func=cmd_check)
    calibrate = sub.add_parser("calibrate")
    calibrate.add_argument("--camera", choices=("front", "rear"), required=True)
    calibrate.add_argument("--device", default=None, help="por defecto, el de EDGE_CAMERAS para esa cámara")
    calibrate.add_argument("--layout", default="maqueta/bus_2101_layout.json")
    calibrate.add_argument("--bus", default="2101")
    calibrate.set_defaults(func=cmd_calibrate)
    preview = sub.add_parser("preview")
    preview.add_argument("--camera", choices=("front", "rear"), default="front")
    preview.add_argument("--people", type=int, default=25, help="figuras en la maqueta simulada (0-49)")
    preview.add_argument("--seed", type=int, default=7)
    preview.add_argument("--bus", default="2101")
    preview.add_argument("--out", default=None, help="imagen PNG (por defecto vista_<cámara>.png)")
    preview.set_defaults(func=cmd_preview)
    layout = sub.add_parser("layout")
    layout.add_argument("--output", default="maqueta/bus_2101_layout.json")
    layout.add_argument("--bus", default="2101")
    layout.add_argument("--force", action="store_true")
    layout.add_argument("--projected", action="store_true", help="polígonos proyectados desde la pose de las cámaras")
    layout.set_defaults(func=cmd_layout)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as error:
        print("Configuración incompleta:")
        for problem in error.problems:
            print(f"  - {problem}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
