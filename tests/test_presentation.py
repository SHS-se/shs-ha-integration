"""Shared status, stable names, source sharing and device permissions."""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from presentation import operational_status, timeline, complete_device_views, device_name, system_fields, device_readiness
from configuration_schema import configuration_defaults, shared_devices
from planning import unplanned_services


class PresentationTests(unittest.TestCase):
    def test_readiness_counts_only_included_equipment_including_home_battery(self):
        devices = [
            {"name": "Fridge", "included": False, "mapping_status": "not_configured"},
            {"name": "Battery", "included": True, "mapping_status": "ready"},
            {"name": "Heater", "included": True, "mapping_status": "invalid"},
            {"name": "Excluded heater", "included": False, "mapping_status": "ready"},
        ]
        self.assertEqual(device_readiness(devices), {
            "requested_devices": 2, "ready_devices": 1, "device_mapping_gaps": ["Heater"],
        })

    def setUp(self):
        self.plan = json.loads((Path(__file__).parent / 'fixtures/schema-7-device-plan.json').read_text())['plan']
        self.now = datetime.fromisoformat(self.plan['issued_at']) + timedelta(minutes=6)
        self.options = {**configuration_defaults(0, 0), 'planning_mode': 'live', 'battery_enabled': False, 'pool_enabled': False, 'ev_enabled': False}
        self.device = {'key': 'sensor.laundry_energy', 'statistic_id': 'sensor.laundry_energy', 'name': 'Old name Energy',
            'category': 'heating', 'planning_role': 'controllable', 'planning_choice_at': None, 'control_type': 'setpoint',
            'mapping': {'control_type': 'setpoint', 'actuator_entity_ids': ['climate.laundry'], 'room_area_id': 'laundry'},
            'mapping_status': 'ready', 'mapping_summary': {}, 'fields': []}
        self.choices = {'home': {}, 'refreshed_at': self.now.isoformat()}

    def view(self, devices=None, **kwargs):
        return complete_device_views(devices or [self.device], self.options, self.choices,
            operational_status(self.plan, 'live', [], self.now), self.plan, {},
            {'sensor.laundry_energy': 'Laundry floor Energy'}, {'laundry': 'Laundry'}, self.now, **kwargs)

    def test_invalid_cached_ready_plan_never_exposes_timeline(self):
        self.plan['plans']['priority']['slots'][0]['device_commands'] = {}
        status = operational_status(self.plan, 'live', [], self.now)
        self.assertEqual(status['state'], 'invalid')
        self.assertFalse(status['actionable'])
        self.assertEqual(timeline(self.plan, status)['slots'], [])

    def test_expired_disabled_and_missing_inputs_are_not_ready(self):
        for mode, missing, now, expected in (
            ('disabled', [], self.now, 'disabled'), ('live', ['source'], self.now, 'not_configured'),
            ('live', [], datetime.fromisoformat(self.plan['valid_until']), 'expired')):
            with self.subTest(expected=expected):
                status = operational_status(self.plan, mode, missing, now)
                self.assertEqual(status['state'], expected)
                self.assertEqual(timeline(self.plan, status)['slots'], [])
        self.assertEqual(operational_status(self.plan, 'live', [], self.now)['state'], 'ready')

    def test_live_names_change_without_any_identity_or_mapping_changes(self):
        before = deepcopy(self.device)
        view = self.view()[0]
        self.assertEqual(view['name'], 'Laundry floor')
        self.assertEqual(view['key'], self.device['key'])
        self.assertEqual(view['mapping'], self.device['mapping'])
        self.assertEqual(self.device, before)
        self.assertEqual(device_name('Energy'), 'Energy')
        self.assertEqual(device_name('Heater energy sensor'), 'Heater')

    def test_empty_or_excluded_equipment_has_no_ghost_rows(self):
        self.assertEqual(len(self.view()), 1)
        self.options['pool_enabled'] = True  # default alone is not evidence
        self.assertEqual(len(self.view()), 1)
        views = self.view(configured_keys=['pool_enabled'])
        self.assertEqual([d['key'] for d in views], ['sensor.laundry_energy', '$pool'])

    def test_pool_category_room_heater_survives_a_home_without_pool(self):
        self.device['category'] = 'pool_heating'
        self.assertEqual(self.view()[0]['key'], self.device['key'])

    def test_exclusion_keeps_setup_and_permission_but_clears_setup_warning(self):
        self.device.update(planning_role='base_load', planning_choice_at=self.now.isoformat(), mapping_error='missing bounds')
        self.device['mapping']['control_enabled'] = True
        view = self.view()[0]
        self.assertTrue(view['permission']['enabled']) # user can still turn it off
        self.assertIn('Include', view['permission']['reason'])
        self.assertIsNone(view['mapping_error'])
        self.assertEqual(view['choice_label'], 'Excluded')
        self.assertTrue(view['mapping']['control_enabled'])

    def test_battery_shares_the_same_two_choice_contract_and_requires_setup(self):
        self.options.update(battery_enabled=True, battery_soc_entity='sensor.battery')
        self.choices['home'] = {'battery': {'included': True, 'choice_at': self.now.isoformat()}}
        self.plan['capabilities']['battery'] = True
        # Supply known actionable status: this test isolates permission setup, not plan validation.
        views = complete_device_views([], self.options, self.choices, {'actionable': True}, self.plan, {}, {}, {}, self.now)
        battery = views[0]
        self.assertEqual(battery['choice_label'], 'Included')
        self.assertFalse(battery['permission']['enabled'])
        self.assertIn('required', battery['permission']['reason'])
        fields = [f['key'] for f in system_fields('battery')]
        self.assertEqual(len(fields), len(set(fields)))
        self.assertNotIn('battery_control_enabled', fields)

    def test_sharing_exclusion_preserves_declared_inventory(self):
        inventory = [deepcopy(self.device), {**self.device, 'key': 'other'}]
        self.options['excluded_device_readings'] = [self.device['key']]
        self.assertEqual([d['key'] for d in shared_devices(inventory, self.options)], ['other'])
        self.assertEqual(len(inventory), 2)

    def test_deliberate_exclusion_suppresses_unplanned_warning_but_unreviewed_does_not(self):
        options = {'pool_enabled': True, 'pool_water_temperature_entity': 'sensor.pool'}
        meter = {'category': 'pool_heating', 'name': 'Pool', 'planning_role': 'base_load'}
        self.assertTrue(unplanned_services(options, set(), {'pool': meter}))
        meter['planning_choice_at'] = self.now.isoformat()
        self.assertEqual(unplanned_services(options, set(), {'pool': meter}), [])
        meter['planning_role'] = 'controllable'
        self.assertTrue(unplanned_services(options, set(), {'pool': meter}))

    def test_no_plan_and_invalid_plan_have_no_timeline_or_capabilities(self):
        state = operational_status(None, 'live', [], self.now)
        self.assertEqual(timeline(None, state), {'slots': [], 'capabilities': {}, 'reason': 'Waiting for a plan'})

    def test_excluded_pool_is_not_confused_with_a_room_heater_in_pool_category(self):
        options = {'pool_enabled': True, 'pool_water_temperature_entity': 'sensor.pool'}
        meters = {'pool': {'category': 'pool_heating', 'planning_role': 'base_load', 'planning_choice_at': self.now.isoformat()},
                  'floor': {'category': 'pool_heating', 'planning_role': 'controllable', 'control_type': 'setpoint'}}
        self.assertEqual(unplanned_services(options, set(), meters), [])

    def test_real_exchange_preserves_choice_timestamps_and_clears_excluded_warnings(self):
        import ast
        from types import SimpleNamespace
        from typing import Any
        from device_controls import planning_path
        tree = ast.parse((Path(__file__).parents[1] / 'custom_components/shs_energy/coordinator.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ShsStatusCoordinator')
        function = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_record_device_exchange')
        module = ast.Module(body=[function], type_ignores=[])
        namespace = {'Any': Any, 'datetime': datetime, 'timezone': timezone, 'ShsApiError': ValueError,
                     'resolved_options': lambda hass, options: options, 'planning_path': planning_path, 'unplanned_services': unplanned_services}
        exec(compile(module, 'coordinator.py', 'exec'), namespace)
        calls = {}
        fake = SimpleNamespace(hass=None, entry=SimpleNamespace(options=self.options), optimisation_degraded_devices=[{'key': 'pool'}],
            _sync_device_control_issue=lambda *args: None, _sync_degraded_device_issue=lambda: None,
            _sync_unplanned_service_issue=lambda: None,
            _sync_battery_control_issue=lambda options, **kw: calls.update(battery=kw['included']),
            _sync_pool_control_issue=lambda options, **kw: calls.update(pool=kw['included']))
        device = {'key': 'pool', 'statistic_id': 'pool', 'name': 'Pool', 'category': 'pool_heating', 'load_type': 'duty_cycle',
                  'planning_role': 'base_load', 'control_type': None, 'planning_choice_at': self.now.isoformat()}
        stored = {}
        namespace['_record_device_exchange'](fake, stored, [{'key': 'pool', 'active_power_w': 1000, 'profile_sample_count': 10, 'inference': {}}],
            {'device_configuration': [device], 'home_configuration': {'battery': {'included': False, 'choice_at': self.now.isoformat()}}})
        self.assertEqual(stored['optimisation_device_configuration']['pool']['planning_choice_at'], self.now.isoformat())
        self.assertEqual(fake.optimisation_degraded_devices, [])
        self.assertEqual(calls, {'battery': False, 'pool': False})
