"""Reading a supplier price sensor, without a Home Assistant runtime."""

from __future__ import annotations

from pathlib import Path
import re
import unittest

SENSOR = (
    Path(__file__).parents[1]
    / "custom_components"
    / "shs_energy"
    / "sensor.py"
).read_text(encoding="utf-8")
INIT = (
    Path(__file__).parents[1]
    / "custom_components"
    / "shs_energy"
    / "__init__.py"
).read_text(encoding="utf-8")
COORDINATOR = (
    Path(__file__).parents[1]
    / "custom_components"
    / "shs_energy"
    / "coordinator.py"
).read_text(encoding="utf-8")
CONFIGURATION = (
    Path(__file__).parents[1]
    / "custom_components"
    / "shs_energy"
    / "configuration.py"
).read_text(encoding="utf-8")
CONFIG_FLOW = (
    Path(__file__).parents[1]
    / "custom_components"
    / "shs_energy"
    / "config_flow.py"
).read_text(encoding="utf-8")
CONFIG_PANEL = (
    Path(__file__).parents[1]
    / "custom_components"
    / "shs_energy"
    / "config_panel.py"
).read_text(encoding="utf-8")

# sensor.py imports Home Assistant, so lift out the one pure helper.
_SOURCE = SENSOR[SENSOR.index("def price_from_state") : SENSOR.index("class ShsTotalPriceSensor")]
_NAMESPACE: dict[str, object] = {}
exec(  # noqa: S102 - trusted first-party source
    "from __future__ import annotations\nfrom math import isfinite\nfrom typing import Any\n"
    + _SOURCE,
    _NAMESPACE,
)
price_from_state = _NAMESPACE["price_from_state"]


class SupplierPriceTests(unittest.TestCase):
    def test_reads_a_normal_price(self) -> None:
        self.assertEqual(price_from_state("0.307"), 0.307)
        self.assertEqual(price_from_state("-0.05"), -0.05)
        self.assertEqual(price_from_state(0.5), 0.5)

    def test_unusable_states_give_no_price(self) -> None:
        for raw in (None, "", "unknown", "unavailable", "abc", "1,5"):
            with self.subTest(raw=raw):
                self.assertIsNone(price_from_state(raw))

    def test_non_finite_values_are_rejected(self) -> None:
        for raw in ("nan", "inf", "-inf"):
            with self.subTest(raw=raw):
                self.assertIsNone(price_from_state(raw))

    def test_totals_add_the_grid_share_to_the_supplier_price(self) -> None:
        # The live figures: 0.71 grid + 0.307 supplier.
        self.assertAlmostEqual(round(0.71 + price_from_state("0.307"), 5), 1.017)


class SensorWiringTests(unittest.TestCase):
    """Guard the parts a Home-Assistant-free test cannot exercise directly."""

    def test_total_sensors_are_registered(self) -> None:
        self.assertIn('ShsTotalPriceSensor(coordinator, "import")', SENSOR)
        self.assertIn('ShsTotalPriceSensor(coordinator, "export")', SENSOR)

    def test_ev_current_target_sensor_is_registered(self) -> None:
        self.assertIn("ShsEvPlanCurrentSensor(coordinator)", SENSOR)
        self.assertIn('slot["ev_target_current_a"]', SENSOR)

    def test_a_missing_supplier_price_leaves_the_total_unknown(self) -> None:
        # Falling back to the grid share alone would read as an all-in price.
        body = SENSOR[SENSOR.index("class ShsTotalPriceSensor") :]
        self.assertIn("if prices is None or supplier is None:", body)
        self.assertIn(
            "return None", body[body.index("if prices is None or supplier is None:") :]
        )

    def test_it_follows_the_supplier_sensor(self) -> None:
        self.assertIn("async_track_state_change_event", SENSOR)

    def test_changing_the_options_reloads_the_entry(self) -> None:
        # Entities subscribe to the supplier price sensor when they are added.
        # A price entity chosen after setup is only watched if the entry is
        # rebuilt, so re-pushing alone would leave totals on the hourly poll.
        listener = INIT[INIT.index("async def _async_options_updated") :]
        self.assertIn("async_reload(entry.entry_id)", listener)

    def test_startup_planning_waits_for_entity_providers(self) -> None:
        helper = INIT[
            INIT.index("async def _async_delayed_startup_optimisation_push") :
            INIT.index("def _entry_for_call")
        ]
        self.assertIn(
            "await asyncio.sleep(OPTIMISATION_STARTUP_DELAY_SECONDS)", helper
        )
        self.assertIn("OPTIMISATION_STARTUP_RETRY_SECONDS", helper)
        self.assertIn("optimisation_input_gap_is_transient()", helper)
        self.assertIn(
            "_async_delayed_startup_optimisation_push(coordinator)", INIT
        )

    def test_transient_startup_gaps_do_not_raise_an_immediate_repair(self) -> None:
        issue_sync = COORDINATOR[
            COORDINATOR.index("def _sync_optimisation_issue") :
            COORDINATOR.index("@property", COORDINATOR.index("def _sync_optimisation_issue"))
        ]
        self.assertIn("self._optimisation_issue_grace_until", issue_sync)
        self.assertIn("optimisation_input_gap_is_transient()", issue_sync)
        self.assertIn('" does not exist"', issue_sync)
        self.assertIn('" is unavailable"', issue_sync)

    def test_battery_export_preference_reaches_the_planner_snapshot(self) -> None:
        for option in (
            "OPT_BATTERY_EXPORT_ENABLED",
            "OPT_BATTERY_EXPORT_RESERVE_SOC",
            "OPT_BATTERY_EXPORT_MIN_PRICE",
        ):
            with self.subTest(option=option):
                self.assertIn(option, CONFIGURATION)
                self.assertIn(option, CONFIG_PANEL)
                self.assertIn(option, COORDINATOR)
        self.assertIn('"battery_export_enabled"', COORDINATOR)
        self.assertIn('"battery_export_reserve_soc"', COORDINATOR)
        self.assertIn(
            '"battery_export_min_price_sek_per_kwh"', COORDINATOR
        )

    def test_integration_cogwheel_opens_the_full_page_panel(self) -> None:
        self.assertNotIn("OptionsFlow", CONFIG_FLOW)
        self.assertIn("config_panel_domain=shs_const.DOMAIN", CONFIG_PANEL)
        self.assertIn("await async_register_config_panel(hass)", INIT)


if __name__ == "__main__":
    unittest.main()
