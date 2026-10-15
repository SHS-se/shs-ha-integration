"""Binary scheduling with measured finish-heating execution."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import unittest
import test_device_commands as fixtures
from shs_core.hot_water import heating_permission
from shs_core.optimisation import validate_plan_contract
from shs_core.controller import ScheduledController
from command_fixture import controller_inputs, native_executor
from shs_core.verification import VerificationJournal
import test_controller as controller_fixtures


class HotWaterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.DeviceExecutionTests.setUp(self)
        self.coordinator.optimisation_plan['device_models'][0]['category'] = 'hot_water'
        self.coordinator.async_cached_device_configuration.return_value[0]['category'] = 'hot_water'
        self.options['device_control_mappings']['heater'].update(power='sensor.water_power')
        self.states['sensor.water_power'] = fixtures.State(3000, unit_of_measurement='W')

    async def test_heating_finishes_before_scheduled_block_in_same_quarter(self):
        await self.controller.async_start()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.assertTrue(self.controller.status['device:heater']['decision']['finishing_heating'])
        self.states['sensor.water_power'].state = '500'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.states['sensor.water_power'].state = '499'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'off')
        self.calls.clear()
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])

    async def test_old_available_power_never_becomes_a_setup_or_verification_error(self):
        from datetime import timedelta
        self.states['sensor.water_power'].last_reported -= timedelta(days=3)
        await self.controller.async_start()
        self.assertTrue(self.controller.status['device:heater']['decision']['finishing_heating'])
        self.assertNotIn('fix', self.controller.status['device:heater'])
        self.states['sensor.water_power'].state = '0'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'off')
        self.controller.verification = VerificationJournal(controller_fixtures.Store(), controller_fixtures.Store())
        self.options['device_modes']['heater'] = 'control_verification'
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['device:heater']['state'], 'verified')
        self.assertNotIn('fix', self.controller.status['device:heater'])

    async def test_compressor_preheat_cannot_extend_boiler_permission(self):
        self.states['sensor.compressor_power'] = fixtures.State(1274, unit_of_measurement='W')
        self.states['sensor.water_power'].state = '0'
        await self.controller.async_start()
        self.assertEqual(self.states['switch.heater'].state, 'off')

    async def test_power_sensor_alone_allows_finishing_and_clears_setup_fault(self):
        self.options['device_control_mappings']['heater'].pop('power')
        await self.controller.async_start()
        status = self.controller.status['device:heater']
        self.assertEqual(status['state'], 'fault')
        self.assertEqual(status['fix']['fields'][0]['key'], 'power')
        self.assertEqual(self.calls, [])
        self.options['device_control_mappings']['heater']['power'] = 'sensor.water_power'
        await self.controller.async_tick()
        self.assertTrue(self.controller.status['device:heater']['decision']['finishing_heating'])
        self.assertEqual(self.states['switch.heater'].state, 'on')

    async def test_idle_heater_is_blocked_immediately(self):
        self.states['sensor.water_power'].state = '0'
        await self.controller.async_start()
        self.assertEqual(self.states['switch.heater'].state, 'off')

    async def test_new_allow_resumes_permission(self):
        self.states['sensor.water_power'].state = '0'
        await self.controller.async_start()
        self.slot['device_commands']['heater']['permitted'] = True
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'on')

    async def test_unknown_power_holds_the_last_setting(self):
        await self.controller.async_start()
        self.states['sensor.water_power'].state = 'unavailable'
        self.calls.clear()
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.status['device:heater']['state'], 'pending')

    async def test_restart_continues_finishing(self):
        await self.controller.async_start()
        self.calls.clear()
        other = ScheduledController(controller_inputs(self.hass), self.coordinator, self.store,
                                    lambda: deepcopy(self.options), native_executor=native_executor(self.hass))
        await other.async_start()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.assertEqual(self.calls, [])
        self.states['sensor.water_power'].state = '0'
        await other.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'off')

    async def test_static_rating_is_not_completion_evidence_and_links_to_power_editor(self):
        self.options['device_control_mappings']['heater']['power'] = 3000
        await self.controller.async_start()
        status = self.controller.status['device:heater']
        self.assertEqual(status['state'], 'fault')
        self.assertEqual(status['fix']['fields'][0]['key'], 'power')
        self.assertEqual(status['fix']['fields'][0]['scope'], 'mapping')
        self.assertEqual(self.calls, [])

    async def test_verification_reports_finishing_without_writes(self):
        self.controller.verification = VerificationJournal(controller_fixtures.Store(), controller_fixtures.Store())
        self.options['device_modes']['heater'] = 'control_verification'
        await self.controller.async_start()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.status['device:heater']['state'], 'verified')
        self.states['sensor.water_power'].state = '0'
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertIn('inhibit', self.controller.verification.attempts[-1]['operations'])

    def test_boundary_and_no_start_when_already_blocked(self):
        self.assertTrue(heating_permission(False, True, 501))
        self.assertTrue(heating_permission(False, True, 500))
        self.assertFalse(heating_permission(False, True, 499))
        self.assertFalse(heating_permission(False, False, 3000))
        self.assertTrue(heating_permission(True, False, 0))


class HotWaterContractTests(unittest.TestCase):
    def test_generated_producer_fixture_validates_in_real_consumer(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/schema-8-hot-water-demand-plan.json').read_text())
        now = datetime.fromisoformat(fixture['validation_time'].replace('Z', '+00:00'))
        validate_plan_contract(fixture['plan'], now)
        slots = fixture['plan']['plans']['priority']['slots']
        self.assertEqual(sum(slot['boiler_permitted'] for slot in slots), 10)
        for slot in slots:
            self.assertEqual(slot['device_commands']['water'], {'type': 'permit_inhibit', 'permitted': slot['boiler_permitted']})
