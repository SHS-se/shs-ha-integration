"""When the car was plugged in, kept across replans and restarts.

The planner's grace below the car's floor runs from this moment, so nothing but
the cable actually going in may move it. A restart, a replan or an unavailable
reading never does: a restart also resets Home Assistant's own last-changed
time, which is why that time is read only when the cable is first seen going in.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def observe_plug(
    record: dict[str, Any] | None,
    entity_id: str,
    connected: bool,
    last_changed: datetime | None,
    now: datetime,
) -> dict[str, Any]:
    """The plug record after one confirmed cable reading. Pure and idempotent."""
    if not connected:
        return {"entity_id": entity_id, "connected": False, "plugged_in_at": None}
    if (
        record
        and record.get("entity_id") == entity_id
        and record.get("connected") is True
        and record.get("plugged_in_at")
    ):
        return record
    changed = min(last_changed, now) if last_changed else now
    return {"entity_id": entity_id, "connected": True, "plugged_in_at": _utc(changed)}
