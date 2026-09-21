"""Only the execution-mode select releases control.

Restarts, integration updates, unavailable or stale entities, missing plans,
faults and unrelated configuration changes never change a device's state: it
keeps the last setting SHS sent, and SHS resumes the plan when it can. Setting
a device's select to Verification is what hands it back to its own settings.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import unittest
import test_controller as fixtures
from test_controller import State
from controller import ScheduledController
from verification import VerificationJournal


def later(test, minutes):
    """Move the controller's clock on."""
    offset = timedelta(minutes=minutes)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + offset

    patcher = patch('controller.datetime', Clock)
    patcher.start()
    test.addCleanup(patcher.stop)


class Continuity(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.controller.verification = VerificationJournal(fixtures.Store(), fixtures.Store())

    def restarted(self):
        """A new process, as after an HA restart or an integration update."""
        controller = ScheduledController(self.hass, self.coordinator, self.store, self.controller.options)
        controller.confirm = self.controller.confirm
        controller.verification = VerificationJournal(fixtures.Store(), fixtures.Store())
        controller.device = 'battery'
        return controller


class GenericDeviceTests(Continuity):
    def setUp(self):
        super().setUp()
        self.states['switch.heater'] = State('on')
        self.options['device_control_mappings']['heater'] = {
            'control_type': 'permit_inhibit', 'actuator_entity_ids': ['switch.heater'], 'max_inhibit_slots': 2}
        self.options['device_modes']['heater'] = 'controlling'
        self.coordinator.optimisation_plan.update(schema_version=7, device_models=[
            {'key': 'heater', 'control_type': 'permit_inhibit'}])
        self.coordinator.async_cached_device_configuration = AsyncMock(return_value=[
            {'key': 'heater', 'control_type': 'permit_inhibit'}])
        self.slot['device_commands'] = {'heater': {'type': 'permit_inhibit', 'permitted': False}}

    async def start(self):
        await self.controller.async_start()
        self.assertEqual(self.states['switch.heater'].state, 'off')
        self.calls.clear()

    def status(self, controller=None):
        return (controller or self.controller).status['device:heater']

    async def test_an_unavailable_switch_is_never_an_external_change(self):
        await self.start()
        self.states['switch.heater'] = State('unavailable')
        await self.controller.async_tick()
        self.assertEqual(self.status()['state'], 'pending', self.status())
        self.assertNotIn('device:heater', self.controller.overrides)
        self.assertNotIn('restoration_pending', self.controller.records['device:heater'])
        self.states['switch.heater'] = State('off')
        await self.controller.async_tick()
        self.assertEqual(self.status()['state'], 'commanded', self.status())
        self.assertEqual(self.calls, [])

    async def test_a_long_absence_needs_attention_but_changes_nothing(self):
        await self.start()
        self.states['switch.heater'] = State('unavailable')
        await self.controller.async_tick()
        later(self, 6)
        await self.controller.async_tick()
        self.assertEqual(self.status()['state'], 'fault', self.status())
        self.assertTrue(self.status()['retry_automatically'])
        self.assertNotIn('device:heater', self.controller.overrides)
        self.assertEqual(self.calls, [])

    async def test_a_genuine_external_change_latches_until_the_select_leaves_controlling(self):
        await self.start()
        self.states['switch.heater'] = State('on')
        await self.controller.async_tick()
        self.assertEqual(self.status()['state'], 'overridden', self.status())
        self.assertEqual(self.calls, [], 'the manual setting is left alone')
        await self.controller.async_tick()
        self.assertEqual(self.status()['state'], 'overridden')
        self.options['device_modes']['heater'] = 'control_verification'
        await self.controller.async_tick()
        self.assertNotIn('device:heater', self.controller.overrides)
        self.options['device_modes']['heater'] = 'controlling'
        await self.controller.async_tick()
        self.assertEqual(self.status()['state'], 'commanded', self.status())
        self.assertEqual(self.calls, [('switch.heater', 'off')])

    async def test_a_restart_keeps_the_last_setting_and_resumes_without_handing_back(self):
        await self.start()
        await self.controller.async_stop()
        self.assertEqual(self.calls, [], 'stopping hands nothing back')
        controller = self.restarted()
        await controller.async_start()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.status(controller)['state'], 'commanded', self.status(controller))
        self.assertEqual(controller.records['device:heater']['originals'], {'switch.heater': 'on'})
        # The baseline captured before the restart is still what a release returns to.
        self.options['device_modes']['heater'] = 'control_verification'
        await controller.async_tick()
        self.assertEqual(self.calls, [('switch.heater', 'on')])
        self.assertNotIn('device:heater', controller.records)

    async def test_a_restart_before_the_switch_reports_waits_for_it(self):
        await self.start()
        await self.controller.async_stop()
        self.states['switch.heater'] = State('unavailable')
        controller = self.restarted()
        await controller.async_start()
        self.assertEqual(self.status(controller)['state'], 'pending', self.status(controller))
        self.assertNotIn('device:heater', controller.overrides)
        self.states['switch.heater'] = State('off')
        await controller.async_tick()
        self.assertEqual(self.status(controller)['state'], 'commanded')
        self.assertEqual(self.calls, [])

    async def test_a_plan_without_a_command_for_the_device_holds_its_last_setting(self):
        await self.start()
        self.slot['device_commands']['heater'] = {'type': 'unavailable', 'reason': 'No executable planning model for this device'}
        await self.controller.async_tick()
        self.assertEqual(self.status()['state'], 'unsupported', self.status())
        self.assertIn('device:heater', self.controller.records)
        self.assertEqual(self.calls, [])

    async def test_the_maximum_pause_allows_a_quarter_of_running_while_keeping_control(self):
        await self.start()
        self.controller.records['device:heater']['inhibited_since'] = (
            datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        await self.controller.async_tick()
        self.assertEqual(self.status()['state'], 'limited', self.status())
        self.assertEqual(self.calls, [('switch.heater', 'on')])
        self.assertIn('device:heater', self.controller.records, 'control is kept')
        self.calls.clear()
        await self.controller.async_tick()
        self.assertEqual(self.calls, [], 'it may run for the rest of that quarter')
        later(self, 16)
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('switch.heater', 'off')])

    async def test_the_maximum_pause_also_applies_while_no_plan_is_available(self):
        await self.start()
        self.coordinator.current_plan_slot = None
        self.controller.records['device:heater']['inhibited_since'] = (
            datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('switch.heater', 'on')])
        self.assertEqual(self.status()['state'], 'limited', self.status())


