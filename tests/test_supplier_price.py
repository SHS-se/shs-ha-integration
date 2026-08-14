"""Reading a supplier price sensor, without a Home Assistant runtime."""

from __future__ import annotations

from pathlib import Path
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
CONFIG_PANEL_FRONTEND = (
    Path(__file__).parents[1]
    / "custom_components"
    / "shs_energy"
    / "frontend"
    / "shs-energy-config-panel.js"
).read_text(encoding="utf-8")


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

    def test_totals_use_server_owned_supplier_prices(self) -> None:
        body = SENSOR[SENSOR.index("class ShsTotalPriceSensor") :]
        self.assertIn("current_supplier_prices(self.coordinator.supplier_prices)", body)
        self.assertNotIn("supplier_entity_id", body)

    def test_old_supplier_entity_options_are_removed_on_setup(self) -> None:
        setup = INIT[INIT.index("async def async_setup_entry") :]
        self.assertIn(
            "RETIRED_SUPPLIER_PRICE_OPTIONS.intersection(migrated_options)", setup
        )
        self.assertIn("migrated_options.pop(key)", setup)

    def test_device_cards_have_an_independent_save_button(self) -> None:
        self.assertIn('data-action="save-device"', CONFIG_PANEL_FRONTEND)
        self.assertIn("Save configuration", CONFIG_PANEL_FRONTEND)

    def test_panel_asset_uses_a_new_component_and_cache_key(self) -> None:
        self.assertIn('PANEL_ELEMENT = "shs-energy-config-panel-v3"', CONFIG_PANEL)
        self.assertIn("?v={FRONTEND_ASSET_VERSION}", CONFIG_PANEL)

    def test_setpoint_room_is_derived_instead_of_edited(self) -> None:
        fields = CONFIG_PANEL[
            CONFIG_PANEL.index("CONTROL_FIELDS") :
            CONFIG_PANEL.index("def _configuration_sections")
        ]
        self.assertNotIn('"area_id"', fields)

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

    def test_configuration_panel_websockets_require_an_admin(self) -> None:
        self.assertEqual(CONFIG_PANEL.count("@websocket_api.require_admin"), 4)
        self.assertNotIn("connection.require_admin", CONFIG_PANEL)


if __name__ == "__main__":
    unittest.main()
