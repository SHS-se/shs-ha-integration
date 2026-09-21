"""Shared status, stable names, source sharing and device permissions."""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from presentation import controller_explanation, operational_status, timeline, complete_device_views, device_name, system_fields, device_readiness
from configuration_schema import configuration_defaults, shared_devices
from planning import unplanned_services


class PresentationTests(unittest.TestCase):
    def test_no_forecast_or_old_website_refresh_does_not_block_permission_correction(self):
        from test_device_controls import _battery
        from operating_modes import execution_mode_options
        options={**self.options, **_battery(), 'device_modes':{'$battery':'control_verification'}}
        choices={'home':{'battery':{'included':True}},'refreshed_at':(self.now-timedelta(days=3)).isoformat()}
        status={'actionable':False,'reason':'No forecast schedule is available'}
        def view():
            return complete_device_views([],options,choices,status,None,{}, {},{},self.now)
        options.pop('grid_power_entity')
        self.assertIn('Signed grid power',view()[0]['permission']['reason'])
        with self.assertRaisesRegex(ValueError,'Signed grid power'):
            execution_mode_options(options,view(),'$battery','controlling')
        options['grid_power_entity']='sensor.grid'
        self.assertIsNone(view()[0]['permission']['reason'])
        options=execution_mode_options(options,view(),'$battery','controlling')
        self.assertEqual(options['device_modes']['$battery'],'controlling')
        self.assertEqual(view()[0]['planning_support']['reason'],status['reason'])
        self.assertFalse(view()[0]['execution_eligibility']['eligible'])

    def test_mode_change_retains_actionable_schedule(self):
        from operating_modes import operating_mode_identity
        plan = deepcopy(self.plan)
        plan['operating_scope'] = {'modes': operating_mode_identity(self.options), 'device_owners': {}}
        options = {**self.options, 'device_modes': {'$battery': 'controlling'}}
        status = operational_status(plan, 'live', [], self.now, options=options)
        self.assertEqual(status['state'], 'ready')
        self.assertIn('retained schedule', status['reason'])
        self.assertTrue(status['actionable'])
        self.assertEqual(status['plan_id'], plan['plan_id'])
        self.assertTrue(timeline(plan, status)['slots'])

    def test_timeline_preserves_battery_intent_separately_from_forecast(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/schema-8-battery-plan.json').read_text())
        plan = fixture['plan']
        result = timeline(plan, {'state': 'ready'})
        source = plan['plans']['priority']['slots'][0]
        self.assertEqual(result['slots'][0]['battery_command'], source['battery_command'])
        result['slots'][0]['battery_command']['charge_limit_w'] = -1
        self.assertNotEqual(source['battery_command']['charge_limit_w'], -1)

    def test_timeline_includes_prices_and_expected_house_demand_inputs(self):
        plan = json.loads((Path(__file__).parent / 'fixtures/schema-8-battery-plan.json').read_text())['plan']
        plan['plans']['priority']['slots'][0].update(shadow_import_sek_per_kwh=1.25, shadow_export_sek_per_kwh=0.4)
        source = plan['plans']['priority']['slots'][0]
        slot = timeline(plan, {'state': 'ready'})['slots'][0]
        self.assertEqual((slot['shadow_import_sek_per_kwh'], slot['shadow_export_sek_per_kwh']), (1.25, 0.4))
        self.assertEqual((slot['load_w'], slot['duration_hours']), (source['load_w'], source['duration_hours']))

    def test_timeline_includes_each_slots_read_only_command_preview(self):
        preview = lambda slot, **context: {'device:heater': {'fields': [{'label': 'Target', 'value': slot['start']}]}}
        result = timeline(self.plan, {'state': 'ready'}, command_preview=preview)
        for slot in result['slots']:
            self.assertEqual(slot['command_previews']['device:heater']['fields'][0]['value'], slot['start'])
        self.assertEqual(timeline(self.plan, {'state': 'invalid', 'reason': 'bad'},
                                 command_preview=lambda slot, **context: self.fail('invalid plan previewed'))['slots'], [])

    def test_readiness_counts_only_included_equipment_including_home_battery(self):
        devices = [
            {"name": "Fridge", "included": False, "mapping_status": "not_configured"},
            {"name": "Battery", "included": True, "mapping_status": "ready"},
            {"name": "Heater", "included": True, "mapping_status": "invalid"},
            {"name": "Excluded heater", "included": False, "mapping_status": "ready"},
        ]
        readiness = device_readiness(devices)
        self.assertEqual({key: readiness[key] for key in ("requested_devices", "ready_devices", "device_mapping_gaps")}, {
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

    def test_battery_cannot_show_ready_with_missing_shared_measurements(self):
        from test_device_controls import _battery
        self.options.update(_battery())
        self.choices['home']['battery'] = {'included': True, 'choice_at': self.now.isoformat()}
        self.options['device_modes'] = {'$battery': 'controlling'}
        missing = ('house_consumption_power_entity', 'solar_production_power_entity', 'grid_power_entity')
        for key in missing:
            self.options.pop(key)
        battery = next(d for d in self.view(configured_keys=['battery_enabled']) if d.get('system') == 'battery')
        self.assertEqual(battery['mapping_status'], 'not_configured')
        self.assertFalse(battery['execution_eligibility']['eligible'])
        for label in ('Instantaneous house consumption', 'Instantaneous solar production', 'Signed grid power'):
            self.assertIn(label, battery['permission']['reason'])
        for key in missing:
            self.options[key] = 'sensor.' + key
        battery = next(d for d in self.view(configured_keys=['battery_enabled']) if d.get('system') == 'battery')
        self.assertEqual(battery['mapping_status'], 'ready')

    def test_room_zones_ignore_the_planned_battery_and_other_system_entries(self):
        from test_device_controls import _battery
        from device_controls import room_thermal_zones
        self.options.update(_battery(), ev_enabled=True, ev_connected_entity='binary_sensor.car')
        self.choices['home']['battery'] = {'included': True, 'choice_at': self.now.isoformat()}
        views = self.view(configured_keys=['battery_enabled'])
        self.assertEqual({view['key'] for view in views}, {'sensor.laundry_energy', '$battery', '$ev'})
        self.assertEqual([zone['key'] for zone in room_thermal_zones(views)], ['sensor.laundry_energy'])

    def test_battery_model_properties_are_separate_from_controls(self):
        self.options['battery_enabled'] = True
        self.choices['home']['battery'] = {'included': True}
        battery = next(d for d in self.view(configured_keys=['battery_enabled']) if d.get('system') == 'battery')
        planning = {f['key'] for f in battery['planning_fields']}
        controls = {f['key'] for f in battery['system_fields']} - planning
        self.assertIn('battery_capacity_kwh', planning)
        self.assertIn('battery_charge_efficiency', planning)
        self.assertIn('battery_soc_entity', controls)
        self.assertIn('battery_charge_max_w', controls)
        self.assertIn('battery_min_soc', controls)
        self.assertIn('battery_charge_limit_entity', controls)
        self.assertIn('battery_discharging_entity', controls)
        self.assertNotIn('battery_max_soc', controls)

    def test_pool_explanation_shares_simulated_temperature_and_switch_decision(self):
        status = {'water_temperature_c': 31.5, 'stop_temperature_c': 32,
                  'decision_reason': 'The pool heater is allowed to run as planned.'}
        text = controller_explanation('pool', 'control_verification', status, {'pool_w': 2000})['explanation']
        self.assertIn('not being changed', text)
        self.assertIn('31.5 °C', text)
        self.assertIn('32.0 °C', text)
        self.assertIn('would be allowed to run', text)

    def test_pool_water_sensor_is_labelled_as_water_in_controls(self):
        self.options.update(pool_enabled=True, pool_water_temperature_entity='sensor.water')
        heater = deepcopy(self.device)
        heater.update(category='pool_heating', fields=[{'key': 'temperature_entity_id', 'label': 'Room temperature'}])
        heater['mapping']['temperature_entity_id'] = 'sensor.water'
        pump = {**deepcopy(heater), 'key': 'sensor.pump', 'statistic_id': 'sensor.pump',
                'name': 'Pool pump', 'control_type': 'switch_schedule',
                'mapping': {'control_type': 'switch_schedule'}, 'fields': []}
        views = self.view([heater, pump])
        view = views[0]
        pool = next(d for d in views if d.get('system') == 'pool')
        self.assertNotIn('pool_water_temperature_entity', {f['key'] for f in pool['planning_fields']})
        self.assertIn('pool_water_temperature_entity', {f['key'] for f in pool['system_fields']})
        self.assertEqual(pool['key'], heater['key'])
        self.assertEqual(view['planning_system'], 'pool')
        self.assertNotIn('temperature_entity_id', {f['key'] for f in view['fields']})
        self.assertEqual(heater['fields'][0]['label'], 'Room temperature')

    def test_invalid_cached_ready_plan_never_exposes_timeline(self):
        self.plan['plans']['priority']['slots'][0]['device_commands'] = {}
        status = operational_status(self.plan, 'live', [], self.now)
        self.assertEqual(status['state'], 'invalid')
        self.assertFalse(status['actionable'])
        self.assertEqual(timeline(self.plan, status)['slots'], [])

    def test_expired_disabled_and_missing_inputs_are_not_ready(self):
        for mode, missing, now, expected in (
            ('disabled', [], self.now, 'disabled'),
            ('live', [], datetime.fromisoformat(self.plan['valid_until']), 'expired')):
            with self.subTest(expected=expected):
                status = operational_status(self.plan, mode, missing, now)
                self.assertEqual(status['state'], expected)
                self.assertEqual(timeline(self.plan, status)['slots'], [])
        self.assertEqual(operational_status(self.plan, 'live', [], self.now)['state'], 'ready')
        self.assertTrue(operational_status(self.plan, 'live', ['source offline'], self.now)['actionable'])
        self.assertEqual(operational_status(None, 'live', ['source'], self.now)['state'], 'not_configured')

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
        self.options['device_modes'] = {self.device['key']: 'controlling'}
        view = self.view()[0]
        self.assertTrue(view['permission']['enabled']) # user can still turn it off
        self.assertIn('Planned', view['permission']['reason'])
        self.assertIsNone(view['mapping_error'])
        self.assertFalse(view['planned'])
        self.assertNotIn('choice_label', view)
        self.assertEqual(view['mode'], 'controlling')

    def test_battery_shares_the_same_two_choice_contract_and_requires_setup(self):
        self.options.update(battery_enabled=True, battery_soc_entity='sensor.battery')
        self.choices['home'] = {'battery': {'included': True, 'choice_at': self.now.isoformat()}}
        self.plan['capabilities']['battery'] = True
        # Supply known actionable status: this test isolates permission setup, not plan validation.
        views = complete_device_views([], self.options, self.choices, {'actionable': True}, self.plan, {}, {}, {}, self.now)
        battery = views[0]
        self.assertTrue(battery['planned'])
        self.assertNotIn('choice_label', battery)
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
        for node in ast.walk(function):
            if isinstance(node, ast.ImportFrom): node.level = 0
        module = ast.Module(body=[function], type_ignores=[])
        namespace = {'Any': Any, 'datetime': datetime, 'timezone': timezone, 'ShsApiError': ValueError,
                     'resolved_options': lambda hass, options: options, 'planning_path': planning_path, 'unplanned_services': unplanned_services}
        exec(compile(module, 'coordinator.py', 'exec'), namespace)
        calls = {}
        fake = SimpleNamespace(hass=SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=lambda entry, **kw: setattr(entry, 'options', kw['options']))), entry=SimpleNamespace(options=self.options), optimisation_degraded_devices=[{'key': 'pool'}],
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

    def test_pool_planning_support_uses_the_system_path(self):
        self.options.update(pool_enabled=True, pool_water_temperature_entity='sensor.water',
                            device_modes={'$pool': 'control_verification'})
        self.device.update(key='thermostat', category='pool_heating', execution_reason='No executable planning model for this device')
        self.device['mapping']['temperature_entity_id'] = 'sensor.water'
        self.plan['capabilities']['pool'] = True
        for slot in self.plan['plans']['priority']['slots']:
            slot['device_commands'][self.device['key']] = {'type': 'unavailable', 'reason': 'No executable planning model for this device'}
        pool = next(d for d in self.view() if d.get('system') == 'pool')
        self.assertIsNone(pool['execution_reason'])
        self.assertEqual(pool['planning_support'], {'state': 'available', 'reason': None, 'path': 'pool'})
        self.assertFalse(pool['execution_eligibility']['writes_permitted_by_mode'])

    def test_a_planned_pool_pump_is_shown_as_part_of_the_pool(self):
        # The fixture plan models these two keys; its commands must name exactly them.
        self.options.update(pool_enabled=True, pool_water_temperature_entity='sensor.water',
                            device_modes={'$pool': 'control_verification', 'relay': 'control_verification'})
        self.plan['capabilities']['pool'] = True
        heater = deepcopy(self.device)
        heater.update(key='thermostat', statistic_id='thermostat', name='Pool heater', category='pool_heating')
        heater['mapping']['temperature_entity_id'] = 'sensor.water'
        pump = {**deepcopy(self.device), 'key': 'relay', 'statistic_id': 'relay', 'name': 'Pool pump',
                'category': 'pool_heating', 'control_type': 'switch_schedule',
                'mapping': {'control_type': 'switch_schedule', 'actuator_entity_ids': ['switch.pump']}}
        unavailable = {'type': 'unavailable', 'reason': 'No executable planning model for this device'}
        for slot in self.plan['plans']['priority']['slots']:
            slot['device_commands']['relay'] = unavailable
        # The stale controller of its own must not surface once the pump belongs to the pool.
        controllers = {'pool': {'state': 'verified', 'reason': 'Commands logged'},
                       'device:relay': {'state': 'fault', 'reason': unavailable['reason']}}
        views = complete_device_views([heater, pump], self.options, self.choices,
            operational_status(self.plan, 'live', [], self.now), self.plan, controllers, {}, {}, self.now)
        pool = next(d for d in views if d.get('system') == 'pool')
        member = next(d for d in views if d['key'] == 'relay')
        self.assertEqual(pool['key'], heater['key'])
        self.assertNotIn('system_member', pool)
        self.assertIsNone(member.get('system'))
        self.assertEqual(member['system_member'], 'pool')
        self.assertEqual(member['permission']['controller_id'], 'pool')
        self.assertIsNone(member['permission']['reason'])
        self.assertEqual(member['mode'], 'control_verification')
        self.assertEqual(member['execution_status']['state'], 'verified')
        self.assertEqual(member['planning_support'], {'state': 'available', 'reason': None, 'path': 'pool'})
        self.assertIsNone(member['execution_reason'])
        self.assertEqual(member['system_fields'], [])
        self.assertEqual(member['planning_system'], 'pool')
        # A Monitoring meter on the pool path is background consumption, not a member.
        pump['planning_role'] = 'base_load'
        monitored = next(d for d in complete_device_views([heater, pump], self.options, self.choices,
            operational_status(self.plan, 'live', [], self.now), self.plan, controllers, {}, {}, self.now)
            if d['key'] == 'relay')
        self.assertNotIn('system_member', monitored)
        self.assertEqual(monitored['permission']['controller_id'], 'device:relay')

    def test_mapping_readiness_does_not_imply_executable_or_mode_eligibility(self):
        self.options['device_modes'] = {self.device['key']: 'planning'}
        device = self.view()[0]
        self.assertEqual(device['mapping_readiness']['state'], 'ready')
        self.assertFalse(device['execution_eligibility']['eligible'])
        self.assertEqual(device['execution_status']['state'], 'planning')
        readiness = device_readiness([device])
        self.assertEqual(readiness['ready_devices'], 1)
        self.assertEqual(readiness['execution_eligible_devices'], 0)

    def test_diagnostic_device_view_survives_invalid_plan_timestamps(self):
        self.plan['plans']['priority']['slots'][0]['start'] = 'invalid'
        device = self.view()[0]
        self.assertEqual(device['planning_support']['state'], 'unavailable')
        self.assertFalse(device['execution_eligibility']['eligible'])

    def test_passive_mode_does_not_display_old_verification_as_current_work(self):
        from presentation import execution_view
        status = {'state': 'verified', 'reason': 'Commands logged'}
        self.assertEqual(execution_view('planning', status)['state'], 'planning')
        self.assertEqual(status['state'], 'verified')
