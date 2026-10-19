"""Laser-cut files of the bus model for LEGO minifigures (MDF 3 mm), generated from the same layout the cameras use.

    python maqueta/make_cut_files.py          # writes maqueta/corte_1_carroceria.svg and corte_2_asientos.svg

Real bus: 12 m x 2.5 m x 3.1 m, doors on the right side. The model is sized for LEGO minifigures (about 40 mm
tall, 16 mm at the hips): roughly 1:15 in plan (a seat is 30 x 30 mm = 0.45 m) and lower than 1:15 in height, so
the walls are 130 mm high instead of 207 mm. Model: 720 x 166 x 130 mm. Interior as in RF-6.4 of
the design document: the front door row (row 1) and the rear door row (row 10) only have seats on the left,
across from the door; rows 2-9 have 2 + 2 seats and a standing position in the aisle; the rear door opens
right in front of the last row, the rear bench of 5 (row 11). 2 + 32 + 2 + 5 = 41 seats, 8 standing positions.

Colours follow the usual laser convention: **red = cut** (0.1 mm hairline), **blue = engrave**. Units are
millimetres (the SVG declares width/height in mm, so LightBurn/LaserGRBL import it at true size).

The floor is engraved with the outline and id of every seat and standing position (``S03-L1``, ``P04``...),
exactly the ids of ``bus_2101_layout.json``: when calibrating, the camera image shows which polygon is which.

Sheet 1 needs a 900 x 600 mm bed (the side walls are 720 mm long). With a 600 x 400 mm laser use
``--split`` to cut each side wall in two halves of 360 mm (joined inside by a 20 mm strip, included).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

T = 3.0  # MDF thickness
LENGTH, WIDTH, HEIGHT = 720.0, 166.0, 130.0
INNER_W = WIDTH - 2 * T  # floor and end walls sit between the side walls: 160 mm
FLOOR_L = LENGTH - 2 * T  # 714 mm between front and back walls

# Longitudinal plan of the floor (mm from the inner face of the front wall)
DRIVER_ZONE = 54.0  # driver's seat, left side; the front door opens on the right
FRONT_ZONE = 108.0  # driver + front door row (row 1)
ROW_PITCH = 54.0  # 0.81 m: a seated minifigure (legs forward) fits in its 30 mm seat with room to spare
REGULAR_ROWS = 8  # rows 2-9, 2 + 2 seats
REAR_DOOR_ZONE = 72.0  # row 10: rear door on the right, 2 seats on the left
DOOR_ROWS = (1, 10)  # rows whose right side is a door (RF-6.4 a)
LAST_ROW = 11  # rear bench of 5
SEAT_W, SEAT_D = 30.0, 30.0  # one seat: 0.45 m x 0.45 m, enough for a minifigure's hips (16 mm) and legs (16 mm)
DOUBLE_W = 2 * SEAT_W + 1.0
AISLE = INNER_W - 2 * DOUBLE_W  # 38 mm (0.57 m): a standing minifigure is ~22 mm wide with its arms
REAR_BENCH_D = 30.0
SEAT_H = 14.0  # floor to seat: the legs of a seated minifigure hang free of the floor but its feet stay near it
BACK_H = 34.0  # backrest, from the floor: 20 mm above the seat, up to the shoulders of the minifigure

WINDOW_BOTTOM, WINDOW_TOP = 50.0, 105.0
FRONT_DOOR = (14.0, 84.0)  # along the right wall, from the front end
DOOR_HEIGHT = 70.0  # a 42 mm minifigure (studs included) walks through with room to spare

CUT, ENGRAVE = "#ff0000", "#0000ff"


@dataclass
class Piece:
    name: str
    w: float
    h: float
    holes: list[tuple[float, float, float, float]] = field(default_factory=list)  # rect cut-outs x, y, w, h
    circles: list[tuple[float, float, float]] = field(default_factory=list)  # x, y, r
    marks: list[tuple[float, float, float, float]] = field(default_factory=list)  # engraved rects
    texts: list[tuple[float, float, str, float]] = field(default_factory=list)  # engraved text x, y, text, size
    x: float = 0.0
    y: float = 0.0

    def svg(self) -> str:
        out = [f'<g transform="translate({self.x:.2f},{self.y:.2f})">']
        out.append(_rect(0, 0, self.w, self.h, CUT, 0.1))
        out += [_rect(hx, hy, hw, hh, CUT, 0.1) for hx, hy, hw, hh in self.holes]
        for cx, cy, r in self.circles:
            out.append(
                f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="none" stroke="{CUT}" stroke-width="0.1"/>'
            )
        out += [_rect(mx, my, mw, mh, ENGRAVE, 0.2) for mx, my, mw, mh in self.marks]
        for tx, ty, text, size in self.texts:
            out.append(
                f'<text x="{tx:.2f}" y="{ty:.2f}" font-size="{size}" font-family="Arial, sans-serif" '
                f'fill="{ENGRAVE}" text-anchor="middle">{text}</text>'
            )
        out.append("</g>")
        return "\n".join(out)


def _rect(x: float, y: float, w: float, h: float, color: str, stroke: float) -> str:
    return (
        f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" fill="none" '
        f'stroke="{color}" stroke-width="{stroke}"/>'
    )


REAR_DOOR_START = FRONT_ZONE + REGULAR_ROWS * ROW_PITCH  # 540 mm
BENCH_X = REAR_DOOR_START + REAR_DOOR_ZONE + 4  # the bench starts right behind the rear door


def row_starts() -> list[float]:
    """Front edge (mm from the front wall) of each row with double seats, rows 1-10."""
    regular = [FRONT_ZONE + i * ROW_PITCH for i in range(REGULAR_ROWS)]
    return [DRIVER_ZONE, *regular, REAR_DOOR_START]


def seat_offset(row: int) -> float:
    """Where the seat begins inside its row (the backrest goes right behind it)."""
    if row == 1:
        return 2.0  # right across the front door
    if row == 10:
        return 20.0  # across the rear door, with legroom for the bench behind
    return 10.0


def seat_sides(row: int) -> tuple[str, ...]:
    return ("L",) if row in DOOR_ROWS else ("R", "L")


def rear_door() -> tuple[float, float]:
    begin = REAR_DOOR_START + T  # outer coordinates of the side wall
    return begin + 5, begin + REAR_DOOR_ZONE - 5


def floor() -> Piece:
    """Engraved plan seen from above with the front on the left: the bus's right side (doors) is at the top."""
    piece = Piece("PISO", FLOOR_L, INNER_W)
    for row, start in enumerate(row_starts(), start=1):
        x = start + seat_offset(row)
        for side in seat_sides(row):
            for seat in (1, 2):  # seat 1 by the window, seat 2 by the aisle
                offset = (seat - 1) * (SEAT_W + 1.0)
                y = offset if side == "R" else INNER_W - SEAT_W - offset
                piece.marks.append((x, y, SEAT_D, SEAT_W))
                piece.texts.append((x + SEAT_D / 2, y + SEAT_W / 2 + 1.5, f"S{row:02d}-{side}{seat}", 4))
        if row not in DOOR_ROWS:  # a standing position needs seats on both sides of the aisle (RF-6.4 c)
            piece.marks.append((start + 8, DOUBLE_W + 4, ROW_PITCH - 12, AISLE - 8))
            piece.texts.append((start + 8 + (ROW_PITCH - 12) / 2, INNER_W / 2 + 1.5, f"P{row:02d}", 4.5))
    piece.marks.append((BENCH_X, 2, REAR_BENCH_D, INNER_W - 4))
    for seat in range(1, 6):
        y = 2 + (seat - 0.5) * (INNER_W - 4) / 5
        piece.texts.append((BENCH_X + REAR_BENCH_D / 2, y + 1.5, f"S{LAST_ROW}-T{seat}", 4))
    piece.texts.append((FRONT_ZONE / 2, INNER_W / 2 + 4, "FRENTE", 5))
    piece.texts.append((DRIVER_ZONE / 2, INNER_W - DOUBLE_W / 2 + 1.5, "CONDUCTOR", 4))
    piece.texts.append((FRONT_DOOR[0] + 25, 10, "D-FRONT", 4.5))
    piece.texts.append((REAR_DOOR_START + REAR_DOOR_ZONE / 2, 10, "D-REAR", 4.5))
    return piece


