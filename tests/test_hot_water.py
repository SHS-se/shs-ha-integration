"""Planner demand authority executed by the real controller and HA boundary."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

import test_device_commands as device_fixtures
from shs_core.hot_water import demand_permission, validate_demand_command
from shs_core.optimisation import validate_plan_contract
from shs_core.controller import ScheduledController
from command_fixture import controller_inputs, native_executor
from shs_core.verification import VerificationJournal
import test_controller as controller_fixtures

COMMAND = {"type": "house_demand_permission", "expected_permitted": False,
           "allow_below_w": 1500, "inhibit_above_w": 3500}


class HotWaterDemandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        device_fixtures.DeviceExecutionTests.setUp(self)
        self.coordinator.optimisation_plan['device_models'][0]['category'] = 'hot_water'
        self.coordinator.async_cached_device_configuration.return_value[0]['category'] = 'hot_water'
        self.slot['device_commands']['heater'] = deepcopy(COMMAND)
        self.states['sensor.house_power'] = device_fixtures.State(1000, unit_of_measurement='W')

    async def test_measured_spike_and_fall_change_permission_inside_the_same_slot(self):
        await self.controller.async_start()
        self.assertEqual(self.states['switch.heater'].state, 'on', 'expected permission is not execution authority')
        self.states['sensor.house_power'].state = '5000'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'off')
        self.states['sensor.house_power'].state = '2500'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'off', 'deadband holds permission')
        self.states['sensor.house_power'].state = '1000'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.states['sensor.house_power'].state = '3000'  # Includes its own 2 kW draw.
        self.calls.clear()
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.assertEqual(self.calls, [], 'the heater does not toggle itself or receive duplicate writes')

    async def test_price_pause_holds_at_low_load_then_resumes_measured_load_control(self):
        self.slot['device_commands']['heater'] = {'type': 'permit_inhibit', 'permitted': False}
        await self.controller.async_start()
        self.assertEqual(self.states['switch.heater'].state, 'off')
        self.states['sensor.house_power'].state = '0'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'off', 'low demand cannot overrule a price pause')
        self.slot['device_commands']['heater'] = deepcopy(COMMAND)
        self.states['sensor.house_power'].state = '1000'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.states['sensor.house_power'].state = '5000'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'off', 'allowed quarters retain the live demand gate')

    async def test_inherited_pause_metadata_does_not_force_a_run(self):
        self.states['sensor.house_power'].state = '5000'
        await self.controller.async_start()
        record = self.controller.ownership.records['device:heater']
        record['inhibited_since'] = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        record['permit_forced_until'] = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        self.calls.clear()
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'off')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.status['device:heater']['state'], 'commanded')

    async def test_missing_measurement_holds_the_last_command(self):
        self.states['sensor.house_power'].state = '5000'
        await self.controller.async_start()
        self.states['sensor.house_power'].state = 'unavailable'
        self.calls.clear()
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'off')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.status['device:heater']['state'], 'pending')

    async def test_missing_binding_links_to_shared_energy_field(self):
        self.options.pop('house_consumption_power_entity')
        await self.controller.async_start()
        status = self.controller.status['device:heater']
        self.assertEqual(status['state'], 'fault')
        self.assertEqual(status['fix']['fields'][0]['key'], 'house_consumption_power_entity')
        self.assertEqual(self.calls, [])

    async def test_verification_observes_policy_without_writing(self):
        self.controller.verification = VerificationJournal(controller_fixtures.Store(), controller_fixtures.Store())
        self.options['device_modes']['heater'] = 'control_verification'
        self.slot['device_commands']['heater']['expected_permitted'] = True
        self.states['sensor.house_power'].state = '5000'
        await self.controller.async_start()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.status['device:heater']['state'], 'verified')
        self.assertIn('inhibit', self.controller.verification.attempts[-1]['operations'])
        self.states['sensor.house_power'].state = '5500'
        await self.controller.async_tick()
        self.assertEqual(len(self.controller.verification.attempts), 1, 'changing watts is supporting evidence, not a changed decision')
        self.states['sensor.house_power'].state = '1000'
        await self.controller.async_tick()
        self.assertIn('permit', self.controller.verification.attempts[-1]['operations'])
        self.assertEqual(self.calls, [])

    async def test_restart_in_deadband_preserves_the_paused_switch(self):
        self.states['sensor.house_power'].state = '5000'
        await self.controller.async_start()
        self.states['sensor.house_power'].state = '2500'
        self.calls.clear()
        other = ScheduledController(controller_inputs(self.hass), self.coordinator, self.store,
                                    lambda: deepcopy(self.options), native_executor=native_executor(self.hass))
        await other.async_start()
        self.assertEqual(self.states['switch.heater'].state, 'off')
        self.assertEqual(self.calls, [])


class HotWaterContractTests(unittest.TestCase):
    def test_generated_producer_fixture_validates_in_real_consumer(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/schema-8-hot-water-demand-plan.json').read_text())
        now = datetime.fromisoformat(fixture['validation_time'].replace('Z', '+00:00'))
        validate_plan_contract(fixture['plan'], now)
        commands = [slot['device_commands']['water'] for slot in fixture['plan']['plans']['priority']['slots']]
        self.assertTrue(any(c == {'type': 'permit_inhibit', 'permitted': False} for c in commands))
        command = next(c for c in commands if c['type'] == 'house_demand_permission')
        self.assertTrue(validate_demand_command(command))
        command['inhibit_above_w'] = command['allow_below_w']
        with self.assertRaises(ValueError):
            validate_plan_contract(fixture['plan'], now)

    def test_boundaries_and_heater_draw_are_stable(self):
        self.assertTrue(demand_permission(COMMAND, 1500, False))
        self.assertTrue(demand_permission(COMMAND, 3500, True))
        self.assertFalse(demand_permission(COMMAND, 3501, True))
        self.assertFalse(demand_permission(COMMAND, 2500, False))
        with self.assertRaises(ValueError):
            demand_permission(COMMAND, float('nan'), True)
