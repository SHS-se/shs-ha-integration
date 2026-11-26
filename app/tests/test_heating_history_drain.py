"""Independently synthetic durable history receipts and foreground scheduling."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from shs_app.heating_history_drain import CaptureRecord, CaptureSuperseded, HeatingHistoryDrain
from shs_core.household import Household
from shs_core.controller import ScheduledController
from shs_app.records import RecordStore
from shs_core.durable_record import DurableRecord


class HistoryDrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'synthetic-cloud'
        self.end = datetime(2026, 1, 2, tzinfo=timezone.utc)
        self.old = (self.end - timedelta(days=3)).isoformat()
        self.revision = 1
        self.fail = False
        self.partial = False
        self.capture_gate = None
        self.upload_gate = None
        self.bodies = []
        self.cursor_inputs = []
        self.errors = []
        self.h = SimpleNamespace(_push_lock=asyncio.Lock(), _recovering=False,
            ports=SimpleNamespace(utcnow=lambda:self.end),
            _prepared_device_inventory=AsyncMock(return_value=[]), resolved_options=lambda:{},
            async_replan_poll=AsyncMock(),client=SimpleNamespace(submit_optimisation=self.submit),
            _heating_history_upload=self.capture, _store=self.store())
        await self.h._store.async_save({'accepted_plan':{'commands':['retained-synthetic-command']},
            'heating_history_sources':{},'heating_history_cursor':{'rooms':{
                room:{'source_key':room,'signature':'s','before':self.old,'accepted_until':self.end.isoformat()}
                for room in ['room_a','room_b']}}})
        self.drain = self.new_drain()

    def store(self):
        return DurableRecord(RecordStore(self.path),lambda value:json.dumps(value).encode(),json.loads)

    def new_drain(self):
        return HeatingHistoryDrain(self.h,lambda:self.revision,self.errors.append,lambda _:False)

    async def capture(self, options, devices, end, cursor, **kwargs):
        self.cursor_inputs.append(deepcopy(cursor))
        if self.capture_gate:
            await self.capture_gate.wait()
        rooms = deepcopy(cursor['rooms'])
        for room in rooms.values():
            room['before'] = (datetime.fromisoformat(room['before']) - timedelta(hours=4)).isoformat()
        rows = [{'room_key':room,'source_key':room,'hour':room_cursor['before']} for room,room_cursor in rooms.items()]
        return rows, [], {'rooms':rooms,'deferred_rooms':[], 'probe_evidence':{},
            'probe_capture':{'windows':[], 'read_failed_windows':[]}}

    async def submit(self, body):
        self.bodies.append(deepcopy(body))
        if self.upload_gate:
            await self.upload_gate.wait()
        if self.fail:
            raise TimeoutError('Synthetic lost reply')
        accepted = body['heating_observations'][:1] if self.partial else body['heating_observations']
        return {'heating_observations_accepted':len(accepted),
            'heating_observations_accepted_by_source':[{'room_key':r['room_key'],
                'source_key':r['source_key'],'accepted':1} for r in accepted],
            'heating_probe_observations_accepted':0, 'heating_power_peaks_supported':True}

    async def test_successive_chunks_are_telemetry_only_and_keep_accepted_commands(self):
        self.assertTrue(await self.drain.step())
        self.assertTrue(await self.drain.step())
        stored = await self.h._store.async_load()
        self.assertEqual(stored['accepted_plan'],{'commands':['retained-synthetic-command']})
        before = stored['heating_history_cursor']['rooms']['room_a']['before']
        self.assertEqual(datetime.fromisoformat(before),datetime.fromisoformat(self.old)-timedelta(hours=8))
        for body in self.bodies:
            self.assertEqual(body['actual_slots'],[])
            self.assertEqual(body['devices'],[])
            for key in ['snapshot','device_inventory_complete','replan_request_id','equipment','commands']:
                self.assertNotIn(key,body)

    async def test_lost_reply_restart_resends_same_source_interval(self):
        self.fail = True
        with self.assertRaises(TimeoutError):
            await self.drain.step()
        self.h._store = self.store()  # Real disk read after a process restart.
        self.fail = False
        await self.new_drain().step()
        self.assertEqual(self.bodies[0],self.bodies[1])
        self.assertEqual(self.cursor_inputs[0],self.cursor_inputs[1])

    async def test_partial_receipt_commits_only_confirmed_source(self):
        self.partial = True
        with self.assertRaisesRegex(RuntimeError,'retry automatically'):
            await self.drain.step()
        rooms = (await self.store().async_load())['heating_history_cursor']['rooms']
        self.assertLess(rooms['room_a']['before'],self.old)
        self.assertEqual(rooms['room_b']['before'],self.old)
        self.partial = False
        await self.drain.step()
        self.assertLess((await self.store().async_load())['heating_history_cursor']['rooms']['room_b']['before'],self.old)

    async def test_configuration_change_during_capture_or_reply_cannot_commit_old_cursor(self):
        for phase in ['capture_gate','upload_gate']:
            with self.subTest(phase=phase):
                gate = asyncio.Event()
                setattr(self,phase,gate)
                task = asyncio.create_task(self.drain.step())
                while (not self.cursor_inputs if phase=='capture_gate' else not self.bodies):
                    await asyncio.sleep(0)
                self.revision += 1
                gate.set()
                await task
                self.assertEqual((await self.store().async_load())['heating_history_cursor']['rooms']['room_a']['before'],self.old)
                setattr(self,phase,None)
                self.cursor_inputs.clear()
                self.bodies.clear()

    async def test_foreground_poll_has_priority_and_prevents_another_chunk(self):
        self.upload_gate = asyncio.Event()
        background = asyncio.create_task(self.drain.step())
        while not self.bodies:
            await asyncio.sleep(0)
        foreground_gate = asyncio.Event()
        async def poll():
            self.assertFalse(self.h._push_lock.locked())
            await foreground_gate.wait()
        self.h.async_replan_poll.side_effect = poll
        foreground = asyncio.create_task(self.drain.plan_exchange())
        await asyncio.sleep(0)
        self.assertEqual(self.drain.foreground,1)
        self.upload_gate.set()
        await background
        await asyncio.sleep(0)
        self.assertFalse(await self.drain.step())
        self.assertEqual(len(self.bodies),1)
        foreground_gate.set()
        await foreground
        self.h.async_replan_poll.assert_awaited_once()

    async def test_failure_backoff_is_bounded_and_wake_cannot_spin(self):
        self.fail = True
        self.drain.backoff.jitter = lambda low,high:high
        delays = []
        async def sleep(delay):
            delays.append(delay)
            self.drain.wake.set()
            if len(delays)==6:
                raise asyncio.CancelledError()
        with patch('shs_app.heating_history_drain.asyncio.sleep',sleep):
            with self.assertRaises(asyncio.CancelledError):
                await self.drain.run()
        self.assertEqual(delays,[5,10,20,40,60,60])
        self.assertEqual(len(self.bodies),6)
        self.assertEqual(len(self.errors),6)

    async def test_caught_up_history_waits_and_configuration_change_discovers_new_source(self):
        stored = await self.h._store.async_load()
        for row in stored['heating_history_cursor']['rooms'].values():
            row['before']=(self.end-timedelta(days=400)).isoformat()
        await self.h._store.async_save(stored)
        self.drain.checked_revision=self.revision
        self.assertFalse(await self.drain.step())
        self.assertEqual(self.bodies,[])
        self.revision+=1
        await self.drain.step()
        self.assertEqual(len(self.bodies),1)

    async def test_probe_repair_requires_its_own_receipt_and_survives_restart(self):
        start = (self.end - timedelta(days=3)).isoformat()
        finish = (self.end - timedelta(days=3) + timedelta(hours=4)).isoformat()
        stored = await self.h._store.async_load()
        stored['heating_history_cursor']['probe_evidence']={'repair_windows':[[start,finish]]}
        await self.h._store.async_save(stored)
        original_capture=self.h._heating_history_upload
        async def capture(*args,**kwargs):
            rows,_,pending=await original_capture(*args,**kwargs)
            pending['probe_evidence']={'repair_windows':[[start,finish]]}
            pending['probe_capture']={'windows':[[start,finish]],'repair_window':[start,finish],
                                      'read_failed_windows':[]}
            return rows,[{'source_key':'synthetic-probe','start':start}],pending
        self.h._heating_history_upload=capture
        with self.assertRaises(RuntimeError):
            await self.drain.step()  # Thermal ACK alone does not clear probe repair.
        self.h._store=self.store()
        self.assertEqual((await self.h._store.async_load())['heating_history_cursor']['probe_evidence']['repair_windows'],[[start,finish]])
        original_submit=self.h.client.submit_optimisation
        async def submit(body):
            return {**await original_submit(body),'heating_probe_observations_accepted':1}
        self.h.client.submit_optimisation=submit
        await self.new_drain().step()
        self.assertEqual((await self.store().async_load())['heating_history_cursor']['probe_evidence']['repair_windows'],[])

    async def test_real_controller_execution_journal_does_not_wait_for_history_io(self):
        self.h.control_authority=True
        self.h.resolved_options=lambda:{'device_modes':{'$ev':'controlling'}}
        self.h._suppressed_execution_owners=lambda _:set()
        self.h._planning_job_wake=asyncio.Event()
        self.h.async_update_listeners=Mock()
        self.h.async_execution_observed=MethodType(Household.async_execution_observed,self.h)
        controller=SimpleNamespace(verifying=False,restoring=False,active_options=self.h.resolved_options(),coordinator=self.h)
        for phase in ['capture_gate','upload_gate']:
            with self.subTest(phase=phase):
                gate=asyncio.Event()
                setattr(self,phase,gate)
                background=asyncio.create_task(self.drain.step())
                while (not self.cursor_inputs if phase=='capture_gate' else not self.bodies):
                    await asyncio.sleep(0)
                await asyncio.wait_for(ScheduledController.record_execution(controller,'ev',
                    {'plan_id':'synthetic-accepted-plan'},
                    {'start':self.end.isoformat(),'ev_target_current_a':10},
                    {'status':'temporarily_absent','cause':'synthetic-'+phase}),.5)
                self.assertFalse(background.done())
                journal=(await self.store().async_load())['execution_replanning']
                self.assertEqual(journal['owners']['$ev']['cause'],'synthetic-'+phase)
                gate.set()
                await background
                self.assertEqual((await self.store().async_load())['execution_replanning'],journal)
                setattr(self,phase,None)
                self.cursor_inputs.clear()
                self.bodies.clear()

    async def test_collector_ledger_merge_is_source_fenced_and_preserves_other_writers(self):
        captured=await self.h._store.async_load()
        record=CaptureRecord(self.drain,self.revision,captured)
        current=await self.h._store.async_load()
        current['execution_replanning']={'synthetic':'newer-than-capture'}
        await self.h._store.async_save(current)
        captured['counter_peak_evidence']={'synthetic-source':{'qualified':True}}
        await record.async_save(captured)
        saved=await self.store().async_load()
        self.assertEqual(saved['execution_replanning'],current['execution_replanning'])
        self.assertEqual(saved['counter_peak_evidence'],captured['counter_peak_evidence'])
        self.revision+=1
        captured['counter_peak_evidence']={'obsolete':True}
        with self.assertRaises(CaptureSuperseded):
            await record.async_save(captured)
        self.assertEqual(await self.store().async_load(),saved)

    async def test_foreground_source_progress_during_upload_cannot_be_rewound(self):
        self.upload_gate=asyncio.Event()
        background=asyncio.create_task(self.drain.step())
        while not self.bodies:
            await asyncio.sleep(0)
        async with self.h._push_lock:
            stored=await self.h._store.async_load()
            stored['heating_history_cursor']['rooms']['room_a']['before']=(self.end-timedelta(days=20)).isoformat()
            await self.h._store.async_save(stored)
        self.upload_gate.set()
        self.assertTrue(await background)
        self.assertEqual(await self.store().async_load(),stored)
