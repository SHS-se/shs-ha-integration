"""Durable planning acceptance and delivery through the real household owner."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.append(str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from household_fixture import Rig
from shs_core.api import ShsApiError


class PlanningJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/schema-9-mixed-mode-plan.json').read_text())
        self.plan = fixture['plan']
        self.rig = Rig(now=datetime.fromisoformat(fixture['validation_time']),
            options={'planning_mode': 'live', 'device_modes': deepcopy(self.plan['operating_scope']['modes'])},
            stored={'optimisation_plan': deepcopy(self.plan)})
        self.h = h = self.rig.household
        h.optimisation_plan = deepcopy(self.plan)
        h._configured_entities = lambda: {}
        h._prepared_device_inventory = AsyncMock(return_value=[])
        h._observe_calibration = Mock()
        h._thermal_quarters = AsyncMock(return_value=[])
        h._optimisation_options = lambda: self.rig.options
        h._build_optimisation_snapshot = AsyncMock(return_value={'snapshot_id': 'snapshot'})
        h._price_quarters = lambda *args: []
        h.equipment_presence = lambda: {}
        h._record_device_exchange = AsyncMock(return_value={})
        h._sync_plan_refused_issue = Mock()
        h._sync_optimisation_issue = Mock()
        h.async_report_runtime = AsyncMock()
        h.async_battery_inputs_refresh = AsyncMock()
        h.client = SimpleNamespace(push_optimisation=AsyncMock(return_value={
            'job_id': 'job', 'state': 'pending', 'pending': True, 'retry_after_ms': 1000,
            'actuals_accepted_until': self.rig.now.isoformat(),
        }), planning_status=AsyncMock(), planning_submission_status=AsyncMock(), acknowledge_optimisation_plan=AsyncMock(), report_replan_failure=AsyncMock())
        self.candidate = deepcopy(self.plan)
        self.candidate['plan_id'] = self.candidate['execution_plan']['plan_id'] = 'c0debabe-1111-4222-8333-123456789abc'

    def published(self, job_id='job'):
        return {'job_id': job_id, 'state': 'published', 'pending': False, 'plan': self.candidate}

    async def accept(self, manual=None):
        await self.h.async_optimisation_push(force_plan=True, replan_request_id=manual)
        return (await self.h._store.async_load())['optimisation_pending_job']

    async def test_acceptance_advances_watermarks_and_returns_before_delivery(self):
        pending = await self.accept('manual')
        self.assertEqual(pending['job_id'], 'job')
        self.assertEqual(pending['replan_request_id'], 'manual')
        self.assertEqual(self.rig.records.saved['optimisation_actuals_accepted_until'], self.rig.now.isoformat())
        self.assertEqual(self.h.optimisation_plan, self.plan)
        self.assertFalse(self.h._push_lock.locked())
        self.h.client.planning_status.assert_not_awaited()
        self.h.client.planning_status.return_value = {'job_id': 'job', 'state': 'pending', 'pending': True, 'retry_after_ms': 2345}
        self.assertEqual(await self.h._poll_planning_job(pending), 2.345)
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(pending)
        self.assertEqual(self.h.optimisation_plan, self.candidate)
        self.assertNotIn('optimisation_pending_job', self.rig.records.saved)
        self.h.client.acknowledge_optimisation_plan.assert_awaited_once()
        self.assertEqual(self.h.client.acknowledge_optimisation_plan.call_args.args[1], 'accepted')

    async def test_delayed_durable_result_is_installed_and_acknowledged_with_its_original_issue_time(self):
        pending = await self.accept('manual')
        issued = self.candidate['issued_at']
        self.rig.now += timedelta(minutes=32)
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(pending)
        self.assertEqual(self.h.optimisation_plan, self.candidate)
        self.assertEqual(self.h.optimisation_plan['issued_at'], issued)
        self.assertEqual(self.rig.records.saved['optimisation_plan'], self.candidate)
        self.assertNotIn('optimisation_pending_job', self.rig.records.saved)
        self.assertEqual(self.h.client.acknowledge_optimisation_plan.call_args.args[1], 'accepted')

    async def test_normal_exchange_does_not_recapture_accepted_work_and_keeps_settings_available(self):
        await self.accept()
        self.h._build_optimisation_snapshot.reset_mock()
        self.h.client.push_optimisation.reset_mock()
        self.h.client.push_optimisation.return_value = {}
        self.h._price_quarters = lambda *args: [{'start': self.rig.now.isoformat()}]
        await self.h.async_optimisation_push()
        self.h._build_optimisation_snapshot.assert_not_awaited()
        self.assertIsNone(self.h.client.push_optimisation.call_args.args[1])
        self.assertFalse(self.h.configuration_busy)
        self.assertEqual((await self.h.async_cached_exchange_status())['planning_job'], {'job_id': 'job', 'state': 'pending'})
        self.h.client.push_optimisation.return_value = {'job_id': 'new-job', 'state': 'pending', 'pending': True, 'retry_after_ms': 1000}
        await self.h.async_optimisation_push(force_plan=True, replan_request_id='new-manual')
        self.h._build_optimisation_snapshot.assert_awaited_once()
        self.assertEqual(self.rig.records.saved['optimisation_pending_job']['job_id'], 'new-job')

    async def test_host_loop_restores_receipt_and_manual_answer_after_restart(self):
        await self.accept('manual')
        # Restoring the same durable record starts no task by itself.
        restarted = Rig(now=self.rig.now, options=self.rig.options, stored=self.rig.records.saved).household
        restarted.client = self.h.client
        restarted.async_report_runtime = AsyncMock()
        restarted.async_battery_inputs_refresh = AsyncMock()
        await restarted.async_restore_plan()
        self.assertEqual(restarted._answered_replan_request_id, 'manual')
        self.assertFalse(await restarted.async_answer_replan('manual'))
        restarted.client.planning_status.return_value = self.published()
        installed = asyncio.Event()
        restarted.async_update_listeners = installed.set
        task = asyncio.create_task(restarted.async_planning_delivery())
        await asyncio.wait_for(installed.wait(), timeout=1)
        self.assertEqual(restarted.optimisation_plan, self.candidate)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_poll_wait_and_transport_failure_keep_lock_free_and_receipt_durable(self):
        pending = await self.accept()
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(job):
            entered.set()
            await release.wait()
            raise ShsApiError('connection lost')
        self.h.client.planning_status.side_effect = delayed
        task = asyncio.create_task(self.h._poll_planning_job(pending))
        await entered.wait()
        async with self.h._push_lock:
            self.assertFalse(task.done())
        release.set()
        self.assertEqual(await task, 5)
        self.assertEqual(self.rig.records.saved['optimisation_pending_job'], pending)
        self.assertEqual(self.h.optimisation_plan, self.plan)
        self.h.client.acknowledge_optimisation_plan.assert_not_awaited()

    async def test_failed_or_superseded_jobs_retain_plan_and_do_not_report_manual_failure(self):
        for state in ('failed', 'superseded'):
            pending = await self.accept('manual')
            self.h.client.planning_status.return_value = {
                'job_id': 'job', 'state': state, 'pending': False, 'detail': 'worker terminated',
            }
            await self.h._poll_planning_job(pending)
            self.assertEqual(self.h.optimisation_plan, self.plan)
            self.assertNotIn('optimisation_pending_job', self.rig.records.saved)
            self.h.client.report_replan_failure.assert_not_awaited()
            self.h.client.acknowledge_optimisation_plan.assert_not_awaited()

    async def test_lost_acceptance_reply_recovers_by_snapshot_and_then_polls_job_only(self):
        self.h.client.push_optimisation.side_effect = ShsApiError('reply lost')
        await self.h.async_answer_replan('manual')
        pending = (await self.h._store.async_read())['optimisation_pending_submission']
        self.assertEqual(pending['snapshot_id'], 'snapshot')
        self.assertNotIn('job_id', pending)
        self.h.client.report_replan_failure.assert_not_awaited()
        self.h.client.planning_submission_status.return_value = {
            'job_id': 'job', 'snapshot_id': 'snapshot', 'state': 'pending', 'pending': True,
            'retry_after_ms': 1000, 'actuals_accepted_until': self.rig.now.isoformat(),
        }
        await self.h._poll_planning_job(pending)
        accepted = (await self.h._store.async_read())['optimisation_pending_job']
        self.assertEqual(accepted['job_id'], 'job')
        self.assertNotIn('snapshot_id', accepted)
        self.assertNotIn('devices', accepted)
        self.assertEqual(self.rig.records.saved['answered_replan_request_id'], 'manual')
        self.assertEqual(self.rig.records.saved['optimisation_actuals_accepted_until'], self.rig.now.isoformat())
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(accepted)
        self.assertEqual(self.h.optimisation_plan, self.candidate)
        self.h.client.planning_submission_status.assert_awaited_once_with('snapshot')
        self.h.client.planning_status.assert_awaited_once_with('job')
        self.h.client.push_optimisation.assert_awaited_once()

    async def test_restart_recovers_a_lost_acceptance_reply_without_resending_capture(self):
        self.h.client.push_optimisation.side_effect = ShsApiError('reply lost')
        await self.h.async_answer_replan('manual')
        rig = Rig(now=self.rig.now, options=self.rig.options, stored=self.rig.records.saved)
        restarted = rig.household
        self.assertEqual(rig.spawned, [])
        restarted.client = self.h.client
        restarted._record_device_exchange = AsyncMock(return_value={})
        restarted.async_report_runtime = AsyncMock()
        restarted.async_battery_inputs_refresh = AsyncMock()
        await restarted.async_restore_plan()
        self.assertFalse(await restarted.async_answer_replan('manual'))
        restarted.client.planning_submission_status.return_value = {
            **self.published(), 'snapshot_id': 'snapshot',
        }
        installed = asyncio.Event()
        restarted.async_update_listeners = installed.set
        task = asyncio.create_task(restarted.async_planning_delivery())
        await asyncio.wait_for(installed.wait(), timeout=1)
        self.assertEqual(restarted.optimisation_plan, self.candidate)
        restarted.client.planning_submission_status.assert_awaited_once_with('snapshot')
        restarted.client.push_optimisation.assert_awaited_once()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_never_accepted_submission_is_terminal_and_reports_only_that_capture(self):
        self.h.client.push_optimisation.side_effect = ShsApiError('reply lost')
        await self.h.async_answer_replan('manual')
        pending = (await self.h._store.async_read())['optimisation_pending_submission']
        self.h.client.planning_submission_status.side_effect = ShsApiError('not accepted', code='planning_job_not_found')
        await self.h._poll_planning_job(pending)
        self.assertNotIn('optimisation_pending_submission', self.rig.records.saved)
        self.assertEqual(self.h.optimisation_plan, self.plan)
        self.h.client.report_replan_failure.assert_awaited_once_with('manual', 'not accepted')

    async def test_forced_telemetry_only_exchange_retains_existing_accepted_job(self):
        accepted = await self.accept()
        self.h.client.push_optimisation.return_value = {}
        await self.h.async_optimisation_push(force_plan=True)
        self.assertEqual(self.rig.records.saved['optimisation_pending_job'], accepted)
        self.assertNotIn('optimisation_pending_submission', self.rig.records.saved)
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(accepted)
        self.assertEqual(self.h.optimisation_plan, self.candidate)

    async def test_lost_replacement_lookup_not_found_retains_existing_accepted_job(self):
        accepted = await self.accept()
        self.h._build_optimisation_snapshot.return_value = {'snapshot_id': 'new-snapshot'}
        self.h.client.push_optimisation.side_effect = ShsApiError('reply lost')
        await self.h.async_optimisation_push(force_plan=True)
        submission = self.rig.records.saved['optimisation_pending_submission']
        self.assertEqual(self.rig.records.saved['optimisation_pending_job'], accepted)
        self.h.client.planning_submission_status.side_effect = ShsApiError('not accepted', code='planning_job_not_found')
        await self.h._poll_planning_job(submission)
        self.assertNotIn('optimisation_pending_submission', self.rig.records.saved)
        self.assertEqual(self.rig.records.saved['optimisation_pending_job'], accepted)
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(accepted)
        self.assertEqual(self.h.optimisation_plan, self.candidate)

    async def test_missing_durable_job_is_terminal_and_retains_the_last_plan(self):
        pending = await self.accept()
        self.h.client.planning_status.side_effect = ShsApiError('accepted job is no longer available', code='planning_job_not_found')
        self.assertEqual(await self.h._poll_planning_job(pending), 0)
        self.assertEqual(self.h.optimisation_plan, self.plan)
        self.assertNotIn('optimisation_pending_job', self.rig.records.saved)
        self.assertIn('no longer available', self.h.last_optimisation_error)

    async def test_configuration_changed_during_solve_rejects_candidate_and_retains_plan(self):
        pending = await self.accept()
        self.rig.options['another_setting'] = True
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(pending)
        self.assertEqual(self.h.optimisation_plan, self.plan)
        ack = self.h.client.acknowledge_optimisation_plan.call_args.args
        self.assertEqual(ack[1], 'rejected')
        self.assertEqual(ack[2]['code'], 'local_configuration_changed')

    async def test_older_local_poll_cannot_install_after_new_acceptance(self):
        pending = await self.accept()
        stored = await self.h._store.async_load()
        stored['optimisation_pending_job']['job_id'] = 'new-job'
        await self.h._store.async_save(stored)
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(pending)
        self.assertEqual(self.h.optimisation_plan, self.plan)
        self.assertEqual(self.rig.records.saved['optimisation_pending_job']['job_id'], 'new-job')
        self.h.client.acknowledge_optimisation_plan.assert_not_awaited()

    async def test_acceptance_is_journalled_even_if_configuration_processing_fails(self):
        self.h._record_device_exchange.side_effect = ShsApiError('configuration unavailable')
        await self.h.async_answer_replan('manual')
        self.assertEqual(self.rig.records.saved['optimisation_pending_job']['job_id'], 'job')
        self.assertEqual(self.rig.records.saved['answered_replan_request_id'], 'manual')
        self.h.client.report_replan_failure.assert_not_awaited()
        pending = (await self.h._store.async_read())['optimisation_pending_job']
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(pending)
        self.assertEqual(self.h.optimisation_plan, self.plan)
        self.assertEqual(self.h.client.acknowledge_optimisation_plan.call_args.args[1], 'rejected')
