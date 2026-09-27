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
