"""Store-and-forward queue on SQLite (RNF-8.1, RNF-8.2).

Every reading is written here **before** any attempt to send it, so a lost 4G/Wi-Fi link loses nothing.
When the link returns the oldest readings go first, in chronological order and with their original
timestamps (the core service stores them in the history without turning them into the live state).

* WAL journal: a crash or power cut leaves a consistent file;
* bounded: at most ``max_rows`` readings (default 60 000 = more than 48 h at one reading every 3 s) and at
  most ``max_age_s`` old; beyond that the oldest are dropped first, never the newest;
* thread-safe (one connection, one lock); the agent is the only writer.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    bus_id   TEXT    NOT NULL,
    ts       REAL    NOT NULL,
    payload  TEXT    NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings (ts, id);
"""


@dataclass(frozen=True)
class Pending:
    id: int
    bus_id: str
    ts: float
    payload: dict[str, Any]
    attempts: int


class Outbox:
    def __init__(self, path: str | Path, max_rows: int = 60_000, max_age_s: float = 48 * 3600.0) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._max_rows = max_rows
        self._max_age = max_age_s

    def put(self, bus_id: str, payload: dict[str, Any]) -> int:
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO readings (bus_id, ts, payload) VALUES (?, ?, ?)",
                (bus_id, float(payload["ts"]), json.dumps(payload, separators=(",", ":"))),
            )
            self._enforce_limits(float(payload["ts"]))
            return int(cursor.lastrowid or 0)

    def oldest(self, limit: int) -> list[Pending]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, bus_id, ts, payload, attempts FROM readings ORDER BY ts, id LIMIT ?", (limit,)
            ).fetchall()
        return [Pending(r[0], r[1], r[2], json.loads(r[3]), r[4]) for r in rows]

    def remove(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            self._db.executemany("DELETE FROM readings WHERE id = ?", [(i,) for i in ids])

    def failed(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            self._db.executemany("UPDATE readings SET attempts = attempts + 1 WHERE id = ?", [(i,) for i in ids])

    def __len__(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT count(*) FROM readings").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _enforce_limits(self, newest_ts: float) -> None:
        self._db.execute("DELETE FROM readings WHERE ts < ?", (newest_ts - self._max_age,))
        excess = int(self._db.execute("SELECT count(*) FROM readings").fetchone()[0]) - self._max_rows
        if excess > 0:
            self._db.execute(
                "DELETE FROM readings WHERE id IN (SELECT id FROM readings ORDER BY ts, id LIMIT ?)", (excess,)
            )
