"""Bounded, in-memory HTTP payload counters; never store credentials or bodies."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from time import monotonic
from typing import Any


class NetworkTraffic:
    """Counters since client creation, exposed through HA diagnostics."""

    def __init__(self) -> None:
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._started = monotonic()
        self._endpoints: dict[str, dict[str, int | float]] = {}

    def record(self, method: str, path: str, body: Any, status: int | None,
               response_bytes: int | None, failed: bool, elapsed_ms: float) -> None:
        endpoint = path.split("?", 1)[0]
        if not re.fullmatch(r"[a-z][a-z-]{0,79}", endpoint):
            endpoint = "other"
        verb = method if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"} else "OTHER"
        key = f"{verb} {endpoint}"
        if key not in self._endpoints and len(self._endpoints) >= 64:
            key = "OTHER overflow"
        totals = self._endpoints.setdefault(key, dict(
            requests=0, failed_requests=0, http_errors=0, responses_without_status=0,
            response_body_bytes=0, responses_measured=0, responses_unmeasured=0,
            request_body_bytes_estimate=0, request_bodies_unmeasured=0, elapsed_ms=0.0,
        ))
        totals["requests"] += 1
        totals["failed_requests"] += int(failed)
        totals["http_errors"] += int(status is not None and status >= 400)
        totals["responses_without_status"] += int(status is None)
        if response_bytes is None:
            totals["responses_unmeasured"] += 1
        else:
            totals["responses_measured"] += 1
            totals["response_body_bytes"] += response_bytes
        try:
            totals["request_body_bytes_estimate"] += len(json.dumps(body).encode("utf-8")) if body is not None else 0
        except (TypeError, ValueError):
            totals["request_bodies_unmeasured"] += 1
        totals["elapsed_ms"] += elapsed_ms

    def snapshot(self) -> dict[str, Any]:
        endpoints = {key: dict(value) for key, value in self._endpoints.items()}
        total: dict[str, int | float] = {}
        for values in endpoints.values():
            for key, value in values.items():
                total[key] = total.get(key, 0) + value
        return dict(schema_version=1, source="home_assistant",
                    started_at=self._started_at, observed_seconds=monotonic() - self._started,
                    byte_basis="decoded_response_body; request_body_estimate; excludes_headers_and_transport",
                    total=total, endpoints=endpoints)
