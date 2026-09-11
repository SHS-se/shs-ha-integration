"""Current configuration writes and runtime views, without Home Assistant IO."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from configuration_schema import prepare_options, resolve_configuration, save_device
from migration import migrate_options
from configuration_fields import _control_fields
from device_commands import execution_setup_errors

ENTITIES = {key: {'state': '20', 'attributes': {}} for key in
            ('sensor.old', 'sensor.new', 'climate.a', 'climate.b', 'number.current')}


def save(existing, key, mapping):
    return save_device(existing, key, mapping,
        {'control_type': 'setpoint', 'category': 'heating', 'name': key}, ENTITIES.get,
        entity_names={key: key for key in ENTITIES}, area_names={'office': 'Office'},
        entity_area_ids={'climate.a': 'office', 'climate.b': 'office'})


class CurrentConfigurationTests(unittest.TestCase):
    def test_control_cards_use_one_actuator_and_a_consistent_field_order(self):
        for kind in ("setpoint", "switch_schedule", "permit_inhibit", "variable_power"):
            fields = _control_fields({"control_type": kind, "category": "heating"})
            self.assertEqual(fields[0]["key"], "control_entity_id" if kind == "variable_power" else "actuator_entity_ids")
            self.assertEqual(fields[1]["key"], "power")
            self.assertNotIn("companion_actuator_entity_ids", [f["key"] for f in fields])
            if kind != "variable_power":
                self.assertEqual(fields[0]["max_items"], 1)

    def test_saving_or_executing_multiple_actuators_is_rejected(self):
        for kind in ("setpoint", "switch_schedule", "permit_inhibit"):
            mapping = {"control_type": kind, "actuator_entity_ids": ["climate.a", "climate.b"],
                       "temperature_entity_id": "sensor.old", "max_inhibit_slots": 2}
            # Only include public fields for the selected control type.
            keys = {f["key"] for f in _control_fields({"control_type": kind, "category": "heating"})}
            mapping = {k: v for k, v in mapping.items() if k in keys or k == "control_type"}
            with self.assertRaisesRegex(ValueError, "must be one entity"):
                save_device({}, "heater", mapping, {"control_type": kind, "category": "heating", "name": "Heater"},
                            ENTITIES.get, entity_names={}, area_names={}, entity_area_ids={})
            self.assertIn("choose exactly one control entity", execution_setup_errors(mapping))

    def test_companion_actuators_are_not_public_configuration(self):
        mapping = {"control_type": "setpoint", "actuator_entity_ids": ["climate.a"],
                   "temperature_entity_id": "sensor.old", "companion_actuator_entity_ids": ["climate.b"]}
        with self.assertRaisesRegex(ValueError, "unknown device fields"):
            save({}, "heater", mapping)

    def test_pool_switch_can_be_reentered_after_version_six_migration(self):
        options, _ = migrate_options({"entities_pool_heating": ["sensor.pool"],
                                     "device_control_mappings": {}}, source_version=6)
        mapping = {"control_type": "switch_schedule",
                   "actuator_entity_ids": ["switch.esphome_pool_pump_switch"]}
        entities = {"switch.esphome_pool_pump_switch": {"state": "off", "attributes": {}}}
        saved = save_device(options, "sensor.pool", mapping,
                            {"control_type": "switch_schedule", "category": "pool_heating", "name": "Pool pump"},
                            entities.get, entity_names={key: key for key in entities},
                            area_names={}, entity_area_ids={})
        self.assertEqual(saved["device_control_mappings"]["sensor.pool"], mapping)
        self.assertEqual(saved["device_modes"]["$pool"], "monitoring")

    def test_patch_does_not_persist_defaults_or_mutate_input(self):
        existing = {'ev_enabled': False, 'device_control_mappings': {'inactive': {
            'control_type': 'variable_power', 'control_entity_id': 'number.current',
            'minimum_value': 5, 'maximum_value': 16}}}
        before = deepcopy(existing)
        saved = prepare_options(existing, {'ev_kwh_per_km': 0.18}, ENTITIES.get)
        self.assertEqual(set(saved), set(existing) | {'ev_kwh_per_km'})
        for _ in range(3):
            saved = json.loads(json.dumps(saved))
            runtime = resolve_configuration(saved, 59, 18)
            self.assertEqual(runtime['ev_kwh_per_km'], 0.18)
            self.assertFalse(runtime['ev_enabled'])
            self.assertFalse(runtime['ev_control_enabled'])
            self.assertIn('inactive', runtime['device_control_mappings'])
            runtime['device_control_mappings']['inactive']['maximum_value'] = 99
            self.assertEqual(saved['device_control_mappings']['inactive']['maximum_value'], 16)
        self.assertEqual(existing, before)

    def test_invalid_public_writes_are_rejected(self):
        for patch in ({'ev_phase_count': 3}, {'ev_phase_voltage': 230},
                      {'ev_charge_efficiency': float('nan')}, {'battery_min_soc': 1},
                      {'ev_soc_entity': 'switch.absent'}, {'ev_soc_entity': ['sensor.old']},
                      {'ev_control_enabled': True}, {'battery_control_enabled': True},
                      {'rooms': {}}, {'automatic_setup': False}, {'forecast_resolution_minutes': 60}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                prepare_options({}, patch, ENTITIES.get)

    def test_removed_mapping_power_stays_explicitly_empty(self):
        mapping = {'control_type': 'setpoint', 'actuator_entity_ids': ['climate.a'],
                   'temperature_entity_id': 'sensor.old', 'power': None}
        saved = save({}, 'heater', mapping)
        self.assertIsNone(saved['device_control_mappings']['heater']['power'])
        self.assertIsNone(resolve_configuration(saved)['device_control_mappings']['heater']['power'])

    def test_removed_default_stays_empty_after_reload(self):
        saved = prepare_options({'battery_charge_efficiency': .95},
                                {'battery_charge_efficiency': None}, ENTITIES.get)
        for _ in range(3):
            saved = json.loads(json.dumps(saved))
            self.assertIsNone(resolve_configuration(saved)['battery_charge_efficiency'])
        saved = prepare_options(saved, {'battery_charge_efficiency': .92}, ENTITIES.get)
        self.assertEqual(resolve_configuration(saved)['battery_charge_efficiency'], .92)

    def test_retired_battery_fields_cannot_be_saved_or_restored(self):
        retired = {'battery_max_soc': .8, 'battery_measurement_charge_positive': False,
                   'battery_discharge_is_negative': False, 'battery_power_unit': 'W',
                   'battery_power_entity': 'number.signed'}
        for key, value in retired.items():
            with self.assertRaises(ValueError):
                prepare_options({}, {key: value}, ENTITIES.get)
        saved, _ = migrate_options({**retired, 'battery_control_enabled': True,
            'battery_mode_charge': 'binary_sensor.sigen_plant_battery_charging',
            'battery_mode_discharge': 'binary_sensor.sigen_plant_battery_discharging'}, source_version=9)
        current = resolve_configuration(saved)
        self.assertFalse(current['battery_control_enabled'])
        self.assertFalse(set(retired) & set(current))
        self.assertEqual(current['battery_charging_entity'], 'binary_sensor.sigen_plant_battery_charging')
        self.assertEqual(current['battery_discharging_entity'], 'binary_sensor.sigen_plant_battery_discharging')
        self.assertNotIn('battery_mode_charge', current)
        self.assertNotIn('battery_mode_discharge', current)

    def test_modes_must_be_options_of_the_selected_entity(self):
        entities = {'select.ems': {'state': 'Standby', 'attributes': {'options': ['Standby', 'Maximum Self Consumption']}}}
        saved = prepare_options({}, {'battery_mode_entity': 'select.ems',
                                      'battery_mode_baseline': 'Maximum Self Consumption'}, entities.get)
        self.assertEqual(saved['battery_mode_baseline'], 'Maximum Self Consumption')
        with self.assertRaisesRegex(ValueError, 'choose an option'):
            prepare_options(saved, {'battery_mode_baseline': 'Maximum Self Consumptoin'}, entities.get)

    def test_optional_unset_is_removed(self):
        self.assertEqual(prepare_options({'ev_soc_entity': 'sensor.old'},
                         {'ev_soc_entity': None}, ENTITIES.get), {'ev_soc_entity': None})

    def test_shared_room_source_survives_save_reload_and_device_removal(self):
        old = {'device_control_mappings': {key: {'control_type': 'setpoint',
            'actuator_entity_ids': ['climate.' + key], 'temperature_entity_id': 'sensor.old',
            'room_area_id': 'office'} for key in ('a', 'b')}}
        current, _ = migrate_options(old)
        self.assertEqual(current['rooms'], {'office': {'temperature_entity_id': 'sensor.old'}})
        for mapping in current['device_control_mappings'].values():
            self.assertNotIn('temperature_entity_id', mapping)
        draft = resolve_configuration(current)['device_control_mappings']['a']
        draft['temperature_entity_id'] = 'sensor.new'
        saved = save(current, 'a', draft)
        restored = resolve_configuration(json.loads(json.dumps(saved)))
        for key in ('a', 'b'):
            self.assertEqual(restored['device_control_mappings'][key]['temperature_entity_id'], 'sensor.new')
            self.assertEqual(restored['device_control_mappings'][key]['room_area_id'], 'office')
        self.assertEqual(current['rooms']['office']['temperature_entity_id'], 'sensor.old')
        removed = save(saved, 'a', None)
        self.assertEqual(resolve_configuration(removed)['device_control_mappings']['b']['temperature_entity_id'], 'sensor.new')
        self.assertEqual(migrate_options(saved), (saved, False))

    def test_invalid_device_save_is_atomic(self):
        current = {'rooms': {'office': {'temperature_entity_id': 'sensor.old'}}}
        before = deepcopy(current)
        for mapping in ({'control_type': 'setpoint'},
                        {'control_type': 'setpoint', 'temperature_entity_id': 'sensor.new',
                         'actuator_entity_ids': ['number.current']},
                        {'control_type': 'setpoint', 'power_w': 1000}):
            with self.assertRaises(ValueError):
                save(current, 'a', mapping)
            self.assertEqual(current, before)

    def test_conflicting_room_migration_does_not_pick_or_mutate(self):
        current = {'device_control_mappings': {key: {'control_type': 'setpoint',
            'room_area_id': 'office', 'temperature_entity_id': 'sensor.' + key,
            'actuator_entity_ids': ['climate.a']} for key in ('old', 'new')}}
        before = deepcopy(current)
        with self.assertRaisesRegex(ValueError, 'conflicting temperature sources'):
            migrate_options(current)
        self.assertEqual(current, before)
