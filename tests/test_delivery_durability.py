"""Synthetic loss, crash, upgrade and owner interleavings through Household."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from household_fixture import Rig
import test_planning_jobs as jobs
import test_execution_replan as execution
from shs_core.api import ShsApiClient, ShsApiError
from shs_core.command_journal import encoded
from shs_core.durable_record import DurableRecord


class DeliveryDurabilityTests(IsolatedAsyncioTestCase):
    setUp = jobs.PlanningJobTests.setUp
    accept = jobs.PlanningJobTests.accept
    published = jobs.PlanningJobTests.published

    def pending_receipt(self, snapshot='snapshot', job='job'):
        return {'job_id': job, 'snapshot_id': snapshot, 'state': 'pending',
                'pending': True, 'retry_after_ms': 1000}

    async def restart(self):
        rig = Rig(now=self.rig.now, options=self.rig.options,
                  stored=json.loads(encoded(self.rig.records.saved)))
        h = rig.household
        h.client = self.h.client
        h._record_device_exchange = AsyncMock(return_value={})
        h.async_report_runtime = AsyncMock()
        h.async_battery_inputs_refresh = AsyncMock()
        await h.async_restore_plan()
        return rig, h

    async def test_exact_first_body_survives_lost_before_acceptance_and_sorted_restart(self):
        # Deliberately non-canonical nested maps: the real journal sorts keys.
        self.h._build_optimisation_snapshot.return_value = {
            'snapshot_id': 'snapshot', 'z': {'second': 2, 'first': 1},
            'a': [{'z': 0, 'a': 1}, {'second': 5, 'first': 3}]}
        bodies = []
        async def submit(body):
            bodies.append(json.dumps(body, separators=(',', ':')))
            saved = await self.h._store.async_read()
            self.assertEqual(json.dumps(saved['optimisation_pending_submission']['body'], separators=(',', ':')), bodies[-1])
            raise ShsApiError('lost before acceptance', retryable=True)
        self.h.client.submit_optimisation.side_effect = submit
        await self.h.async_answer_replan('manual')
        rig, restarted = await self.restart()
        restarted.client.planning_submission_status.side_effect = ShsApiError('absent', code='planning_job_not_found')
        async def accepted(body):
            bodies.append(json.dumps(body, separators=(',', ':')))
            return self.pending_receipt()
        restarted.client.submit_optimisation.side_effect = accepted
        await restarted._poll_planning_job((await restarted._store.async_read())['optimisation_pending_submission'])
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(hashlib.sha256(bodies[0].encode()).digest(), hashlib.sha256(bodies[1].encode()).digest())
        self.assertEqual(json.loads(bodies[1])['snapshot']['a'][0], {'a': 1, 'z': 0})
        self.assertNotIn('Authorization', bodies[1])
        self.assertNotIn('token', bodies[1])
        self.assertEqual(rig.records.saved['optimisation_pending_job']['replan_request_id'], 'manual')
        self.h._build_optimisation_snapshot.assert_awaited_once()
        restarted.client.report_replan_failure.assert_not_awaited()

    async def test_crash_after_durable_save_before_first_post_recovers_exact_capture(self):
        self.h.client.submit_optimisation.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.h.async_optimisation_push(force_plan=True, replan_request_id='manual')
        rig, restarted = await self.restart()
        pending = rig.records.saved['optimisation_pending_submission']
        self.assertNotIn('submission_error', pending)
        restarted.client.planning_submission_status.side_effect = ShsApiError('absent', code='planning_job_not_found')
        restarted.client.submit_optimisation.side_effect = None
        restarted.client.submit_optimisation.return_value = self.pending_receipt()
        await restarted._poll_planning_job(pending)
        self.assertEqual(restarted.client.submit_optimisation.call_args.args[0], pending['body'])
        self.assertNotIn('optimisation_pending_submission', rig.records.saved)
        self.assertEqual(rig.records.saved['optimisation_pending_job']['job_id'], 'job')

    async def test_late_absent_receipt_cannot_retransmit_over_new_manual_submission(self):
        self.h.client.submit_optimisation.side_effect = ShsApiError('lost', retryable=True)
        await self.h.async_optimisation_push(force_plan=True, replan_request_id='first')
        old = self.rig.records.saved['optimisation_pending_submission']
        entered, release = asyncio.Event(), asyncio.Event()
        async def lookup(_snapshot):
            entered.set()
            await release.wait()
            raise ShsApiError('absent', code='planning_job_not_found')
        self.h.client.planning_submission_status.side_effect = lookup
        task = asyncio.create_task(self.h._poll_planning_job(old))
        await entered.wait()
        self.h._build_optimisation_snapshot.return_value = {'snapshot_id': 'new'}
        await self.h.async_optimisation_push(force_plan=True, replan_request_id='new-manual')
        calls = self.h.client.submit_optimisation.await_count
        release.set()
        await task
        self.assertEqual(self.h.client.submit_optimisation.await_count, calls)
        self.assertEqual(self.rig.records.saved['optimisation_pending_submission']['snapshot_id'], 'new')

    async def test_retryable_loss_preserves_older_job_and_later_ack_independently(self):
        old = await self.accept()
        self.h._build_optimisation_snapshot.return_value = {'snapshot_id': 'new'}
        self.h.client.submit_optimisation.side_effect = ShsApiError('lost', retryable=True)
        await self.h.async_optimisation_push(force_plan=True, replan_request_id='new-manual')
        pending = self.rig.records.saved['optimisation_pending_submission']
        self.h.client.planning_submission_status.side_effect = ShsApiError('absent', code='planning_job_not_found')
        await self.h._poll_planning_job(pending)
        self.assertEqual(self.rig.records.saved['optimisation_pending_job'], old)
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(old)
        self.assertEqual(self.h.optimisation_plan, self.candidate)
        self.assertEqual(self.rig.records.saved['optimisation_pending_submission']['snapshot_id'], 'new')
        self.h.client.acknowledge_optimisation_plan.assert_awaited_once()

    async def test_ack_only_delivery_retries_after_restart_without_upload_or_solve(self):
        stored = await self.h._store.async_load()
        await self.h._accept_returned_plan(stored, self.candidate)
        await self.h._store.async_save(stored)
        rig, restarted = await self.restart()
        attempted, done = asyncio.Event(), asyncio.Event()
        async def ack(*args):
            self.assertEqual(args[0]['plan_id'], self.candidate['plan_id'])
            if not attempted.is_set():
                attempted.set()
                raise ShsApiError('ACK reply lost', retryable=True)
            done.set()
        restarted.client.acknowledge_optimisation_plan.side_effect = ack
        task = asyncio.create_task(restarted.async_planning_delivery())
        try:
            await asyncio.wait_for(attempted.wait(), 1)
            await asyncio.sleep(0)
            pending = rig.records.saved['optimisation_pending_plan_ack']
            self.assertIn('retry_at', pending)
            rig.now += timedelta(seconds=10)
            restarted._planning_job_wake.set()
            await asyncio.wait_for(done.wait(), 1)
            await asyncio.sleep(0)
            self.assertNotIn('optimisation_pending_plan_ack', rig.records.saved)
            restarted.client.submit_optimisation.assert_not_awaited()
            restarted.client.planning_status.assert_not_awaited()
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_terminal_ack_remains_actionable_without_hot_retry(self):
        stored = await self.h._store.async_load()
        await self.h._accept_returned_plan(stored, self.candidate)
        self.h.client.acknowledge_optimisation_plan.side_effect = ShsApiError('unauthorized', code='unauthorized')
        self.assertTrue(await self.h._retry_pending_plan_ack(stored))
        self.assertIn('terminal_error', stored['optimisation_pending_plan_ack'])
        self.assertFalse(await self.h._retry_pending_plan_ack(stored))
        self.h.client.acknowledge_optimisation_plan.assert_awaited_once()
        notice = next(item for item in self.h.attention_items if item['key'] == 'pipeline:acknowledgement')
        self.assertNotIn('automatically', notice['next_step'])
        # A subsequent exchange after account repair can retry this exact ACK.
        self.h.client.acknowledge_optimisation_plan.side_effect = None
        self.assertTrue(await self.h._retry_pending_plan_ack(stored, exchange=True))
        self.assertNotIn('optimisation_pending_plan_ack', stored)

    async def test_ack_send_is_serialized_before_new_acceptance(self):
        stored = await self.h._store.async_load()
        await self.h._accept_returned_plan(stored, self.candidate)
        await self.h._store.async_save(stored)
        entered, release = asyncio.Event(), asyncio.Event()
        async def ack(*args):
            entered.set()
            await release.wait()
            raise ShsApiError('old ACK', code='stale_plan_ack')
        self.h.client.acknowledge_optimisation_plan.side_effect = ack
        async def deliver():
            async with self.h._push_lock:
                row = await self.h._store.async_load()
                await self.h._retry_pending_plan_ack(row)
                await self.h._store.async_save(row)
        task = asyncio.create_task(deliver())
        await entered.wait()
        newer = deepcopy(self.candidate)
        newer['plan_id'] = 'new-plan'
        async def install():
            async with self.h._push_lock:
                row = await self.h._store.async_load()
                await self.h._accept_returned_plan(row, newer)
                await self.h._store.async_save(row)
        install_task = asyncio.create_task(install())
        await asyncio.sleep(0)
        self.assertFalse(install_task.done())
        release.set()
        await task
        await install_task
        self.assertEqual(self.rig.records.saved['optimisation_pending_plan_ack']['plan']['plan_id'], 'new-plan')

    async def test_upgrade_receipt_recaptures_same_manual_purpose_with_new_snapshot(self):
        pending = await self.accept('manual')
        self.h.client.planning_status.return_value = {'job_id': 'job', 'state': 'failed',
            'code': 'planner_upgraded', 'recapture': {'replaces_job_id': 'job'}}
        await self.h._poll_planning_job(pending)
        self.assertEqual(self.rig.records.saved['optimisation_recapture'],
                         {'replan_request_id': 'manual', 'replaces_job_id': 'job'})
        # A process restart preserves the intent; no retired capture is executed.
        self.h._store = DurableRecord(self.rig.records, encoded, json.loads)
        await self.h.async_restore_plan()
        self.h._build_optimisation_snapshot.return_value = {'snapshot_id': 'fresh'}
        self.h.client.submit_optimisation.return_value = self.pending_receipt('fresh', 'fresh-job')
        await self.h.async_optimisation_push(force_plan=True)
        body = self.h.client.submit_optimisation.call_args.args[0]
        self.assertEqual((body['replan_request_id'], body['replaces_job_id'], body['snapshot']['snapshot_id']), ('manual', 'job', 'fresh'))
        self.assertNotIn('optimisation_recapture', self.rig.records.saved)
        self.assertEqual(self.h.optimisation_plan, self.plan)

    async def test_bodyless_and_version_two_outboxes_migrate_only_after_missing_receipt(self):
        for body in (None, {'planning_exchange_version': 2, 'snapshot': {'snapshot_id': 'obsolete'}}):
            with self.subTest(body=body):
                stored = await self.h._store.async_load()
                pending = {'snapshot_id': 'obsolete', 'devices': [], 'replan_request_id': 'manual'}
                if body:
                    pending['body'] = body
                stored['optimisation_pending_submission'] = pending
                await self.h._store.async_save(stored)
                self.h.client.planning_submission_status.side_effect = ShsApiError('absent', code='planning_job_not_found')
                self.h.client.submit_optimisation.reset_mock()
                await self.h._poll_planning_job(pending)
                self.h.client.submit_optimisation.assert_not_awaited()
                self.assertNotIn('optimisation_pending_submission', self.rig.records.saved)
                self.assertEqual(self.rig.records.saved['optimisation_recapture'], {'replan_request_id': 'manual'})
                self.h._build_optimisation_snapshot.return_value = {'snapshot_id': 'fresh'}
                self.h.client.submit_optimisation.return_value = self.pending_receipt('fresh', 'fresh-job')
                await self.h.async_optimisation_push(force_plan=True)
                sent = self.h.client.submit_optimisation.call_args.args[0]
                self.assertEqual(sent['planning_exchange_version'], 3)
                self.assertEqual(sent['snapshot']['snapshot_id'], 'fresh')
                self.assertNotIn('replaces_job_id', sent)

    async def test_actual_horizon_end_recaptures_via_authority_without_local_age_gate(self):
        pending = await self.accept('manual')
        self.h.client.planning_status.return_value = {'job_id': 'job', 'state': 'failed',
            'code': 'capture_horizon_ended', 'recapture': {'replaces_job_id': 'job'}}
        await self.h._poll_planning_job(pending)
        self.assertEqual(self.rig.records.saved['optimisation_recapture'],
                         {'replan_request_id': 'manual', 'replaces_job_id': 'job'})
        self.h._build_optimisation_snapshot.return_value = {'snapshot_id': 'fresh'}
        await self.h.async_optimisation_push(force_plan=True)
        body = self.h.client.submit_optimisation.call_args.args[0]
        self.assertEqual(body['replaces_job_id'], 'job')
        self.assertEqual(body['replan_request_id'], 'manual')
        self.assertEqual(body['snapshot']['snapshot_id'], 'fresh')

    async def test_new_manual_request_replaces_queued_upgrade_purpose(self):
        pending = await self.accept('old-manual')
        self.h.client.planning_status.return_value = {'job_id': 'job', 'state': 'failed',
            'code': 'planner_upgraded', 'recapture': {'replaces_job_id': 'job'}}
        await self.h._poll_planning_job(pending)
        self.h._build_optimisation_snapshot.return_value = {'snapshot_id': 'new'}
        await self.h.async_optimisation_push(force_plan=True, replan_request_id='new-manual')
        sent = self.h.client.submit_optimisation.call_args.args[0]
        self.assertEqual(sent['replan_request_id'], 'new-manual')
        self.assertNotIn('replaces_job_id', sent)
        self.assertNotIn('optimisation_recapture', self.rig.records.saved)

    async def test_late_upgraded_old_job_cannot_queue_recapture_over_new_submission(self):
        pending = await self.accept('old-manual')
        self.h._build_optimisation_snapshot.return_value = {'snapshot_id': 'new'}
        self.h.client.submit_optimisation.side_effect = ShsApiError('lost', retryable=True)
        await self.h.async_optimisation_push(force_plan=True, replan_request_id='new-manual')
        self.h.client.planning_status.return_value = {'job_id': 'job', 'state': 'failed',
            'code': 'planner_upgraded', 'recapture': {'replaces_job_id': 'job'}}
        await self.h._poll_planning_job(pending)
        self.assertNotIn('optimisation_recapture', self.rig.records.saved)
        self.assertEqual(self.rig.records.saved['optimisation_pending_submission']['replan_request_id'], 'new-manual')

    async def test_host_delivery_loop_automatically_recaptures_upgraded_manual_job(self):
        await self.accept('manual')
        self.rig.now += timedelta(seconds=2)
        self.h.client.planning_status.return_value = {'job_id': 'job', 'state': 'failed',
            'code': 'planner_upgraded', 'recapture': {'replaces_job_id': 'job'}}
        self.h._build_optimisation_snapshot.return_value = {'snapshot_id': 'fresh'}
        submitted = asyncio.Event()
        async def submit(body):
            self.assertEqual(body['snapshot']['snapshot_id'], 'fresh')
            self.assertEqual(body['replaces_job_id'], 'job')
            self.assertEqual(body['replan_request_id'], 'manual')
            submitted.set()
            return self.pending_receipt('fresh', 'fresh-job')
        self.h.client.submit_optimisation.side_effect = submit
        task = asyncio.create_task(self.h.async_planning_delivery())
        try:
            await asyncio.wait_for(submitted.wait(), 1)
            await asyncio.sleep(0)
            self.assertEqual(self.rig.records.saved['optimisation_pending_job']['job_id'], 'fresh-job')
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_worker_retry_attention_and_due_time_survive_restart(self):
        pending = await self.accept('manual')
        self.h._pipeline_error('planning', 'Earlier plan failed')
        error = {'code': 'planning_worker_unavailable', 'detail': 'Synthetic worker restart'}
        self.h.client.planning_status.return_value = {**self.pending_receipt(), 'last_error': error, 'retry_after_ms': 30000}
        self.assertEqual(await self.h._poll_planning_job(pending), 30)
        saved = self.rig.records.saved['optimisation_pending_job']
        self.assertEqual(saved['last_error'], error)
        rig, restarted = await self.restart()
        self.assertEqual(restarted._delivery_delay(rig.records.saved['optimisation_pending_job']), 30)
        self.assertTrue(any(item['key'] == 'pipeline:planning_retry' for item in restarted.attention_items))
        self.h.client.planning_status.return_value = self.pending_receipt()
        await self.h._poll_planning_job(saved)
        notices = {item['key'] for item in self.h.attention_items}
        self.assertNotIn('pipeline:planning_retry', notices)
        self.assertIn('pipeline:planning', notices)



class OwnerDurabilityTests(IsolatedAsyncioTestCase):
    setUp = execution.ExecutionReplanTests.setUp
    observe = execution.ExecutionReplanTests.observe

    async def test_harmless_off_slot_recovery_cannot_poison_real_pool_repair(self):
        off = {**self.slot, 'ev_target_current_a': 0, 'ev_w': 0}
        await self.observe('ev', 'faulted', 'synthetic-off-fault', slot=off)
        await self.h.async_execution_observed('ev', self.plan, off, None)
        await self.observe('pool', 'faulted', 'synthetic-pool-fault')
        await self.h.async_optimisation_push()
        body = self.h.client.submit_optimisation.call_args.args[0]
        self.assertEqual([row['owner'] for row in body['execution_replan']['changes']], ['$pool'])
        self.assertEqual([row['owner'] for row in body['snapshot']['execution_availability']], ['$pool'])

    async def test_failed_unpublished_fault_prunes_its_recovery_before_next_capture(self):
        await self.observe()
        await self.h.async_optimisation_push()
        pending = self.rig.records.saved['optimisation_pending_job']
        await self.h.async_execution_observed('ev', self.plan, self.slot, None)
        self.h.client.planning_status.return_value = {'job_id': 'job', 'state': 'failed', 'code': 'invalid_snapshot'}
        await self.h._poll_planning_job(pending)
        await self.observe('pool', 'faulted', 'synthetic-pool-fault')
        await self.h.async_optimisation_push()
        body = self.h.client.submit_optimisation.call_args.args[0]
        self.assertEqual([row['owner'] for row in body['execution_replan']['changes']], ['$pool'])

    async def test_ack_failure_blocks_evidence_until_exact_ack_recovers(self):
        stored = await self.h._store.async_load()
        await self.h._accept_returned_plan(stored, self.candidate)
        await self.h._store.async_save(stored)
        await self.observe()
        self.h.client.acknowledge_optimisation_plan.side_effect = ShsApiError('lost', retryable=True)
        await self.h.async_optimisation_push(force_plan=True)
        self.h.client.submit_optimisation.assert_not_awaited()
        self.assertIn('$ev', self.rig.records.saved['execution_replanning']['pending'])
        self.rig.now += timedelta(seconds=10)
        self.h.client.acknowledge_optimisation_plan.side_effect = None
        await self.h.async_optimisation_push(force_plan=True)
        self.assertIn('execution_replan', self.h.client.submit_optimisation.call_args.args[0])

    async def test_recapture_keeps_new_owner_evidence_and_never_resurrects_recovered_fault(self):
        await self.observe()
        await self.h.async_optimisation_push()
        pending = self.rig.records.saved['optimisation_pending_job']
        old_request = pending['execution_replan']['request_id']
        await self.h.async_execution_observed('ev', self.plan, self.slot, None)
        await self.observe('pool', 'faulted', 'synthetic-pool-fault')
        self.h.client.planning_status.return_value = {'job_id': 'job', 'state': 'failed',
            'code': 'planner_upgraded', 'recapture': {'replaces_job_id': 'job'}}
        await self.h._poll_planning_job(pending)
        await self.h.async_optimisation_push(force_plan=True)
        body = self.h.client.submit_optimisation.call_args.args[0]
        self.assertEqual(body['replaces_job_id'], 'job')
        self.assertNotEqual(body['execution_replan']['request_id'], old_request)
        self.assertEqual(body['execution_replan']['accepted_plan_id'], self.plan['plan_id'])
        self.assertEqual([row['owner'] for row in body['execution_replan']['changes']], ['$pool'])
        self.assertEqual([row['owner'] for row in body['snapshot']['execution_availability']], ['$pool'])
    async def test_positive_capacity_recovery_replans_suppressed_owner_without_executable_slot(self):
        await self.observe('ev', 'temporarily_absent', 'house_import_capacity')
        await self.observe('pool', 'faulted', 'synthetic-actuator-refusal')
        stored = await self.h._store.async_load()
        captured = deepcopy(stored['execution_replanning']['owners'])
        stored['execution_replanning']['pending'] = {}
        self.h.optimisation_plan['execution_availability'] = list(captured.values())
        await self.h._store.async_save(stored)
        await self.h.async_capacity_recovered(['$ev', '$pool'])
        journal = self.rig.records.saved['execution_replanning']
        self.assertEqual(journal['pending'], {'$ev': {'owner': '$ev', 'status': 'recovered',
            'cause': 'execution_recovered', 'blocked_intervals': []}})
        self.assertEqual(journal['owners'], {'$pool': captured['$pool']})
        await self.h.async_optimisation_push()
        body = self.h.client.submit_optimisation.call_args.args[0]
        self.assertEqual([change['owner'] for change in body['execution_replan']['changes']], ['$ev'])
        self.assertEqual(body['snapshot']['execution_availability'], [captured['$pool']])
        await self.h.async_capacity_recovered(['$ev'])
        self.assertEqual(self.rig.records.saved['execution_replanning']['pending'], {})

    async def test_capacity_recovery_does_not_grant_control_or_replan_harmless_off_fault(self):
        off = {**self.slot, 'ev_target_current_a': 0, 'ev_w': 0}
        await self.observe('ev', 'temporarily_absent', 'house_import_capacity', slot=off)
        self.rig.options['device_modes']['$ev'] = 'control_verification'
        await self.h.async_capacity_recovered(['$ev'])
        self.assertIn('$ev', self.rig.records.saved['execution_replanning']['owners'])
        self.rig.options['device_modes']['$ev'] = 'controlling'
        await self.h.async_capacity_recovered(['$ev'])
        self.assertEqual(self.rig.records.saved['execution_replanning']['pending'], {})
        self.assertEqual(self.rig.records.saved['execution_replanning']['owners'], {})



class ReceiptCrashTests(IsolatedAsyncioTestCase):
    setUp = jobs.PlanningJobTests.setUp
    published = jobs.PlanningJobTests.published
    restart = DeliveryDurabilityTests.restart
    pending_receipt = DeliveryDurabilityTests.pending_receipt

    # Crash between HTTP acceptance and local consumption retains the job.
    async def test_crash_after_inline_publication_receipt_recovers_without_resolving(self):
        self.h.client.submit_optimisation.return_value = self.published()
        self.h._record_device_exchange.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.h.async_optimisation_push(force_plan=True, replan_request_id='manual')
        self.assertEqual(self.rig.records.saved['optimisation_pending_job']['job_id'], 'job')
        rig, restarted = await self.restart()
        restarted.client.planning_status.return_value = self.published()
        await restarted._poll_planning_job(rig.records.saved['optimisation_pending_job'])
        self.assertEqual(restarted.optimisation_plan, self.candidate)
        restarted.client.submit_optimisation.assert_awaited_once()
        self.assertNotIn('optimisation_pending_job', rig.records.saved)

    async def test_crash_after_inline_upgrade_receipt_retains_original_manual_purpose(self):
        self.h.client.submit_optimisation.return_value = {'job_id': 'job', 'state': 'failed',
            'code': 'planner_upgraded', 'recapture': {'replaces_job_id': 'job'}}
        self.h._record_device_exchange.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.h.async_optimisation_push(force_plan=True, replan_request_id='manual')
        rig, restarted = await self.restart()
        restarted.client.planning_status.return_value = self.h.client.submit_optimisation.return_value
        await restarted._poll_planning_job(rig.records.saved['optimisation_pending_job'])
        self.assertEqual(rig.records.saved['optimisation_recapture'],
                         {'replan_request_id': 'manual', 'replaces_job_id': 'job'})

    async def test_old_exchange_with_existing_receipt_recovers_job_without_fresh_capture(self):
        stored = await self.h._store.async_load()
        pending = {'snapshot_id': 'old', 'devices': [], 'replan_request_id': 'manual',
                   'body': {'planning_exchange_version': 2, 'snapshot': {'snapshot_id': 'old'}}}
        stored['optimisation_pending_submission'] = pending
        await self.h._store.async_save(stored)
        self.h.client.planning_submission_status.return_value = self.pending_receipt('old')
        await self.h._poll_planning_job(pending)
        self.assertEqual(self.rig.records.saved['optimisation_pending_job']['job_id'], 'job')
        self.assertNotIn('optimisation_recapture', self.rig.records.saved)
        self.h.client.submit_optimisation.assert_not_awaited()


class ApiDispositionTests(IsolatedAsyncioTestCase):
    async def response(self, status, data):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        class Session:
            @asynccontextmanager
            async def request(self, *args, **kwargs):
                yield SimpleNamespace(status=status, headers={},
                    read=AsyncMock(return_value=json.dumps(data).encode()), json=AsyncMock(return_value=data))
        return await ShsApiClient(Session(), 'https://synthetic.invalid', 'synthetic-token')._request('POST', 'synthetic')

    async def test_structured_terminal_503_is_actionable_and_transient_429_is_retryable(self):
        from shs_core.api import ShsConnectionError
        with self.assertRaises(ShsApiError) as raised:
            await self.response(503, {'error_info': {'code': 'planner_configuration_missing',
                'message': 'Configure planner', 'retryable': False}})
        self.assertNotIsInstance(raised.exception, ShsConnectionError)
        self.assertFalse(raised.exception.retryable)
        with self.assertRaises(ShsConnectionError) as raised:
            await self.response(429, {})
        self.assertTrue(raised.exception.retryable)

    async def test_manual_and_recapture_purposes_cannot_be_acknowledged_as_telemetry(self):
        client = ShsApiClient(None, 'https://synthetic.invalid', 'synthetic-token')
        client._request = AsyncMock(return_value={})
        for purpose in ({'replan_request_id': 'manual'}, {'replaces_job_id': 'old-job'}):
            with self.subTest(purpose=purpose), self.assertRaises(ShsApiError):
                await client.submit_optimisation(ShsApiClient.prepare_optimisation_submission(
                    [], {'snapshot_id': 'fresh'}, **purpose))

    async def test_recapture_directive_cannot_reference_another_job_or_error(self):
        receipt = {'job_id': 'job', 'state': 'failed', 'pending': False,
                   'code': 'planner_upgraded', 'recapture': {'replaces_job_id': 'job'}}
        ShsApiClient._validate_planning_receipt(receipt)
        ShsApiClient._validate_planning_receipt({**receipt, 'code': 'capture_horizon_ended'})
        for change in ({'recapture': {'replaces_job_id': 'other'}}, {'code': 'invalid_snapshot'},
                       {'recapture': {'replaces_job_id': 'job', 'force': True}}):
            with self.subTest(change=change), self.assertRaises(ShsApiError):
                ShsApiClient._validate_planning_receipt({**receipt, **change})


class PublishedHorizonRecoveryTests(IsolatedAsyncioTestCase):
    setUp = execution.ExecutionReplanTests.setUp
    observe = execution.ExecutionReplanTests.observe
    accept = jobs.PlanningJobTests.accept
    published = jobs.PlanningJobTests.published

    def after_horizon(self):
        from datetime import datetime
        self.rig.now = datetime.fromisoformat(self.candidate['plans']['priority']['slots'][-1]['start']) + timedelta(minutes=15, seconds=1)

    async def deliver_late(self, *, manual='original-manual'):
        pending = await self.accept(manual)
        self.after_horizon()
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(pending)
        return pending

    async def test_late_publication_automatically_requests_fresh_capture_after_exact_ack(self):
        pending = await self.accept('original-manual')
        self.after_horizon()
        self.h.client.planning_status.return_value = self.published()
        order = []
        async def ack(plan, outcome, error):
            order.append('ack')
            self.assertEqual(plan['plan_id'], self.candidate['plan_id'])
            self.assertEqual(outcome, 'rejected')
        async def request(**kwargs):
            order.append('request')
            self.assertEqual(kwargs, {'reason': 'capture_horizon_ended'})
            return 'current-request'
        submitted = asyncio.Event()
        async def submit(body):
            order.append('capture')
            self.assertEqual(body['replan_request_id'], 'current-request')
            self.assertNotIn('execution_replan', body)
            self.assertNotIn('replaces_job_id', body)
            submitted.set()
            return {'job_id': 'fresh-job', 'state': 'pending', 'pending': True, 'retry_after_ms': 1000}
        self.h.client.acknowledge_optimisation_plan.side_effect = ack
        self.h.client.request_replan = AsyncMock(side_effect=request)
        self.h.client.submit_optimisation.side_effect = submit
        task = asyncio.create_task(self.h.async_planning_delivery())
        try:
            await asyncio.wait_for(submitted.wait(), 1)
            await asyncio.sleep(0)
            self.assertEqual(order, ['ack', 'request', 'capture'])
            self.assertEqual(self.rig.records.saved['optimisation_pending_job']['job_id'], 'fresh-job')
            self.assertEqual(self.h.optimisation_plan, self.plan)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_ack_loss_and_restart_retain_origin_then_consume_already_answered_request_id(self):
        self.h.client.acknowledge_optimisation_plan.side_effect = ShsApiError('ACK lost', retryable=True)
        await self.deliver_late()
        intent = self.rig.records.saved['optimisation_recapture']
        self.assertEqual(intent['published_job_id'], 'job')
        self.assertEqual(intent['original_replan_request_id'], 'original-manual')
        self.h.client.request_replan = AsyncMock(return_value='original-manual')
        self.h._store = DurableRecord(self.rig.records, encoded, json.loads)
        await self.h.async_restore_plan()
        self.assertEqual(self.h._answered_replan_request_id, 'original-manual')
        await self.h.async_optimisation_push(force_plan=True)
        self.h.client.request_replan.assert_not_awaited()
        self.rig.now += timedelta(seconds=10)
        self.h.client.acknowledge_optimisation_plan.side_effect = None
        await self.h.async_optimisation_push(force_plan=True)
        self.h.client.request_replan.assert_awaited_once()
        self.assertEqual(self.h.client.submit_optimisation.call_args.args[0]['replan_request_id'], 'original-manual')
        self.assertNotIn('optimisation_recapture', self.rig.records.saved)

    async def test_unresolved_fault_evidence_survives_normal_request_capture_and_admission(self):
        await self.observe('ev', 'faulted', 'synthetic-ev-fault')
        pending = await self.deliver_late(manual=None)
        await self.observe('pool', 'faulted', 'synthetic-pool-fault')
        expected = deepcopy(self.rig.records.saved['execution_replanning']['owners'])
        self.assertEqual(pending['execution_replan']['accepted_plan_id'], self.plan['plan_id'])
        self.h.client.request_replan = AsyncMock(return_value='current-request')
        entered = []
        async def submit(body):
            entered.append(deepcopy(body))
            self.assertEqual(set(self.rig.records.saved['execution_replanning']['pending']), {'$ev', '$pool'})
            raise ShsApiError('capture reply lost', retryable=True)
        self.h.client.submit_optimisation.side_effect = submit
        await self.h.async_optimisation_push(force_plan=True)
        body = entered[0]
        self.assertEqual(body['replan_request_id'], 'current-request')
        self.assertEqual(body['execution_replan']['accepted_plan_id'], self.plan['plan_id'])
        self.assertEqual({row['owner'] for row in body['execution_replan']['changes']}, {'$ev', '$pool'})
        self.assertEqual({row['owner']: row for row in body['snapshot']['execution_availability']}, expected)
        self.assertEqual(self.rig.records.saved['execution_replanning']['owners'], expected)
        self.assertEqual(set(self.rig.records.saved['execution_replanning']['pending']), {'$ev', '$pool'})
        outbox = self.rig.records.saved['optimisation_pending_submission']
        self.h.client.planning_submission_status.return_value = {
            'job_id': 'fresh-job', 'snapshot_id': 'snapshot', 'state': 'pending', 'pending': True, 'retry_after_ms': 1000}
        await self.h._poll_planning_job(outbox)
        self.assertEqual(self.rig.records.saved['execution_replanning']['owners'], expected)
        self.assertEqual(self.rig.records.saved['execution_replanning']['pending'], {})

    async def test_lost_request_reply_is_paced_and_recovers_without_newer_request_overwrite(self):
        await self.deliver_late()
        self.h.client.request_replan = AsyncMock(side_effect=ShsApiError('request reply lost', retryable=True))
        await self.h.async_optimisation_push(force_plan=True)
        intent = self.rig.records.saved['optimisation_recapture']
        self.assertIn('retry_at', intent)
        self.assertEqual(intent['request_reason'], 'capture_horizon_ended')
        await self.h.async_optimisation_push(force_plan=True)
        self.h.client.request_replan.assert_awaited_once()
        self.rig.now += timedelta(seconds=10)
        self.h.client.request_replan.side_effect = None
        self.h.client.request_replan.return_value = 'newer-outstanding-manual'
        self.h._build_optimisation_snapshot.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.h.async_optimisation_push(force_plan=True)
        self.assertEqual(self.rig.records.saved['optimisation_recapture']['replan_request_id'], 'newer-outstanding-manual')
        self.h._store = DurableRecord(self.rig.records, encoded, json.loads)
        self.h._build_optimisation_snapshot.side_effect = None
        self.h._build_optimisation_snapshot.return_value = {'snapshot_id': 'fresh'}
        await self.h.async_optimisation_push(force_plan=True)
        self.assertEqual(self.h.client.request_replan.await_count, 2)
        self.assertEqual(self.h.client.submit_optimisation.call_args.args[0]['replan_request_id'], 'newer-outstanding-manual')

    async def test_early_validity_expiry_or_schema_rejection_is_not_horizon_refresh_authority(self):
        for schema in (9, 999):
            with self.subTest(schema=schema):
                pending = await self.accept('manual')
                candidate = deepcopy(self.candidate)
                candidate['schema_version'] = schema
                candidate['valid_until'] = (self.rig.now - timedelta(seconds=1)).isoformat()
                self.h.client.planning_status.return_value = {**self.published(), 'plan': candidate}
                await self.h._poll_planning_job(pending)
                self.assertNotIn('optimisation_recapture', self.rig.records.saved)

    async def test_newer_manual_capture_retires_pending_horizon_request_intent(self):
        await self.deliver_late()
        self.h.client.request_replan = AsyncMock(return_value='obsolete')
        await self.h.async_optimisation_push(force_plan=True, replan_request_id='newer-manual')
        self.h.client.request_replan.assert_not_awaited()
        self.assertEqual(self.h.client.submit_optimisation.call_args.args[0]['replan_request_id'], 'newer-manual')
        self.assertNotIn('optimisation_recapture', self.rig.records.saved)
