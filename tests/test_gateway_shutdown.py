"""Synthetic shutdown races; durable SQLite outcomes and native ownership stay true."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest

import test_gateway_stream as stream_fixtures
import test_gateway_metadata as source_fixtures
import test_battery_gateway as battery_fixtures
from shs_core.gateway_journal import GatewayConflict
from shs_core.gateway_service import AppConnection, GatewayService
from shs_core.gateway_stream import GatewayStream


class StreamShutdownTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = stream_fixtures.StreamTests.asyncSetUp
    connect = stream_fixtures.StreamTests.connect

    def gate(self, operation):
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        original = self.stream.executor
        async def execute(function,*args):
            result = await original(function,*args)
            if function.__name__ == operation:
                entered.set()
                await release.wait()
            return result
        self.stream.executor=execute
        return entered,release

    async def test_cancelled_concurrent_close_drains_disconnect_and_full_queue(self):
        await self.connect()
        entered,release=self.gate('disconnect')
        peer=asyncio.create_task(self.connection.close())
        await asyncio.wait_for(entered.wait(),1)
        self.stream.queue._maxsize=1
        queued=asyncio.create_task(self.stream.call('save_record','entity_catalogue',{'value':7}))
        await asyncio.sleep(0)
        first=asyncio.create_task(self.stream.close())
        second=asyncio.create_task(self.stream.close())
        await asyncio.sleep(0)
        for _ in range(3):
            first.cancel()
            await asyncio.sleep(0)
        self.assertFalse(self.stream.task.cancelled())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(first,1)
        await asyncio.wait_for(asyncio.gather(second,peer,queued),1)
        self.assertEqual(self.journal.load_record('entity_catalogue'),{'value':7})
        with self.journal.connect(readonly=True) as db:
            self.assertIsNone(db.execute('SELECT session FROM authority').fetchone()[0])
        await asyncio.wait_for(self.stream.queue.join(),1)
        await self.stream.close()

    async def test_unexpected_worker_cancellation_rejects_receivers_after_write_settles(self):
        await self.connect()
        entered,release=self.gate('disconnect')
        peer=asyncio.create_task(self.connection.close())
        await asyncio.wait_for(entered.wait(),1)
        queued=asyncio.create_task(self.stream.call('save_record','source_admission',{'value':1}))
        await asyncio.sleep(0)
        worker=self.stream.task
        with self.assertLogs('shs_core.gateway_stream',level='ERROR'):
            for _ in range(3):
                worker.cancel()
                await asyncio.sleep(0)
            release.set()
            for receiver in (peer,queued):
                with self.assertRaises(GatewayConflict):
                    await asyncio.wait_for(receiver,1)
            with self.assertRaises(asyncio.CancelledError):
                await self.stream.close()
        self.assertIsNone(self.journal.load_record('source_admission'))
        with self.journal.connect(readonly=True) as db:
            self.assertIsNone(db.execute('SELECT session FROM authority').fetchone()[0])
        await asyncio.wait_for(self.stream.queue.join(),1)

    async def test_worker_cancelled_before_start_rejects_already_accepted_work(self):
        await self.stream.close()
        self.stream=GatewayStream(self.journal)
        self.stream.start()
        accepted=self.stream.capture('observation',{'entity_id':'sensor.synthetic','state':'1'})
        with self.assertLogs('shs_core.gateway_stream',level='ERROR'):
            self.stream.task.cancel()
            with self.assertRaises(GatewayConflict):
                await asyncio.wait_for(accepted,1)
            with self.assertRaises(asyncio.CancelledError):
                await self.stream.close()
        await asyncio.wait_for(self.stream.queue.join(),1)

    async def test_listener_crash_settles_batch_lookahead_and_queued_receivers(self):
        await self.connect()
        entered,release=self.gate('save_record')
        barrier=asyncio.create_task(self.stream.call('save_record','house_import_limit',{}))
        await asyncio.wait_for(entered.wait(),1)
        facts=[self.stream.capture('observation',{'entity_id':'sensor.synthetic','state':str(i)}) for i in range(2)]
        lookahead=asyncio.create_task(self.stream.call('load_record','house_import_limit'))
        await asyncio.sleep(0)
        tail=self.stream.capture('observation',{'entity_id':'sensor.tail','state':'0'})
        def broken():raise RuntimeError('synthetic notification failure')
        self.stream.listeners.add(broken)
        with self.assertLogs('shs_core.gateway_stream',level='ERROR'):
            release.set()
            await barrier
            for receiver in (*facts,lookahead,tail):
                with self.assertRaises(GatewayConflict):
                    await asyncio.wait_for(receiver,1)
            with self.assertRaisesRegex(RuntimeError,'synthetic notification'):
                await self.stream.close()
        snapshot=self.journal.snapshot(self.connection.session)
        self.assertEqual(snapshot['observations']['sensor.synthetic']['value']['state'],'1')
        self.assertNotIn('sensor.tail',snapshot['observations'])
        await asyncio.wait_for(self.stream.queue.join(),1)

    async def test_cancelled_socket_close_does_not_abandon_accepted_disconnect(self):
        await self.connect()
        entered,release=self.gate('disconnect')
        peer=asyncio.create_task(self.connection.close())
        await asyncio.wait_for(entered.wait(),1)
        peer.cancel()
        with self.assertRaises(asyncio.CancelledError):await peer
        release.set()
        await asyncio.wait_for(self.stream.close(),1)
        with self.journal.connect(readonly=True) as db:
            self.assertIsNone(db.execute('SELECT session FROM authority').fetchone()[0])


class SourceShutdownTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = source_fixtures.MetadataTests.asyncSetUp

    async def test_closed_source_ignores_state_and_metadata_callbacks(self):
        self.source.closed=True
        await self.stream.close()
        before=(self.source.live_revision,self.source.intake_counts.copy(),len(self.tasks))
        self.source.entities={'sensor.synthetic'}
        self.source.power_entities=set()
        self.source.execution_source=lambda _:True
        self.source.report=lambda *args,**kwargs:{'entity_id':'sensor.synthetic','state':'0','attributes':{}}
        self.service.physical.observation_changed=asyncio.Event()
        with self.assertNoLogs(level='ERROR'):
            self.hass.fire('state_changed',entity_id='sensor.synthetic',new_state=None)
            self.hass.fire('state_reported',entity_id='sensor.synthetic',new_state=None)
            self.hass.fire(source_fixtures.CORE_CONFIG_UPDATE)
            self.hass.fire(source_fixtures.REGISTRY_UPDATED)
            await asyncio.sleep(0)
        self.assertEqual((self.source.live_revision,self.source.intake_counts,len(self.tasks)),before)
        self.assertFalse(self.service.physical.observation_changed.is_set())

    async def test_accepted_observation_completion_does_not_restart_closed_source(self):
        entered,release=asyncio.Event(),asyncio.Event()
        self.addCleanup(release.set)
        original=self.stream.executor
        async def gate(function,*args):
            result=await original(function,*args)
            if function.__name__=='record':
                entered.set()
                await release.wait()
            return result
        self.stream.executor=gate
        self.source.entities={'sensor.synthetic'}
        self.source.capacity_entities={'sensor.synthetic'}
        self.source.power_entities=set()
        self.source.execution_source=lambda _:True
        self.source.report=lambda *args,**kwargs:{'entity_id':'sensor.synthetic','state':'0','attributes':{}}
        self.service.physical.observation_changed=asyncio.Event()
        before=len(self.tasks)
        self.hass.fire('state_changed',entity_id='sensor.synthetic',new_state=None)
        await asyncio.wait_for(entered.wait(),1)
        self.source.closed=True
        release.set()
        await self.stream.close()
        self.assertEqual(len(self.tasks),before)
        self.assertFalse(self.source.capacity_refresh_pending)


class NativeShutdownTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = battery_fixtures.BatteryGatewayTests.asyncSetUp
    send = battery_fixtures.BatteryGatewayTests.send
    step = battery_fixtures.BatteryGatewayTests.step

    async def test_worker_failure_after_native_outcome_commit_keeps_truth_on_reopen(self):
        entered,release=asyncio.Event(),asyncio.Event()
        self.addCleanup(release.set)
        original=self.stream.executor
        async def gate(function,*args):
            result=await original(function,*args)
            if function.__name__=='finish_command':
                entered.set()
                await release.wait()
            return result
        self.stream.executor=gate
        sending=asyncio.create_task(self.step())
        await asyncio.wait_for(entered.wait(),1)
        with self.assertLogs('shs_core.gateway_stream',level='ERROR'):
            self.stream.task.cancel()
            release.set()
            with self.assertRaises(GatewayConflict):await asyncio.wait_for(sending,1)
            with self.assertRaises(asyncio.CancelledError):await self.stream.close()
        self.assertEqual(len(self.calls),1)
        command='route:'+self.route['route_id']+':0:attempt-0'
        self.journal.close()
        self.journal.open()
        self.assertEqual(self.journal.command_outcome(command)['status'],'service_returned')

    async def test_service_close_is_single_cancellation_safe_owner_without_handback(self):
        await self.step()
        before_calls=deepcopy(self.calls)
        before_settings=deepcopy(self.rows)
        source=SimpleNamespace(closed=False)
        service=GatewayService(self.stream,{},source)
        service.battery=self.gateway
        service.physical=self.physical
        async def maintain():pass
        service.physical.maintain_obligations=maintain
        peer=AppConnection(service)
        peer.session=self.session
        service.connection=peer
        entered,release=asyncio.Event(),asyncio.Event()
        self.addCleanup(release.set)
        original=self.stream.executor
        async def gate(function,*args):
            result=await original(function,*args)
            if function.__name__=='disconnect':
                entered.set()
                await release.wait()
            return result
        self.stream.executor=gate
        first=asyncio.create_task(service.close())
        await asyncio.wait_for(entered.wait(),1)
        second=asyncio.create_task(service.close())
        for _ in range(3):
            first.cancel()
            await asyncio.sleep(0)
        self.assertTrue(source.closed)
        self.assertFalse(service.active)
        self.assertIsNotNone(self.journal.lease)
        release.set()
        with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(first,1)
        await asyncio.wait_for(second,1)
        await service.close()
        self.assertIsNone(self.journal.lease)
        self.assertEqual(self.calls,before_calls)
        self.assertEqual(self.rows,before_settings)

    async def test_cancelled_background_obligation_still_releases_journal(self):
        source=SimpleNamespace(closed=False)
        service=GatewayService(self.stream,{},source)
        service.battery=self.gateway
        service.physical=self.physical
        async def maintain():pass
        service.physical.maintain_obligations=maintain
        service.obligation_task=asyncio.create_task(asyncio.Event().wait())
        service.obligation_task.cancel()
        await service.close()
        self.assertIsNone(self.journal.lease)
        self.assertEqual(self.calls,[])
