"""Callback order, bounded queues and disconnect/cancellation boundaries."""
import asyncio
import tempfile
import unittest
from unittest.mock import patch

from gateway_fixture import IDENTITY, seed
from shs_core.gateway_journal import GatewayConflict
from shs_core.gateway_stream import GatewayConnection, GatewayStream


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        _, self.journal = seed(self.temp.name)
        self.journal.open()
        self.addCleanup(self.journal.close)
        self.stream = GatewayStream(self.journal)
        self.stream.start()
        self.addAsyncCleanup(self.stream.close)
        self.connection = GatewayConnection(self.stream)

    async def connect(self):
        return (await self.connection.request(dict(id=1, operation='connect', body=dict(identity=IDENTITY, instance='app'))))['result']

    async def test_callbacks_capture_mutable_data_before_await_and_keep_arrival_order(self):
        await self.connect()
        payload = {'entity_id':'sensor.power', 'source_at':100, 'value':1}
        first = self.stream.capture('observation', payload)
        payload.update(value=2, source_at=50)
        second = self.stream.capture('observation', payload)
        payload['value'] = 3
        self.assertLess(await first, await second)
        page = (await self.connection.request(dict(id=2, operation='receipts', body=dict(after=0, limit=100))))['result']
        self.assertEqual([r['payload']['value'] for r in page['receipts'][-2:]], [1,2])
        with self.assertRaises(ValueError):
            await self.connection.request(dict(id=3, operation='service', body={'entity':'switch.pool'}))
        with self.assertRaises(ValueError):
            await self.connection.request(dict(id=4, operation='activate', body={}))

    async def test_adjacent_facts_share_one_commit_and_snapshot_barrier_keeps_order(self):
        await self.connect()
        notices=[];self.stream.listeners.add(lambda:notices.append(self.stream.metrics['facts']))
        before=self.stream.metrics['fact_commits']
        first=[self.stream.capture('observation',{'entity_id':'sensor.a','state':str(i)}) for i in range(20)]
        snapshot=asyncio.create_task(self.stream.call('snapshot',self.connection.session))
        await asyncio.sleep(0)
        second=self.stream.capture('observation',{'entity_id':'sensor.a','state':'later'})
        ordinals=await asyncio.gather(*first)
        captured=await snapshot
        later=await second
        self.assertEqual(ordinals,list(range(ordinals[0],ordinals[0]+20)))
        self.assertEqual(captured['through'],ordinals[-1])
        self.assertGreater(later,captured['through'])
        self.assertEqual(self.stream.metrics['fact_commits']-before,2)
        self.assertEqual(len(notices),2)

    async def test_failed_batch_rolls_back_every_fact_and_sends_no_notification(self):
        await self.connect()
        notices=[];self.stream.listeners.add(lambda:notices.append(True))
        first=self.stream.capture('observation',{'entity_id':'sensor.a'})
        invalid=self.stream.capture('observation',{})
        for pending in (first,invalid):
            with self.assertRaises(ValueError):await pending
        self.assertEqual(self.journal.snapshot(self.connection.session)['observations'],{})
        self.assertEqual(notices,[])

    async def test_cancelled_connect_settles_and_revokes_its_committed_session(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(fn, *args):
            result = await asyncio.to_thread(fn, *args)
            entered.set()
            await release.wait()
            return result
        self.stream.executor = blocked
        task = asyncio.create_task(self.connect())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.connection.closed)
        self.assertIsNone(self.connection.session)
        with self.journal.connect(readonly=True) as db:
            self.assertIsNone(db.execute('SELECT session FROM authority').fetchone()[0])

    async def test_failed_persistence_faults_queued_requests_and_never_publishes_receipt(self):
        await self.connect()
        with self.assertLogs('shs_core.gateway_stream', level='ERROR') as logs, patch.object(self.journal, 'record', side_effect=OSError('disk full')):
            saving = self.stream.capture('observation', {'entity_id':'sensor.power'})
            request = asyncio.create_task(self.connection.request(dict(id=2, operation='snapshot', body={})))
            with self.assertRaises(OSError):
                await saving
            with self.assertRaisesRegex(GatewayConflict, 'OSError: disk full'):
                await request
            with self.assertRaisesRegex(GatewayConflict, 'OSError: disk full') as rejected:
                self.stream.capture('configuration', {})
            self.assertIs(rejected.exception.__cause__, self.stream.failure)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('Traceback', logs.output[0])
        self.assertIn('OSError: disk full', logs.output[0])
        self.assertEqual(self.journal.snapshot(self.connection.session)['observations'], {})

    async def test_bounded_queue_exhaustion_fails_closed_instead_of_silently_dropping(self):
        await self.stream.close()
        self.stream = GatewayStream(self.journal, capacity=1)
        self.stream.start()
        self.addAsyncCleanup(self.stream.close)
        first = self.stream.capture('configuration', {})
        with self.assertRaisesRegex(GatewayConflict, 'exhausted'):
            self.stream.capture('configuration', {})
        with self.assertRaises(GatewayConflict):
            await first
        self.assertFalse(self.stream.accepting)
