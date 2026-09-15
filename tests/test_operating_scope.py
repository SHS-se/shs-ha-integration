"""Mixed-mode plans preserve actual demand and never acquire authority by reuse."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from operating_modes import operating_mode_identity, scoped_plan
from planning import build_operating_scope
from optimisation import validate_plan_contract, OptimisationInputError
import test_controller as fixtures


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 15, 11, 15, tzinfo=timezone.utc)
        self.horizon = [self.start + timedelta(minutes=15*i) for i in range(4)]
        self.devices = [dict(key='heater', category='pool_heating', control_type='setpoint'),
                        dict(key='pump', category='pool_heating', control_type='switch_schedule')]
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
        self.assertEqual(scope['device_owners'], {'heater': '$pool', 'pump': 'pump'})
        self.assertEqual(scope['external_demands']['heater']['recent_observation']['average_w'], 1360)
        self.assertEqual(scope['external_demands']['pump']['recent_observation']['average_w'], 768)
        self.models[0]['forecast_w_by_slot'][0] = 0
        self.assertEqual(scope['external_demands']['heater']['forecast_w_by_slot'][0], 300)

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

    def test_scope_fences_other_devices_mode_changes_and_legacy_plans(self):
        plan = {'operating_scope': self.scope(), 'execution_plan': {'plan_id': 'real'}}
        self.assertIs(scoped_plan(plan, self.options, 'battery'), plan['execution_plan'])
        self.assertIs(scoped_plan(plan, self.options, 'pool'), plan)
        self.options['device_modes']['$pool'] = 'controlling'
        self.assertIsNone(scoped_plan(plan, self.options, 'battery'))
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
            lambda p: p['execution_plan']['capabilities'].update(pool=True),
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
        options = {'device_modes': deepcopy(plan['operating_scope']['modes'])}
        select = lambda device: namespace['binding_plan_for'](coordinator, device, options)
        self.assertIs(select('battery')[0], plan['execution_plan'])
        self.assertIs(select('battery')[1], plan['execution_plan']['plans']['priority']['slots'][0])
        self.assertIs(select('pool')[0], plan)
        plan['execution_plan']['status'] = 'infeasible'
        self.assertIsNone(select('battery')[1])
        options['device_modes']['$pool'] = 'controlling'
        self.assertEqual(select('battery'), ({}, None))

    def test_runtime_status_uses_execution_feasibility_and_current_modes(self):
        from presentation import operational_status
        fixture = json.loads((Path(__file__).parent/'fixtures/schema-9-mixed-mode-plan.json').read_text())
        plan = fixture['plan']; now = datetime.fromisoformat(fixture['validation_time'])
        options = {'device_modes': deepcopy(plan['operating_scope']['modes'])}
        plan['execution_plan']['status'] = 'infeasible'
        self.assertEqual(operational_status(plan, 'live', [], now, options=options)['state'], 'infeasible')
        options['device_modes']['$pool'] = 'controlling'
        self.assertEqual(operational_status(plan, 'live', [], now, options=options)['state'], 'not_configured')

    def test_timeline_keeps_live_and_verification_requests_and_fences_mode_changes(self):
        from presentation import timeline
        plan = json.loads((Path(__file__).parent/'fixtures/schema-9-mixed-mode-plan.json').read_text())['plan']
        options = {'device_modes': plan['operating_scope']['modes']}
        display = timeline(plan, {'state': 'ready'}, options=options)
        self.assertEqual(display['slots'][0]['execution']['battery_command'], plan['execution_plan']['plans']['priority']['slots'][0]['battery_command'])
        self.assertEqual(display['slots'][0]['battery_command'], plan['plans']['priority']['slots'][0]['battery_command'])
        options = deepcopy(options); options['device_modes']['$pool'] = 'controlling'
        self.assertEqual(timeline(plan, {'state': 'ready'}, options=options)['slots'], [])


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
        self.controller.verification = VerificationJournal(fixtures.Store())
        await self.controller.async_start()
        self.assertIn(('number.charge_limit', .5), self.calls)
        self.assertNotIn(('number.charge_limit', 2), self.calls)
        self.assertFalse(any(key in ('number.start', 'number.stop', 'switch.pool') for key, _ in self.calls))
        self.assertEqual(self.controller.status['battery']['state'], 'confirmed')

    async def test_entering_control_cannot_execute_cached_hypothetical_actions(self):
        self.options['device_modes']['$pool'] = 'controlling'
        await self.controller.async_start()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.controller.records)

    async def test_scope_change_during_awaited_command_stops_remaining_optimisation_writes(self):
        original = self.hass.services.async_call
        async def change_mode(domain, service, data, blocking):
            await original(domain, service, data, blocking)
            self.options['device_modes']['$pool'] = 'planning'
        self.hass.services.async_call = change_mode
        await self.controller.async_start()
        self.assertNotIn(('number.charge_limit', .5), self.calls)
