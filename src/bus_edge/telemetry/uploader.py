"""HTTP client towards the core service ingestion API (standard library only, TLS verified).

* one reading:  ``POST /api/v1/internal/buses/{bus_id}/telemetry``
* a backlog:    ``POST /api/v1/internal/telemetry/batch`` (up to 500, oldest first)

Errors are classified so the agent knows what to do with the queued readings:
``retryable`` (network down, timeout, 429, 5xx: keep them and retry with back-off) or permanent
(400/404/409/422: that reading will never be accepted; drop it instead of blocking the queue forever).
The ingest key travels only in the ``X-Ingest-Key`` header and is never logged.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

BATCH_LIMIT = 500
# Measured against PostgreSQL + Valkey on the development PC: ~19 ms per reading, 8.9 s for a batch of 500. With
# the 8 s of a single reading a full batch timed out every time and the backlog never drained.
BATCH_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class SendResult:
    ok: bool
    retryable: bool
    status: int | None = None
    rejected_indexes: tuple[int, ...] = field(default_factory=tuple)


class IngestClient:
    def __init__(
        self,
        base_url: str,
        ingest_key: str,
        timeout_s: float = 8.0,
        verify_tls: bool = True,
        batch_timeout_s: float = BATCH_TIMEOUT_S,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = ingest_key
        self._timeout = timeout_s
        self._batch_timeout = batch_timeout_s
        self._context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()  # noqa: S323

    def send_one(self, bus_id: str, payload: dict[str, Any]) -> SendResult:
        status, _ = self._post(f"/api/v1/internal/buses/{bus_id}/telemetry", payload)
        return _classify(status)

    def send_batch(self, items: list[tuple[str, dict[str, Any]]]) -> SendResult:
        body = {"readings": [{**payload, "bus_id": bus_id} for bus_id, payload in items[:BATCH_LIMIT]]}
        status, response = self._post("/api/v1/internal/telemetry/batch", body, self._batch_timeout)
        result = _classify(status)
        if result.ok and isinstance(response, dict):
            rejected = tuple(
                int(item["index"]) for item in response.get("items", []) if item.get("status") == "rejected"
            )
            return SendResult(True, False, status, rejected)
        return result

    def _post(self, path: str, body: dict[str, Any], timeout_s: float | None = None) -> tuple[int | None, Any]:
        request = urllib.request.Request(  # noqa: S310 - the URL comes from the configuration, checked at start
            self._base + path,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Ingest-Key": self._key},
            method="POST",
        )
        try:
            timeout = timeout_s or self._timeout
            with urllib.request.urlopen(request, timeout=timeout, context=self._context) as response:  # noqa: S310
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            return error.code, None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None, None


def _classify(status: int | None) -> SendResult:
    if status is not None and 200 <= status < 300:
        return SendResult(True, False, status)
    retryable = status is None or status == 429 or status >= 500 or status in (401, 403)
    # 401/403 are retried: a wrong or rotated key is fixed on the server or in the config, and the readings
    # must still be there when that happens.
    return SendResult(False, retryable, status)
