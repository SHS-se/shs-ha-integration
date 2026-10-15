"""Historical receipts retain accounting without reauthorizing live feedback."""
import sys
from pathlib import Path
from types import SimpleNamespace
from dataclasses import asdict, replace
from copy import deepcopy
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
    async def test_bad_counter_receipt_is_visible_until_the_same_counter_is_valid(self):
        from shs_core.operational_attention import execution_attention
        rig,runtime=await self.archival_runtime()
        entity='sensor.grid_import'
        row=dict(rig.rows[entity],state='not-a-number',entity_id=entity,kind='state_report')
        await runtime.consume_receipt(IDENTITY,dict(ordinal=1,kind='observation',payload=row))
        errors=execution_attention({},runtime.snapshot(),[])
        issue=next(item for item in errors if item['key']=='pipeline:battery-input:'+entity)
        self.assertEqual(issue['fix'],{'kind':'entity','entity_id':entity})
        await runtime.consume_receipt(IDENTITY,dict(ordinal=2,kind='observation',payload=dict(
            rig.rows['sensor.grid_export'],entity_id='sensor.grid_export',kind='state_report')))
        self.assertIn(entity,runtime.snapshot()['input_errors'])
        await runtime.consume_receipt(IDENTITY,dict(ordinal=3,kind='observation',payload=dict(
            rig.rows[entity],entity_id=entity,kind='state_report')))
        self.assertNotIn(entity,runtime.snapshot()['input_errors'])

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

    async def test_startup_replay_keeps_counter_and_soc_facts_without_per_receipt_commits(self):
        from shs_core import home_runtime as rt
        fixtures = [await self.archival_runtime() for _ in range(2)]
        for rig, runtime in fixtures:
            runtime.host.state = replace(runtime.host.state, groups=tuple(
                replace(group, grant_confirmed=False) for group in runtime.host.state.groups))
            runtime.physical = SimpleNamespace(enabled=False)
        writes = [len(rig.store.writes) for rig, runtime in fixtures]
        selects = rt._refresh_execution
        def no_selection(state, now, effects, *, select=True):
            self.assertFalse(select, 'startup evidence requested economic selection')
            return selects(state, now, effects, select=select)
        with patch('shs_core.home_runtime._refresh_execution', side_effect=no_selection):
            for ordinal in range(1,181):
                for index,(rig,runtime) in enumerate(fixtures):
                    rig.now += 1
                    entity = ('sensor.soc','sensor.grid_import','sensor.grid_export')[ordinal % 3]
                    row = rig.rows[entity]
                    row['last_reported'] = iso(rig.now - (2 if ordinal % 17 == 0 else 0))
                    if entity == 'sensor.soc':
                        row['state'] = str(50 + ordinal / 10000)
                    elif ordinal % 4:
                        row['state'] = str(100 + ordinal / 10000)
                    receipt = dict(ordinal=ordinal, kind='observation',
                        payload=dict(row,entity_id=entity,kind='state_report'))
                    if index == 0:
                        await runtime.consume_receipt(IDENTITY, receipt)
                    else:
                        await runtime.ingest_receipt(IDENTITY, receipt, replay=True)
            batched = fixtures[1][1]
            self.assertIsNone(batched._processing)
            self.assertEqual(len(fixtures[1][0].store.writes), writes[1])
            await batched.commit_evidence(force=True)
        for name in ('meters','observations'):
            facts = [[asdict(row) for row in getattr(runtime.host.state.execution.account,name)]
                     for rig,runtime in fixtures]
            self.assertEqual(facts[0],facts[1])
        self.assertEqual(len(fixtures[1][0].store.writes), writes[1] + 1)
        self.assertGreater(len(fixtures[0][0].store.writes) - writes[0], 180)
        self.assertEqual(batched._processing, batched.received_checkpoint())
        self.assertEqual(batched._processing['receipt'], 180)

    async def test_startup_recovery_and_control_reports_do_not_emit_an_economic_tick(self):
        rig,runtime = await self.archival_runtime()
        runtime.physical = SimpleNamespace(enabled=False)
        runtime._observation_error = 'earlier unavailable reading'
        def recovered(row):
            runtime._observation_error = None
            return ()
        with (patch.object(runtime,'_receipt_observation',AsyncMock(side_effect=recovered)),
              patch.object(runtime.host,'accept',AsyncMock(side_effect=AssertionError('startup emitted a Tick'))),
              patch.object(runtime.host,'accept_received',AsyncMock(side_effect=AssertionError('startup used urgent decisions')))):
            row = rig.rows['number.charge']
            await runtime.ingest_receipt(IDENTITY,dict(ordinal=1,kind='observation',
                payload=dict(row,entity_id='number.charge',kind='state_change')),replay=True)
            await runtime.ingest_receipt(IDENTITY,dict(ordinal=2,kind='configuration',payload={}),replay=True)
            await runtime.commit_evidence(force=True)
        self.assertEqual(runtime._processing['receipt'], 2)

    async def test_startup_expired_plan_defers_cloud_requests_until_accounting_is_committed(self):
        rig,runtime = await self.archival_runtime()
        runtime.physical = SimpleNamespace(enabled=False)
        runtime.host.state = replace(runtime.host.state, groups=tuple(
            replace(group,grant_confirmed=False) for group in runtime.host.state.groups))
        rig.now = 900001
        for row in rig.rows.values():
            row['last_reported'] = iso(rig.now)
        row = rig.rows['sensor.soc']
        cloud = AsyncMock()
        rig.coordinator.async_optimisation_push = cloud
        await runtime.ingest_receipt(IDENTITY,dict(ordinal=1,kind='observation',
            payload=dict(row,entity_id='sensor.soc',kind='state_report')),replay=True)
        await runtime.host.idle()
        self.assertTrue(runtime._replan_pending)
        cloud.assert_not_awaited()
        await runtime.commit_evidence(force=True)
        async def request(**body):
            self.assertEqual(rig.store.saved['gateway_processing']['receipt'], 1)
        cloud.side_effect = request
        runtime.physical.enabled = True
        await runtime.refresh()
        await runtime._replan_task
        cloud.assert_awaited_once()

    async def test_committed_deferred_replan_survives_a_second_restart(self):
        rig,runtime = await self.archival_runtime()
        runtime.physical = SimpleNamespace(enabled=False)
        runtime.host.state = replace(runtime.host.state,groups=tuple(
            replace(group,grant_confirmed=False) for group in runtime.host.state.groups))
        rig.now = 900001
        for row in rig.rows.values():
            row['last_reported'] = iso(rig.now)
        row = rig.rows['sensor.soc']
        cloud = AsyncMock()
        rig.coordinator.async_optimisation_push = cloud
        await runtime.ingest_receipt(IDENTITY,dict(ordinal=1,kind='observation',
            payload=dict(row,entity_id='sensor.soc',kind='state_report')),replay=True)
        await runtime.host.idle()
        await runtime.commit_evidence(force=True)
        self.assertEqual(runtime.host.state.execution.replan_reason,'plan_expired')
        cloud.assert_not_awaited()
        await runtime.close(release=False)
        replacement = rig.runtime = AppBatteryRuntime(rig.coordinator,rig.controller,rig.store,lambda:rig.now)
        replacement.physical = SimpleNamespace(enabled=False)
        self.addAsyncCleanup(replacement.close,release=False)
        await replacement.open()
        self.assertTrue(replacement._replan_pending)
        self.assertEqual(replacement._processing['receipt'],1)
        cloud.assert_not_awaited()
        replacement.physical.enabled = True
        await replacement.refresh()
        await replacement._replan_task
        cloud.assert_awaited_once()

    async def test_startup_does_not_confirm_a_pending_release_until_the_writer_is_enabled(self):
        rig,runtime = await self.archival_runtime()
        group = runtime.host.state.groups[0]
        release = runtime._release_request(group,tuple(zip(group.spec.control_keys,
            ('Maximum Self Consumption',4000,4000))))
        runtime.host.state = replace(runtime.host.state,groups=(replace(group,
            owned=True,release=release,release_pending=True),))
        before = deepcopy(runtime.host.state.groups[0])
        runtime.physical = SimpleNamespace(enabled=False)
        with patch.object(rig.fence,'take_over',AsyncMock(side_effect=AssertionError('startup acquired a writer'))):
            self.assertEqual(await runtime._confirm('battery'),())
        self.assertEqual(runtime.host.state.groups[0],before)

    async def test_partial_counter_restart_finishes_receipt_without_duplicating_the_meter(self):
        from shs_core.receipt_inbox import processing_checkpoint
        rig,runtime = await self.archival_runtime()
        rig.now += 1
        row = rig.rows['sensor.grid_import']
        row.update(state='100.2',last_reported=iso(rig.now))
        receipt = dict(ordinal=1,kind='observation',
            payload=dict(row,entity_id='sensor.grid_import',kind='state_report'))
        checkpoint = processing_checkpoint(IDENTITY,1,0,complete=False)
        await runtime._meter('sensor.grid_import',row['state'],row['attributes'],rig.now,
            '1',processing=checkpoint,receiver=runtime.host.accept_received)
        count = len(runtime.host.state.execution.account.meters)
        await runtime.close(release=False)
        replacement = rig.runtime = AppBatteryRuntime(rig.coordinator,rig.controller,rig.store,lambda:rig.now)
        replacement.physical = SimpleNamespace(enabled=False)
        self.addAsyncCleanup(replacement.close,release=False)
        await replacement.open()
        await replacement.ingest_receipt(IDENTITY,receipt,replay=True)
        await replacement.commit_evidence(force=True)
        self.assertEqual(len(replacement.host.state.execution.account.meters),count)
        self.assertEqual(replacement._processing,processing_checkpoint(IDENTITY,1,1,complete=True))

    async def test_crash_before_batch_commit_replays_the_counter_once(self):
        rig,runtime = await self.archival_runtime()
        rig.now += 1
        row = rig.rows['sensor.grid_import']
        row.update(state='100.3',last_reported=iso(rig.now))
        receipt = dict(ordinal=1,kind='observation',
            payload=dict(row,entity_id='sensor.grid_import',kind='state_report'))
        count = len(runtime.host.state.execution.account.meters)
        runtime.physical = SimpleNamespace(enabled=False)
        await runtime.ingest_receipt(IDENTITY,receipt,replay=True)
        self.assertIsNone(runtime._processing)
        self.assertEqual(len(runtime.host.state.execution.account.meters),count+1)
        await runtime.close(release=False)
        replacement = rig.runtime = AppBatteryRuntime(rig.coordinator,rig.controller,rig.store,lambda:rig.now)
        replacement.physical = SimpleNamespace(enabled=False)
        self.addAsyncCleanup(replacement.close,release=False)
        await replacement.open()
        self.assertEqual(len(replacement.host.state.execution.account.meters),count)
        await replacement.ingest_receipt(IDENTITY,receipt,replay=True)
        await replacement.commit_evidence(force=True)
        await replacement.ingest_receipt(IDENTITY,receipt,replay=True)
        self.assertEqual(len(replacement.host.state.execution.account.meters),count+1)
        self.assertEqual(replacement._processing['receipt'],1)

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

    async def test_accepted_command_does_not_make_every_power_report_urgent(self):
        from shs_core import home_runtime as rt
        rig,runtime=await self.archival_runtime()
        state=runtime.host.state
        group=state.groups[0]
        target=group.desired.target
        old=tuple((key,0 if key=='number.charge' else value) for key,value in target)
        step=rt.Step('number.charge',dict(target)['number.charge'],old,(),group.spec.maximum,
            5000,100000,True,'delayed native publication')
        attempt=rt.Attempt('battery:lag',state.revision,group.generation,group.desired.id,
            group.desired.revision,'optimisation',0,step,group.observation_revision,
            rig.now,rig.now+5000,rig.now+5000,rig.now+100000,'accepted',group.grant)
        runtime.host.state=replace(state,groups=(replace(group,plan=None,transition_work=None,
            observation=replace(group.observation,controls=old),attempts=(attempt,)),))
        rig.rows['number.charge']['state']='0'
        runtime._last_capture=None
        writes=len(rig.store.writes)
        decisions=runtime.profiler.operations['decision']['calls']
        for ordinal in range(1,51):
            rig.now+=10
            row=rig.rows['sensor.house']
            row.update(state=str(1000+ordinal),last_reported=iso(rig.now))
            await runtime.ingest_receipt(IDENTITY,dict(ordinal=ordinal,kind='observation',
                payload=dict(row,entity_id='sensor.house',kind='state_report')))
        self.assertEqual(len(rig.store.writes),writes)
        self.assertEqual(runtime.profiler.operations['decision']['calls'],decisions)
        self.assertEqual(runtime.host.state.groups[0].attempts,(attempt,))
        # State reports, too, must commit a real confirmation and its entire prefix.
        row=rig.rows['number.charge']
        row.update(state=str(dict(target)['number.charge']/1000),last_reported=iso(rig.now))
        await runtime.ingest_receipt(IDENTITY,dict(ordinal=51,kind='observation',
            payload=dict(row,entity_id='number.charge',kind='state_report')))
        await runtime.host.idle()
        self.assertFalse(runtime.host.state.groups[0].attempts)
        self.assertGreater(len(rig.store.writes),writes)
        self.assertEqual(runtime._processing['receipt'],51)
        self.assertEqual(runtime.profiler.operations['decision']['calls'],decisions)

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
