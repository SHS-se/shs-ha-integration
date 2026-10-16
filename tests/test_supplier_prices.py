"""Server-owned supplier-price contract."""

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace

sys.path.append(str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from shs_core.supplier import (  # noqa: E402
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


class HistoryStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_prices_its_readings_before_the_periodic_cache_is_loaded(self):
        from household_fixture import Rig
        h = Rig().household
        start = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
        end = datetime(2026, 8, 13, 13, tzinfo=timezone.utc)
        h._hourly_grid_readings = AsyncMock(return_value=[
            SimpleNamespace(start=start, import_kwh=2, export_kwh=1)])
        h.client.prices = AsyncMock(return_value=_payload())
        result = await h._supplier_daily_costs({}, start, end)
        self.assertEqual(result, [{'date': '2026-08-13', 'import_kwh': 2,
            'import_cost_sek': 2.6, 'export_kwh': 1, 'export_credit_sek': 0.8, 'priced_hours': 1}])
        self.assertEqual(h.client.prices.await_args_list[0].args, ())
        self.assertEqual(h.client.prices.await_args_list[1].args, ('2026-08-13', '2026-08-13'))

    async def test_startup_price_download_failure_is_not_successful_empty_history(self):
        from household_fixture import Rig
        from shs_core.api import ShsApiError
        h = Rig().household
        h._hourly_grid_readings = AsyncMock(return_value=[object()])
        h.client.prices = AsyncMock(side_effect=ShsApiError('price lookup failed'))
        with self.assertRaisesRegex(ShsApiError, 'price lookup failed'):
            await h._supplier_daily_costs({}, h.ports.utcnow(), h.ports.utcnow())
