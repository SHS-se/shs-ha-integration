"""Admission reads schedules; each executor owns its commands and equipment."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest

import test_device_commands as device_fixtures
import test_controller as controller_fixtures
from shs_core.verification import VerificationJournal
from shs_core.optimisation import validate_plan_contract, OptimisationInputError, discrete_current_control
from shs_core.presentation import operational_status, timeline
from shs_core.operational_attention import execution_attention
from shs_core.configuration_fields import control_fields
from shs_core.device_commands import execution_setup_errors


class PlanAdmissionTests(unittest.TestCase):
    def setUp(self):
        fixture = json.loads((Path(__file__).parent/'fixtures/schema-9-mixed-mode-plan.json').read_text())
        self.plan = fixture['plan']
        self.now = datetime.fromisoformat(fixture['validation_time'])

    def test_forecasts_and_comparisons_do_not_veto_commands_or_mutate_the_plan(self):
        self.plan.update(services=None, device_models=[], status='incomplete')
        self.plan['plans']['cost'] = {'status': 'infeasible', 'slots': []}
        self.plan['plans']['baseline'] = None
        self.plan['operating_scope']['external_demands'] = {
            'uncontrolled': {'forecast_w_by_slot': [200000], 'recent_observation': {'end': 'yesterday'}}}
        self.plan['execution_plan']['plans']['priority']['slots'][0].update(
            device_loads_w={}, battery_soc=2, battery_discharge_w=-1)
        before = deepcopy(self.plan)
        validate_plan_contract(self.plan, self.now)
        self.assertEqual(self.plan, before)
        status = operational_status(self.plan, 'live', [], self.now)
        self.assertTrue(status['actionable'], status)
        self.assertTrue(timeline(self.plan, status)['slots'])

    def test_short_long_and_gapped_schedules_have_no_invented_horizon_gate(self):
        for count in (1, 400):
            with self.subTest(count=count):
                plan = deepcopy(self.plan)
                for branch in (plan, plan['execution_plan']):
                    template = branch['plans']['priority']['slots'][0]
                    start = datetime.fromisoformat(template['start'])
                    branch['plans']['priority']['slots'] = [
                        {**deepcopy(template), 'start': (start+timedelta(minutes=30*i)).isoformat()}
                        for i in range(count)]
                validate_plan_contract(plan, self.now)

    def test_unreadable_or_ambiguous_schedule_still_refused(self):
        for mutate in (
            lambda p: p.update(schema_version=999),
            lambda p: p.update(mode='demo'),
            lambda p: p['plans']['priority'].update(slots=[]),
            lambda p: p['plans']['priority']['slots'][0].update(start='unknown'),
            lambda p: p['plans']['priority']['slots'][1].update(start=p['plans']['priority']['slots'][0]['start']),
        ):
            with self.subTest(mutate=mutate):
                plan = deepcopy(self.plan)
                mutate(plan)
                with self.assertRaises(OptimisationInputError):
                    validate_plan_contract(plan, self.now)
        with self.assertRaisesRegex(OptimisationInputError, 'expired'):
            validate_plan_contract(self.plan, datetime.fromisoformat(self.plan['valid_until']))

    def test_different_branch_horizons_render_by_time_without_index_coupling(self):
        self.plan['execution_plan']['plans']['priority']['slots'] = self.plan['execution_plan']['plans']['priority']['slots'][1:2]
        validate_plan_contract(self.plan, self.now)
        status = operational_status(self.plan, 'live', [], self.now)
        rendered = timeline(self.plan, status)['slots']
        self.assertNotIn('start', rendered[0]['execution'])
        self.assertEqual(rendered[1]['execution']['start'], rendered[1]['start'])

    def test_ev_capabilities_follow_equipment_instead_of_static_current_voltage_caps(self):
        control = discrete_current_control(.05, 120, .05, 3, 600)
        self.assertEqual(control['max_current_a'], 120)
        self.assertEqual(control['voltage_v'], 600)
        for args in ((5, 16, 0, 3, 230), (16, 5, 1, 3, 230), (5, 16.5, 1, 3, 230)):
            with self.assertRaises(OptimisationInputError):
                discrete_current_control(*args)


class DeviceCommandIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        device_fixtures.DeviceExecutionTests.setUp(self)
        self.states['switch.broken'] = device_fixtures.State('on')
        self.options['device_control_mappings']['broken'] = {
            'control_type': 'permit_inhibit', 'actuator_entity_ids': ['switch.broken'], 'max_inhibit_slots': 2}
        self.options['device_modes']['broken'] = 'controlling'
        model = {'key': 'broken', 'control_type': 'permit_inhibit'}
        self.coordinator.optimisation_plan['device_models'].append(model)
        self.coordinator.async_cached_device_configuration.return_value.append(model)
        self.slot['device_commands']['broken'] = {'type': 'permit_inhibit', 'permitted': 'off'}

    async def test_bad_command_holds_only_its_device_and_corrected_command_clears_attention(self):
        await self.controller.async_start()
        self.assertEqual(self.calls, [('switch.heater', 'off')])
        self.assertEqual(self.controller.status['device:broken']['state'], 'fault')
        self.assertEqual(self.controller.status['device:heater']['state'], 'commanded')
        issues = execution_attention(self.controller.status, {}, [])
        self.assertTrue(any(i['key']=='controller:device:broken' for i in issues))
        self.assertEqual(self.states['switch.broken'].state, 'on')
        self.slot['device_commands']['broken'] = {'type': 'permit_inhibit', 'permitted': False}
        await self.controller.async_tick()
        self.assertIn(('switch.broken', 'off'), self.calls)
        self.assertEqual(self.controller.status['device:broken']['state'], 'commanded')
        self.assertFalse(any(i['key']=='controller:device:broken'
                             for i in execution_attention(self.controller.status, {}, [])))

    async def test_missing_device_command_does_not_block_other_commands(self):
        del self.slot['device_commands']['broken']
        await self.controller.async_start()
        self.assertEqual(self.calls, [('switch.heater', 'off')])
        self.assertEqual(self.controller.status['device:broken']['state'], 'idle')
        self.assertTrue(self.controller.status['device:broken']['execution_blocked'])

    async def test_missing_device_command_in_verification_is_not_an_execution_alarm(self):
        del self.slot['device_commands']['broken']
        self.options['device_modes']['broken'] = 'control_verification'
        await self.controller.async_start()
        status = self.controller.status['device:broken']
        self.assertEqual((status['state'], status['reason']), ('idle', 'No current plan for this device'))
        self.assertFalse(status['execution_blocked'])
        self.assertFalse(any(i['key']=='controller:device:broken'
                             for i in execution_attention(self.controller.status, {}, [])))

    async def test_verification_fault_is_not_an_execution_alarm_until_controlling(self):
        mapping = self.options['device_control_mappings']['heater']
        mapping.update(control_type='setpoint', setpoint_entity_id='number.target')
        self.states['number.target'] = device_fixtures.State(40, min=30, max=60, step=.5, unit_of_measurement='°F')
        self.coordinator.optimisation_plan['device_models'][0]['control_type'] = 'setpoint'
        self.coordinator.async_cached_device_configuration.return_value[0]['control_type'] = 'setpoint'
        self.slot['device_commands']['heater'] = {'type':'setpoint', 'target_c':45, 'minimum_c':40, 'maximum_c':55}
        self.options['device_modes']['heater'] = 'control_verification'
        self.controller.verification = VerificationJournal(controller_fixtures.Store(), controller_fixtures.Store())
        await self.controller.async_start()
        status = self.controller.status['device:heater']
        self.assertEqual(status['state'], 'fault')
        self.assertIn('temperature control must use Celsius', status['reason'])
        self.assertEqual((status['mode'], status['holding']), ('control_verification', False))
        self.assertFalse(any(i['key']=='controller:device:heater'
                             for i in execution_attention(self.controller.status, {}, [])))
        self.options['device_modes']['heater'] = 'controlling'
        await self.controller.async_tick()
        self.assertTrue(any(i['key']=='controller:device:heater'
                            for i in execution_attention(self.controller.status, {}, [])))

    async def test_website_command_bounds_need_no_local_temperature_range(self):
        # The room schedule on the website owns the comfort range; Home
        # Assistant asks for no second Lowest/Highest target of its own.
        mapping = self.options['device_control_mappings']['heater']
        mapping.update(control_type='setpoint', setpoint_entity_id='number.target')
        self.assertEqual(execution_setup_errors(mapping), [])
        self.states['number.target'] = device_fixtures.State(40, min=30, max=60, step=.5, unit_of_measurement='°C')
        self.coordinator.optimisation_plan['device_models'][0]['control_type'] = 'setpoint'
        self.coordinator.async_cached_device_configuration.return_value[0]['control_type'] = 'setpoint'
        self.slot['device_commands']['heater'] = {'type':'setpoint', 'target_c':45, 'minimum_c':40, 'maximum_c':55}
        await self.controller.async_start()
        self.assertEqual(self.controller.status['device:heater']['state'], 'commanded')
        self.assertIn(('number.target', 45), self.calls)
        self.calls.clear()
        self.slot['device_commands']['heater']['target_c'] = 52
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['device:heater']['state'], 'commanded')
        self.assertEqual(self.calls, [('number.target', 52)])
        self.assertNotIn('minimum_temperature_c', {f['key'] for f in control_fields('setpoint', None)})
