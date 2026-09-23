"""Retained forecasts survive mode changes; local grants alone authorize writes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from operating_modes import execution_mode_options, operating_mode_identity, reconcile_admissions, scoped_plan, system_device_keys
from planning import build_operating_scope
from optimisation import validate_plan_contract, OptimisationInputError, PlanContractCache
import test_controller as fixtures


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 15, 11, 15, tzinfo=timezone.utc)
        self.horizon = [self.start + timedelta(minutes=15*i) for i in range(4)]
        self.devices = [dict(key='heater', category='pool_heating', control_type='setpoint', planning_role='controllable'),
                        dict(key='pump', category='pool_heating', control_type='switch_schedule', planning_role='controllable')]
        self.models = [{**d, 'forecast_w_by_slot': [300, 350, 400, 450]} for d in self.devices]
        self.options = {'device_modes': {'$battery': 'controlling', '$pool': 'control_verification', 'pump': 'planning'},
                        'pool_water_temperature_entity': 'sensor.water', 'device_control_mappings': {
                            'heater': {'control_type': 'setpoint', 'temperature_entity_id': 'sensor.water'},
                            'pump': {'control_type': 'switch_schedule'}}}

    def scope(self, rows=()):
        return build_operating_scope(self.options, self.devices, self.models, rows, self.horizon)

    def test_actual_quarter_and_empirical_forecast_are_separate_immutable_evidence(self):
        row = {'start': (self.start-timedelta(minutes=15)).isoformat(), 'device_energy_kwh': {'heater': .34, 'pump': .192}}
        scope = self.scope([row])
        self.assertEqual(scope['device_owners'], {'heater': '$pool', 'pump': '$pool'})
        self.assertEqual(scope['external_demands']['heater']['recent_observation']['average_w'], 1360)
        self.assertEqual(scope['external_demands']['pump']['recent_observation']['average_w'], 768)
        self.models[0]['forecast_w_by_slot'][0] = 0
        self.assertEqual(scope['external_demands']['heater']['forecast_w_by_slot'][0], 300)

    def test_pool_pump_runs_with_the_pool_and_has_no_authority_of_its_own(self):
        # A grant saved for the pump before it joined the pool changes nothing.
        for mode in (None, 'monitoring', 'control_verification', 'controlling'):
            with self.subTest(mode=mode):
                self.options['device_modes'].pop('pump', None)
                if mode is not None:
                    self.options['device_modes']['pump'] = mode
                scope = self.scope()
                self.assertEqual(scope['device_owners'], {'heater': '$pool', 'pump': '$pool'})
                # Each member keeps its own forecast, so Verification still charts the pump.
                self.assertEqual(set(scope['external_demands']), {'heater', 'pump'})
                self.assertEqual(scope['external_demands']['pump']['forecast_w_by_slot'], [300, 350, 400, 450])
                plan = {'operating_scope': scope, 'execution_plan': {'plan_id': 'real'}}
                self.assertIs(scoped_plan(plan, self.options, 'pool'), plan)

    def test_pool_members_are_live_together_or_not_at_all(self):
        # The website refuses pool execution unless $pool is Controlling and sizes the
        # pool from every live member, so a member owned elsewhere broke both rules.
        for pool in ('control_verification', 'controlling'):
            for pump in (None, 'control_verification', 'controlling'):
                with self.subTest(pool=pool, pump=pump):
                    self.options['device_modes'] = {'$pool': pool, **({'pump': pump} if pump else {})}
                    scope = self.scope()
                    live = {key for key, owner in scope['device_owners'].items() if scope['modes'][owner] == 'controlling'}
                    self.assertEqual(live, {'heater', 'pump'} if pool == 'controlling' else set())
                    self.assertEqual(set(scope['external_demands']), {'heater', 'pump'} - live)

    def test_monitoring_owner_survives_plan_validation_status_and_timeline(self):
        from presentation import operational_status, timeline
        fixture = json.loads((Path(__file__).parent/'fixtures/schema-9-mixed-mode-plan.json').read_text())
        plan = fixture['plan']
        now = datetime.fromisoformat(fixture['validation_time'])
        scope = plan['operating_scope']
        # Pick a hypothetical model, which must stay outside the execution branch.
        key = next(key for key in scope['external_demands'])
        scope['device_owners'][key] = key
        scope['modes'][key] = 'monitoring'
        options = {'device_modes': {k: v for k, v in scope['modes'].items() if k != key}}
        validate_plan_contract(plan, now)
        self.assertIs(scoped_plan(plan, options, 'battery'), plan['execution_plan'])
        self.assertEqual(operational_status(plan, 'live', [], now, options=options)['state'], 'ready')
        self.assertTrue(timeline(plan, {'state': 'ready'}, options=options)['slots'])
        options['device_modes'][key] = 'controlling'
        self.assertIs(scoped_plan(plan, options, 'battery'), plan['execution_plan'])
        self.assertTrue(operational_status(plan, 'live', [], now, options=options)['actionable'])
        self.assertTrue(timeline(plan, {'state': 'ready'}, options=options)['slots'])

    def test_zero_is_observed_and_missing_or_old_quarters_are_not_zero(self):
        recent = {'start': (self.start-timedelta(minutes=15)).isoformat(), 'device_energy_kwh': {'heater': 0}}
        scope = self.scope([recent])
        self.assertEqual(scope['external_demands']['heater']['recent_observation']['average_w'], 0)
        self.assertIsNone(scope['external_demands']['pump']['recent_observation'])
        recent['start'] = (self.start-timedelta(minutes=30)).isoformat()
        self.assertIsNone(self.scope([recent])['external_demands']['heater']['recent_observation'])

    def test_controlling_devices_are_not_counted_as_external(self):
        self.options['device_modes'].update({'$pool': 'controlling', 'pump': 'controlling'})
        self.assertEqual(self.scope()['external_demands'], {})

    def test_retains_each_branch_after_other_devices_mode_changes(self):
        plan = {'operating_scope': self.scope(), 'execution_plan': {'plan_id': 'real'}}
        self.assertIs(scoped_plan(plan, self.options, 'battery'), plan['execution_plan'])
        self.assertIs(scoped_plan(plan, self.options, 'pool'), plan)
        self.options['device_modes']['$pool'] = 'controlling'
        self.assertIs(scoped_plan(plan, self.options, 'battery'), plan['execution_plan'])
        self.assertIs(scoped_plan(plan, self.options, 'pool'), plan)
        self.assertIsNone(scoped_plan({'schema_version': 8}, self.options, 'battery'))

    def test_explicit_monitoring_and_absent_monitoring_have_same_identity(self):
        self.assertEqual(operating_mode_identity({}), operating_mode_identity({'device_modes': {'pump': 'monitoring'}}))

    def test_generated_schema9_fixture_and_nested_inventory_are_validated(self):
        fixture = json.loads((Path(__file__).parent/'fixtures/schema-9-mixed-mode-plan.json').read_text())
        plan = fixture['plan']
        now = datetime.fromisoformat(fixture['validation_time'].replace('Z', '+00:00'))
        validate_plan_contract(plan, now)
        for mutate in (
            lambda p: p.pop('execution_plan'),
            lambda p: p['execution_plan'].update(plan_id='wrong'),
            lambda p: p['execution_plan']['device_models'].append(p['device_models'][0]),
            lambda p: p['operating_scope']['external_demands'].clear(),
            lambda p: p['operating_scope']['device_owners'].update(pool_heater=[]),
            lambda p: p['operating_scope']['external_demands']['pool_heater']['forecast_w_by_slot'].__setitem__(0, float('nan')),
            lambda p: p['execution_plan']['capabilities'].update(pool=False),
        ):
            broken = deepcopy(plan); mutate(broken)
            with self.assertRaises(OptimisationInputError): validate_plan_contract(broken, now)


    def test_production_coordinator_selects_and_validates_the_execution_branch(self):
        import ast
        from types import SimpleNamespace
        fixture = json.loads((Path(__file__).parent/'fixtures/schema-9-mixed-mode-plan.json').read_text())
        plan = fixture['plan']; now = datetime.fromisoformat(fixture['validation_time'])
        tree = ast.parse((Path(__file__).parents[1]/'custom_components/shs_energy/coordinator.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ShsStatusCoordinator')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'binding_plan_for')
        for node in ast.walk(method):
            if isinstance(node, ast.ImportFrom): node.level = 0
        namespace = {'datetime': datetime, 'timedelta': timedelta, 'dt_util': SimpleNamespace(utcnow=lambda: now)}
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'coordinator.py', 'exec'), namespace)
        coordinator = SimpleNamespace(optimisation_plan=plan, _plan_configuration_changed=False)
        checked = []
        def validate(candidate, at, **kwargs):
            checked.append(candidate)
            validate_plan_contract(candidate, at, **kwargs)
        coordinator._plan_contract = PlanContractCache(lambda: coordinator.optimisation_plan, validate)
        options = {'device_modes': deepcopy(plan['operating_scope']['modes'])}
        select = lambda device: namespace['binding_plan_for'](coordinator, device, options)
        self.assertIs(select('battery')[0], plan['execution_plan'])
        self.assertIs(select('battery')[1], plan['execution_plan']['plans']['priority']['slots'][0])
        self.assertIs(select('pool')[0], plan)
        for _ in range(5):
            select('battery'); select('pool')
        # Repeated lookups reuse each branch's verdict instead of re-checking the contract.
        self.assertEqual([id(candidate) for candidate in checked], [id(plan['execution_plan']), id(plan)])
        coordinator._plan_configuration_changed = True
        options['device_modes']['$pool'] = 'controlling'
        self.assertIsNotNone(select('battery')[1])
        self.assertIsNotNone(select('pool')[1])
        plan['execution_plan']['status'] = 'infeasible'
        self.assertIsNone(select('battery')[1])
        self.assertIs(select('battery')[0], plan['execution_plan'])

    def test_runtime_status_uses_execution_feasibility_and_current_modes(self):
        from presentation import operational_status
        fixture = json.loads((Path(__file__).parent/'fixtures/schema-9-mixed-mode-plan.json').read_text())
        plan = fixture['plan']; now = datetime.fromisoformat(fixture['validation_time'])
        options = {'device_modes': deepcopy(plan['operating_scope']['modes'])}
        plan['execution_plan']['status'] = 'infeasible'
        self.assertEqual(operational_status(plan, 'live', [], now, options=options)['state'], 'infeasible')
        options['device_modes']['$pool'] = 'controlling'
        self.assertEqual(operational_status(plan, 'live', [], now, options=options)['state'], 'infeasible')

    def test_timeline_keeps_original_branches_after_mode_changes(self):
        from presentation import timeline
        plan = json.loads((Path(__file__).parent/'fixtures/schema-9-mixed-mode-plan.json').read_text())['plan']
        options = {'device_modes': plan['operating_scope']['modes']}
        display = timeline(plan, {'state': 'ready'}, options=options)
        self.assertEqual(display['slots'][0]['execution']['battery_command'], plan['execution_plan']['plans']['priority']['slots'][0]['battery_command'])
        self.assertEqual(display['slots'][0]['battery_command'], plan['plans']['priority']['slots'][0]['battery_command'])
        options = deepcopy(options); options['device_modes']['$pool'] = 'controlling'
        self.assertEqual(timeline(plan, {'state': 'ready'}, options=options), display)
        self.assertEqual(display['slots'][0]['execution_owners'], ['$battery'])


class PoolMembershipTests(unittest.TestCase):
    """The pool as exported on 2026-09-21: a heat pump with a power dial plus a pump
    that Node-RED switches with it. The pump's own admission faulted every quarter
    with "No executable planning model for this device"."""

    def setUp(self):
        self.options = {
            'pool_water_temperature_entity': 'sensor.filtered_pool_water_temperature',
            'device_control_mappings': {
                'sensor.pool_heater_energy': {
                    'actuator_entity_ids': ['switch.pool_heater'], 'control_type': 'variable_power',
                    'power': 'sensor.pool_heater_power',
                    'power_setting_entity_id': 'number.desired_charge_power_pool_1_main_unit_43040',
                    'temperature_entity_id': 'sensor.filtered_pool_water_temperature'},
                'sensor.pool_pump_energy': {
                    'actuator_entity_ids': ['switch.esphome_pool_pump_switch'], 'control_type': 'switch_schedule',
                    'power': 'sensor.esphome_pool_pump_power'},
                'sensor.pool_room_floor_heater_energy': {
                    'actuator_entity_ids': ['climate.pool_bathroom_floor_thermostat'], 'control_type': 'setpoint',
                    'room_area_id': 'basement_bathroom', 'temperature_entity_id': 'sensor.basement_bathroom_th_temperature'},
                'sensor.car_charging_total_energy': {
                    'control_entity_id': 'number.tesla_model_y_charge_current', 'control_type': 'variable_power',
                    'maximum_value': 16.0, 'minimum_value': 5.0, 'power': 'sensor.tesla_model_y_charger_power'},
                'sensor.hot_water_energy': {
                    'actuator_entity_ids': ['switch.water_boiler'], 'control_type': 'permit_inhibit',
                    'max_inhibit_slots': 20.0, 'power': 'sensor.water_boiler_power'}},
            'device_modes': {'$battery': 'controlling', '$ev': 'control_verification', '$pool': 'control_verification',
                             'sensor.hot_water_energy': 'control_verification',
                             'sensor.pool_pump_energy': 'control_verification'},
            'planning_admissions': {
                '$battery': [['$battery', None, 'battery']],
                '$ev': [['sensor.car_charging_total_energy', '2026-09-10T19:39:05.799467+00:00', 'variable_power']],
                '$pool': [['sensor.pool_heater_energy', '2026-09-20T17:02:12.654464+00:00', 'variable_power']],
                'sensor.hot_water_energy': [['sensor.hot_water_energy', None, 'permit_inhibit']],
                'sensor.pool_pump_energy': [['sensor.pool_pump_energy', '2026-09-21T08:34:38.644113+00:00', 'switch_schedule']]},
        }
        # The website's device configuration, in the shape the coordinator reconciles.
        self.devices = [
            {'key': 'sensor.pool_heater_energy', 'category': 'pool_heating', 'planning_role': 'controllable',
             'planning_choice_at': '2026-09-20T17:02:12.654464+00:00', 'control_type': 'variable_power'},
            {'key': 'sensor.pool_pump_energy', 'category': 'pool_heating', 'planning_role': 'controllable',
             'planning_choice_at': '2026-09-21T08:34:38.644113+00:00', 'control_type': 'switch_schedule'},
            {'key': 'sensor.pool_room_floor_heater_energy', 'category': 'pool_heating', 'planning_role': 'base_load',
             'planning_choice_at': '2026-09-16T11:26:32.634673+00:00', 'control_type': 'setpoint'},
            {'key': 'sensor.car_charging_total_energy', 'category': 'ev_charging', 'planning_role': 'controllable',
             'planning_choice_at': '2026-09-10T19:39:05.799467+00:00', 'control_type': 'variable_power'},
            {'key': 'sensor.hot_water_energy', 'category': 'hot_water', 'planning_role': 'controllable',
             'planning_choice_at': None, 'control_type': 'permit_inhibit'},
        ]
        self.home = {'battery': {'included': True, 'choice_at': None}}

    def test_the_pump_joins_the_pools_admission_instead_of_holding_its_own(self):
        admitted = reconcile_admissions(self.options, self.devices, self.home)
        self.assertEqual(admitted['planning_admissions']['$pool'], [
            ['sensor.pool_heater_energy', '2026-09-20T17:02:12.654464+00:00', 'variable_power'],
            ['sensor.pool_pump_energy', '2026-09-21T08:34:38.644113+00:00', 'switch_schedule']])
        self.assertNotIn('sensor.pool_pump_energy', admitted['planning_admissions'])
        self.assertNotIn('sensor.pool_pump_energy', admitted['device_modes'])
        # A changed membership is a new admission, so the pool restarts in Verification...
        self.assertEqual(admitted['device_modes']['$pool'], 'control_verification')
        # ...while every unrelated grant, including the live battery, is kept.
        self.assertEqual({key: admitted['device_modes'][key] for key in ('$battery', '$ev', 'sensor.hot_water_energy')},
                         {'$battery': 'controlling', '$ev': 'control_verification',
                          'sensor.hot_water_energy': 'control_verification'})
        # A second exchange changes nothing, so it neither replans nor resets the pool again.
        self.assertEqual(reconcile_admissions(admitted, self.devices, self.home), admitted)
        admitted['device_modes']['$pool'] = 'controlling'
        self.assertEqual(reconcile_admissions(admitted, self.devices, self.home)['device_modes']['$pool'], 'controlling')

    def test_the_heater_remains_the_only_actuator_the_pool_drives(self):
        from device_controls import pool_control_mapping
        self.assertEqual(system_device_keys(self.devices, self.options),
                         {'sensor.pool_heater_energy': 'pool', 'sensor.car_charging_total_energy': 'ev'})
        key, mapping = pool_control_mapping(self.options, self.devices)
        self.assertEqual((key, mapping['actuator_entity_ids']), ('sensor.pool_heater_energy', ['switch.pool_heater']))

    def test_a_member_cannot_be_granted_a_mode_of_its_own(self):
        views = [{'key': 'sensor.pool_pump_energy', 'name': 'Pool pump', 'planned': True, 'system_member': 'pool',
                  'permission': {'reason': None, 'controller_id': 'pool'}}]
        for mode in ('control_verification', 'controlling'):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'Pool pump runs with the pool heater'):
                execution_mode_options(self.options, views, 'sensor.pool_pump_energy', mode)


class ScopeControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.options['device_modes'] = {'$battery': 'controlling', '$pool': 'control_verification'}
        self.execution_slot = {**self.slot, 'battery_charge_w': 500,
                               'battery_command': fixtures.battery_command('grid_charge', 500, 0)}
        parent = self.coordinator.optimisation_plan
        parent['schema_version'] = 9
        parent['operating_scope'] = {'modes': operating_mode_identity(self.options)}
        parent['execution_plan'] = {**parent, 'schema_version': 8, 'capabilities': {'battery': True, 'pool': False, 'ev': False}}
        parent['execution_plan'].pop('operating_scope')
        def binding(device, options):
            selected = scoped_plan(self.coordinator.optimisation_plan, options, device)
            slot = self.execution_slot if device == 'battery' else self.slot
            return selected or {}, slot if selected else None
        self.coordinator.binding_plan_for = binding

    async def test_live_battery_uses_execution_command_and_verification_does_not_write(self):
        from verification import VerificationJournal
        self.controller.verification = VerificationJournal(fixtures.Store(), fixtures.Store())
        await self.controller.async_start()
        self.assertIn(('number.charge_limit', .5), self.calls)
        self.assertNotIn(('number.charge_limit', 2), self.calls)
        self.assertFalse(any(key in ('number.start', 'number.stop', 'switch.pool') for key, _ in self.calls))
        self.assertEqual(self.controller.status['battery']['state'], 'confirmed')

    async def test_entering_control_executes_retained_schedule_with_explicit_permission(self):
        self.options['device_modes']['$pool'] = 'controlling'
        await self.controller.async_start()
        self.assertIn(('number.charge_limit', .5), self.calls)
        self.assertIn('pool', self.controller.records)

    async def test_scope_change_during_awaited_command_stops_remaining_optimisation_writes(self):
        original = self.hass.services.async_call
        async def change_mode(domain, service, data, blocking):
            await original(domain, service, data, blocking)
            self.options['device_modes']['$pool'] = 'planning'
        self.hass.services.async_call = change_mode
        await self.controller.async_start()
        self.assertNotIn(('number.charge_limit', .5), self.calls)
