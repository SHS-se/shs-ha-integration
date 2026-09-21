"""An HA restart or Nibe reconnect makes the pool switch briefly unavailable.

The heater keeps its own state through the gap. SHS can neither write nor hand
the switch back until it reports again, so a handover attempted during the gap
only flipped the heater to its baseline and straight back once it returned.
"""
from datetime import datetime, timedelta
from unittest.mock import patch
import unittest
import test_controller as fixtures
from controller import ScheduledController
from verification import VerificationJournal


def later(test, minutes):
    """Move the controller's clock on, as time passes while the switch is away."""
    offset = timedelta(minutes=minutes)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + offset

    patcher = patch('controller.datetime', Clock)
    patcher.start()
    test.addCleanup(patcher.stop)


class ControllingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.options['device_modes']['$pool'] = 'controlling'

    def switch(self, value):
        self.states['switch.pool'] = fixtures.State(value)

    async def test_a_reconnect_resumes_the_plan_without_touching_the_heater(self):
        await self.controller.async_start()
        self.assertEqual(self.states['switch.pool'].state, 'on')
        self.assertEqual(self.controller.records['pool']['originals'], {'switch.pool': 'off'})
        self.calls.clear()
        self.switch('unavailable')
        await self.controller.async_tick()
        status = self.controller.status['pool']
        self.assertEqual(status['state'], 'pending', status)
        self.assertIn('switch.pool', status['reason'])
        self.assertTrue(status['retry_automatically'])
        self.assertIn('unavailable_since', status)
        # Ownership is kept and no handover is queued for the switch's return.
        self.assertNotIn('restoration_pending', self.controller.records['pool'])
        self.assertEqual(self.calls, [])
        self.switch('on')
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'scheduled')
        self.assertEqual(self.calls, [], 'the heater was already in its planned state')
        self.assertNotIn('unavailable_since', self.controller.status['pool'])

    async def test_a_switch_that_returns_in_another_state_is_set_to_the_plan(self):
        await self.controller.async_start()
        self.calls.clear()
        self.switch('unavailable')
        await self.controller.async_tick()
        self.switch('off')
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('switch.pool', 'on')])
        self.assertEqual(self.controller.records['pool']['originals'], {'switch.pool': 'off'})

    async def test_a_long_gap_needs_attention_but_still_hands_nothing_back(self):
        await self.controller.async_start()
        self.calls.clear()
        self.switch('unavailable')
        await self.controller.async_tick()
        later(self, 6)
        await self.controller.async_tick()
        status = self.controller.status['pool']
        self.assertEqual(status['state'], 'fault', status)
        self.assertTrue(status['retry_automatically'])
        self.assertIn('more than 5 minutes', status['reason'])
        self.assertNotIn('restoration_pending', self.controller.records['pool'])
        self.assertEqual(self.calls, [])
        self.switch('on')
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'scheduled')

    async def test_each_gap_gets_its_own_grace_period(self):
        await self.controller.async_start()
        self.switch('unavailable')
        await self.controller.async_tick()
        self.switch('on')
        await self.controller.async_tick()
        later(self, 6)
        self.switch('unavailable')
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'pending')

    async def test_a_handover_required_during_the_gap_completes_when_it_returns(self):
        await self.controller.async_start()
        self.calls.clear()
        self.switch('unavailable')
        self.options['device_modes']['$pool'] = 'control_verification'
        await self.controller.async_tick()
        status = self.controller.status['pool']
        self.assertEqual(status['state'], 'pending', status)
        self.assertIn('hand', status['reason'])
        self.assertTrue(self.controller.records['pool']['restoration_pending'])
        self.switch('on')
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('switch.pool', 'off')])
        self.assertNotIn('pool', self.controller.records)

    async def test_a_clean_restart_waits_for_the_switch_instead_of_faulting(self):
        self.switch('unavailable')
        await self.controller.async_start()
        self.assertEqual(self.controller.status['pool']['state'], 'pending')
        self.assertEqual(self.calls, [])
        self.switch('off')
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'scheduled')
        self.assertEqual(self.calls, [('switch.pool', 'on')])

    async def test_a_restart_with_journalled_ownership_resumes_without_a_flip(self):
        await self.controller.async_start()
        journal = self.store.saved
        restarted = ScheduledController(self.hass, self.coordinator, self.store, self.controller.options)
        restarted.confirm = self.controller.confirm
        self.store.saved = journal
        self.calls.clear()
        self.switch('unavailable')
        await restarted.async_start()
        status = restarted.status['pool']
        self.assertEqual(status['state'], 'pending', status)
        self.assertIn('resume the plan', status['reason'])
        self.switch('on')
        await restarted.async_tick()
        self.assertEqual(self.calls, [], 'the heater kept its setting through the restart')
        self.assertEqual(restarted.status['pool']['state'], 'scheduled')
        self.assertEqual(restarted.records['pool']['originals'], {'switch.pool': 'off'})


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.controller.verification = VerificationJournal(fixtures.Store(), fixtures.Store())
        self.controller.confirm = ScheduledController.confirm.__get__(self.controller)
        self.options['device_modes']['$pool'] = 'control_verification'

    async def test_verification_waits_for_the_switch_and_resumes_when_it_returns(self):
        self.states['switch.pool'] = fixtures.State('unavailable')
        await self.controller.async_start()
        status = self.controller.status['pool']
        self.assertEqual(status['state'], 'pending', status)
        self.assertIn('verification', status['reason'])
        later(self, 6)
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'fault')
        self.states['switch.pool'] = fixtures.State('off')
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'verified')
        self.assertNotIn('unavailable_since', self.controller.status['pool'])
        self.assertEqual(self.calls, [])
