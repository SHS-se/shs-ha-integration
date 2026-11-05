"""Synthetic owner failures never interrupt healthy siblings or require a new plan."""
import unittest
from unittest.mock import AsyncMock
import test_controller as fixtures


class IsolationTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ControllerTests.setUp

    async def started(self):
        self.options['device_modes'].update({'$ev': 'controlling', '$pool': 'controlling'})
        await self.controller.async_start()
        self.addAsyncCleanup(self.controller.async_stop)
        self.coordinator.async_execution_observed = AsyncMock()
        self.controller.execute_pool = AsyncMock(return_value={'state': 'scheduled'})

    async def test_binding_error_is_owner_local_and_recovers_under_same_plan(self):
        await self.started()
        original = self.coordinator.binding_plan_for
        def broken(device, options):
            if device == 'ev': raise ValueError('Synthetic invalid EV binding')
            return original(device, options)
        self.coordinator.binding_plan_for = broken
        await self.controller.async_tick()
        self.controller.execute_pool.assert_awaited_once()
        self.assertEqual(self.controller.status['ev']['state'], 'fault')
        self.assertTrue(self.controller.status['ev']['retry_automatically'])
        self.assertFalse(any(c.args[0] == 'ev' for c in self.coordinator.async_execution_observed.await_args_list))
        self.coordinator.binding_plan_for = original
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['ev']['state'], 'commanded')

    async def test_diagnostic_setup_failure_does_not_skip_the_next_owner(self):
        await self.started()
        original = self.controller.begin_diagnostic_evaluation
        def broken(device, *args):
            if device == 'ev': raise TypeError('Synthetic diagnostic shape')
            return original(device, *args)
        self.controller.begin_diagnostic_evaluation = broken
        await self.controller.async_tick()
        self.controller.execute_pool.assert_awaited_once()

    async def test_diagnostic_completion_failure_does_not_skip_the_next_owner(self):
        await self.started()
        original = self.controller.finish_diagnostic_evaluation
        async def broken():
            await original()
            if self.controller.device == 'ev': raise TypeError('Synthetic diagnostic completion')
        self.controller.finish_diagnostic_evaluation = broken
        await self.controller.async_tick()
        self.controller.execute_pool.assert_awaited_once()

    async def test_execution_evidence_failure_does_not_escape_its_owner(self):
        await self.started()
        async def broken(device, *args, **kwargs):
            if device == 'ev': raise RuntimeError('Synthetic evidence publication')
        self.coordinator.async_execution_observed.side_effect = broken
        await self.controller.async_tick()
        self.controller.execute_pool.assert_awaited_once()

    async def test_unclassified_value_error_retries_same_plan_without_physical_failure(self):
        await self.started()
        self.controller.execute_ev = AsyncMock(side_effect=[ValueError('Synthetic transient parse'), {'state':'commanded'}])
        await self.controller.async_tick()
        self.assertNotIn('ev', self.controller.failed)
        self.assertFalse(any(c.args[0] == 'ev' for c in self.coordinator.async_execution_observed.await_args_list))
        await self.controller.async_tick()
        self.assertEqual(self.controller.execute_ev.await_count,2)
        self.assertEqual(self.controller.status['ev']['state'],'commanded')

    async def test_corrupt_durable_journal_is_not_reclassified_as_one_device_failure(self):
        from shs_core.gateway_journal import JournalStorageError
        await self.started()
        self.controller.execute_ev = AsyncMock(side_effect=JournalStorageError('Synthetic corrupt control journal'))
        with self.assertRaises(JournalStorageError):
            await self.controller.async_tick()
        self.controller.execute_pool.assert_not_awaited()

    async def test_failed_status_listener_cannot_interrupt_owner_execution(self):
        await self.started()
        def broken():raise TypeError('Synthetic display subscriber')
        self.controller.add_listener(broken)
        await self.controller.async_tick()
        self.controller.execute_pool.assert_awaited_once()
