"""Server-owned supplier-price contract."""

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from supplier import (  # noqa: E402
    SupplierPriceError,
    current_supplier_prices,
    hourly_supplier_price_means,
    validate_supplier_prices,
)


def _payload() -> dict:
    slots = []
    for minute, import_price in zip((0, 15, 30, 45), (1.0, 1.2, 1.4, 1.6)):
        slots.append({
            "start": f"2026-08-13T12:{minute:02d}:00+00:00",
            "end": (
                f"2026-08-13T12:{minute + 15:02d}:00+00:00"
                if minute < 45
                else "2026-08-13T13:00:00+00:00"
            ),
            "spot_price_sek_per_kwh": 0.8,
            "supplier_import_price_sek_per_kwh": import_price,
            "supplier_export_price_sek_per_kwh": 0.8,
        })
    return {
        "schema_version": 1,
        "configuration": {"supplier": "tibber", "price_area": "SE3"},
        "missing_inputs": [],
        "current": slots[0],
        "forecast": slots,
        "terms_valid_from": "2026-08-13",
    }


class SupplierPriceContractTests(unittest.TestCase):
    def test_valid_prices_expose_current_and_hourly_mean(self) -> None:
        payload = _payload()
        validate_supplier_prices(payload)
        current = current_supplier_prices(
            payload, datetime(2026, 8, 13, 12, 5, tzinfo=timezone.utc)
        )
        self.assertEqual(current["supplier_export_price_sek_per_kwh"], 0.8)
        means = hourly_supplier_price_means(payload)
        hour = means[datetime(2026, 8, 13, 12, tzinfo=timezone.utc)]
        self.assertAlmostEqual(hour["import"], 1.3)
        self.assertAlmostEqual(hour["export"], 0.8)

    def test_gap_is_rejected(self) -> None:
        payload = _payload()
        payload["forecast"][1]["start"] = "2026-08-13T12:20:00+00:00"
        payload["forecast"][1]["end"] = "2026-08-13T12:35:00+00:00"
        with self.assertRaisesRegex(SupplierPriceError, "gap"):
            validate_supplier_prices(payload)