class EvTests(Continuity):
    def setUp(self):
        super().setUp()
        self.options['device_modes']['$ev'] = 'controlling'

    async def start(self):
        await self.controller.async_start()
        self.assertEqual((self.states['switch.charge'].state, float(self.states['number.current'].state)), ('on', 10))
        self.calls.clear()

    async def test_an_unavailable_charger_holds_and_resumes_without_a_baseline_flip(self):
        await self.start()
        self.states['number.current'] = State('unavailable')
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['ev']['state'], 'pending', self.controller.status['ev'])
        self.assertNotIn('restoration_pending', self.controller.records['ev'])
        self.states['number.current'] = State(10, min=5, max=16, step=1, unit_of_measurement='A')
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['ev']['state'], 'commanded', self.controller.status['ev'])
        self.assertEqual(self.calls, [])

    async def test_stale_vehicle_readings_hold_the_charger(self):
        await self.start()
        self.states['sensor.ev_soc'].last_reported -= timedelta(minutes=16)
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['ev']['state'], 'pending', self.controller.status['ev'])
        self.assertIn('ev', self.controller.records)
        self.assertEqual(self.calls, [])

    async def test_a_restart_keeps_the_vehicle_charging(self):
        await self.start()
        await self.controller.async_stop()
        controller = self.restarted()
        await controller.async_start()
        self.assertEqual(self.calls, [])
        self.assertEqual(controller.status['ev']['state'], 'commanded', controller.status['ev'])


class PoolTests(Continuity):
    def setUp(self):
        super().setUp()
        self.options['device_modes']['$pool'] = 'controlling'

    async def start(self):
        await self.controller.async_start()
        self.assertEqual(self.states['switch.pool'].state, 'on')
        self.calls.clear()

    async def test_a_restart_keeps_the_heater_running(self):
        await self.start()
        await self.controller.async_stop()
        self.assertEqual(self.calls, [])
        controller = self.restarted()
        await controller.async_start()
        self.assertEqual(self.calls, [])
        self.assertEqual(controller.status['pool']['state'], 'scheduled', controller.status['pool'])

    async def test_stale_water_holds_the_heater(self):
        await self.start()
        self.states['sensor.water'].last_reported -= timedelta(minutes=16)
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'pending', self.controller.status['pool'])
        self.assertIn('pool', self.controller.records)
        later(self, 6)
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'fault', self.controller.status['pool'])
        self.assertEqual(self.calls, [])

    async def test_a_missing_plan_holds_the_heater(self):
        await self.start()
        self.coordinator.current_plan_slot = None
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertIn('pool', self.controller.records)
        self.assertIn('holding', self.controller.status['pool']['reason'])

    async def test_unreadable_website_choices_hold_every_device(self):
        await self.start()
        self.coordinator.async_cached_device_configuration = AsyncMock(side_effect=RuntimeError('bad cache'))
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertIn('pool', self.controller.records)

    async def test_an_unrelated_configuration_change_hands_nothing_back(self):
        await self.start()
        self.options['planning_admissions'] = {'$pool': [['pool', None, 'switch_schedule']]}
        self.options['grid_import_limit_w'] = 13800
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.status['pool']['state'], 'scheduled', self.controller.status['pool'])

    async def test_a_new_control_entity_waits_for_the_select(self):
        await self.start()
        self.states['switch.new_pool'] = State('off')
        self.options['device_control_mappings']['pool']['actuator_entity_ids'] = ['switch.new_pool']
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'fault', self.controller.status['pool'])
        self.assertIn('Verification', self.controller.status['pool']['reason'])
        self.assertEqual(self.calls, [], 'neither switch is touched')
        self.options['device_modes']['$pool'] = 'control_verification'
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('switch.pool', 'off')], 'the old switch is handed back')
        self.options['device_modes']['$pool'] = 'controlling'
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('switch.pool', 'off'), ('switch.new_pool', 'on')])

    async def test_only_the_select_hands_the_heater_back(self):
        await self.start()
        self.options['device_modes']['$pool'] = 'control_verification'
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('switch.pool', 'off')])
        self.assertNotIn('pool', self.controller.records)
        self.assertEqual(self.controller.status['pool']['state'], 'verified', self.controller.status['pool'])
