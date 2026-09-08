"""Current configuration writes and runtime views, without Home Assistant IO."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from configuration_schema import prepare_options, resolve_configuration, save_device
from migration import migrate_options

ENTITIES = {key: {'state': '20', 'attributes': {}} for key in
            ('sensor.old', 'sensor.new', 'climate.a', 'climate.b', 'number.current')}


def save(existing, key, mapping):
    return save_device(existing, key, mapping,
        {'control_type': 'setpoint', 'category': 'heating', 'name': key}, ENTITIES.get,
        entity_names={key: key for key in ENTITIES}, area_names={'office': 'Office'},
        entity_area_ids={'climate.a': 'office', 'climate.b': 'office'})


class CurrentConfigurationTests(unittest.TestCase):
    def test_patch_does_not_persist_defaults_or_mutate_input(self):
        existing = {'ev_enabled': False, 'device_control_mappings': {'inactive': {
            'control_type': 'variable_power', 'control_entity_id': 'number.current',
            'minimum_value': 5, 'maximum_value': 16}}}
        before = deepcopy(existing)
        saved = prepare_options(existing, {'ev_phase_count': 1}, ENTITIES.get)
        self.assertEqual(set(saved), set(existing) | {'ev_phase_count'})
        for _ in range(3):
            saved = json.loads(json.dumps(saved))
            runtime = resolve_configuration(saved, 59, 18)
            self.assertEqual(runtime['ev_phase_count'], 1)
            self.assertFalse(runtime['ev_enabled'])
            self.assertFalse(runtime['ev_control_enabled'])
            self.assertIn('inactive', runtime['device_control_mappings'])
            runtime['device_control_mappings']['inactive']['maximum_value'] = 99
            self.assertEqual(saved['device_control_mappings']['inactive']['maximum_value'], 16)
        self.assertEqual(existing, before)

    def test_invalid_public_writes_are_rejected(self):
        for patch in ({'ev_phase_count': 1.5}, {'ev_phase_count': True},
                      {'ev_charge_efficiency': float('nan')}, {'battery_min_soc': 1},
                      {'ev_soc_entity': 'switch.absent'}, {'ev_soc_entity': ['sensor.old']},
                      {'ev_control_enabled': True}, {'battery_control_enabled': True},
                      {'rooms': {}}, {'automatic_setup': False}, {'forecast_resolution_minutes': 60}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                prepare_options({}, patch, ENTITIES.get)

    def test_optional_unset_is_removed(self):
        self.assertEqual(prepare_options({'ev_soc_entity': 'sensor.old'},
                         {'ev_soc_entity': None}, ENTITIES.get), {})

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
