"""Historical receipts retain accounting without reauthorizing live feedback."""
import sys
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
import json
from shs_core.home_runtime import ExecutionTrace, TRACE_RETENTION_MS
from shs_core.controller_diagnostics import report_parts, gzip_report
import gzip
import unittest
from unittest.mock import AsyncMock, patch

sys.path.append(str(Path(__file__).parents[2]/'tests'))
from test_battery_runtime import Rig
from shs_app.runtime import AppBatteryRuntime
from shs_core.battery_runtime import BatteryRuntime, iso
from gateway_fixture import IDENTITY


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def archival_runtime(self):
        from shs_core.battery_writer import BatteryWriterFence
        rig=Rig()
        runtime=AppBatteryRuntime(rig.coordinator,rig.controller,rig.store,lambda:rig.now)
        rig.runtime=runtime
        rig.fence=BatteryWriterFence(rig.fence_store,rig.controller.lock,rig.controller.options,lambda:rig.now,runtime.identity)
        rig.coordinator.battery_writer=rig.fence
        await rig.start()
        await rig.advance(1)
        runtime.receipt_driven=True
        self.addAsyncCleanup(runtime.close,release=False)
        self.assertFalse(any(group.attempts or group.transition_work for group in runtime.host.state.groups))
        return rig,runtime

    async def test_archival_storm_keeps_every_soc_fact_without_reductions_or_saves_then_flushes_in_silence(self):
        rig,runtime=await self.archival_runtime()
        before=len(rig.store.writes)
        observed=len(runtime.host.state.execution.account.observations)
        with patch('shs_core.home_runtime.reduce_home',side_effect=AssertionError('archival facts ran economics')):
            for ordinal in range(1,51):
                rig.now+=1
                row=rig.rows['sensor.soc']
                row['last_reported']=iso(rig.now)
                await runtime.ingest_receipt(IDENTITY,dict(ordinal=ordinal,kind='observation',
                    payload=dict(row,entity_id='sensor.soc',kind='state_report')))
            self.assertEqual(len(rig.store.writes),before)
            self.assertEqual(len(runtime.host.state.execution.account.observations),observed+50)
            self.assertIsNone(runtime._processing)
            rig.now+=5000
            await runtime.commit_evidence()
        self.assertEqual(len(rig.store.writes),before+1)
        self.assertEqual(runtime._processing,runtime.received_checkpoint())
        self.assertEqual(rig.store.saved['gateway_processing']['receipt'],50)

    async def test_recovery_decision_is_immediate_and_includes_archived_prefix(self):
        rig,runtime=await self.archival_runtime()
        rig.rows['sensor.soc']['state']='unavailable'
        await runtime.ingest_receipt(IDENTITY,dict(ordinal=1,kind='observation',
            payload=dict(rig.rows['sensor.soc'],entity_id='sensor.soc',kind='state_change')))
        self.assertIsNone(runtime._processing)
        rig.rows['sensor.soc']['state']='51'
        await runtime.ingest_receipt(IDENTITY,dict(ordinal=2,kind='observation',
            payload=dict(rig.rows['sensor.soc'],entity_id='sensor.soc',kind='state_change')))
        await runtime.host.idle()
        self.assertEqual(runtime._processing['receipt'],2)
        self.assertTrue(runtime._processing['complete'])
        self.assertIsNotNone(runtime.host.state.conditions)

    async def test_explicit_verification_handover_commits_archived_evidence_before_writing(self):
        rig,runtime=await self.archival_runtime()
        rig.now+=1
        rig.rows['sensor.soc']['last_reported']=iso(rig.now)
        await runtime.ingest_receipt(IDENTITY,dict(ordinal=1,kind='observation',
            payload=dict(rig.rows['sensor.soc'],entity_id='sensor.soc',kind='state_report')))
        rig.options['device_modes']['$battery']='control_verification'
        await runtime.refresh();await runtime.host.idle()
        self.assertEqual(runtime._processing['receipt'],1)
        self.assertEqual(rig.rows['select.mode']['state'],'Maximum Self Consumption')
        self.assertFalse(runtime.host.state.groups[0].owned)

    async def test_receipt_before_resume_epoch_commits_counter_but_not_live_observation(self):
        rig = Rig()
        runtime = AppBatteryRuntime(rig.coordinator,rig.controller,rig.store,lambda:rig.now)
        rig.runtime = runtime
        self.addAsyncCleanup(runtime.close,release=False)
        await runtime.open()
        await runtime.refresh()
        await runtime.host.idle()
        runtime.receipt_driven = True
        epoch = runtime.host.state.resume_after_ms
        old = SimpleNamespace(observed=SimpleNamespace(observation=SimpleNamespace(at_ms=epoch-1)))
        receipt = dict(ordinal=1,kind='observation',payload=dict(entity_id='sensor.grid_import',
            state='100.1',attributes=rig.rows['sensor.grid_import']['attributes'],last_reported=iso(rig.now)))
        with patch.object(BatteryRuntime,'_observe',AsyncMock(return_value=(old,))):
            await runtime.consume_receipt(IDENTITY,receipt)
        self.assertTrue(runtime._processing['complete'])
        self.assertEqual(runtime._processing['receipt'],1)
        sample = runtime.host.state.execution.account.meter_index.neighbours('sensor.grid_import',rig.now)[0]
        self.assertEqual(sample.total_mwh,100100000)
        current = SimpleNamespace(observed=SimpleNamespace(observation=SimpleNamespace(at_ms=epoch)))
        with patch.object(BatteryRuntime,'_observe',AsyncMock(return_value=(current,))):
            self.assertEqual(await runtime._observe('battery'),(current,))

    async def test_diagnostics_do_not_traverse_lifetime_accounting_and_filter_old_traces(self):
        rig = Rig()
        runtime = AppBatteryRuntime(rig.coordinator, rig.controller, rig.store, lambda: rig.now)
        rig.runtime = runtime
        self.addAsyncCleanup(runtime.close, release=False)
        await runtime.open()
        await runtime.refresh()
        await runtime.host.idle()
        def trace(at):
            return ExecutionTrace(at, 0, 0, 0, None, '{}', '[]', None, None, None)
        current = trace(rig.now)
        state = runtime.host.state
        runtime.host.state = replace(state, execution=replace(state.execution,
            traces=(trace(rig.now-TRACE_RETENTION_MS-1), current)))
        with patch('shs_core.plan_execution.feedback', side_effect=AssertionError('Lifetime history requested')):
            value = runtime.snapshot(include_evidence=True)
            self.assertEqual(value['execution_traces'].value, (current,))
            self.assertNotIn('accounting_journal', value)
            dumps = lambda v: json.dumps(v, separators=(',', ':')).encode()
            document = json.loads(gzip.decompress(gzip_report(report_parts(value, dumps), dumps)))
            self.assertEqual(len(document['execution_traces']), 1)
            self.assertEqual(document['execution_trace_retention']['max_age_days'], 3)

    async def test_empty_sub_event_keeps_durable_cursor_until_complete_receipt(self):
        from copy import deepcopy
        from shs_core.receipt_inbox import processing_checkpoint
        rig = Rig()
        runtime = AppBatteryRuntime(rig.coordinator, rig.controller, rig.store, lambda: rig.now)
        rig.runtime = runtime
        self.addAsyncCleanup(runtime.close, release=False)
        await runtime.open()
        await runtime.refresh()
        await runtime.host.idle()
        state = runtime.host.state
        await runtime._persist_state(state)
        before = deepcopy(rig.store.saved)
        partial = processing_checkpoint(IDENTITY, 1, 0, complete=False)
        complete = processing_checkpoint(IDENTITY, 1, 1, complete=True)
        await runtime._persist_received(state, partial)
        # A restart sees the previous durable cursor, so no effect can be lost.
        self.assertEqual(rig.store.saved, before)
        await runtime._persist_received(state, complete)
        self.assertEqual(rig.store.saved['gateway_processing'], complete)
        changed = replace(state, revision=state.revision+1)
        partial = processing_checkpoint(IDENTITY, 2, 0, complete=False)
        await runtime._persist_received(changed, partial)
        self.assertEqual(rig.store.saved['gateway_processing'], partial)

    async def test_slow_accounting_result_is_shared_after_computation_and_new_accounts_invalidate(self):
        from shs_core.battery_runtime import ACCOUNTING_REUSE_MS
        from shs_core.plan_execution import Account
        rig=Rig()
        runtime=AppBatteryRuntime(rig.coordinator,rig.controller,rig.store,lambda:rig.now)
        account=Account()
        def slow_read(account,at):
            rig.now+=2*ACCOUNTING_REUSE_MS
            return {'sampled_at':at}
        with patch('shs_core.plan_execution.live_feedback',side_effect=slow_read) as read:
            first=runtime._accounting(account,False)
            self.assertLess(first[0],rig.now)
            self.assertEqual(runtime._accounting(account,False),first)
            self.assertEqual(read.call_count,1)
            rig.now+=ACCOUNTING_REUSE_MS
            self.assertNotEqual(runtime._accounting(account,False),first)
            self.assertEqual(read.call_count,2)
            runtime._accounting(Account(),False)
            self.assertEqual(read.call_count,3)
