"""3D model of the assembled bus model (OBJ + MTL, millimetres), built from the pieces of ``make_cut_files.py``.

    python maqueta/make_3d_model.py          # writes maqueta/maqueta_3d.obj and maqueta_3d.mtl

Opens in Windows 3D Viewer, Blender or any online OBJ viewer. Every piece comes from the same constants and
functions as the cut files (walls with their doors and windows, floor marks, seats, camera bars, the four FC-51
brackets), so a change there shows up here. Added for reference only: the base board, the Arduino and breadboard,
the laptop beside the board, the webcams, the FC-51 boards and a few LEGO minifigures.

Axes: x along the bus from the outer face of the front (0..720), y across from the outer face of the right wall
(the door side, 0..166), z up from the underside of the floor. The OBJ is written Y-up, as viewers expect.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import make_cut_files as cut  # noqa: E402

T, L, W, H = cut.T, cut.LENGTH, cut.WIDTH, cut.HEIGHT
FLOOR_TOP = T

MATERIALS = {
    "mdf": (0.80, 0.66, 0.47),
    "asiento": (0.55, 0.40, 0.27),
    "grabado": (0.62, 0.48, 0.33),
    "base": (0.35, 0.35, 0.38),
    "camara": (0.08, 0.08, 0.08),
    "lente": (0.25, 0.27, 0.32),
    "fc51": (0.10, 0.30, 0.70),
    "arduino": (0.00, 0.50, 0.55),
    "protoboard": (0.95, 0.95, 0.95),
    "portatil": (0.20, 0.20, 0.22),
    "pantalla": (0.10, 0.15, 0.25),
    "lego_piel": (0.98, 0.80, 0.05),
    "lego_piernas": (0.10, 0.20, 0.60),
    "lego_rojo": (0.80, 0.10, 0.10),
    "lego_verde": (0.10, 0.55, 0.20),
    "lego_blanco": (0.92, 0.92, 0.92),
}

Box = tuple[float, float, float, float, float, float]  # x0, x1, y0, y1, z0, z1
objects: list[tuple[str, str, list[Box]]] = []  # (name, material, boxes)


def add(name: str, material: str, *boxes: Box) -> None:
    objects.append((name, material, list(boxes)))


def panel_cells(w: float, h: float, holes: list[tuple[float, float, float, float]]) -> list[tuple[float, ...]]:
    """Rectangles (u0, u1, v0, v1) that cover a w x h panel except its rectangular holes (u0, v0, hw, hh)."""
    us = sorted({0.0, w, *(u for hu, _, hw, _ in holes for u in (hu, hu + hw))})
    vs = sorted({0.0, h, *(v for _, hv, _, hh in holes for v in (hv, hv + hh))})
    cells: list[tuple[float, ...]] = []
    for v0, v1 in zip(vs, vs[1:]):
        run: list[float] | None = None
        for u0, u1 in zip(us, us[1:]):
            cu, cv = (u0 + u1) / 2, (v0 + v1) / 2
            solid = not any(hu < cu < hu + hw and hv < cv < hv + hh for hu, hv, hw, hh in holes)
            if solid and run:
                run[1] = u1
            elif solid:
                run = [u0, u1]
            elif run:
                cells.append((run[0], run[1], v0, v1))
                run = None
        if run:
            cells.append((run[0], run[1], v0, v1))
    return cells


def svg_holes(piece: cut.Piece) -> list[tuple[float, float, float, float]]:
    """Holes of a cut piece with v measured upwards (the SVG y grows downwards)."""
    return [(hx, piece.h - hy - hh, hw, hh) for hx, hy, hw, hh in piece.holes]


def body() -> None:
    for right in (True, False):
        wall = cut.side_wall(right)
        y0 = 0.0 if right else W - T
        # seen from outside the right wall has the front on the right: its u axis runs from the rear
        boxes = [
            (L - u1 if right else u0, L - u0 if right else u1, y0, y0 + T, v0, v1)
            for u0, u1, v0, v1 in panel_cells(wall.w, wall.h, svg_holes(wall))
        ]
        add(wall.name, "mdf", *boxes)
    for front in (True, False):
        wall = cut.end_wall(front)
        x0 = 0.0 if front else L - T
        boxes = [(x0, x0 + T, T + u0, T + u1, v0, v1) for u0, u1, v0, v1 in panel_cells(wall.w, wall.h, svg_holes(wall))]
        add(wall.name, "mdf", *boxes)
        bar_x = T if front else L - T - 30
        top = H + cut.ARCH_RAISE  # the arch stands on two spacers glued inside the side walls
        tag = "DELANTERA" if front else "TRASERA"
        add(f"ARCO CAMARA {tag}", "mdf", (bar_x, bar_x + 30, T, W - T, top - T, top))
        add(f"SEPARADORES ARCO {tag}", "mdf",
            (bar_x + 5, bar_x + 25, T, 2 * T, H - 20, top - T), (bar_x + 5, bar_x + 25, W - 2 * T, W - T, H - 20, top - T))  # fmt: skip
        # webcam hanging under its bar, lens towards the cabin at 160 mm (the 45 degree tilt is not modelled)
        cam_x, lens = (T + 3, 1) if front else (L - T - 27, -1)
        add("WEBCAM " + ("FRONTAL" if front else "TRASERA"), "camara",
            (cam_x, cam_x + 24, W / 2 - 19, W / 2 + 19, top - T - 30, top - T))  # fmt: skip
        lens_x = cam_x + 24 if lens > 0 else cam_x - 5
        add("LENTE " + ("FRONTAL" if front else "TRASERA"), "lente",
            (lens_x, lens_x + 5, W / 2 - 7, W / 2 + 7, top - T - 22, top - T - 8))  # fmt: skip
    floor = cut.floor()
    add("PISO", "mdf", (T, L - T, T, W - T, 0.0, T))
    add("PISO GRABADO", "grabado",
        *((T + x, T + x + w, T + y, T + y + h, T, T + 0.3) for x, y, w, h in floor.marks))  # fmt: skip


def double_seat(name: str, x0: float, y0: float, y1: float, legs: int = 2) -> None:
    """Seat plate on its legs, backrest standing right behind it (towards the rear of the bus)."""
    seat_top = FLOOR_TOP + cut.SEAT_H
    leg_ys = [y0 + i * (y1 - y0 - T) / (legs - 1) for i in range(legs)]
    add(f"ASIENTO {name}", "asiento",
        (x0, x0 + cut.SEAT_D, y0, y1, seat_top, seat_top + T),
        (x0 + cut.SEAT_D, x0 + cut.SEAT_D + T, y0, y1, FLOOR_TOP, FLOOR_TOP + cut.BACK_H),
        *((x0, x0 + cut.SEAT_D - T, ly, ly + T, FLOOR_TOP, seat_top) for ly in leg_ys))  # fmt: skip


def seats() -> None:
    for row, start in enumerate(cut.row_starts(), start=1):
        x0 = T + start + cut.seat_offset(row)
        for side in cut.seat_sides(row):
            y0 = T if side == "R" else T + cut.INNER_W - cut.DOUBLE_W
            double_seat(f"{row:02d}{side}", x0, y0, y0 + cut.DOUBLE_W)
    double_seat(f"{cut.LAST_ROW} banca", T + cut.BENCH_X, T + 2, W - T - 2, legs=3)


def door_sensors() -> None:
    """Two brackets on the rear jamb of each door: the outer one out to the street, the inner one into the cabin."""
    for door, (_, jamb) in (("DEL", cut.FRONT_DOOR), ("TRAS", cut.rear_door())):
        for inner in (False, True):
            y0 = T if inner else -20.0
            tag = f"{door} {'INT' if inner else 'EXT'}"
            add(f"SOPORTE FC-51 {tag}", "mdf", (jamb - T, jamb, y0, y0 + 20, FLOOR_TOP, FLOOR_TOP + 45))
            yc, zc = y0 + 10, FLOOR_TOP + 26  # the M3 hole: torso height of a minifigure
            add(f"FC-51 {tag}", "fc51", (jamb - T - 31, jamb - T, yc - 7, yc + 7, zc - 3, zc + 3))


def minifigure(name: str, x: float, y: float, z: float, torso: str, seated: bool = False) -> None:
    """A LEGO minifigure (about 40 mm standing) facing the front of the bus; seated, its legs stick forward."""
    hips = z + (8 if seated else 14)
    legs = (x - 14, x + 4, y - 8, y + 8, z, hips) if seated else (x - 4, x + 4, y - 8, y + 8, z, hips)
    add(f"{name} piernas", "lego_piernas", legs)
    add(f"{name} torso", torso, (x - 4, x + 4, y - 10, y + 10, hips, hips + 13))
    add(f"{name} cabeza", "lego_piel",
        (x - 4.5, x + 4.5, y - 4.5, y + 4.5, hips + 13, hips + 23), (x - 2.5, x + 2.5, y - 2.5, y + 2.5, hips + 23, hips + 25))  # fmt: skip


def figures() -> None:
    starts = cut.row_starts()
    seat_top = FLOOR_TOP + cut.SEAT_H + T
    for row, side, seat, torso in ((2, "R", 1, "lego_rojo"), (4, "L", 2, "lego_verde"), (7, "L", 1, "lego_blanco")):
        x = T + starts[row - 1] + cut.seat_offset(row) + cut.SEAT_D - 8
        first = cut.SEAT_W / 2 + (seat - 1) * (cut.SEAT_W + 1)
        y = T + (first if side == "R" else cut.INNER_W - first)
        minifigure(f"FIGURA S{row:02d}-{side}{seat}", x, y, seat_top, torso, seated=True)
    for row, torso in ((5, "lego_rojo"), (8, "lego_verde")):
        minifigure(f"FIGURA P{row:02d}", T + starts[row - 1] + cut.ROW_PITCH / 2, W / 2, FLOOR_TOP, torso)
    a, b = cut.FRONT_DOOR
    minifigure("FIGURA subiendo", (a + b) / 2, T + 10, FLOOR_TOP, "lego_blanco")


def bench() -> None:
    """Base board under the bus, electronics on the left side, laptop next to the board (not to scale for wiring)."""
    add("TABLERO BASE", "base", (-65.0, 785.0, -30.0, 270.0, -9.0, 0.0))
    add("ARDUINO UNO", "arduino", (300.0, 368.6, 195.0, 248.4, 0.0, 12.0))
    add("PROTOBOARD", "protoboard", (390.0, 472.0, 195.0, 250.0, 0.0, 9.0))
    add("PORTATIL", "portatil", (190.0, 530.0, 300.0, 535.0, -9.0, 9.0), (190.0, 530.0, 535.0, 541.0, 9.0, 225.0))
    add("PANTALLA", "pantalla", (200.0, 520.0, 534.0, 535.0, 20.0, 215.0))


# vertex i of a box has the bits (dx, dy, dz) = (i & 1, i >> 1 & 1, i >> 2 & 1); quads wound outwards
FACES = ((0, 4, 6, 2), (1, 3, 7, 5), (0, 1, 5, 4), (2, 6, 7, 3), (0, 2, 3, 1), (4, 5, 7, 6))


def build() -> tuple[str, str]:
    objects.clear()
    body()
    seats()
    door_sensors()
    figures()
    bench()
    obj = ["# Maqueta del bus 2101 para minifiguras de Lego, en mm (generada por make_3d_model.py)", "mtllib maqueta_3d.mtl"]
    count = 0
    for name, material, boxes in objects:
        obj += [f"o {name.replace(' ', '_')}", f"usemtl {material}"]
        for x0, x1, y0, y1, z0, z1 in boxes:
            for i in range(8):
                x, y, z = (x1 if i & 1 else x0), (y1 if i >> 1 & 1 else y0), (z1 if i >> 2 & 1 else z0)
                obj.append(f"v {x:.2f} {z:.2f} {-y:.2f}")  # z up -> Y up (a rotation: the winding is kept)
            obj += ["f " + " ".join(str(count + i + 1) for i in face) for face in FACES]
            count += 8
    mtl = [f"newmtl {name}\nKd {r:.2f} {g:.2f} {b:.2f}\nKa 0.2 0.2 0.2\nillum 1" for name, (r, g, b) in MATERIALS.items()]
    return "\n".join(obj) + "\n", "\n\n".join(mtl) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(Path(__file__).parent))
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    obj, mtl = build()
    (out / "maqueta_3d.obj").write_text(obj, encoding="utf-8")
    (out / "maqueta_3d.mtl").write_text(mtl, encoding="utf-8")
    boxes = sum(len(b) for _, _, b in objects)
    print(f"Escritos en {out}: maqueta_3d.obj y maqueta_3d.mtl ({len(objects)} objetos, {boxes} bloques, en mm).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
