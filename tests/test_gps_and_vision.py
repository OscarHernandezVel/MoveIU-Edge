"""GPS (NMEA and route replay) and edge vision (layout, detector decoding, occupancy, calibration, privacy)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from bus_edge.calibrate import Calibrator
from bus_edge.gps.nmea import parse_rmc
from bus_edge.gps.sources import NmeaSerialGps, Polyline, RouteReplayGps, TerminalDetector, haversine_m, load_route
from bus_edge.vision.cameras import CameraWorker, OpenCvCamera, usable
from bus_edge.vision.detector import Detection, NullDetector, decode_yolov8, letterbox, nms
from bus_edge.vision.layout import BusLayout, Kind, Position, load_layout, maqueta_layout, point_in_polygon
from bus_edge.vision.occupancy import DOOR_DWELL_S, CameraObservation, OccupancyEstimator

PACKAGE = Path(__file__).resolve().parents[2] / "backend" / "demo_data" / "pasto_c1_c4.json"


def nmea(body: str) -> str:
    value = 0
    for char in body.encode():
        value ^= char
    return f"${body}*{value:02X}"


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# ---------------------------------------------------------------------- GPS
class TestNmea:
    def test_a_valid_rmc_gives_position_and_speed(self) -> None:
        fix = parse_rmc(nmea("GPRMC,123519,A,0112.816,N,07716.866,W,10.0,084.4,230394,003.1,W"))
        assert fix is not None
        assert fix.lat == pytest.approx(1 + 12.816 / 60) and fix.lng == pytest.approx(-(77 + 16.866 / 60))
        assert fix.speed_kmh == pytest.approx(18.52) and fix.course_deg == pytest.approx(84.4)
        assert parse_rmc(nmea("GNRMC,123519,A,0112.816,S,07716.866,E,,,230394,,")).lat < 0  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        "sentence",
        [
            nmea("GPRMC,123519,V,,,,,,,230394,,"),  # no fix yet (indoors)
            "$GPRMC,123519,A,0112.816,N,07716.866,W,10.0,084.4,230394,003.1,W*00",  # bad checksum
            nmea("GPGGA,123519,0112.816,N,07716.866,W,1,08,0.9,545.4,M,46.9,M,,"),  # another sentence
            "garbage",
        ],
    )
    def test_anything_else_is_ignored(self, sentence: str) -> None:
        assert parse_rmc(sentence) is None

    def test_a_stale_fix_is_dropped(self) -> None:
        clock = FakeClock()
        gps = NmeaSerialGps("/dev/null", clock=clock)
        gps.feed(nmea("GPRMC,123519,A,0112.816,N,07716.866,W,0.0,0.0,230394,,"))
        assert gps.current() is not None
        clock.t += 10
        assert gps.current() is None


class TestReplay:
    def test_it_follows_the_real_route_out_and_back_with_terminal_stops(self) -> None:
        outbound, back, stops = load_route(PACKAGE, "C1")
        assert len(stops) > 40 and outbound.length_m > 5_000
        clock = FakeClock(0.0)
        gps = RouteReplayGps(outbound, back, speed_kmh=36.0, dwell_s=20.0, clock=clock)
        start = gps.current()
        assert start is not None and haversine_m((start.lat, start.lng), outbound.points[0]) < 1
        clock.t = 100.0  # 10 m/s for 100 s = 1 km along the trace
        moved = gps.current()
        assert moved is not None and moved.speed_kmh == pytest.approx(36.0)
        assert haversine_m((moved.lat, moved.lng), outbound.at(1_000)) < 1
        clock.t = outbound.length_m / 10.0 + 5  # at the far terminal
        assert gps.at_terminal() and gps.current().speed_kmh == 0.0  # type: ignore[union-attr]

    def test_polyline_and_errors(self) -> None:
        line = Polyline([(0.0, 0.0), (0.0, 0.001)])
        assert line.at(-5) == (0.0, 0.0) and line.at(10_000) == (0.0, 0.001)
        with pytest.raises(ValueError):
            Polyline([(0.0, 0.0)])
        with pytest.raises(ValueError):
            load_route(PACKAGE, "ZZ")

    def test_terminal_arrival_is_reported_once(self) -> None:
        from bus_edge.gps.nmea import Fix

        detector = TerminalDetector([(1.2, -77.3)], radius_m=40)
        near = Fix(1.2001, -77.3, 2.0)
        assert detector.arrived(near) and not detector.arrived(near)
        assert not detector.arrived(Fix(1.3, -77.3, 2.0))
        assert detector.arrived(near)
        assert not detector.arrived(Fix(1.2001, -77.3, 30.0))  # passing by fast is not an arrival


# ------------------------------------------------------------------- layout
def square(x: float, y: float, size: float = 0.1) -> tuple[tuple[float, float], ...]:
    return ((x, y), (x + size, y), (x + size, y + size), (x, y + size))


def small_layout() -> BusLayout:
    positions = [
        Position("S1", Kind.SEAT, 1, "left", "front", square(0.1, 0.1)),
        Position("S2", Kind.SEAT, 1, "right", "front", square(0.3, 0.1)),
        Position("P1", Kind.STANDING, 1, "aisle", "front", square(0.2, 0.1)),
        Position("S3", Kind.SEAT, 2, "left", "rear", square(0.1, 0.1)),
        Position("D1", Kind.DOOR, 1, "right", "front", square(0.6, 0.6, 0.2)),
    ]
    return BusLayout("T1", 3, 1, 0, 1, positions, {}, {"front": square(0.0, 0.0, 0.9)})


def person_at(x: float, y: float, score: float = 0.9) -> Detection:
    # anchor = (centre x, 80 % down the box): a 0.1 x 0.1 box whose anchor lands on (x, y)
    return Detection(x - 0.05, y - 0.08, x + 0.05, y + 0.02, score)


class TestLayout:
    def test_the_model_layout_matches_the_bus_capacity(self, tmp_path: Path) -> None:
        layout = maqueta_layout()
        assert layout.problems() == []
        counts = {kind: sum(1 for p in layout.positions if p.kind is kind) for kind in Kind}
        assert counts == {Kind.SEAT: 41, Kind.STANDING: 8, Kind.WHEELCHAIR: 0, Kind.DOOR: 2}
        assert {p.camera for p in layout.positions} == {"front", "rear"}
        path = tmp_path / "layout.json"
        layout.positions[0] = Position("S01-L1", Kind.SEAT, 1, "left", "front", square(0.1, 0.1))
        layout.save(path)
        again = load_layout(path)
        assert again.positions[0].calibrated and again.calibrated_share() == pytest.approx(1 / 49)

    def test_the_layout_file_is_accepted_by_the_core_catalogue(self) -> None:
        core = pytest.importorskip("core_service.ingestion.package")
        data = maqueta_layout().to_dict()
        bus = {"bus_id": "2101", "route_id": "C1", "model": "Maqueta 1:20", **data["capacity"], "status": "active"}
        bus["positions"] = data["positions"]
        stop = {"stop_id": "S1", "name": "A", "lat": 1.2, "lng": -77.3}
        stop2 = {"stop_id": "S2", "name": "B", "lat": 1.21, "lng": -77.3}
        route = {"route_id": "C1", "name": "R", "path": [[-77.3, 1.2], [-77.3, 1.21]], "stops": ["S1", "S2"]}
        package = core.parse_package(
            {"format": "core-catalog/1", "stops": [stop, stop2], "routes": [route], "buses": [bus]}
        )
        assert len(package.buses[0].positions) == 51

    def test_problems_and_polygons(self) -> None:
        layout = small_layout()
        layout.seats = 4
        assert layout.problems() == ["seat: 3 positions, capacity says 4"]
        assert point_in_polygon((0.15, 0.15), square(0.1, 0.1)) and not point_in_polygon((0.5, 0.5), square(0.1, 0.1))
        assert not point_in_polygon((0.1, 0.1), ())


# ---------------------------------------------------------------- occupancy
class TestOccupancy:
    def test_people_are_assigned_to_positions(self) -> None:
        estimator = OccupancyEstimator(small_layout())
        observations = {
            "front": CameraObservation(True, (person_at(0.15, 0.15), person_at(0.25, 0.15, 0.7)), 10.0),
            "rear": CameraObservation(True, (person_at(0.15, 0.15),), 10.0),
        }
        result = estimator.estimate(observations, 10.0, moving=True)
        assert (result.seated, result.standing, result.standing_out_of_position) == (2, 1, 0)
        assert result.occupied == {"S1", "P1", "S3"}
        assert result.confidence == pytest.approx((0.9 + 0.7 + 0.9) / 3, abs=1e-3)
        assert not result.approximate_count and result.total == 3

    def test_two_people_in_one_seat_and_people_on_the_floor_are_excess(self) -> None:
        estimator = OccupancyEstimator(small_layout())
        detections = (person_at(0.15, 0.15), person_at(0.16, 0.16), person_at(0.5, 0.4), person_at(0.95, 0.95))
        result = estimator.estimate({"front": CameraObservation(True, detections, 1.0)}, 1.0, moving=False)
        # (0.95, 0.95) is outside the cabin: someone seen through the window, ignored
        assert (result.seated, result.standing_out_of_position) == (1, 2)

    def test_a_door_zone_is_a_passage_until_someone_stays_there_while_moving(self) -> None:
        estimator = OccupancyEstimator(small_layout())
        at_door = {"front": CameraObservation(True, (person_at(0.7, 0.7),), 0.0)}
        assert estimator.estimate(at_door, 0.0, moving=False).standing_out_of_position == 0
        at_door_later = {"front": CameraObservation(True, (person_at(0.7, 0.7),), DOOR_DWELL_S + 1)}
        assert estimator.estimate(at_door_later, DOOR_DWELL_S + 1, moving=True).standing_out_of_position == 1
        cleared = {"front": CameraObservation(True, (), DOOR_DWELL_S + 2)}
        estimator.estimate(cleared, DOOR_DWELL_S + 2, moving=True)
        back = {"front": CameraObservation(True, (person_at(0.7, 0.7),), DOOR_DWELL_S + 3)}
        assert estimator.estimate(back, DOOR_DWELL_S + 3, moving=True).standing_out_of_position == 0  # timer restarted

    def test_a_missing_or_stale_camera_lowers_confidence_and_marks_the_count_approximate(self) -> None:
        estimator = OccupancyEstimator(small_layout())
        only_front = estimator.estimate({"front": CameraObservation(True, (), 5.0)}, 5.0, moving=True)
        assert only_front.visible_share == pytest.approx(3 / 4) and not only_front.approximate_count
        stale = estimator.estimate(
            {"front": CameraObservation(True, (), 0.0), "rear": CameraObservation(True, (), 5.0)}, 5.0, moving=True
        )
        assert stale.visible_share == pytest.approx(1 / 4) and stale.approximate_count
        assert stale.confidence == pytest.approx(0.95 / 4, abs=1e-3)
        nothing = estimator.estimate({}, 5.0, moving=True)
        assert nothing.confidence == 0 and nothing.approximate_count


# ----------------------------------------------------------------- detector
class TestDetector:
    def test_yolov8_output_is_decoded_to_normalised_person_boxes(self) -> None:
        # 3 candidates, 2 classes: a person, the same person again (overlap), and a class-1 object
        output = np.zeros((1, 6, 3), dtype=np.float32)
        output[0, :4, 0] = [160, 160, 40, 80]
        output[0, 4, 0] = 0.9
        output[0, :4, 1] = [162, 161, 40, 80]
        output[0, 4, 1] = 0.8
        output[0, :4, 2] = [50, 50, 20, 20]
        output[0, 5, 2] = 0.95
        _, scale, pad_x, pad_y = letterbox(np.zeros((480, 640, 3), dtype=np.uint8), 320)
        found = decode_yolov8(output, scale, pad_x, pad_y, 640, 480, 0.4, 0.45)
        assert len(found) == 1 and found[0].score == pytest.approx(0.9)
        box = found[0]
        assert box.x1 == pytest.approx((140 - pad_x) / scale / 640) and 0 <= box.y1 < box.y2 <= 1
        assert decode_yolov8(output * 0, scale, pad_x, pad_y, 640, 480, 0.4, 0.45) == []

    def test_a_custom_class_id_selects_the_lego_minifigure_of_a_retrained_model(self) -> None:
        # class 1 = "lego_minifigure" in a model trained on the model bus; class 0 must then be ignored
        output = np.zeros((1, 6, 2), dtype=np.float32)
        output[0, :4, 0] = [160, 160, 30, 40]
        output[0, 5, 0] = 0.8  # class 1
        output[0, :4, 1] = [60, 60, 30, 40]
        output[0, 4, 1] = 0.9  # class 0 (a "person" the retrained model should not report)
        _, scale, pad_x, pad_y = letterbox(np.zeros((480, 640, 3), dtype=np.uint8), 320)
        found = decode_yolov8(output, scale, pad_x, pad_y, 640, 480, 0.4, 0.45, classes=(1,))
        assert [round(d.score, 2) for d in found] == [0.8]

    def test_letterbox_keeps_the_aspect_ratio(self) -> None:
        canvas, scale, pad_x, pad_y = letterbox(np.zeros((480, 640, 3), dtype=np.uint8), 320)
        assert canvas.shape == (320, 320, 3) and scale == 0.5 and (pad_x, pad_y) == (0, 40)

    def test_nms_keeps_the_best_of_overlapping_boxes(self) -> None:
        boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]], dtype=np.float32)
        assert nms(boxes, np.array([0.5, 0.9, 0.7]), 0.5) == [1, 2]

    def test_a_recorded_video_plays_in_a_loop_instead_of_a_camera(self, tmp_path: Path) -> None:
        cv2 = pytest.importorskip("cv2")
        video = tmp_path / "frente.avi"
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 5, (64, 48))
        for brightness in (60, 120, 180):
            writer.write(np.full((48, 64, 3), brightness, dtype=np.uint8))
        writer.release()
        camera = OpenCvCamera(str(video))
        frames = [camera.read() for _ in range(7)]  # 3 frames, read 7 times: it starts over twice
        camera.close()
        assert all(frame is not None and usable(frame) for frame in frames)


# ------------------------------------------------------------ camera worker
class FakeCamera:
    def __init__(self, brightness: int) -> None:
        self.brightness = brightness

    def read(self) -> Any:
        return np.full((48, 64, 3), self.brightness, dtype=np.uint8)

    def close(self) -> None:
        pass


class CountingDetector(NullDetector):
    def __init__(self) -> None:
        self.calls = 0

    def detect(self, frame: Any) -> list[Detection]:
        self.calls += 1
        return [Detection(0.1, 0.1, 0.2, 0.3, 0.8)]


class TestCameraWorker:
    def test_it_publishes_boxes_and_keeps_no_image(self) -> None:
        detector = CountingDetector()
        worker = CameraWorker("front", lambda: FakeCamera(120), detector, fps=30)
        worker._camera = FakeCamera(120)
        worker._cycle()
        latest = worker.latest()
        assert latest.ok and len(latest.detections) == 1 and detector.calls == 1
        assert not any(isinstance(value, np.ndarray) for value in vars(worker).values())  # PB-N07

    def test_a_dark_or_missing_image_is_not_an_empty_bus(self) -> None:
        detector = CountingDetector()
        worker = CameraWorker("rear", lambda: FakeCamera(3), detector)
        worker._camera = FakeCamera(3)
        worker._cycle()
        assert not worker.latest().ok and detector.calls == 0
        assert not usable(None) and usable(np.full((2, 2, 3), 100, dtype=np.uint8))

    def test_the_thread_recovers_from_a_camera_that_fails_to_open(self) -> None:
        import time

        attempts = {"n": 0}

        def opener() -> FakeCamera:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("no camera")
            return FakeCamera(120)

        worker = CameraWorker("front", opener, CountingDetector(), fps=20)
        worker.start()
        deadline = time.monotonic() + 6
        while not worker.latest().ok and time.monotonic() < deadline:
            time.sleep(0.05)
        worker.stop()
        assert worker.latest().ok and attempts["n"] >= 2


# -------------------------------------------------------------- calibration
class TestCalibrator:
    def test_four_clicks_store_a_polygon_and_move_on(self) -> None:
        layout = maqueta_layout()
        session = Calibrator(layout, "rear")
        first = session.current.id
        for x, y in ((0.1, 0.1), (0.2, 0.1), (0.2, 0.2), (1.5, -1)):
            session.click(x, y)
        stored = next(p for p in layout.positions if p.id == first)
        assert stored.calibrated and stored.polygon[-1] == (1.0, 0.0)  # clamped to the image
        assert session.current.id != first and session.done() == 1
        session.click(0.5, 0.5)
        session.undo()
        assert session.points == []
        session.move(-1)
        session.clear()
        assert session.done() == 0

    def test_cabin_outline(self) -> None:
        layout = maqueta_layout()
        session = Calibrator(layout, "front")
        session.toggle_cabin()
        for point in ((0, 0), (1, 0), (1, 1)):
            session.click(*point)
        session.close_cabin()
        assert len(layout.cabin["front"]) == 3 and not session.cabin_mode
        with pytest.raises(ValueError):
            Calibrator(small_layout(), "roof")