def side_wall(right: bool) -> Piece:
    """Seen from outside (engraving on the outer face). From outside, the front is on the left of the left wall and
    on the right of the right wall. The right wall has the two doors; windows run between the pillars."""
    name = "LATERAL DERECHO (puertas)" if right else "LATERAL IZQUIERDO"
    piece = Piece(name, LENGTH, HEIGHT)

    def to_y(height_from_floor: float) -> float:  # SVG y grows downwards; the floor is at the bottom edge
        return HEIGHT - height_from_floor

    doors = [FRONT_DOOR, rear_door()] if right else []
    for a, b in doors:
        x0 = LENGTH - b if right else a
        piece.holes.append((x0, to_y(DOOR_HEIGHT + T), b - a, DOOR_HEIGHT))
    # windows: 60 mm panes with 8 mm pillars (one pillar at the middle, where --split cuts), skipping doors
    x = 100.0
    while x + 60 <= LENGTH - 30:
        pane = (x, x + 60)
        blocked = any(not (pane[1] <= a - 4 or pane[0] >= b + 4) for a, b in doors)
        if not blocked:
            piece.holes.append((LENGTH - x - 60 if right else x, to_y(WINDOW_TOP), 60, WINDOW_TOP - WINDOW_BOTTOM))
        x += 68
    piece.texts.append((LENGTH * 0.7 if right else LENGTH / 2, HEIGHT - 12, name, 6))  # clear of the door openings
    return piece


