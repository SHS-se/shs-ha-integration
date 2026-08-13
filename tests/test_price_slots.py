"""All-in quarter prices pushed to the portal, and the backfill that fills them.

The portal stores no historical price of its own and deliberately will not
derive one, so this is the single source
(ENERGY_OPTIMISATION_ARCHITECTURE.md §1.3.7).
"""

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from supplier import all_in_price_slots  # noqa: E402

COORDINATOR = (
    Path(__file__).parents[1]
    / "custom_components"
    / "shs_energy"
    / "coordinator.py"
).read_text(encoding="utf-8")
INIT = (
    Path(__file__).parents[1]
    / "custom_components"
    / "shs_energy"
    / "__init__.py"
).read_text(encoding="utf-8")
SERVICES = (
    Path(__file__).parents[1]
    / "custom_components"
    / "shs_energy"
    / "services.yaml"
).read_text(encoding="utf-8")
API = (
    Path(__file__).parents[1]
    / "custom_components"
    / "shs_energy"
    / "api.py"
).read_text(encoding="utf-8")


def _supplier_payload(minutes=(0, 15, 30, 45)) -> dict:
    slots = [
        {
            "start": f"2026-08-13T12:{minute:02d}:00+00:00",
            "end": f"2026-08-13T{12 + (minute + 15) // 60}:{(minute + 15) % 60:02d}:00+00:00",
            "spot_price_sek_per_kwh": 0.8,
            "supplier_import_price_sek_per_kwh": 1.0,
            "supplier_export_price_sek_per_kwh": 0.6,
        }
        for minute in minutes
    ]
    return {
        "schema_version": 1,
        "configuration": {"supplier": "tibber", "price_area": "SE3"},
        "missing_inputs": [],
        "current": slots[0],
        "forecast": slots,
        "terms_valid_from": "2026-08-13",
    }


def _grid_slots(minutes=(0, 15, 30, 45)) -> dict:
    return {
        datetime(2026, 8, 13, 12, minute, tzinfo=timezone.utc): {
            "start": f"2026-08-13T12:{minute:02d}:00+00:00",
            "import_price_sek_per_kwh": 0.5,
            "export_price_sek_per_kwh": 0.1,
        }
        for minute in minutes
    }


class AllInPriceTests(unittest.TestCase):
    def test_price_is_supplier_plus_grid(self) -> None:
        slots = all_in_price_slots(_supplier_payload(), _grid_slots())
        self.assertEqual(len(slots), 4)
        self.assertAlmostEqual(slots[0]["import_price_sek_per_kwh"], 1.5)
        self.assertAlmostEqual(slots[0]["export_price_sek_per_kwh"], 0.7)

    def test_a_quarter_missing_either_half_is_omitted(self) -> None:
        # A supplier price with no grid component would look like a complete
        # price while understating the marginal cost by the transfer and tax.
        slots = all_in_price_slots(
            _supplier_payload(), _grid_slots(minutes=(0, 15))
        )
        self.assertEqual(len(slots), 2)

    def test_unconfigured_supplier_prices_nothing(self) -> None:
        self.assertEqual(all_in_price_slots(None, _grid_slots()), [])
        self.assertEqual(
            all_in_price_slots(
                {"configuration": None, "forecast": []}, _grid_slots()
            ),
            [],
        )

    def test_slots_are_ordered_and_utc(self) -> None:
        slots = all_in_price_slots(
            _supplier_payload(minutes=(45, 0, 30, 15)), _grid_slots()
        )
        starts = [slot["start"] for slot in slots]
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(all(start.endswith("+00:00") for start in starts))


class PricePushWiringTests(unittest.TestCase):
    """The wiring is asserted by reading the source: there is no HA runtime."""

    def test_every_exchange_pushes_prices(self) -> None:
        self.assertIn("price_slots = self._price_quarters(", COORDINATOR)
        self.assertIn(
            "actuals, snapshot, devices, thermal_slots, price_slots",
            COORDINATOR,
        )

    def test_prices_alone_are_worth_a_push(self) -> None:
        # Without this a backfill on a home with no new actuals would return
        # early and send nothing.
        self.assertIn("and not price_slots", COORDINATOR)

    def test_price_slots_are_omitted_when_empty(self) -> None:
        # An older portal must keep seeing the request shape it accepts.
        self.assertIn("if price_slots:", API)
        self.assertIn('body["price_slots"] = price_slots', API)

    def test_backfill_is_bounded_and_chunked(self) -> None:
        self.assertIn("async def async_backfill_prices", COORDINATOR)
        self.assertIn("PRICE_BACKFILL_MAX_DAYS", COORDINATOR)
        self.assertIn("PRICE_BACKFILL_CHUNK_DAYS", COORDINATOR)

    def test_backfill_refetches_prices_for_the_requested_dates(self) -> None:
        # The cached forecast only covers today and tomorrow, so a backfill
        # reading it would silently price nothing.
        self.assertIn("await self.client.prices(", COORDINATOR)
        self.assertIn("validate_supplier_prices(payload)", COORDINATOR)

    def test_backfill_is_registered_as_a_service(self) -> None:
        self.assertIn('SERVICE_BACKFILL_PRICES = "backfill_prices"', INIT)
        self.assertIn("async_backfill_prices", INIT)
        self.assertIn("backfill_prices:", SERVICES)


if __name__ == "__main__":
    unittest.main()
