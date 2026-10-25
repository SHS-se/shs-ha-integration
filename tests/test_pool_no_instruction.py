"""Forecast demand is not authority to issue a pool instruction."""
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock
import test_controller as fixtures
from shs_core.device_controls import pool_permission
from shs_core.device_port import device_intent
from shs_core.execution_replan import requested


class PoolNoInstructionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)

    async def test_owned_pool_holds_without_writes_or_execution_failure(self):
        self.options['device_modes']['$pool'] = 'controlling'
        await self.controller.async_start()
        self.assertEqual(self.states['switch.pool'].state, 'on')
        ownership = deepcopy(self.controller.ownership.records['pool'])
        self.calls.clear()
        self.controller.record_execution = AsyncMock()
        self.slot['pool_permitted'] = None
        self.slot['pool_w'] = 1200
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.ownership.records['pool'], ownership)
        self.assertEqual(self.controller.status['pool']['state'], 'idle')
        self.assertIn('existing settings', self.controller.status['pool']['reason'])
        self.assertFalse(any(call.args[0] == 'pool' for call in self.controller.record_execution.call_args_list))
        self.assertFalse(requested('$pool', self.slot))
        self.assertNotIn('error', self.controller.preview_commands(self.slot)['pool'])
        with self.assertRaisesRegex(ValueError, 'No new pool instruction'):
            device_intent('pool', 'apply', self.coordinator.optimisation_plan, self.slot,
                          configuration_revision=1, policy_revision=1)

    async def test_verification_does_not_infer_permission_from_forecast(self):
        self.options['device_modes']['$pool'] = 'control_verification'
        self.slot['pool_permitted'] = None
        self.controller.verify = AsyncMock()
        await self.controller.async_start()
        self.assertEqual(self.controller.status['pool']['state'], 'idle')
        self.assertFalse(any(call.args[0] == 'pool' for call in self.controller.verify.call_args_list))
        self.assertEqual(self.calls, [])

    def test_partial_absent_instruction_is_not_reinterpreted(self):
        self.assertIsNone(pool_permission({}, {'pool_permitted': None, 'pool_w': 3000}))
        with self.assertRaisesRegex(ValueError, 'absent pool instruction'):
            pool_permission({}, {'pool_permitted': None, 'pool_command_w': 3000})
