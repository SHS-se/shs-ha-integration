"""Exercise price sensor getters without a Home Assistant runtime."""

import ast
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sys
from types import SimpleNamespace, MethodType
import unittest

ROOT = Path(__file__).parents[1] / "custom_components" / "shs_energy"
sys.path.insert(0, str(ROOT))
from supplier import all_in_price_slots
from tariff import grid_price_forecast, TariffError
from optimisation import quarter_start
from test_grid_price_parity import _catalog


def method(filename, class_name, name, **namespace):
    tree = ast.parse((ROOT / filename).read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    fn.decorator_list = []
    module = ast.parse("from __future__ import annotations")
    module.body.append(fn)
    exec(compile(module, filename, "exec"), namespace)
    return namespace[name]


class PriceSensorForecastTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 9, 11, 52, tzinfo=timezone.utc)
        self.starts = ["2026-09-09T11:30:00+00:00", "2026-09-09T11:45:00+00:00",
                       "2026-09-09T12:00:00+00:00", "2026-09-10T22:00:00+00:00"]
        self.coordinator = SimpleNamespace(
            tariff_catalog=_catalog({}),
            supplier_prices={"configuration": {"supplier": "test"}, "forecast": [
                {"start": start, "supplier_import_price_sek_per_kwh": price,
                 "supplier_export_price_sek_per_kwh": price - 0.2}
                for start, price in zip(self.starts, [9.0, 1.0, -0.1, 9.0])
            ]},
            grid_prices={}, demand_charge=None, latest_calculation=None,
        )
        price_quarters = method(
            "coordinator.py", "ShsStatusCoordinator", "_price_quarters",
            datetime=datetime, timezone=timezone, grid_price_forecast=grid_price_forecast,
            all_in_price_slots=all_in_price_slots, TariffError=TariffError,
            _LOGGER=logging.getLogger(__name__),
        )
        self.coordinator._price_quarters = MethodType(price_quarters, self.coordinator)
        self.forecast = method(
            "coordinator.py", "ShsStatusCoordinator", "total_price_forecast",
            timedelta=timedelta,
            quarter_start=quarter_start,
            dt_util=SimpleNamespace(
                utcnow=lambda: self.now,
                start_of_local_day=lambda: datetime(2026, 9, 9, tzinfo=timezone(timedelta(hours=2))),
            ),
        )

    def attributes(self, class_name, direction):
        getter = method("sensor.py", class_name, "extra_state_attributes")
        return getter(SimpleNamespace(coordinator=self.coordinator, direction=direction,
                                      _supplier_price=lambda: None))

    def test_total_forecasts_include_current_quarter_and_vary_in_both_directions(self):
        self.coordinator.total_price_forecast = self.forecast(self.coordinator)
        for direction, expected in (("import", [1.71, 0.61]), ("export", [0.833, -0.267])):
            with self.subTest(direction=direction):
                forecast = self.attributes("ShsTotalPriceSensor", direction)["forecast"]
                self.assertEqual(forecast, [
                    {"start": start, "price_sek_per_kwh": price}
                    for start, price in zip(self.starts[1:3], expected)
                ])

    def test_missing_source_produces_empty_total_forecasts(self):
        for source in ("supplier_prices", "tariff_catalog"):
            with self.subTest(source=source):
                original = getattr(self.coordinator, source)
                setattr(self.coordinator, source, None)
                self.coordinator.total_price_forecast = self.forecast(self.coordinator)
                for direction in ("import", "export"):
                    self.assertEqual(self.attributes("ShsTotalPriceSensor", direction)["forecast"], [])
                setattr(self.coordinator, source, original)

    def test_only_grid_export_retains_its_forecast(self):
        self.assertNotIn("forecast", self.attributes("ShsGridPriceSensor", "import"))
        self.coordinator.grid_price_forecast = [
            {"start": self.starts[1], "export_price_sek_per_kwh": 0.033}
        ]
        self.assertEqual(self.attributes("ShsGridPriceSensor", "export")["forecast"], [
            {"start": self.starts[1], "price_sek_per_kwh": 0.033}
        ])
