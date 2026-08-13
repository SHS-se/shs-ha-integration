"""Validation and lookup for server-owned electricity supplier prices."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from math import isfinite
from typing import Any


class SupplierPriceError(ValueError):
    """The SHS supplier-price response is unusable."""


def validate_supplier_prices(payload: dict[str, Any]) -> None:
    """Validate the fail-fast integration-prices contract."""
    if payload.get("schema_version") != 1:
        raise SupplierPriceError("unsupported supplier-price schema")
    missing = payload.get("missing_inputs")
    if not isinstance(missing, list):
        raise SupplierPriceError("supplier prices are missing missing_inputs")
    if payload.get("configuration") is None:
        if not missing or payload.get("current") is not None or payload.get("forecast") != []:
            raise SupplierPriceError("unconfigured supplier-price response is inconsistent")
        return
    configuration = payload["configuration"]
    if configuration.get("supplier") is None or configuration.get("price_area") not in {
        "SE1", "SE2", "SE3", "SE4"
    }:
        raise SupplierPriceError("supplier-price configuration is invalid")
    try:
        datetime.fromisoformat(payload["terms_valid_from"])
    except (KeyError, TypeError, ValueError) as err:
        raise SupplierPriceError("supplier terms have no effective date") from err
    forecast = payload.get("forecast")
    if not isinstance(forecast, list) or not forecast:
        raise SupplierPriceError("supplier-price forecast is empty")
    previous_end: datetime | None = None
    for index, slot in enumerate(forecast):
        try:
            start = datetime.fromisoformat(slot["start"]).astimezone(timezone.utc)
            end = datetime.fromisoformat(slot["end"]).astimezone(timezone.utc)
            values = (
                float(slot["spot_price_sek_per_kwh"]),
                float(slot["supplier_import_price_sek_per_kwh"]),
                float(slot["supplier_export_price_sek_per_kwh"]),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise SupplierPriceError(f"invalid supplier-price slot {index}") from err
        if (end - start).total_seconds() != 15 * 60 or not all(
            isfinite(value) for value in values
        ):
            raise SupplierPriceError(f"invalid supplier-price slot {index}")
        if previous_end is not None and start != previous_end:
            raise SupplierPriceError(f"supplier-price forecast has a gap at slot {index}")
        previous_end = end


def current_supplier_prices(
    payload: dict[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the server's current native-quarter supplier prices."""
    if not payload or payload.get("configuration") is None:
        return None
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return next(
        (
            slot
            for slot in payload.get("forecast", [])
            if datetime.fromisoformat(slot["start"]).astimezone(timezone.utc)
            <= current_time
            < datetime.fromisoformat(slot["end"]).astimezone(timezone.utc)
        ),
        None,
    )


def supplier_price_forecast(
    payload: dict[str, Any] | None,
) -> dict[datetime, dict[str, float]]:
    """Index forecast slots by UTC start."""
    if not payload or payload.get("configuration") is None:
        return {}
    return {
        datetime.fromisoformat(slot["start"]).astimezone(timezone.utc): {
            "import": float(slot["supplier_import_price_sek_per_kwh"]),
            "export": float(slot["supplier_export_price_sek_per_kwh"]),
        }
        for slot in payload.get("forecast", [])
    }


def hourly_supplier_price_means(
    payload: dict[str, Any],
) -> dict[datetime, dict[str, float]]:
    """Average native quarters for matching recorder hourly energy buckets."""
    grouped: dict[datetime, dict[str, list[float]]] = defaultdict(
        lambda: {"import": [], "export": []}
    )
    for start, prices in supplier_price_forecast(payload).items():
        hour = start.replace(minute=0, second=0, microsecond=0)
        grouped[hour]["import"].append(prices["import"])
        grouped[hour]["export"].append(prices["export"])
    return {
        hour: {
            direction: sum(values) / len(values)
            for direction, values in directions.items()
        }
        for hour, directions in grouped.items()
        if all(len(values) == 4 for values in directions.values())
    }
