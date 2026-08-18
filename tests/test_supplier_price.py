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
PLANNING = (
    Path(__file__).parents[1]
    / "custom_components"
    / "shs_energy"
    / "planning.py"
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
CONSTANTS = (
    Path(__file__).parents[1]
    / "custom_components"
    / "shs_energy"
    / "const.py"
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

    def test_old_supplier_entity_options_are_archived_on_setup(self) -> None:
        setup = INIT[INIT.index("async def async_setup_entry") :]
        self.assertIn(
            "RETIRED_SUPPLIER_PRICE_OPTIONS.intersection(migrated_options)", setup
        )
        self.assertIn("legacy_archive.setdefault(key, migrated_options[key])", setup)
        self.assertIn("migrated_options.pop(key)", setup)

    def test_the_ev_electrical_model_is_configurable_again(self) -> None:
        """Reversing a deliberate retirement, and why.

        These options were removed to stop the panel filling with knobs, which
        is right for anything a household would have to *tune* (§8.10: a
        setting the customer revisits is a defect). A charge cable's phase
        count is not that. It is a fact about the wiring that the software
        cannot observe, cannot infer, and gets wrong by a factor of three when
        it assumes: a single-phase 16 A charger delivers 3.7 kW and was planned
        at 11 kW. A wrong constant does not fail — it produces a confident plan
        — so nothing ever surfaced it.

        The distinction this test now protects is between an installation fact,
        which must be correctable without a release, and a preference or a
        derived value, which must not come back.
        """
        sections = CONFIG_PANEL[
            CONFIG_PANEL.index('"id": "ev"') :
            CONFIG_PANEL.index("def _entry_state")
        ]
        for installation_fact in (
            "OPT_EV_PHASE_COUNT",
            "OPT_EV_PHASE_VOLTAGE",
            "OPT_EV_CHARGE_EFFICIENCY",
            "OPT_EV_KWH_PER_KM",
        ):
            with self.subTest(field=installation_fact):
                self.assertIn(installation_fact, sections)
        # Still retired: a confirmation flag, a capacity now derived from
        # energy-remaining over SOC, a departure that is an entity, and a
        # minimum run nobody holds an opinion about.
        for retired_field in (
            "OPT_EV_ELECTRICAL_CONFIRMED",
            "OPT_EV_BATTERY_KWH",
            "OPT_EV_MIN_RUN_SLOTS",
            "OPT_EV_DEFAULT_DEPARTURE",
        ):
            with self.subTest(field=retired_field):
                self.assertNotIn(retired_field, sections)
        # The constants survive as defaults, which is what makes an existing
        # installation keep behaving exactly as it did.
        self.assertIn("EV_PHASE_COUNT = 3", CONSTANTS)
        self.assertIn("EV_PHASE_VOLTAGE = 230.0", CONSTANTS)
        self.assertNotIn("OPT_EV_DEFAULT_DEPARTURE", PLANNING)
        setup = INIT[INIT.index("async def async_setup_entry") :]
        self.assertIn(
            "RETIRED_PLANNING_OPTIONS.intersection(migrated_options)", setup
        )
        self.assertIn("recover_legacy_ev_options", setup)

    def test_mapped_loads_are_automatic_and_ev_departure_is_optional(self) -> None:
        sections = CONFIG_PANEL[
            CONFIG_PANEL.index('"id": "ev"') :
            CONFIG_PANEL.index("def _entry_state")
        ]
        for obsolete in (
            "OPT_EV_PLANNING_ENABLED",
            "OPT_EV_DEFERRABLE_CONFIRMED",
            "required_when=",
        ):
            self.assertNotIn(obsolete, sections)
        self.assertNotIn("field.required_when", CONFIG_PANEL_FRONTEND)
        self.assertIn('"Optional departure timestamp"', sections)
        self.assertNotIn("departure timestamp entity is not configured", PLANNING)
        # Service selection and sizing are executed in tests/test_planning.py.
        # This only guards the boundary that makes that possible: planning
        # logic stays importable without Home Assistant.
        self.assertNotIn("homeassistant", PLANNING)
        self.assertNotIn('"id": "hot_water"', CONFIG_PANEL)
        # The pool section is state, not a planning switch. It carries the
        # water-temperature sensor and volume the store model needs, and must
        # never regrow the retired per-service enable/confirm toggles.
        pool_section = CONFIG_PANEL[
            CONFIG_PANEL.index('"id": "pool"'):CONFIG_PANEL.index('"id": "ev"')
        ]
        self.assertIn("OPT_POOL_WATER_TEMPERATURE_ENTITY", pool_section)
        self.assertIn("OPT_POOL_VOLUME_M3", pool_section)
        for retired in ("planning_enabled", "deferrable_confirmed"):
            self.assertNotIn(retired, pool_section)

    def test_retired_planning_switches_are_archived_and_ignored(self) -> None:
        setup = INIT[INIT.index("async def async_setup_entry") :]
        self.assertIn(
            "RETIRED_PLANNING_OPTIONS.intersection(migrated_options)", setup
        )
        for retired_key in (
            '"pool_planning_enabled"',
            '"pool_deferrable_confirmed"',
            '"pool_deadline"',
            '"pool_baseline_start"',
            '"boiler_planning_enabled"',
            '"boiler_deferrable_confirmed"',
            '"ev_planning_enabled"',
            '"ev_deferrable_confirmed"',
        ):
            self.assertIn(retired_key, CONSTANTS)
            self.assertNotIn(retired_key, COORDINATOR)

    def test_general_configuration_replans_after_reload(self) -> None:
        listener = INIT[INIT.index("async def _async_options_updated") :]
        self.assertIn("async_reload(entry.entry_id)", listener)
        self.assertIn("async_optimisation_push(force_plan=True)", listener)

    def test_device_cards_have_an_independent_save_button(self) -> None:
        self.assertIn('data-action="save-device"', CONFIG_PANEL_FRONTEND)
        self.assertIn("Save configuration", CONFIG_PANEL_FRONTEND)

    def test_device_card_save_replans_and_refreshes_readiness_immediately(self) -> None:
        save = CONFIG_PANEL[
            CONFIG_PANEL.index("async def async_apply_device_mapping") :
            CONFIG_PANEL.index("async def websocket_get_configuration")
        ]
        self.assertIn("async_optimisation_push(force_plan=True)", save)
        refresh = COORDINATOR[
            COORDINATOR.index("async def async_refresh_device_configuration") :
            COORDINATOR.index("async def async_report_device_mapping")
        ]
        self.assertIn("async_optimisation_push(force_plan=True)", refresh)
        self.assertIn('"panel": panel', CONFIG_PANEL)
        self.assertIn("if (result.panel) this._data = result.panel", CONFIG_PANEL_FRONTEND)
        self.assertIn("options_update_requires_reload()", INIT)
        live_update = COORDINATOR[
            COORDINATOR.index("def options_update_requires_reload") :
            COORDINATOR.index("def optimisation_input_gap_is_transient")
        ]
        self.assertIn("OPT_DEVICE_CONTROL_MAPPINGS", live_update)

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
        self.assertIn("options_update_requires_reload()", listener)
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
