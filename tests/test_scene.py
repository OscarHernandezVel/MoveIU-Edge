"""3D simulation of the model bus seen by its cameras, the yellow minifigure detector and the projected layout:
what is rendered must be recognised, position by position."""

from __future__ import annotations

import importlib.util
import random
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from bus_edge.config import ConfigError, EdgeSettings
from bus_edge.vision.layout import Kind, maqueta_layout, point_in_polygon
from bus_edge.vision.occupancy import CameraObservation, OccupancyEstimator

cv2 = pytest.importorskip("cv2")
scene = pytest.importorskip("bus_edge.vision.scene")
from bus_edge.vision.lego import YellowFigureDetector, parse_hsv  # noqa: E402

KEY = "clave-de-prueba-0123456789"
CUT_FILES = Path(__file__).resolve().parents[1] / "maqueta" / "make_cut_files.py"


@pytest.fixture(scope="module")
def cut():  # noqa: ANN201
    spec = importlib.util.spec_from_file_location("make_cut_files", CUT_FILES)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_cut_files"] = module
    spec.loader.exec_module(module)
    return module


def recognise(occupied: dict[str, tuple[int, int, int]]) -> tuple[set[str], int]:
    detector = YellowFigureDetector()
    layout = scene.projected_layout()
    estimator = OccupancyEstimator(layout, anchor_height=detector.anchor_height)
    seen = {
        name: CameraObservation(True, tuple(detector.detect(scene.render(scene.camera_pose(name), occupied))), 0.0)
        for name in ("front", "rear")
    }
    result = estimator.estimate(seen, 0.0, moving=False)
    return set(result.occupied), result.standing_out_of_position


class TestGeometry:
    def test_positions_are_the_engraved_floor_of_the_cut_files(self, cut) -> None:  # noqa: ANN001
        floor = cut.floor()
        marks = {(round(x + cut.T, 1), round(y + cut.T, 1)) for x, y, w, h in floor.marks if (w, h) == (30.0, 30.0)}
        seats = [s for s in scene.spots().values() if s.kind is Kind.SEAT and not s.id.startswith("S11")]
        assert {(round(s.x0, 1), round(s.y0, 1)) for s in seats} == marks
        engraved = {text for *_, text, _ in floor.texts}
        assert set(scene.spots()) - {"D-FRONT", "D-REAR"} <= engraved
        assert {p.id for p in maqueta_layout().positions} == set(scene.spots())

    def test_windows_doors_and_arch_match_the_cut_files(self, cut) -> None:  # noqa: ANN001
        assert (scene.FRONT_DOOR, scene.REAR_DOOR) == (cut.FRONT_DOOR, cut.rear_door())
        assert (scene.WINDOW_Z, scene.DOOR_HEIGHT) == ((cut.WINDOW_BOTTOM, cut.WINDOW_TOP), cut.DOOR_HEIGHT)
        for right in (True, False):
            wall = cut.side_wall(right)
            panes = sorted(
                (round(cut.LENGTH - x - w if right else x, 1), round(cut.LENGTH - x if right else x + w, 1))
                for x, _, w, h in wall.holes
                if h != cut.DOOR_HEIGHT
            )
            assert panes == [(round(a, 1), round(b, 1)) for a, b in scene.windows()["right" if right else "left"]]
        assert scene.ARCH_SPACER_MM == cut.ARCH_RAISE
        assert scene.CAMERA_HEIGHT_MM == pytest.approx(cut.HEIGHT + cut.ARCH_RAISE - cut.T - 12, abs=3)


class TestProjectedLayout:
    def test_every_position_is_calibrated_on_its_own_camera(self) -> None:
        layout = scene.projected_layout()
        assert layout.problems() == [] and layout.calibrated_share() == 1.0
        assert {p.camera for p in layout.positions if p.row_no <= 5 and p.id != "D-REAR"} == {"front"}
        for camera in ("front", "rear"):
            assert len(layout.cabin[camera]) >= 3

    def test_a_head_falls_inside_its_own_position_only(self) -> None:
        layout = scene.projected_layout()
        for name in ("front", "rear"):
            pose = scene.camera_pose(name)
            for spot in (s for s in scene.spots().values() if s.camera == name):
                cx, cy = spot.centre
                (u, v), = pose.to_pixels(pose.to_camera(np.array([[cx, cy, scene.HEAD_Z]])))
                point = (u / pose.width, v / pose.height)
                inside = [p.id for p in layout.for_camera(name) if point_in_polygon(point, p.polygon)]
                assert inside == [spot.id]
                assert point_in_polygon(point, layout.cabin[name])

    def test_the_layout_survives_a_round_trip_to_disk(self, tmp_path: Path) -> None:
        from bus_edge.vision.layout import load_layout

        path = tmp_path / "layout.json"
        scene.projected_layout().save(path)
        assert load_layout(path).calibrated_share() == 1.0


