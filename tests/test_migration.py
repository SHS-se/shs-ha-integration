"""Exercise the one-time import and current write contracts without runtime IO."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))
from migration import migrate_options
from configuration_schema import merge_options, validate_mapping_keys, OPTION_KEYS


class OptionMigrationTests(unittest.TestCase):
    def test_beta20_fixture_is_clean_and_stays_clean_after_save_reload(self):
        original = json.loads((Path(__file__).parent / "fixtures/options-beta20.json").read_text())
        before = deepcopy(original)
        migrated, changed = migrate_options(original)
        self.assertTrue(changed)
        self.assertEqual(migrated["ev_phase_count"], 1)
        self.assertEqual(migrated["ev_charge_efficiency"], 0.87)
        self.assertEqual(migrated["ev_phase_voltage"], 240)
        self.assertFalse(migrated["ev_control_enabled"])
        self.assertNotIn("_legacy_configuration_archive", migrated)
        self.assertNotIn("_configuration_schema_version", migrated)
        self.assertNotIn("supplier_import_price_entity", migrated)
        self.assertEqual(migrated["entities_ev_charging"], ["sensor.car_energy"])
        mapping = migrated["device_control_mappings"]["sensor.car_energy"]
        self.assertEqual(mapping, {
            "control_type": "variable_power", "control_entity_id": "number.car_current",
            "minimum_value": 5, "maximum_value": 16, "power": "sensor.car_power",
        })
        for _restart in range(3):
            saved = merge_options(migrated, {"ev_enabled": False})
            loaded = json.loads(json.dumps(saved))
            repeated, changed = migrate_options(loaded)
            self.assertFalse(changed)
            self.assertEqual(repeated, loaded)
            self.assertEqual(repeated["device_control_mappings"], migrated["device_control_mappings"])
        self.assertEqual(original, before)

    def test_current_values_win_including_zero_and_false(self):
        for value in (1, 3, 0, False):
            original = {"ev_phase_count": value, "_legacy_configuration_archive": {"ev_phase_count": 2}}
            migrated, _ = migrate_options(original)
            self.assertEqual(migrated["ev_phase_count"], value)
            self.assertNotIn("_legacy_configuration_archive", migrated)
        self.assertEqual(migrate_options({}), ({}, False))

    def test_all_mapping_aliases_import_once_then_disappear(self):
        options = {"device_control_mappings": {
            "heater": {
                "control_type": "setpoint", "_migrated_room_area_id": "office",
                "temperature_entity_id": "sensor.temperature", "actuator_entity_ids": ["climate.heater"],
                "power_entity_id": "sensor.power", "power_w": 750,
                "comfort_high_entity_id": "input_number.high", "override_entity_id": "input_text.old",
            },
            "car": {
                "control_type": "current_limit", "current_control_entity_id": "number.current",
                "connected_entity_id": "binary_sensor.connected", "soc_entity_id": "sensor.soc",
                "min_current_a": 5, "max_current_a": 16, "phase_count": 1, "charge_efficiency": 0.88,
            },
            "unrequested": {"control_type": "switch_schedule", "actuator_entity_ids": ["switch.other"]},
        }}
        migrated, _ = migrate_options(options)
        self.assertEqual(set(migrated["device_control_mappings"]), {"heater", "car", "unrequested"})
        heater = migrated["device_control_mappings"]["heater"]
        self.assertEqual(heater, {"control_type": "setpoint", "room_area_id": "office",
            "actuator_entity_ids": ["climate.heater"], "power": "sensor.power"})
        self.assertEqual(migrated["rooms"], {"office": {"temperature_entity_id": "sensor.temperature"}})
        self.assertEqual(migrated["device_control_mappings"]["car"], {
            "control_type": "variable_power", "control_entity_id": "number.current",
            "minimum_value": 5, "maximum_value": 16,
        })
        self.assertEqual(migrated["ev_connected_entity"], "binary_sensor.connected")
        self.assertEqual(migrated["ev_soc_entity"], "sensor.soc")
        self.assertEqual(migrated["ev_phase_count"], 1)
        self.assertEqual(migrated["ev_charge_efficiency"], 0.88)
        self.assertEqual(migrate_options(migrated), (migrated, False))

    def test_missing_limits_are_reported_without_guessing(self):
        migrated, _ = migrate_options({"device_control_mappings": {
            "car": {"control_type": "variable_power", "control_entity_id": "number.current"},
        }})
        self.assertNotIn("minimum_value", migrated["device_control_mappings"]["car"])
        self.assertIn("device_control_mappings.car.minimum_value", migrated["_migration_report"]["needs_attention"])

    def test_top_level_ev_aliases_move_only_to_identified_ev_mapping(self):
        migrated, _ = migrate_options({
            "ev_charge_current_entity": "number.current", "ev_min_current_a": 5, "ev_max_current_a": 16,
            "boiler_power_w": 2000, "pool_power_w": 3000, "ev_current_step_a": 1,
            "device_control_mappings": {
                "car": {"control_type": "variable_power", "control_entity_id": "number.current"},
                "other": {"control_type": "variable_power", "control_entity_id": "number.other"},
            },
        })
        self.assertEqual(migrated["device_control_mappings"]["car"]["minimum_value"], 5)
        self.assertNotIn("minimum_value", migrated["device_control_mappings"]["other"])
        for old in ("ev_charge_current_entity", "ev_min_current_a", "ev_max_current_a", "boiler_power_w", "pool_power_w", "ev_current_step_a"):
            self.assertNotIn(old, migrated)

    def test_conflicting_ev_observations_are_not_arbitrarily_selected(self):
        migrated, _ = migrate_options({"device_control_mappings": {
            key: {"control_type": "current_limit", "soc_entity_id": f"sensor.{key}"}
            for key in ("a", "b")
        }})
        self.assertNotIn("ev_soc_entity", migrated)
        self.assertIn("ev_soc_entity", migrated["_migration_report"]["needs_attention"])

    def test_discovery_evidence_and_unknown_options_cannot_retain_legacy_values(self):
        migrated, _ = migrate_options({"unknown_key": "old-value", "discovery_evidence": {
            "ev_battery_kwh": {"value": "old-value"}, "ev_soc_entity": {"source": "equipment"},
        }})
        self.assertNotIn("old-value", json.dumps(migrated))
        self.assertEqual(migrated["discovery_evidence"], {"ev_soc_entity": {"source": "equipment"}})

    def test_old_keys_are_rejected_on_save_instead_of_migrated(self):
        current, _ = migrate_options({"ev_phase_count": 1})
        for key in ("ev_min_current_a", "pool_power_w", "_legacy_configuration_archive", "_migration_report", "device_control_mappings"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                merge_options(current, {key: 5})
        for mapping in (
            {"control_type": "current_limit"},
            {"control_type": "variable_power", "min_current_a": 5},
            {"control_type": "setpoint", "_migrated_room_area_id": "room"},
        ):
            with self.assertRaises(ValueError):
                validate_mapping_keys(mapping)
        validate_mapping_keys({"control_type": "variable_power", "minimum_value": 5})


if __name__ == "__main__":
    unittest.main()

class BatteryCutoverTests(unittest.TestCase):
    def test_signed_settings_are_removed_without_deleting_energy_history_or_granting_permission(self):
        old = {'battery_enabled': True, 'battery_capacity_kwh': 20, 'battery_soc_entity': 'sensor.soc',
               'battery_control_enabled': True, 'battery_power_entity': 'number.inverter_adjustment',
               'battery_mode_entity': 'select.mode', 'battery_mode_charge': 'Old charge',
               'battery_power_unit': 'kW', 'battery_discharge_is_negative': True,
               'battery_control_override_entity': 'input_boolean.old_override',
               'entities_battery_charge': ['sensor.battery_energy']}
        result, changed = migrate_options(old)
        self.assertTrue(changed)
        for key in old:
            if key in ('battery_enabled', 'battery_capacity_kwh', 'battery_soc_entity', 'entities_battery_charge'):
                self.assertEqual(result[key], old[key])
            else:
                self.assertNotIn(key, result)
                with self.assertRaises(ValueError): merge_options(result, {key: old[key]})
        self.assertIn('commissioning', str(result['_migration_report']['needs_attention']))
        self.assertEqual(migrate_options(result), (result, False))

class PoolCutoverTests(unittest.TestCase):
    def options(self):
        return {'pool_enabled': True, 'pool_volume_m3': 55, 'pool_water_temperature_entity': 'sensor.filtered',
                'pool_control_enabled': True, 'pool_start_temperature_entity': 'number.start',
                'pool_stop_temperature_entity': 'number.stop', 'pool_permission_entity': 'switch.permission',
                'pool_temperature_minimum': 20, 'pool_temperature_maximum': 32,
                'entities_pool_heating': ['heater', 'pump', 'room'],
                'device_control_mappings': {
                    'heater': {'control_type': 'switch_schedule', 'actuator_entity_ids': ['switch.group'],
                               'companion_actuator_entity_ids': ['switch.pump'], 'control_enabled': True},
                    'pump': {'control_type': 'switch_schedule', 'actuator_entity_ids': ['switch.pump']},
                    'room': {'control_type': 'setpoint', 'room_area_id': 'bathroom', 'actuator_entity_ids': ['climate.floor']},
                }, 'rooms': {'bathroom': {'temperature_entity_id': 'sensor.floor'}}}

    def test_cutover_retires_old_typed_pool_routes_and_companions_preserving_meter_and_room_identity(self):
        original = self.options(); before = deepcopy(original)
        result, changed = migrate_options(original)
        self.assertTrue(changed)
        self.assertEqual(original, before)
        self.assertEqual(result['entities_pool_heating'], original['entities_pool_heating'])
        self.assertEqual(result['device_control_mappings'], {'room': original['device_control_mappings']['room']})
        self.assertEqual(result['rooms'], original['rooms'])
        for key in ('pool_enabled', 'pool_volume_m3', 'pool_water_temperature_entity'):
            self.assertEqual(result[key], original[key])
        for key in ('pool_control_enabled', 'pool_start_temperature_entity', 'pool_stop_temperature_entity',
                    'pool_permission_entity', 'pool_temperature_minimum', 'pool_temperature_maximum'):
            self.assertNotIn(key, result)
            with self.assertRaises(ValueError): merge_options(result, {key: original[key]})
        self.assertEqual(migrate_options(result), (result, False))

    def test_independent_shared_owner_requires_review_instead_of_silently_removing_room_mapping(self):
        options = self.options()
        options['device_control_mappings']['room']['actuator_entity_ids'].append('switch.pump')
        with self.assertRaisesRegex(ValueError, 'legacy_pool_overlap_review_required'):
            migrate_options(options)
        self.assertTrue(options['device_control_mappings']['heater']['control_enabled'])

    def test_journals_for_band_group_and_companion_cannot_be_discarded_or_reinterpreted(self):
        from migration import assert_legacy_handover_complete
        for key in ('pool', 'device:heater', 'device:pump'):
            options = self.options()
            journal = {'records': {key: {'options': options, 'originals': {}}}}
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'legacy_handover_required'):
                assert_legacy_handover_complete(journal, options)
        assert_legacy_handover_complete({'records': {}}, self.options())