def end_wall(front: bool) -> Piece:
    """Full height, between the side walls; the floor fits between the two end walls."""
    piece = Piece("FRENTE" if front else "TRASERA", INNER_W, HEIGHT)
    if front:
        piece.holes.append((10, 15, INNER_W - 20, 55))  # windscreen
    piece.texts.append((INNER_W / 2, HEIGHT - 20, piece.name, 6))
    return piece


def camera_bar(front: bool) -> Piece:
    """Crossbar glued at the top of each end: holds a webcam (6.5 mm hole for a 1/4" screw, slots for ties)."""
    piece = Piece("ARCO CAMARA " + ("DELANTERA" if front else "TRASERA"), INNER_W, 30)
    piece.circles.append((INNER_W / 2, 15, 3.25))
    piece.holes += [(INNER_W / 2 - 25, 5, 4, 20), (INNER_W / 2 + 21, 5, 4, 20)]
    piece.texts.append((INNER_W / 2, 27, "CAM " + ("F" if front else "R"), 3.5))
    return piece


def sensor_bracket(front: bool) -> Piece:
    """L-bracket glued to the door jamb; the FC-51 is screwed with M3 (3.2 mm hole) looking across the door."""
    piece = Piece("SOPORTE FC-51 " + ("DEL" if front else "TRAS"), 20, 45)
    piece.circles.append((10, 19, 1.6))  # hole 26 mm above the floor: the torso of a minifigure
    piece.texts.append((10, 40, "FC51-" + ("F" if front else "R"), 3))
    return piece


def seat_parts() -> list[Piece]:
    """18 double seats (backrest, seat, 2 supports) and the rear bench."""
    pieces: list[Piece] = []
    for row in range(1, LAST_ROW):
        for side in seat_sides(row):
            tag = f"{row:02d}{side}"
            pieces.append(Piece(f"R{tag}", DOUBLE_W, BACK_H, texts=[(DOUBLE_W / 2, BACK_H - 4, f"R{tag}", 3.5)]))
            pieces.append(Piece(f"A{tag}", DOUBLE_W, SEAT_D, texts=[(DOUBLE_W / 2, SEAT_D / 2 + 1, f"A{tag}", 3.5)]))
            for leg in (1, 2):
                pieces.append(Piece(f"P{tag}{leg}", SEAT_D - T, SEAT_H))
    bench_w = INNER_W - 4
    pieces.append(
        Piece(f"R{LAST_ROW}", bench_w, BACK_H, texts=[(bench_w / 2, BACK_H - 4, f"R{LAST_ROW} respaldo trasero", 3.5)])
    )
    pieces.append(
        Piece(f"A{LAST_ROW}", bench_w, REAR_BENCH_D, texts=[(bench_w / 2, REAR_BENCH_D / 2 + 1, f"A{LAST_ROW}", 3.5)])
    )
    pieces += [Piece(f"P{LAST_ROW}{leg}", REAR_BENCH_D - T, SEAT_H) for leg in (1, 2, 3)]
    return pieces


