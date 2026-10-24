"""The on-board agent: every 3 seconds, one reading from doors + cameras + GPS, queued and sent (RF-8.3).

Cycle (``EdgeAgent.cycle``):

1. position from the GPS (real or replay); arrival at a terminal resets the door counter once (RNF-6.3);
2. on-board count from the door counter; boxes from the camera workers -> seated / standing / excess;
3. the payload is written to the SQLite outbox **first** (nothing is lost if the link is down);
4. the outbox is flushed oldest first: one request when it holds one reading, a batch of up to 500 when a
   backlog built up; network errors back off exponentially (3 s -> 60 s) without blocking the counting.

In :meth:`EdgeAgent.run` step 4 runs in its own thread: a hung server makes a request wait up to its timeout
(8 s, 30 s for a batch), and the readings must still be taken every 3 s.

Without a position (GPS still searching) no reading is produced: the server needs coordinates. Without
cameras the reading still goes out, with confidence 0, and the server relies on the door count.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from bus_edge.config import EdgeSettings
from bus_edge.doors.link import DoorLink
from bus_edge.gps.sources import GpsSource, TerminalDetector
from bus_edge.telemetry.outbox import Outbox
from bus_edge.telemetry.reading import build_payload
from bus_edge.telemetry.uploader import BATCH_LIMIT, IngestClient
from bus_edge.vision.cameras import CameraWorker
from bus_edge.vision.occupancy import OccupancyEstimator, VisionResult

log = logging.getLogger(__name__)
MOVING_KMH = 3.0
MAX_BACKOFF_S = 60.0


@dataclass(frozen=True)
class CycleReport:
    queued: bool
    sent: int
    dropped: int
    backlog: int
    on_board_doors: int
    vision: VisionResult | None
    note: str = ""

    def line(self, route_id: str) -> str:
        cameras = (
            "sin cámaras"
            if self.vision is None or self.vision.visible_share == 0
            else f"cámaras {self.vision.seated} sentados, {self.vision.standing} de pie, "
            f"{self.vision.standing_out_of_position} fuera de posición (conf {self.vision.confidence:.2f})"
        )
        return (
            f"[{route_id}] puertas: {self.on_board_doors} a bordo | {cameras} | "
            f"enviadas {self.sent}, en cola {self.backlog}{' | ' + self.note if self.note else ''}"
        )


class EdgeAgent:
    def __init__(
        self,
        settings: EdgeSettings,
        doors: DoorLink,
        gps: GpsSource,
        workers: dict[str, CameraWorker],
        estimator: OccupancyEstimator,
        outbox: Outbox,
        client: IngestClient,
        terminal: TerminalDetector,
        model_version: str,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._doors = doors
        self._gps = gps
        self._workers = workers
        self._estimator = estimator
        self._outbox = outbox
        self._client = client
        self._terminal = terminal
        self._model_version = model_version
        self._wall = wall_clock
        self._mono = monotonic
        self._backoff = 0.0
        self._next_attempt = 0.0
        self._tally_lock = threading.Lock()
        self._tally = (0, 0)  # (sent, dropped) by the upload thread since the last report

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._doors.start()
        self._gps.start()
        for worker in self._workers.values():
            worker.start()

    def stop(self) -> None:
        for worker in self._workers.values():
            worker.stop()
        self._gps.stop()
        self._doors.stop()
        self._outbox.close()

    def run(self, stop: threading.Event, on_report: Callable[[CycleReport], None] | None = None) -> None:
        interval = self._settings.send_interval_s
        uploader = threading.Thread(target=self._upload_loop, args=(stop,), name="uploader", daemon=True)
        uploader.start()
        next_tick = self._mono()
        while not stop.is_set():
            report = self.cycle(send=False)
            if on_report is not None:
                on_report(report)
            next_tick += interval
            stop.wait(max(0.0, next_tick - self._mono()))
        uploader.join(timeout=5.0)  # a request in flight is abandoned: its readings stay queued

    def _upload_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            sent, dropped = self.flush()
            with self._tally_lock:
                self._tally = (self._tally[0] + sent, self._tally[1] + dropped)
            if sent + dropped == 0 or len(self._outbox) == 0:  # otherwise a backlog is draining: go on at once
                stop.wait(self._settings.send_interval_s)

    def _take_tally(self) -> tuple[int, int]:
        with self._tally_lock:
            tally, self._tally = self._tally, (0, 0)
        return tally

    # -------------------------------------------------------------------- cycle
    def cycle(self, send: bool = True) -> CycleReport:
        """One reading. With ``send`` the outbox is flushed here too; :meth:`run` leaves that to its upload thread
        and reports what the thread sent since the previous cycle."""
        fix = self._gps.current()
        if self._terminal.arrived(fix, self._gps.at_terminal()):
            log.info("Terminal reached: door counter reset")
            self._doors.request_reset()
        doors = self._doors.snapshot()
        now = self._mono()
        vision: VisionResult | None = None
        note = ""
        if fix is None:
            note = "sin posición GPS: no se genera lectura"
        else:
            observations = {name: worker.latest() for name, worker in self._workers.items()}
            vision = self._estimator.estimate(observations, now, (fix.speed_kmh or 0.0) > MOVING_KMH)
            payload = build_payload(self._wall(), fix, doors, vision, self._model_version)
            self._outbox.put(self._settings.bus_id, payload)
        if not doors.healthy(now):
            note = (note + "; " if note else "") + "contador de puertas sin señal"
        sent, dropped = self.flush() if send else self._take_tally()
        return CycleReport(fix is not None, sent, dropped, len(self._outbox), doors.on_board, vision, note)

    def flush(self) -> tuple[int, int]:
        """Send the queue, oldest first. Returns (sent, dropped as permanently rejected)."""
        if self._mono() < self._next_attempt:
            return 0, 0
        pending = self._outbox.oldest(BATCH_LIMIT)
        if not pending:
            return 0, 0
        if len(pending) == 1:
            result = self._client.send_one(pending[0].bus_id, pending[0].payload)
        else:
            result = self._client.send_batch([(p.bus_id, p.payload) for p in pending])
        if result.ok:
            self._backoff = 0.0
            self._outbox.remove([p.id for p in pending])
            dropped = len(result.rejected_indexes)
            if dropped:
                log.warning("%s queued readings were rejected by the server and dropped", dropped)
            return len(pending) - dropped, dropped
        if result.retryable:
            self._backoff = min(MAX_BACKOFF_S, max(self._settings.send_interval_s, self._backoff * 2))
            self._next_attempt = self._mono() + self._backoff
            self._outbox.failed([p.id for p in pending])
            log.warning("Server unreachable (status %s); retrying in %.0f s", result.status, self._backoff)
            return 0, 0
        # Permanent rejection: with a single reading drop it; with a batch the server answers per item.
        self._outbox.remove([pending[0].id])
        log.warning("Reading rejected by the server (status %s) and dropped", result.status)
        return 0, 1
