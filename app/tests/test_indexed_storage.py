"""Differential accounting against the original immutable reference, including old views."""
import asyncio
from dataclasses import replace
from pathlib import Path
import random
import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.append(str(Path(__file__).parents[2]/'tests'))
from test_plan_execution import contract,meter,QUARTER
from test_execution_storage import META,session
from shs_core import plan_execution as ex
from shs_core.execution_storage import ExecutionStorage
from shs_core.home_runtime import ExecutionSession
from shs_app.sources import ObservationMirror
from shs_app.indexed_storage import IndexedStorage
from shs_app.indexed_evidence import EvidenceRows,DAY,BOUNDARY_EDGES


class IndexedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'execution.sqlite'
        self.store=IndexedStorage(self.path,asyncio.to_thread,ObservationMirror())
        _,state=await self.store.load();self.account=state.account

    async def save(self,account):
        await self.store.save(META,ExecutionSession(account=account))

    async def test_unordered_corrections_resets_and_boundary_blocks_match_reference(self):
        reference=ex.Account();indexed=self.account
        rng=random.Random(187)
        old=[]
        for number in range(85):
            at=rng.choice([0,DAY,2*DAY,3*DAY,rng.randrange(4*DAY)])
            kwargs=dict(event_id=str(number),stream='charge',direction='charge',boundary='battery_dc',
                epoch=str(number//22),source_at_ms=at,total_mwh=rng.randrange(500000))
            reference=ex.record_meter(reference,**kwargs);indexed=ex.record_meter(indexed,**kwargs)
            for start,end in ((0,4*DAY),(DAY,2*DAY),(DAY+1,3*DAY-1),(0,at)):
                self.assertEqual(indexed.meter_index.measure('charge',start,end),reference.meter_index.measure('charge',start,end),(number,start,end))
            self.assertEqual(indexed.meter_index.neighbours('charge',at),reference.meter_index.neighbours('charge',at))
            if number%20==0:old.append((reference,indexed))
            await self.save(indexed)
            self.assertLessEqual(len(indexed.meters.tail),1)
        for reference,indexed in old:
            self.assertEqual(indexed.meter_index.measure('charge',0,4*DAY),reference.meter_index.measure('charge',0,4*DAY))
        reopened=IndexedStorage(self.path,asyncio.to_thread,ObservationMirror())
        _,state=await reopened.load()
        self.assertIsInstance(state.account.meters,EvidenceRows)
        self.assertEqual(state.account.meters,tuple(self.store._session.account.meters))

    async def test_objective_feedback_late_corrections_observations_and_reconciliation_match(self):
        reference=ex.Account();indexed=self.account
        async def transition(operation):
            nonlocal reference,indexed
            reference=operation(reference);indexed=operation(indexed)
            for at in (0,QUARTER//2,QUARTER):
                self.assertEqual(ex.planner_feedback(indexed,at),ex.planner_feedback(reference,at))
                self.assertEqual(ex.live_feedback(indexed,at),ex.live_feedback(reference,at))
            await self.save(indexed)
        for direction in ('charge','discharge'):
            await transition(lambda account:meter(account,direction,0,0))
        await transition(lambda account:ex.admit_plan(account,contract(),0,ex.StateObservation(0,5000000,'soc')))
        await transition(lambda account:meter(account,'charge',QUARTER,100000))
        await transition(lambda account:meter(account,'discharge',QUARTER,0))
        await transition(lambda account:ex.observe_state(account,ex.StateObservation(QUARTER,5300000,'soc')))
        captured=indexed
        await transition(ex.request_replan)
        next_plan=replace(contract(generation=1,previous='plan-0',dispositions=(ex.Disposition('cheap-window','retained',None,'same goal'),)),
            source_receipt=reference.receipt,capacity_mwh=12000000)
        await transition(lambda account:ex.admit_plan(account,next_plan,QUARTER//2,ex.StateObservation(QUARTER//2,5100000,'soc')))
        await transition(lambda account:meter(account,'charge',QUARTER,800000))
        self.assertEqual(ex.planner_feedback(captured,QUARTER)['objectives'][0]['outcome'],'missed')
        await transition(lambda account:ex.observe_state(account,ex.StateObservation(QUARTER,5600000,'corrected soc')))
        await transition(lambda account:meter(account,'charge',QUARTER,800000))

    async def test_one_way_upgrade_and_warm_restore_do_not_hydrate_history(self):
        original_path=Path(self.temp.name)/'old.sqlite'
        legacy=ExecutionStorage(original_path,asyncio.to_thread);await legacy.load()
        original=session(1200)
        await legacy.save(META,original)
        indexed=IndexedStorage(original_path,asyncio.to_thread,ObservationMirror())
        _,state=await indexed.load()
        self.assertEqual(ex.planner_feedback(state.account,QUARTER),ex.planner_feedback(original.account,QUARTER))
        self.assertTrue(original_path.with_name(original_path.name+'.pre-indexed').exists())
        second=IndexedStorage(original_path,asyncio.to_thread,ObservationMirror())
        with patch('shs_app.indexed_evidence.decode_row',side_effect=AssertionError('History hydrated during startup')):
            _,restored=await second.load()
        self.assertEqual(len(restored.account.meters),1200)
        self.assertEqual(restored.account.meters.tail,())

    async def test_indexed_account_runs_the_actual_battery_host(self):
        from test_battery_runtime import Rig
        from shs_app.runtime import AppBatteryRuntime
        rig=Rig()
        runtime=AppBatteryRuntime(rig.coordinator,rig.controller,self.store,lambda:rig.now)
        rig.coordinator.battery_runtime=runtime;rig.runtime=runtime
        self.addAsyncCleanup(runtime.close,release=False)
        # The runtime owns loading its store; use a fresh instance over the same DB.
        runtime.store=IndexedStorage(self.path,asyncio.to_thread,ObservationMirror())
        rig.store=runtime.store
        rig.fence._identity=runtime.identity
        await rig.start()
        self.assertIsInstance(runtime.host.state.execution.account.meters,EvidenceRows)
        self.assertTrue(runtime.host.state.execution.account.contract, runtime._status)
        self.assertTrue(rig.calls)

    async def test_interrupted_index_verification_resumes_without_changing_source_facts(self):
        path=Path(self.temp.name)/'interrupted.sqlite'
        original=session(32)
        legacy=ExecutionStorage(path,asyncio.to_thread);await legacy.load();await legacy.save(META,original)
        indexed=IndexedStorage(path,asyncio.to_thread,ObservationMirror())
        from shs_app.indexed_evidence import IndexedMeters
        with patch.object(IndexedMeters,'measure',side_effect=RuntimeError('interrupted verification')):
            with self.assertRaisesRegex(RuntimeError,'interrupted verification'):await indexed.load()
        import sqlite3
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT verified FROM indexed_evidence_schema').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM meters').fetchone()[0],32)
        archive=path.with_name(path.name+'.pre-indexed').read_bytes()
        _,restored=await IndexedStorage(path,asyncio.to_thread,ObservationMirror()).load()
        self.assertEqual(tuple(restored.account.meters),original.account.meters)
        self.assertEqual(path.with_name(path.name+'.pre-indexed').read_bytes(),archive)

    async def test_failed_index_delta_rolls_back_account_and_materialized_edges_together(self):
        before=self.store._revision
        account=meter(self.account,'charge',0,0)
        with patch('shs_app.indexed_storage.index_meter',side_effect=OSError('index disk failure')):
            with self.assertRaisesRegex(OSError,'index disk failure'):await self.save(account)
        self.assertEqual(self.store._revision,before)
        fresh=IndexedStorage(self.path,asyncio.to_thread,ObservationMirror())
        _,restored=await fresh.load()
        self.assertEqual(len(restored.account.meters),0)
        self.assertEqual(restored.account.meter_index.measure('charge',0,QUARTER),ex.Account().meter_index.measure('charge',0,QUARTER))
        await self.save(account)
        self.assertEqual(len(self.store._session.account.meters),1)

    async def test_live_objectives_share_one_reader_and_use_boundary_bucket_index(self):
        account=self.account
        for direction in ('charge','discharge'):
            account=meter(account,direction,0,0)
        account=ex.admit_plan(account,contract(),0,ex.StateObservation(0,5000000,'soc'))
        account=meter(account,'charge',QUARTER,100000)
        account=meter(account,'discharge',QUARTER,0)
        await self.save(account)
        with closing(sqlite3.connect(self.path)) as db:
            plan=db.execute('EXPLAIN QUERY PLAN '+BOUNDARY_EDGES,dict(stream='charge',bucket=0,start=0,end=QUARTER)).fetchall()
            self.assertTrue(any('meter_edge_bounds' in row[-1] for row in plan))
        connect=sqlite3.connect
        with patch('shs_app.indexed_evidence.sqlite3.connect', wraps=connect) as connections:
            rows,count=account._evidence.live_objectives(account,QUARTER)
        self.assertEqual(connections.call_count,1)
        self.assertGreater(count,0)

    async def test_boundary_reuse_is_scoped_to_snapshot_and_preserves_late_corrections(self):
        indexed=self.account;reference=ex.Account()
        for at,total in ((0,0),(DAY//2,100),(DAY-1,300),(2*DAY,500)):
            indexed=meter(indexed,'charge',at,total)
            reference=meter(reference,'charge',at,total)
        await self.save(indexed)
        with self.store.evidence.snapshot():
            for end in (DAY+1,2*DAY):
                self.assertEqual(indexed.meter_index.measure('charge',100,end),reference.meter_index.measure('charge',100,end))
        self.assertIsNone(self.store.evidence.boundaries.get())
        before=indexed.meter_index.measure('charge',100,DAY+1)
        indexed=meter(indexed,'charge',DAY//2,200)
        reference=meter(reference,'charge',DAY//2,200)
        await self.save(indexed)
        after=indexed.meter_index.measure('charge',100,DAY+1)
        self.assertEqual(after,reference.meter_index.measure('charge',100,DAY+1))
        self.assertNotEqual(before,after)

    async def test_covering_index_upgrade_preserves_verified_facts_without_history_hydration(self):
        account=meter(meter(self.account,'charge',0,0),'charge',DAY//2,100)
        await self.save(account)
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('DROP INDEX meter_edge_bounds')
            db.execute('CREATE INDEX meter_edge_bucket ON meter_edges(stream,bucket,right_ms)')
            before=db.execute('SELECT revision,metadata,counts FROM head').fetchone()
        reopened=IndexedStorage(self.path,asyncio.to_thread,ObservationMirror())
        with patch('shs_app.indexed_evidence.decode_row',side_effect=AssertionError('History hydrated')):
            _,restored=await reopened.load()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT revision,metadata,counts FROM head').fetchone(),before)
            self.assertEqual(db.execute('SELECT version,verified FROM indexed_evidence_schema').fetchone(),(1,1))
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='meter_edge_bucket'").fetchone())
            plan=db.execute('EXPLAIN QUERY PLAN '+BOUNDARY_EDGES,dict(stream='charge',bucket=0,start=0,end=DAY)).fetchall()
            self.assertTrue(any('COVERING INDEX meter_edge_bounds' in row[-1] for row in plan))
        self.assertEqual(restored.account.meter_index.measure('charge',0,DAY//2),account.meter_index.measure('charge',0,DAY//2))