def split_piece(piece: Piece, cut: float) -> list[Piece]:
    """Cut a long piece in two at ``cut`` mm; openings and engravings go to the half that contains them."""
    halves: list[Piece] = []
    for index, (x0, x1) in enumerate(((0.0, cut), (cut, piece.w)), start=1):
        part = Piece(f"{piece.name} {index}/2", x1 - x0, piece.h)
        part.holes = [(hx - x0, hy, hw, hh) for hx, hy, hw, hh in piece.holes if hx >= x0 and hx + hw <= x1]
        part.marks = [(mx - x0, my, mw, mh) for mx, my, mw, mh in piece.marks if mx >= x0 and mx + mw <= x1]
        part.circles = [(cx - x0, cy, r) for cx, cy, r in piece.circles if x0 <= cx < x1]
        part.texts = [(tx - x0, ty, text, size) for tx, ty, text, size in piece.texts if x0 <= tx < x1]
        part.texts.append(((x1 - x0) / 2, piece.h - 4 if piece.name == "PISO" else piece.h - 24, f"{index}/2", 4))
        halves.append(part)
    return halves


def split_long(pieces: list[Piece]) -> list[Piece]:
    """For 600 x 400 mm beds: side walls and floor in two halves (plus a joining strip per wall)."""
    result: list[Piece] = []
    for piece in pieces:
        if piece.name.startswith("LATERAL"):
            result += split_piece(piece, _split_point(piece))
            result.append(Piece(f"UNION {piece.name}", 20, piece.h - 10))
        elif piece.name == "PISO":
            result += split_piece(piece, _split_point(piece))
        else:
            result.append(piece)
    return result


def _split_point(piece: Piece, margin: float = 3.0) -> float:
    """Where to cut a piece in two: the point nearest the middle that crosses no opening or engraved outline."""
    middle = piece.w / 2
    for offset in range(0, int(middle)):
        for x in (middle - offset, middle + offset):
            if all(x < hx - margin or x > hx + hw + margin for hx, _, hw, _ in piece.holes + piece.marks):
                return x
    raise ValueError(f"{piece.name} cannot be split")


def pack(pieces: list[Piece], sheet_w: float, sheet_h: float, gap: float = 4.0) -> list[list[Piece]]:
    """Shelf packing, tallest first, opening a new sheet when one is full. Returns the pieces of each sheet."""
    sheets: list[list[Piece]] = [[]]
    x = y = gap
    shelf = 0.0
    for piece in sorted(pieces, key=lambda p: (-p.h, -p.w)):
        if piece.w + 2 * gap > sheet_w or piece.h + 2 * gap > sheet_h:
            raise ValueError(f"{piece.name} does not fit on a {sheet_w:.0f} x {sheet_h:.0f} mm sheet")
        if x + piece.w + gap > sheet_w:
            x, y, shelf = gap, y + shelf + gap, 0.0
        if y + piece.h + gap > sheet_h:
            sheets.append([])
            x, y, shelf = gap, gap, 0.0
        piece.x, piece.y = x, y
        sheets[-1].append(piece)
        x += piece.w + gap
        shelf = max(shelf, piece.h)
    return sheets


def sheet_svg(pieces: list[Piece], sheet_w: float, sheet_h: float, title: str) -> str:
    body = "\n".join(p.svg() for p in pieces)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{sheet_w}mm" height="{sheet_h}mm" '
        f'viewBox="0 0 {sheet_w} {sheet_h}">\n<title>{title}</title>\n{body}\n</svg>\n'
    )


def body_pieces() -> list[Piece]:
    return [
        side_wall(right=False),
        side_wall(right=True),
        floor(),
        end_wall(front=True),
        end_wall(front=False),
        camera_bar(front=True),
        camera_bar(front=False),
        sensor_bracket(front=True),
        sensor_bracket(front=False),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", action="store_true", help="láser de 600 x 400 mm: laterales en dos mitades")
    parser.add_argument("--out", default=str(Path(__file__).parent))
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    body = body_pieces()
    size = (900.0, 600.0)
    if args.split:
        body, size = split_long(body), (600.0, 400.0)
    written = []
    for number, sheet in enumerate(pack(body, *size), start=1):
        name = "corte_1_carroceria.svg" if not args.split else f"corte_1_carroceria_hoja{number}.svg"
        (out / name).write_text(sheet_svg(sheet, *size, f"Carroceria 1:20 MDF 3 mm ({number})"), encoding="utf-8")
        written.append(name)
    for number, sheet in enumerate(pack(seat_parts(), 600.0, 400.0), start=1):
        name = "corte_2_asientos.svg" if number == 1 else f"corte_2_asientos_hoja{number}.svg"
        (out / name).write_text(sheet_svg(sheet, 600.0, 400.0, "Asientos 1:20 MDF 3 mm"), encoding="utf-8")
        written.append(name)
    print(f"Escritos en {out}: {', '.join(written)} (rojo = cortar, azul = grabar).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
