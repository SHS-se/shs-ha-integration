"""Writer handover persists the fence before any grant and never auto-reverts."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from battery_writer import BatteryWriterFence
from home_runtime import WriterIdentity
import test_controller as fixtures


class WriterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=fixtures.Store()
        self.options=dict(battery_mode_entity='select.mode',battery_charge_limit_entity='number.charge',battery_discharge_limit_entity='number.discharge')
        self.identity=WriterIdentity('host','config-1','surface-1')
        self.now=1000
        self.lock=asyncio.Lock()
        self.fence=self.make()
        await self.fence.open()

    def make(self):
        return BatteryWriterFence(self.store,self.lock,lambda:self.options,lambda:self.now,lambda:self.identity)

    async def test_handover_drains_shared_lock_and_persists_before_grant(self):
        writes=[]
        save=self.store.async_save
        async def persist(value):
            writes.append(deepcopy(value))
            self.assertFalse(self.fence.snapshot()['grant_current'])
            with self.assertRaises(ValueError): self.fence.check_legacy('select.mode')
            await save(value)
        self.store.async_save=persist
        release=AsyncMock()
        await self.lock.acquire()
        task=asyncio.create_task(self.fence.take_over(self.identity,5000,release))
        await asyncio.sleep(0)
        release.assert_not_awaited()
        self.fence.check_legacy('select.mode')
        self.lock.release()
        grant=await task
        release.assert_awaited_once()
        self.assertEqual([r['owner'] for r in writes],['fenced','runtime'])
        self.assertTrue(self.fence.is_current(grant,self.identity))
        self.fence.check_legacy('switch.unrelated')
        with self.assertRaises(ValueError): self.fence.check_legacy('select.mode')
        self.now=5000
        self.assertFalse(self.fence.is_current(grant,self.identity))
        with self.assertRaises(ValueError): self.fence.check_legacy('select.mode')

    async def test_restart_fences_old_runtime_and_does_not_restore_legacy(self):
        await self.fence.take_over(self.identity,5000,AsyncMock())
        restarted=self.make()
        await restarted.open()
        self.assertEqual(self.store.saved['owner'],'fenced')
        self.assertFalse(restarted.snapshot()['grant_current'])
        with self.assertRaises(ValueError): restarted.check_legacy('number.charge')
        with self.assertRaises(ValueError): await restarted.take_over(WriterIdentity('host','wrong','surface-1'),5000,AsyncMock())

    async def test_configuration_change_during_persistence_rejects_grant(self):
        async def persist(value):
            self.store.saved=deepcopy(value)
            self.identity=WriterIdentity('host','config-2','surface-2')
        self.store.async_save=persist
        with self.assertRaises(ValueError):
            await self.fence.take_over(self.identity,5000,AsyncMock())
        self.assertFalse(self.fence.snapshot()['grant_current'])
        with self.assertRaises(ValueError): self.fence.check_legacy('select.mode')

    async def test_configuration_change_after_grant_invalidates_final_check(self):
        old=self.identity
        grant=await self.fence.take_over(old,5000,AsyncMock())
        self.identity=WriterIdentity('host','config-2','surface-2')
        self.assertFalse(self.fence.is_current(grant,old))
        self.assertFalse(self.fence.snapshot()['grant_current'])

    async def test_failed_save_and_shutdown_never_resurrect_old_owner(self):
        self.store.async_save=AsyncMock(side_effect=OSError('uncertain disk write'))
        with self.assertRaises(OSError): await self.fence.take_over(self.identity,5000,AsyncMock())
        with self.assertRaises(ValueError): self.fence.check_legacy('select.mode')
        self.fence.close()
        self.assertFalse(self.fence.snapshot()['grant_current'])

    async def test_corrupt_or_unreadable_journal_rejects_setup(self):
        self.store.saved={'owner':'legacy'}
        fence=self.make()
        with self.assertRaises(ValueError): await fence.open()
        with self.assertRaises(ValueError): fence.check_legacy('select.mode')
        self.store.async_load=AsyncMock(side_effect=OSError('unreadable'))
        fence=self.make()
        with self.assertRaises(OSError): await fence.open()
        with self.assertRaises(ValueError): fence.check_legacy('select.mode')

    async def test_no_admitted_identity_cannot_acquire_writer(self):
        self.identity=None
        with self.assertRaises(ValueError):
            await self.fence.take_over(WriterIdentity('host','config','surface'),5000,AsyncMock())
        self.assertEqual(self.fence.snapshot()['owner'],'legacy')

    async def test_legacy_shutdown_restoration_is_fenced_but_other_devices_continue(self):
        fixture=fixtures.ControllerTests()
        fixture.setUp()
        self.options=fixture.options
        self.lock=fixture.controller.lock
        self.fence=self.make()
        await self.fence.open()
        fixture.controller.battery_writer_fence=self.fence
        fixture.options['device_modes']['$battery']='controlling'
        await fixture.controller.async_start()
        self.assertTrue(fixture.calls)
        await self.fence.take_over(self.identity,5000,AsyncMock())
        fixture.calls.clear()
        await fixture.controller.async_stop()
        self.assertFalse(fixture.calls)
        self.assertEqual(fixture.states['select.mode'].state,'Charge')
