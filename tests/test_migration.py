"""Execute startup option conversion without the integration runtime."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from const import CONFIGURATION_SCHEMA_VERSION, RETIRED_PLANNING_OPTIONS
from migration import mapped_entity_ids, migrate_options


class OptionMigrationTests(unittest.TestCase):
    def test_beta20_fixture_recovers_electrical_settings_across_restarts(self):
        original = json.loads(
            (Path(__file__).parent / "fixtures" / "options-beta20.json").read_text()
        )
        before = deepcopy(original)
        migrated, changed = migrate_options(original)
        expected = deepcopy(original)
        expected["ev_phase_count"] = 1
        expected["ev_charge_efficiency"] = 0.87
        expected["_legacy_configuration_archive"] = {
            "pool_planning_enabled": False,
            "supplier_import_price_entity": expected.pop("supplier_import_price_entity"),
        }
        self.assertTrue(changed)
        self.assertEqual(migrated, expected)
        self.assertEqual(original, before)
        for _restart in range(3):
            # Persist and reload exactly as JSON options, without injecting defaults.
            migrated, changed = migrate_options(json.loads(json.dumps(migrated)))
            self.assertFalse(changed)
            self.assertEqual(migrated, expected)
        migrated["device_control_mappings"]["sensor.car_energy"]["maximum_value"] = 10
        self.assertEqual(original, before)

    def test_current_values_win_and_later_edits_survive_restart(self):
        for phase_count, efficiency in ((1, 0.85), (3, 0.95), (0, False)):
            with self.subTest(phase_count=phase_count, efficiency=efficiency):
                options = {
                    "ev_phase_count": phase_count,
                    "ev_charge_efficiency": efficiency,
                    "_legacy_configuration_archive": {
                        "ev_phase_count": 2, "ev_charge_efficiency": 0.7,
                    },
                }
                migrated, _ = migrate_options(options)
                self.assertEqual(migrated["ev_phase_count"], phase_count)
                self.assertEqual(migrated["ev_charge_efficiency"], efficiency)
                self.assertNotIn("_legacy_configuration_archive", migrated)
                migrated.update(ev_phase_count=1, ev_charge_efficiency=0.9)
                restarted, changed = migrate_options(migrated)
                self.assertFalse(changed)
                self.assertEqual(restarted, migrated)

    def test_missing_values_do_not_become_persisted_defaults(self):
        migrated, _ = migrate_options({})
        self.assertEqual(migrated, {"_configuration_schema_version": CONFIGURATION_SCHEMA_VERSION})
        self.assertNotIn("ev_phase_count", RETIRED_PLANNING_OPTIONS)
        self.assertNotIn("ev_charge_efficiency", RETIRED_PLANNING_OPTIONS)

    def test_mapping_conversion_uses_supplied_registry_facts(self):
        options = {"device_control_mappings": {
            "sensor.heater_energy": {
                "control_type": "setpoint",
                "actuator_entity_ids": ["climate.heater"],
                "temperature_entity_id": "sensor.room_temperature",
                "power_w": 750,
            },
            "sensor.car_energy": {
                "control_type": "current_limit",
                "current_control_entity_id": "number.car_current",
                "connected_entity_id": "binary_sensor.car_connected",
            },
        }}
        facts = {
            "entity_area_ids": {"climate.heater": "room"},
            "entity_limits": {"number.car_current": (5, 16)},
        }
        before = deepcopy(options)
        self.assertEqual(mapped_entity_ids(options), {
            "climate.heater", "sensor.room_temperature", "number.car_current",
            "binary_sensor.car_connected",
        })
        migrated, changed = migrate_options(options, **facts)
        self.assertTrue(changed)
        heater = migrated["device_control_mappings"]["sensor.heater_energy"]
        self.assertEqual(heater["_migrated_room_area_id"], "room")
        self.assertEqual(heater["power"], 750)
        car = migrated["device_control_mappings"]["sensor.car_energy"]
        self.assertEqual(car["control_type"], "variable_power")
        self.assertEqual((car["minimum_value"], car["maximum_value"]), (5, 16))
        self.assertEqual(migrated["ev_connected_entity"], "binary_sensor.car_connected")
        self.assertEqual(migrate_options(migrated, **facts), (migrated, False))
        self.assertEqual(options, before)

    def test_retired_settings_still_leave_active_options_in_phase_zero(self):
        migrated, _ = migrate_options({
            "pool_planning_enabled": False,
            "supplier_export_price_entity": "sensor.old_price",
        })
        self.assertNotIn("pool_planning_enabled", migrated)
        self.assertNotIn("supplier_export_price_entity", migrated)
        self.assertEqual(migrated["_legacy_configuration_archive"], {
            "pool_planning_enabled": False,
            "supplier_export_price_entity": "sensor.old_price",
        })


if __name__ == "__main__":
    unittest.main()
