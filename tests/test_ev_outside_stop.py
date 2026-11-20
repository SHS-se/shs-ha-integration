"""A car charging stop made outside SHS pauses planned charging for a minute.

User requirement, 20 November 2026: a car cannot be unplugged while it is
charging, so SHS must not switch an outside stop straight back on. See
docs/control-continuity.md, "Car charging stopped outside SHS".
"""
from datetime import datetime, timedelta, timezone
import unittest

import test_controller as fixtures
from test_controller import State
from test_control_continuity import Continuity, later


def car_reports(value):
    """A poll from the car, later than any call SHS made (tests move SHS's clock on)."""
    state = State(value)
    state.last_reported = state.last_updated = datetime.now(timezone.utc) + timedelta(hours=1)
    return state


class OutsideStopTests(Continuity):
    """Car reports are fresh State objects; an in-call state change is an echo."""

    def setUp(self):
        super().setUp()
        self.options['device_modes']['$ev'] = 'controlling'

    async def charging(self):
        await self.controller.async_start()
        self.assertEqual(self.calls, [('number.current', 10), ('switch.charge', 'on')])
        # The car reports charging after SHS's call returned (not just an echo).
        self.states['switch.charge'] = car_reports('on')
        await self.controller.async_tick()
        self.calls.clear()

    def status(self, controller=None):
        return (controller or self.controller).status['ev']

    async def test_an_outside_stop_pauses_planned_charging_for_a_minute(self):
        await self.charging()
        self.states['switch.charge'] = State('off')
        await self.controller.async_tick()
        self.assertEqual(self.calls, [], 'the stop is not switched back')
        self.assertEqual(self.status()['decision']['reason'], 'charging stopped outside SHS')
        self.assertIn('resumes_at', self.status())
        self.assertNotIn('ev', self.controller.ownership.overrides)
        self.assertIn('ev', self.controller.ownership.records, 'a pause is not a release')

        later(self, 0.5)
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])

        later(self, 1.1)
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('switch.charge', 'on')], 'still connected, so the plan resumes')
        self.assertEqual(self.status()['state'], 'commanded', self.status())

    async def test_each_further_outside_stop_pauses_again(self):
        await self.charging()
        self.states['switch.charge'] = State('off')
        await self.controller.async_tick()
        later(self, 1.1)
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('switch.charge', 'on')])
        self.states['switch.charge'] = car_reports('on')
        await self.controller.async_tick()
        self.calls.clear()

        self.states['switch.charge'] = State('off')
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.status()['decision']['reason'], 'charging stopped outside SHS')

    async def test_unplugging_during_the_pause_stops_without_resuming(self):
        await self.charging()
        self.states['switch.charge'] = State('off')
        await self.controller.async_tick()
        self.states['binary_sensor.connected'] = State('off')
        later(self, 1.1)
        await self.controller.async_tick()
        self.assertNotIn(('switch.charge', 'on'), self.calls)
        self.assertEqual(self.status()['state'], 'stopped')
        self.assertNotIn('outside_stop_until', self.controller.ownership.records['ev'])

    async def test_an_echo_of_shs_switching_on_is_not_the_car_charging(self):
        await self.controller.async_start()
        # Tessie echoed "on" during the call, then polled the car still stopped.
        self.states['switch.charge'] = State('off')
        await self.controller.async_tick()
        self.assertNotIn('outside_stop_until', self.controller.ownership.records['ev'])
        self.assertNotEqual(self.status()['decision'].get('reason'), 'charging stopped outside SHS')

    async def test_a_switch_still_reporting_off_after_shs_switched_it_on_is_not_an_outside_stop(self):
        await self.controller.async_start()
        self.assertEqual(self.calls, [('number.current', 10), ('switch.charge', 'on')])
        # Tessie has not reported the command yet.
        self.states['switch.charge'] = State('off')
        await self.controller.async_tick()
        self.assertNotEqual(self.status()['decision'].get('reason'), 'charging stopped outside SHS', self.status())
        self.assertNotIn('outside_stop_until', self.controller.ownership.records['ev'])

    async def test_the_pause_survives_a_restart(self):
        await self.charging()
        self.states['switch.charge'] = State('off')
        await self.controller.async_tick()
        await self.controller.async_stop()
        controller = self.restarted()
        await controller.async_start()
        self.assertEqual(self.calls, [], 'a restart during the pause does not restart charging')
        later(self, 1.1)
        await controller.async_tick()
        self.assertEqual(self.calls, [('switch.charge', 'on')])

    async def test_an_off_slot_written_by_shs_is_not_an_outside_stop(self):
        await self.charging()
        self.slot['ev_target_current_a'] = 0
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('switch.charge', 'off')])
        self.slot['ev_target_current_a'] = 10
        await self.controller.async_tick()
        self.assertEqual(self.calls[-1], ('switch.charge', 'on'))


if __name__ == '__main__':
    unittest.main()
