"""Synthetic accepted-owner, manual-intent and exact-ACK interleavings."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock

import test_planning_jobs as jobs
import test_execution_replan as execution
import test_battery_runtime as battery
from shs_core.api import ShsApiClient, ShsApiError
from shs_core.command_journal import encoded
from shs_core.durable_record import DurableRecord
from shs_core import execution_replan as evidence


class AcceptedDeliveryTests(IsolatedAsyncioTestCase):
    setUp = execution.ExecutionReplanTests.setUp
    observe = execution.ExecutionReplanTests.observe
    accept = jobs.PlanningJobTests.accept

    async def pending_ack(self):
        stored = await self.h._store.async_load()
        stored['optimisation_pending_plan_ack'] = {
            'plan': {key: self.plan[key] for key in ('plan_id', 'snapshot_id', 'schema_version')},
            'outcome': 'accepted', 'error': None}
        await self.h._store.async_save(stored)
        self.h.client.acknowledge_optimisation_plan.side_effect = ShsApiError('ACK response lost', retryable=True)

    async def test_manual_intent_persists_before_ack_gate_and_telemetry_continues(self):
        await self.pending_ack()
        self.h._price_quarters = lambda *args: [{'start': self.rig.now.isoformat()}]
        self.h.client.submit_optimisation.return_value = {}
        await self.h.async_answer_replan('new-manual')
        self.assertEqual(self.rig.records.saved['optimisation_requested_replan'], {'replan_request_id': 'new-manual'})
        self.h._build_optimisation_snapshot.assert_not_awaited()
        body = self.h.client.submit_optimisation.call_args.args[0]
        self.assertNotIn('snapshot', body)
        self.assertTrue(body['price_slots'])
        self.h.client.report_replan_failure.assert_not_awaited()
        # A restarted delivery owner consumes durable intent, without another click.
        self.h._store = DurableRecord(self.rig.records, encoded, json.loads)
        await self.h.async_restore_plan()
        self.rig.now += timedelta(seconds=10)
        self.h.client.acknowledge_optimisation_plan.side_effect = None
        submitted = asyncio.Event()
        async def accepted(body):
            self.assertEqual(body['replan_request_id'], 'new-manual')
            submitted.set()
            return {'job_id': 'new-job', 'state': 'pending', 'pending': True, 'retry_after_ms': 1000}
        self.h.client.submit_optimisation.side_effect = accepted
        task = asyncio.create_task(self.h.async_planning_delivery())
        try:
            await asyncio.wait_for(submitted.wait(), 1)
            await asyncio.sleep(0)
            self.assertNotIn('optimisation_requested_replan', self.rig.records.saved)
            self.assertEqual(self.rig.records.saved['optimisation_pending_job']['replan_request_id'], 'new-manual')
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_older_late_admission_cannot_erase_newer_queued_manual_intent(self):
        self.h.client.submit_optimisation.side_effect = ShsApiError('old reply lost', retryable=True)
        await self.h.async_answer_replan('old-manual')
        old = self.rig.records.saved['optimisation_pending_submission']
        entered, finish = asyncio.Event(), asyncio.Event()
        async def delayed(_):
            entered.set()
            await finish.wait()
            return {'job_id': 'old-job', 'state': 'pending', 'pending': True, 'retry_after_ms': 1000}
        self.h.client.planning_submission_status.side_effect = delayed
        task = asyncio.create_task(self.h._poll_planning_job(old))
        await entered.wait()
        await self.pending_ack()
        await self.h.async_answer_replan('newer-manual')
        finish.set()
        await task
        self.assertEqual(self.rig.records.saved['optimisation_requested_replan']['replan_request_id'], 'newer-manual')
        self.assertEqual(self.rig.records.saved['optimisation_pending_job']['replan_request_id'], 'old-manual')
        self.rig.now += timedelta(seconds=10)
        self.h.client.acknowledge_optimisation_plan.side_effect = None
        self.h.client.submit_optimisation.side_effect = None
        self.h.client.submit_optimisation.return_value = {'job_id': 'new-job', 'state': 'pending', 'pending': True, 'retry_after_ms': 1000}
        await self.h.async_optimisation_push()
        self.assertEqual(self.h.client.submit_optimisation.call_args.args[0]['replan_request_id'], 'newer-manual')
        self.assertNotIn('optimisation_requested_replan', self.rig.records.saved)

    async def test_ordinary_snapshot_also_waits_for_exact_ack(self):
        await self.pending_ack()
        self.h._price_quarters = lambda *args: [{'start': self.rig.now.isoformat()}]
        self.h.client.submit_optimisation.return_value = {}
        await self.h.async_optimisation_push(force_plan=True)
        self.h._build_optimisation_snapshot.assert_not_awaited()
        self.assertNotIn('snapshot', self.h.client.submit_optimisation.call_args.args[0])

    async def test_manual_and_owner_purposes_share_one_exact_capture(self):
        await self.observe('ev', 'faulted', 'synthetic-ev-refusal')
        fault = deepcopy(self.rig.records.saved['execution_replanning']['owners']['$ev'])
        await self.h.async_optimisation_push(force_plan=True, replan_request_id='manual')
        body = self.h.client.submit_optimisation.call_args.args[0]
        self.assertEqual(body['replan_request_id'], 'manual')
        self.assertEqual(body['execution_replan']['accepted_plan_id'], self.plan['plan_id'])
        self.assertEqual(body['execution_replan']['changes'], [fault])
        self.assertEqual(body['snapshot']['execution_availability'], [fault])
        job = self.rig.records.saved['optimisation_pending_job']
        self.assertEqual(job['replan_request_id'], 'manual')
        self.assertEqual(job['execution_replan'], body['execution_replan'])
        self.assertEqual(self.rig.records.saved['execution_replanning']['owners']['$ev'], fault)
        self.assertFalse(self.rig.records.saved['execution_replanning']['pending'])

    async def test_mirror_keeps_availability_but_never_claims_production_execution_authority(self):
        await self.observe('ev', 'faulted', 'synthetic-production-refusal')
        original = deepcopy(self.rig.records.saved['execution_replanning'])
        self.h.control_authority = False
        await self.h.async_optimisation_push(force_plan=True, replan_request_id='test-manual')
        body = self.h.client.submit_optimisation.call_args.args[0]
        self.assertNotIn('execution_replan', body)
        self.assertEqual(body['snapshot']['execution_availability'], list(original['owners'].values()))
        self.assertEqual(body['replan_request_id'], 'test-manual')
        self.assertEqual(self.rig.records.saved['execution_replanning']['pending'], original['pending'])
        self.h.client.acknowledge_optimisation_plan.assert_not_awaited()
        stored = await self.h._store.async_load()
        stored.pop('optimisation_pending_job')
        await self.h._store.async_save(stored)
        self.h.async_optimisation_push = AsyncMock()
        task = asyncio.create_task(self.h.async_planning_delivery())
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.h.async_optimisation_push.assert_not_awaited()
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_actual_409_recapture_disposition_uses_receipt_then_fresh_capture(self):
        bodies = []
        class Session:
            @asynccontextmanager
            async def request(session, method, url, **kwargs):
                body = deepcopy(kwargs['json'])
                bodies.append(body)
                if 'snapshot_id' in body:
                    status, payload = 404, {'error_info': {'code': 'planning_job_not_found', 'message': 'absent', 'retryable': False}}
                elif body['snapshot']['snapshot_id'] == 'snapshot':
                    status, payload = 409, {'error_info': {'code': 'accepted_reference_changed', 'message': 'Ownership advanced', 'retryable': False, 'details': {'recapture': True}}}
                else:
                    status, payload = 202, {'api_version': 1, 'ok': True, 'request_id': 'result', 'data': {
                        'job_id': 'fresh-job', 'snapshot_id': 'fresh', 'state': 'pending', 'pending': True, 'retry_after_ms': 1000}}
                yield SimpleNamespace(status=status, headers={}, read=AsyncMock(return_value=json.dumps(payload).encode()), json=AsyncMock(return_value=payload))
        self.h.client = ShsApiClient(Session(), 'https://synthetic.invalid', 'synthetic-token')
        self.h.client.report_replan_failure = AsyncMock()
        await self.observe('ev', 'faulted', 'synthetic-refusal')
        await self.h.async_answer_replan('manual')
        pending = self.rig.records.saved['optimisation_pending_submission']
        self.assertTrue(pending['recapture_required'])
        await self.h._poll_planning_job(pending)
        self.assertEqual([body.get('snapshot_id') for body in bodies], [None, 'snapshot'])
        self.assertEqual(self.rig.records.saved['optimisation_recapture']['replan_request_id'], 'manual')
        capture = self.h._build_optimisation_snapshot.side_effect
        async def fresh(*args):
            value = await capture(*args)
            value['snapshot_id'] = 'fresh'
            return value
        self.h._build_optimisation_snapshot.side_effect = fresh
        await self.h.async_optimisation_push(force_plan=True)
        self.assertEqual(bodies[-1]['snapshot']['snapshot_id'], 'fresh')
        self.assertEqual(bodies[-1]['replan_request_id'], 'manual')
        self.assertNotIn('replaces_job_id', bodies[-1])
        self.assertNotEqual(bodies[-1]['execution_replan']['request_id'], bodies[0]['execution_replan']['request_id'])
        self.h.client.report_replan_failure.assert_not_awaited()

    async def test_ownership_ticket_uses_current_household_reference_and_manual_purpose(self):
        await self.observe('ev', 'faulted', 'synthetic-refusal')
        pending = await self.accept('manual')
        old_request = pending['execution_replan']['request_id']
        self.h.client.planning_status.return_value = {'job_id': 'job', 'state': 'failed', 'code': 'accepted_reference_changed', 'recapture': {'replaces_job_id': 'job'}}
        await self.h._poll_planning_job(pending)
        self.h.optimisation_plan = deepcopy(self.candidate)
        await self.h.async_optimisation_push(force_plan=True)
        body = self.h.client.submit_optimisation.call_args.args[0]
        self.assertEqual(body['replan_request_id'], 'manual')
        self.assertEqual(body['replaces_job_id'], 'job')
        self.assertEqual(body['execution_replan']['accepted_plan_id'], self.candidate['plan_id'])
        self.assertNotEqual(body['execution_replan']['request_id'], old_request)

    async def test_late_failure_does_not_resurrect_old_cause_after_new_off_slot_fault(self):
        await self.observe('ev', 'faulted', 'cause-A')
        pending = await self.accept()
        off = {**self.slot, 'ev_target_current_a': 0, 'ev_w': 0}
        await self.observe('ev', 'faulted', 'cause-B', slot=off)
        self.h.client.planning_status.return_value = {'job_id': 'job', 'state': 'failed', 'code': 'invalid_snapshot'}
        await self.h._poll_planning_job(pending)
        journal = self.rig.records.saved['execution_replanning']
        self.assertEqual(journal['owners']['$ev']['cause'], 'cause-B')
        self.assertFalse(journal['pending'])

    async def test_capacity_curtailment_only_masks_current_opportunity(self):
        await self.observe('ev', 'temporarily_absent', 'house_import_capacity')
        await self.h.async_optimisation_push()
        change = self.h.client.submit_optimisation.call_args.args[0]['execution_replan']['changes'][0]
        self.assertEqual(change['status'], 'temporarily_absent')
        self.assertEqual(len(change['blocked_intervals']), 1)
        later = {**self.slot, 'start': (self.rig.now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).isoformat()}
        await self.observe('ev', 'temporarily_absent', 'house_import_capacity', slot=later)
        self.assertNotEqual(self.rig.records.saved['execution_replanning']['pending']['$ev']['blocked_intervals'], change['blocked_intervals'])


class CurrentEvidenceTests(TestCase):
    def test_failed_capture_requeues_current_fact_not_older_admitted_cause(self):
        slot = {'start': '2026-10-23T10:00:00Z', 'ev_target_current_a': 10}
        journal = {}
        evidence.observe(journal, '$ev', slot, {'status': 'faulted', 'cause': 'cause-A'})
        old = deepcopy(journal['pending']['$ev'])
        evidence.observe(journal, '$ev', slot, {'status': 'faulted', 'cause': 'cause-B'})
        current = deepcopy(journal['pending']['$ev'])
        evidence.admitted(journal, [current])
        evidence.failed(journal, [old])
        self.assertEqual(journal['pending']['$ev'], current)
        evidence.failed(journal, [{'owner': '$ev', 'status': 'recovered', 'cause': 'old-recovery', 'blocked_intervals': []}])
        self.assertEqual(journal['pending']['$ev'], current)


class IndependentBatteryEvidenceTests(IsolatedAsyncioTestCase):
    async def test_battery_evidence_uses_accepted_B_when_global_Q_has_no_work(self):
        rig = battery.Rig()
        await rig.start()
        self.addAsyncCleanup(rig.runtime.close)
        runtime = rig.runtime
        accepted = runtime.host.state.execution.account.contract
        rig.plan['plan_id'] = 'household-Q'
        rig.plan['plans']['priority']['slots'][0].update(battery_charge_w=0, battery_discharge_w=0)
        runtime._notify_planning = Mock()
        runtime._execution_observations.clear()
        runtime._execution_condition = None
        runtime._execution_had_work = False
        runtime._queue_execution({'status': 'faulted', 'cause': 'battery_command_failed'})
        observed_plan, slot, condition = runtime._execution_observations[-1]
        self.assertEqual(observed_plan['plan_id'], accepted.plan_id)
        self.assertNotEqual(observed_plan['plan_id'], rig.plan['plan_id'])
        self.assertTrue(evidence.requested('$battery', slot))
        self.assertEqual(slot['start'], '1970-01-01T00:00:00.000Z')
        feedback = await runtime.capture_feedback(5000000, 'synthetic-actual-B')
        self.assertEqual(feedback['previous_contract_id'], accepted.id)
        self.assertEqual(runtime.host.state.execution.account.contract, accepted)

    async def test_positive_battery_recovery_clears_prior_fault_after_B_horizon(self):
        rig = battery.Rig()
        await rig.start()
        self.addAsyncCleanup(rig.runtime.close)
        runtime = rig.runtime
        runtime._notify_planning = Mock()
        runtime._execution_observations.clear()
        runtime._execution_condition = None
        runtime._execution_had_work = False
        runtime._queue_execution({'status': 'faulted', 'cause': 'battery_observation_unavailable'})
        accepted = runtime.host.state.execution.account.contract
        rig.now = accepted.valid_until_ms + 1
        runtime._queue_execution(None)
        self.assertEqual(runtime._execution_observations[-1][0]['plan_id'], accepted.plan_id)
        self.assertIsNone(runtime._execution_observations[-1][2])
        self.assertFalse(evidence.requested('$battery', runtime._execution_observations[-1][1]))

    async def test_unaccepted_global_charge_cannot_fabricate_work_without_B(self):
        rig = battery.Rig()
        rig.plan['plans']['priority']['slots'][0]['battery_charge_w'] = 4000
        rig.runtime._notify_planning = Mock()
        rig.runtime._queue_execution({'status': 'faulted', 'cause': 'battery_command_failed'})
        self.assertEqual(rig.runtime._execution_observations, [])
        rig.runtime._notify_planning.assert_not_called()