class TestRecognition:
    @pytest.mark.parametrize("people", [0, 1, 12, 30, 41, 49])
    def test_every_minifigure_is_found_in_its_position(self, people: int) -> None:
        places = [s.id for s in scene.spots().values() if s.kind is not Kind.DOOR]
        chosen = random.Random(people).sample(places, people)
        occupied = {pid: scene.TORSOS[i % len(scene.TORSOS)] for i, pid in enumerate(chosen)}
        found, out_of_position = recognise(occupied)
        assert found == set(occupied) and out_of_position == 0

    def test_a_minifigure_in_the_doorway_is_in_the_door_zone_not_in_a_seat(self) -> None:
        detector = YellowFigureDetector()
        layout = scene.projected_layout()
        estimator = OccupancyEstimator(layout, anchor_height=detector.anchor_height)
        frame = scene.render(scene.camera_pose("front"), {"D-FRONT": scene.TORSOS[0]})
        heads = tuple(detector.detect(frame))
        first = estimator.estimate({"front": CameraObservation(True, heads, 0.0)}, 0.0, moving=True)
        assert first.occupied == frozenset() and first.standing_out_of_position == 0  # a passage, not a position
        later = estimator.estimate({"front": CameraObservation(True, heads, 11.0)}, 11.0, moving=True)
        assert later.standing_out_of_position == 1  # still in the doorway 11 s later with the bus moving

    def test_an_empty_bus_has_no_yellow(self) -> None:
        for name in ("front", "rear"):
            assert YellowFigureDetector().detect(scene.render(scene.camera_pose(name), {})) == []

    def test_hands_are_not_counted_as_heads(self) -> None:
        frame = scene.render(scene.camera_pose("front"), {"S02-R1": scene.TORSOS[0]})  # the nearest: big hands
        assert len(YellowFigureDetector().detect(frame)) == 1


class TestResponsiveness:
    def test_a_frame_is_rendered_and_analysed_well_within_a_camera_period(self) -> None:
        occupied = {s.id: scene.TORSOS[0] for s in scene.spots().values() if s.kind is not Kind.DOOR}
        pose = scene.camera_pose("rear")
        scene.render(pose, occupied)  # background cached
        started = time.perf_counter()
        YellowFigureDetector().detect(scene.render(pose, occupied))
        assert time.perf_counter() - started < 0.2  # 5 fps per camera leaves 200 ms

    def test_the_simulated_camera_follows_the_door_count_and_reuses_its_frame(self) -> None:
        on_board = [3]
        passengers = scene.SimulatedPassengers(lambda: on_board[0])
        camera = scene.SimulatedCamera("front", passengers)
        first = camera.read()
        assert np.array_equal(first, camera.read())
        assert len(passengers.occupied()) == 3
        assert all(pid.startswith("S") for pid in passengers.occupied())  # people sit down first
        on_board[0] = 120
        assert len(passengers.occupied()) == 49  # the positions, not the people, are bounded


class TestSettings:
    def test_lego_is_the_default_detector_and_its_thresholds_are_checked(self) -> None:
        settings = EdgeSettings.from_env({"EDGE_INGEST_KEY": KEY})
        assert settings.detector == "lego" and parse_hsv(settings.lego_hsv) == (20, 35, 150, 90)
        for env, fragment in (
            ({"EDGE_LEGO_HSV": "40,30,1,1"}, "EDGE_LEGO_HSV"),
            ({"EDGE_DETECTOR": "magic"}, "EDGE_DETECTOR"),
            ({"EDGE_CAMERAS": "front=sim,rear=1"}, "sim"),
        ):
            with pytest.raises(ConfigError) as caught:
                EdgeSettings.from_env({"EDGE_INGEST_KEY": KEY, **env})
            assert fragment in str(caught.value)

    def test_simulated_cameras_bring_their_own_layout(self) -> None:
        from bus_edge.factory import build_detector, build_layout

        settings = EdgeSettings.from_env({"EDGE_INGEST_KEY": KEY, "EDGE_CAMERAS": "front=sim,rear=sim"})
        assert settings.simulated_cameras() and build_layout(settings).calibrated_share() == 1.0
        assert build_detector(settings).anchor_height == 0.5
