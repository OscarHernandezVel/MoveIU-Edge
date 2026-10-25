"""Cut files of the LEGO-scale model: consistent with the camera layout, and every piece fits without overlaps."""

from __future__ import annotations

import importlib.util
import itertools
import sys
from pathlib import Path
from types import ModuleType

import pytest

from bus_edge.vision.layout import Kind, maqueta_layout

SCRIPT = Path(__file__).resolve().parents[1] / "maqueta" / "make_cut_files.py"


@pytest.fixture(scope="module")
def cut() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_cut_files", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_cut_files"] = module
    spec.loader.exec_module(module)
    return module


def overlaps(a: object, b: object) -> bool:
    ax, ay, aw, ah = a.x, a.y, a.w, a.h  # type: ignore[attr-defined]
    bx, by, bw, bh = b.x, b.y, b.w, b.h  # type: ignore[attr-defined]
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def test_the_engraved_floor_names_every_position_of_the_camera_layout(cut: ModuleType) -> None:
    engraved = {text for _, _, text, _ in cut.floor().texts}
    layout = maqueta_layout()
    capacity_ids = {p.id for p in layout.positions if p.kind in (Kind.SEAT, Kind.STANDING)}
    assert capacity_ids <= engraved
    assert {"D-FRONT", "D-REAR"} <= engraved
    seats = sum(1 for text in engraved if text.startswith("S"))
    assert seats == layout.seats == 41


def test_the_seat_parts_make_41_seats(cut: ModuleType) -> None:
    parts = cut.seat_parts()
    backrests = [p for p in parts if p.name.startswith("R")]
    assert len(backrests) == 19  # 18 double seats + the rear bench
    assert sum(2 if p.name != "R11" else 5 for p in backrests) == 41
    door_rows = {p.name for p in backrests if p.name[1:3] in ("01", "10")}
    assert door_rows == {"R01L", "R10L"}  # door rows only have the left double seat


def test_the_floor_plan_fits_the_bus_and_the_aisle_is_usable(cut: ModuleType) -> None:
    starts = cut.row_starts()
    assert len(starts) == 10 and starts == sorted(starts)
    assert cut.BENCH_X + cut.REAR_BENCH_D + cut.T <= cut.FLOOR_L
    assert 34 <= cut.AISLE <= 42  # a standing minifigure (~22 mm with its arms) walks the aisle with room to spare
    assert all(x >= 0 and x + w <= cut.FLOOR_L and y >= 0 and y + h <= cut.INNER_W for x, y, w, h in cut.floor().marks)


def test_everything_is_sized_for_a_lego_minifigure(cut: ModuleType) -> None:
    figure_height, hips, shoulders = 42.0, 16.0, 22.0  # a LEGO minifigure, studs included
    assert 1.6 * hips <= cut.SEAT_W and 1.6 * hips <= cut.SEAT_D  # a seat takes the hips and the forward legs
    assert 1.5 * shoulders <= cut.AISLE
    assert figure_height + 20 <= cut.DOOR_HEIGHT
    for start, end in (cut.FRONT_DOOR, cut.rear_door()):
        assert end - start >= 2 * shoulders  # two figures can cross a door side by side
    assert cut.WINDOW_TOP - cut.WINDOW_BOTTOM >= 40 and cut.HEIGHT >= cut.WINDOW_TOP + 20
    backrests = [p for p in cut.seat_parts() if p.name.startswith("R")]
    assert all(p.h >= cut.SEAT_H + 20 for p in backrests)  # the backrest reaches the figure's shoulder blades


def seat_marks(cut: ModuleType, side: str) -> list[tuple[float, float]]:
    """x-range (inner coordinates) of every engraved seat on one side of the floor, rear bench excluded."""
    floor = cut.floor()
    bench = cut.BENCH_X
    top = side == "R"  # the right side (doors) is at the top of the floor drawing
    return [
        (x, x + w)
        for x, y, w, h in floor.marks
        if w == cut.SEAT_D and h == cut.SEAT_W and x < bench and ((y < cut.INNER_W / 2) == top)
    ]


def door_span(cut: ModuleType, outer: tuple[float, float]) -> tuple[float, float]:
    return outer[0] - cut.T, outer[1] - cut.T  # side-wall coordinates -> floor coordinates


@pytest.mark.parametrize("door", ["front", "rear"])
def test_there_are_seats_across_from_each_door_and_none_in_its_way(cut: ModuleType, door: str) -> None:
    a, b = door_span(cut, cut.FRONT_DOOR if door == "front" else cut.rear_door())
    left = [s for s in seat_marks(cut, "L") if s[0] < b and s[1] > a]
    right = [s for s in seat_marks(cut, "R") if s[0] < b + 3 and s[1] > a - 3]
    assert len(left) == 2  # the 2 seats of the door row face the door from the left side
    assert right == []


def test_the_rear_door_opens_right_in_front_of_the_last_row(cut: ModuleType) -> None:
    _, end = door_span(cut, cut.rear_door())
    assert 0 < cut.BENCH_X - end <= 10
    last_regular = max(x for x, _ in seat_marks(cut, "R"))
    assert last_regular < cut.BENCH_X - cut.REAR_DOOR_ZONE  # no right-side row between the door and the bench


