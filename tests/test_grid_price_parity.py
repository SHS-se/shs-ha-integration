"""This implementation is the reference the portal's port is checked against.

The portal now prices historical quarters itself, using a TypeScript port of
current_grid_prices() in supabase/functions/_shared/energy-grid-pricing.ts.
Two implementations of a price a customer actually pays is a real risk, so the
same fixture is asserted in both repositories: grid-price-parity.fixture.json
is duplicated verbatim, because CI cannot reach across the two repos.

Change the calculation here and this test fails, which is the signal to
regenerate both copies of the fixture and re-check the port. Deleting this test
to make a change pass would silently mis-price a customer's history.

See ENERGY_OPTIMISATION_ARCHITECTURE.md section 1.3.7.6 in the portal repo.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from tariff import current_grid_prices  # noqa: E402

FIXTURE = json.loads(
    (Path(__file__).parent / "grid-price-parity.fixture.json").read_text(
        encoding="utf-8"
    )
)
COMPARED_KEYS = (
    "import_price_sek_per_kwh",
    "export_price_sek_per_kwh",
    "load_period",
    "tariff_revision",
)


def _catalog(overrides: dict) -> dict:
    configuration = dict(FIXTURE["base_configuration"])
    configuration.update(overrides)
    return {
        "schema_version": 2,
        "calculation_version": 2,
        "timezone": FIXTURE["timezone"],
        "configuration": configuration,
        "missing_inputs": [],
        "profiles": [FIXTURE["profile"]],
    }


class GridPriceParityTests(unittest.TestCase):
    def test_fixture_is_the_expected_version(self) -> None:
        self.assertEqual(FIXTURE["fixture_version"], "grid-price-parity-1")
        self.assertGreaterEqual(len(FIXTURE["cases"]), 17)

    def test_every_case_still_produces_its_recorded_price(self) -> None:
        for case in FIXTURE["cases"]:
            with self.subTest(case["label"]):
                result = current_grid_prices(
                    _catalog(case["overrides"]),
                    datetime.fromisoformat(case["at"]),
                )
                if case["expected"] is None:
                    self.assertIsNone(result)
                    continue
                self.assertIsNotNone(result)
                self.assertEqual(
                    {key: result[key] for key in COMPARED_KEYS},
                    case["expected"],
                )


if __name__ == "__main__":
    unittest.main()