def test_capacity_follows_rf_6_4(cut: ModuleType) -> None:
    engraved = {text for _, _, text, _ in cut.floor().texts}
    standing = sorted(t for t in engraved if t.startswith("P"))
    assert standing == [f"P{row:02d}" for row in range(2, 10)]  # door rows and the bench add no standing position
    assert {t for t in engraved if t.startswith(("S01-", "S10-"))} == {"S01-L1", "S01-L2", "S10-L1", "S10-L2"}
    layout = maqueta_layout()
    kinds = [p.kind for p in layout.positions]
    assert (kinds.count(Kind.SEAT), kinds.count(Kind.STANDING)) == (layout.seats, layout.standing_positions) == (41, 8)


def test_doors_are_on_the_right_wall_only_and_line_up_with_the_floor(cut: ModuleType) -> None:
    right, left = cut.side_wall(right=True), cut.side_wall(right=False)
    tall = [h for h in right.holes if h[3] == cut.DOOR_HEIGHT]
    assert len(tall) == 2 and not [h for h in left.holes if h[3] == cut.DOOR_HEIGHT]
    _, end = cut.rear_door()
    _, front_b = cut.FRONT_DOOR
    # seen from outside the right wall has the front on the right: openings are mirrored
    assert sorted(round(h[0], 2) for h in tall) == sorted([round(cut.LENGTH - end, 2), round(cut.LENGTH - front_b, 2)])
    for x, y, w, h in right.holes + left.holes:
        assert x > 0 and x + w < cut.LENGTH and y > 0 and y + h <= cut.HEIGHT


@pytest.mark.parametrize("split", [False, True])
def test_every_piece_fits_its_sheet_without_overlapping(cut: ModuleType, split: bool) -> None:
    body = cut.split_long(cut.body_pieces()) if split else cut.body_pieces()
    size = (600.0, 400.0) if split else (900.0, 600.0)
    sheets = cut.pack(body, *size) + cut.pack(cut.seat_parts(), 600.0, 400.0)
    for sheet in sheets:
        for piece in sheet:
            assert piece.x >= 0 and piece.y >= 0 and piece.x + piece.w <= 900 and piece.y + piece.h <= 600
        for a, b in itertools.combinations(sheet, 2):
            assert not overlaps(a, b), (a.name, b.name)
    if not split:
        assert len(cut.pack(cut.body_pieces(), 900.0, 600.0)) == 1  # one 900 x 600 sheet for the body


def test_split_halves_keep_every_opening_and_engraving(cut: ModuleType) -> None:
    whole = {p.name: p for p in cut.body_pieces()}
    halves = cut.split_long(cut.body_pieces())
    for name in ("LATERAL IZQUIERDO", "LATERAL DERECHO (puertas)", "PISO"):
        parts = [p for p in halves if p.name.startswith(name + " ")]
        assert len(parts) == 2 and all(p.w <= 592 for p in parts)
        assert sum(len(p.holes) for p in parts) == len(whole[name].holes)
        assert sum(len(p.marks) for p in parts) == len(whole[name].marks)


def test_the_script_writes_the_files(cut: ModuleType, tmp_path: Path) -> None:
    assert cut.main(["--out", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["corte_1_carroceria.svg", "corte_2_asientos.svg"]
    text = (tmp_path / "corte_1_carroceria.svg").read_text()
    assert 'width="900.0mm"' in text and "#ff0000" in text and "#0000ff" in text
    (tmp_path / "split").mkdir()
    assert cut.main(["--split", "--out", str(tmp_path / "split")]) == 0
    assert len(list((tmp_path / "split").glob("corte_1_carroceria_hoja*.svg"))) >= 2


def test_the_3d_model_is_a_valid_obj_with_every_seat_and_sensor(cut: ModuleType, tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("make_3d_model", SCRIPT.with_name("make_3d_model.py"))
    assert spec is not None and spec.loader is not None
    model = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(model)
    assert model.main(["--out", str(tmp_path)]) == 0
    lines = (tmp_path / "maqueta_3d.obj").read_text().splitlines()
    vertices = [tuple(map(float, line.split()[1:])) for line in lines if line.startswith("v ")]
    faces = [list(map(int, line.split()[1:])) for line in lines if line.startswith("f ")]
    assert faces and all(1 <= i <= len(vertices) for face in faces for i in face)
    names = [line[2:] for line in lines if line.startswith("o ")]
    assert sum(name.startswith("ASIENTO") for name in names) == 19  # 18 double seats + the rear bench
    assert sum(name.startswith("FC-51") for name in names) == 4
    materials = (tmp_path / "maqueta_3d.mtl").read_text()
    assert all(f"newmtl {m}" in materials for m in {line[7:] for line in lines if line.startswith("usemtl ")})
    # walls: the openings are left out, so the right wall has less material than the left one
    wall_area = {
        name: sum((x1 - x0) * (z1 - z0) for x0, x1, _, _, z0, z1 in boxes)
        for name, _, boxes in model.objects
        if name.startswith("LATERAL")
    }
    assert wall_area["LATERAL DERECHO (puertas)"] < wall_area["LATERAL IZQUIERDO"] < cut.LENGTH * cut.HEIGHT
