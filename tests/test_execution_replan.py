"""Execution evidence travels through real household capture and receipt recovery."""
from copy import deepcopy
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from datetime import timedelta
import unittest
import asyncio
import test_controller as controller_tests
import test_battery_runtime as battery_tests
from unittest.mock import AsyncMock, Mock

import test_planning_jobs as jobs
from household_fixture import Rig
from shs_core import execution_replan as evidence
from shs_core.api import ShsApiClient, ShsApiError
from shs_core.api_contract import API_VERSION
from shs_core.device_operations import ControlObservationError
from shs_core.gateway_journal import AdmissionPending, GatewayRejected


class ExecutionReplanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        jobs.PlanningJobTests.setUp(self)
        self.rig.options['device_modes']['$ev'] = 'controlling'
        self.rig.options['device_modes']['$pool'] = 'controlling'
        self.slot = deepcopy(self.plan['plans']['priority']['slots'][0])
        self.slot['ev_target_current_a'] = 10
        async def capture(*args):
            return {'snapshot_id': 'snapshot', 'operating_scope': {
                'modes': deepcopy(self.rig.options['device_modes']),
                'device_owners': {'charger': '$ev', 'pool_heater': '$pool', 'heater': 'heater'}},
                'device_models': [{'key': 'charger'}, {'key': 'pool_heater'}, {'key': 'heater'}]}
        self.h._build_optimisation_snapshot.side_effect = capture

    async def observe(self, device='ev', status='temporarily_absent', cause='vehicle_disconnected', slot=None):
        await self.h.async_execution_observed(device, self.h.optimisation_plan, slot or self.slot,
            {'status': status, 'cause': cause})

    async def test_absence_is_durable_and_deduplicated_across_plan_ids_but_new_opportunity_replans(self):
        await self.observe()
        await self.h.async_optimisation_push()
        sent = self.h.client.submit_optimisation.call_args.args[0]
        request = sent['execution_replan']
        self.assertEqual(request['accepted_plan_id'], self.plan['plan_id'])
        self.assertEqual(request['changes'][0]['owner'], '$ev')
        self.assertIsNone(sent.get('replan_recommendation'))
        self.assertEqual(self.h.client.submit_optimisation.call_args.args[0].get("snapshot")['execution_availability'], request['changes'])
        pending = self.rig.records.saved['optimisation_pending_job']
        self.assertEqual(pending['execution_replan'], request)
        self.h.client.planning_status.return_value = jobs.PlanningJobTests.published(self)
        await self.h._poll_planning_job(pending)
        await self.observe()
        self.assertFalse(self.rig.records.saved['execution_replanning']['pending'])
        restarted = Rig(options=self.rig.options, stored=self.rig.records.saved).household
        restarted.optimisation_plan = self.h.optimisation_plan
        await restarted.async_execution_observed('ev', restarted.optimisation_plan, self.slot,
            {'status': 'temporarily_absent', 'cause': 'vehicle_disconnected'})
        self.assertFalse((await restarted._store.async_read())['execution_replanning']['pending'])
        later = {**self.slot, 'start': (self.rig.now + timedelta(minutes=15)).isoformat()}
        await self.observe(slot=later)
        self.assertTrue(self.rig.records.saved['execution_replanning']['pending'])

    async def test_fault_observed_while_off_forces_once_when_work_becomes_due(self):
        await self.observe('ev', 'faulted', 'charger_unavailable', slot={**self.slot, 'ev_target_current_a': 0})
        self.assertFalse(self.rig.records.saved['execution_replanning']['pending'])
        self.assertEqual(self.rig.records.saved['execution_replanning']['owners']['$ev']['blocked_intervals'], [])
        await self.observe('ev', 'faulted', 'charger_unavailable')
        self.assertTrue(self.rig.records.saved['execution_replanning']['pending']['$ev']['blocked_intervals'])
        await self.h.async_optimisation_push()
        pending = self.rig.records.saved['optimisation_pending_job']
        self.h.client.planning_status.return_value = jobs.PlanningJobTests.published(self)
        await self.h._poll_planning_job(pending)
        await self.observe('ev', 'faulted', 'charger_unavailable')
        self.assertFalse(self.rig.records.saved['execution_replanning']['pending'])

    async def test_multiple_faults_stay_unavailable_in_ordinary_capture_and_recovery_moves_suppressed_work(self):
        await self.observe('pool', 'faulted', 'ActuatorUnavailableError:switch.pool')
        await self.observe('ev')
        await self.h.async_optimisation_push()
        request = self.h.client.submit_optimisation.call_args.args[0]['execution_replan']
        self.assertEqual({c['owner'] for c in request['changes']}, {'$pool', '$ev'})
        pending = self.rig.records.saved['optimisation_pending_job']
        self.candidate['execution_availability'] = deepcopy(pending['execution_replan']['changes'])
        self.h.client.planning_status.return_value = jobs.PlanningJobTests.published(self)
        await self.h._poll_planning_job(pending)
        await self.h.async_optimisation_push(force_plan=True)
        snapshot = self.h.client.submit_optimisation.call_args.args[0].get("snapshot")
        self.assertIn('$pool', {c['owner'] for c in snapshot['execution_availability']})
        self.assertIsNone(self.h.client.submit_optimisation.call_args.args[0].get('execution_replan'))
        await self.h.async_execution_observed('pool', self.h.optimisation_plan, {**self.slot, 'pool_w': 0}, None)
        journal = self.rig.records.saved['execution_replanning']
        self.assertEqual(journal['pending']['$pool']['status'], 'recovered')
        self.assertNotIn('$pool', journal['owners'])

    async def test_generic_recovery_requires_the_specific_unavailable_source_and_never_guesses_write_repair(self):
        self.rig.options['device_modes']['heater'] = 'controlling'
        slot = {**self.slot, 'device_loads_w': {'heater': 1000},
                'device_commands': {'heater': {'type': 'switch_schedule', 'on_seconds': 900}}}
        await self.h.async_execution_observed('device:heater', self.plan, slot,
            {'status': 'faulted', 'cause': 'ActuatorUnavailableError:switch.heater',
             'recover_when_entities': ['switch.heater']})
        await self.h.async_optimisation_push()
        change = self.h.client.submit_optimisation.call_args.args[0]['execution_replan']['changes'][0]
        self.assertEqual(set(change), {'owner', 'status', 'cause', 'blocked_intervals'})
        off = {**slot, 'device_loads_w': {}, 'device_commands': {'heater': {'type': 'unavailable', 'reason': 'Fault'}}}
        await self.h.async_execution_observed('device:heater', self.plan, off, {'available_entities': ['sensor.power']})
        self.assertIn('heater', self.rig.records.saved['execution_replanning']['owners'])
        await self.h.async_execution_observed('device:heater', self.plan, off, {'available_entities': ['switch.heater']})
        self.assertEqual(self.rig.records.saved['execution_replanning']['pending']['heater']['status'], 'recovered')
        await self.h.async_execution_observed('device:heater', self.plan, slot, {'status': 'faulted', 'cause': 'service_failed'})
        await self.h.async_execution_observed('device:heater', self.plan, off, {'available_entities': ['switch.heater']})
        self.assertEqual(self.rig.records.saved['execution_replanning']['owners']['heater']['cause'], 'service_failed')

    def heater_slots(self):
        self.rig.options['device_modes']['heater'] = 'controlling'
        slot = {**self.slot, 'device_loads_w': {'heater': 1000}, 'device_commands': {'heater': {
            'type': 'setpoint', 'target_c': 20, 'minimum_c': 5, 'maximum_c': 90}}}
        # The replacement keeps the heater's forecast load but cannot run it.
        held = {**slot, 'device_commands': {'heater': {'type': 'unavailable', 'reason': 'Fault'}}}
        return slot, held

    async def test_refused_command_with_no_source_recovers_when_settings_change_and_not_before(self):
        slot, held = self.heater_slots()
        cause = 'GatewayRejected: Interrupted device operation; reconcile physical observations before a new intention'
        await self.h.async_execution_observed('device:heater', self.plan, slot, {'status': 'faulted', 'cause': cause})
        await self.h.async_optimisation_push()
        change = self.h.client.submit_optimisation.call_args.args[0]['execution_replan']['changes'][0]
        self.assertEqual(change, {'owner': 'heater', 'status': 'faulted', 'cause': cause,
                                  'blocked_intervals': change['blocked_intervals']})
        await self.h.async_execution_observed('device:heater', self.plan, held, {'available_entities': ['climate.heater']})
        self.assertIn('heater', self.rig.records.saved['execution_replanning']['owners'])
        self.rig.options['house_consumption_power_entity'] = 'sensor.replacement_house_power'
        await self.h.async_execution_observed('device:heater', self.plan, held, {'available_entities': ['climate.heater']})
        journal = self.rig.records.saved['execution_replanning']
        self.assertNotIn('heater', journal['owners'])
        self.assertNotIn('heater', journal.get('recovery_configuration', {}))
        self.assertEqual(journal['pending']['heater'], {'owner': 'heater', 'status': 'recovered',
            'cause': 'execution_recovered', 'blocked_intervals': []})

    async def test_refusal_journalled_without_settings_identity_is_retried_once(self):
        slot, held = self.heater_slots()
        await self.h.async_execution_observed('device:heater', self.plan, slot, {'status': 'faulted', 'cause': 'GatewayRejected:'})
        legacy = deepcopy(self.rig.records.saved)
        del legacy['execution_replanning']['recovery_configuration']
        restarted = Rig(options=self.rig.options, stored=legacy).household
        restarted.optimisation_plan = self.h.optimisation_plan
        await restarted.async_execution_observed('device:heater', self.plan, held, {'available_entities': []})
        journal = (await restarted._store.async_read())['execution_replanning']
        self.assertNotIn('heater', journal['owners'])
        self.assertEqual(journal['pending']['heater']['status'], 'recovered')
        # Refused again under the same settings, it holds: no replan loop.
        await restarted.async_execution_observed('device:heater', self.plan, slot,
            {'status': 'faulted', 'cause': 'GatewayRejected: Invalid mapped device intention'})
        await restarted.async_execution_observed('device:heater', self.plan, held, {'available_entities': []})
        journal = (await restarted._store.async_read())['execution_replanning']
        self.assertEqual(journal['owners']['heater']['cause'], 'GatewayRejected: Invalid mapped device intention')

    async def test_partial_quarter_reconnection_replans_ev_work_suppressed_by_replacement(self):
        self.assertGreater(self.rig.now.minute % 15, 0)
        await self.observe()
        await self.h.async_optimisation_push()
        pending = self.rig.records.saved['optimisation_pending_job']
        self.candidate['execution_availability'] = deepcopy(pending['execution_replan']['changes'])
        self.h.client.planning_status.return_value = jobs.PlanningJobTests.published(self)
        await self.h._poll_planning_job(pending)
        # The accepted replacement removed the missed current opportunity and
        # has no remaining charging in this same quarter.
        off = {**self.slot, 'ev_target_current_a': 0, 'ev_w': 0}
        await self.h.async_execution_observed('ev', self.h.optimisation_plan, off, None)
        journal = self.rig.records.saved['execution_replanning']
        self.assertNotIn('$ev', journal['owners'])
        self.assertEqual(journal['pending']['$ev']['status'], 'recovered')
        await self.h.async_optimisation_push()
        request = self.h.client.submit_optimisation.call_args.args[0]['execution_replan']
        self.assertEqual(request['accepted_plan_id'], self.candidate['plan_id'])
        self.assertEqual(request['changes'], [{'owner': '$ev', 'status': 'recovered',
            'cause': 'execution_recovered', 'blocked_intervals': []}])

    async def test_reconnection_during_absence_capture_queues_recovery_even_if_old_request_can_resume(self):
        await self.observe()
        await self.h.async_optimisation_push()
        await self.h.async_execution_observed('ev', self.plan, self.slot, None)
        self.assertEqual(self.rig.records.saved['execution_replanning']['pending']['$ev']['status'], 'recovered')

    async def test_mode_exit_ends_episode_without_sending_fictitious_recovery(self):
        await self.observe('pool', 'faulted', 'unavailable')
        self.rig.options['device_modes']['$pool'] = 'planning'
        await self.h.async_optimisation_push(force_plan=True)
        sent = self.h.client.submit_optimisation.call_args
        self.assertEqual(sent.args[0]['snapshot']['execution_availability'], [])
        self.assertIsNone(sent.args[0].get('execution_replan'))
        journal = self.rig.records.saved['execution_replanning']
        self.assertNotIn('$pool', journal['owners'])
        self.assertNotIn('$pool', journal['pending'])

    async def test_removed_generic_owner_cannot_poison_later_snapshot(self):
        self.rig.options['device_modes']['heater'] = 'controlling'
        slot = {**self.slot, 'device_loads_w': {'heater': 1000}}
        await self.h.async_execution_observed('device:heater', self.plan, slot,
            {'status': 'faulted', 'cause': 'ActuatorUnavailableError:switch.heater',
             'recover_when_entities': ['switch.heater']})
        capture = self.h._build_optimisation_snapshot.side_effect
        async def without_heater(*args):
            snapshot = await capture(*args)
            snapshot['device_models'] = [model for model in snapshot['device_models'] if model['key'] != 'heater']
            return snapshot
        self.h._build_optimisation_snapshot.side_effect = without_heater
        await self.h.async_optimisation_push(force_plan=True)
        sent = self.h.client.submit_optimisation.call_args
        self.assertEqual(sent.args[0]['snapshot']['execution_availability'], [])
        self.assertIsNone(sent.args[0].get('execution_replan'))
        journal = self.rig.records.saved['execution_replanning']
        for field in ('owners', 'pending', 'recovery_sources'):
            self.assertNotIn('heater', journal[field])

    async def test_explicit_exclusion_ends_native_owner_episode(self):
        await self.observe('pool', 'faulted', 'unavailable')
        self.rig.options['excluded_device_readings'] = ['$pool']
        await self.h.async_optimisation_push(force_plan=True)
        self.assertEqual(self.h.client.submit_optimisation.call_args.args[0].get("snapshot")['execution_availability'], [])
        self.assertIsNone(self.h.client.submit_optimisation.call_args.args[0].get('execution_replan'))

    async def test_failed_job_retains_evidence_without_per_tick_solve_loop(self):
        await self.observe()
        await self.h.async_optimisation_push()
        pending = self.rig.records.saved['optimisation_pending_job']
        self.h.client.planning_status.return_value = {'state': 'failed', 'code': 'infeasible', 'detail': 'Cannot retain unrelated request'}
        await self.h._poll_planning_job(pending)
        journal = self.rig.records.saved['execution_replanning']
        self.assertTrue(journal['pending'])
        self.assertTrue(journal['retry_on_exchange'])
        await self.observe()
        self.assertTrue(self.rig.records.saved['execution_replanning']['retry_on_exchange'])
        self.assertIn('pipeline:planning', {i['key'] for i in self.h.attention_items})

    async def test_lost_transport_recovers_exact_request_and_newer_failure_survives_receipt(self):
        self.h.client.submit_optimisation.side_effect = ShsApiError('reply lost', retryable=True)
        await self.observe()
        await self.h.async_optimisation_push()
        pending = self.rig.records.saved['optimisation_pending_submission']
        request = deepcopy(pending['execution_replan'])
        await self.observe('pool', 'faulted', 'pool_transport')
        self.h.client.planning_submission_status.return_value = {'state': 'pending', 'job_id': 'recovered', 'retry_after_ms': 1000}
        await self.h._poll_planning_job(pending)
        stored = self.rig.records.saved
        self.assertEqual(stored['optimisation_pending_job']['execution_replan'], request)
        self.assertEqual(set(stored['execution_replanning']['pending']), {'$pool'})

    async def test_recovery_resumes_executable_schedule_but_preserves_inflight_fault_recovery(self):
        await self.observe('pool', 'faulted', 'transport')
        await self.h.async_execution_observed('pool', self.plan, self.slot, None)
        self.assertFalse(self.rig.records.saved['execution_replanning']['pending'])
        await self.observe('pool', 'faulted', 'transport')
        await self.h.async_optimisation_push()
        await self.h.async_execution_observed('pool', self.plan, self.slot, None)
        self.assertEqual(self.rig.records.saved['execution_replanning']['pending']['$pool']['status'], 'recovered')

    async def test_superseded_execution_job_keeps_unmoved_work_pending(self):
        await self.observe()
        await self.h.async_optimisation_push()
        pending = self.rig.records.saved['optimisation_pending_job']
        self.h.client.planning_status.return_value = {'state': 'superseded'}
        await self.h._poll_planning_job(pending)
        self.assertIn('$ev', self.rig.records.saved['execution_replanning']['pending'])
        self.assertTrue(self.rig.records.saved['execution_replanning']['retry_on_exchange'])

    async def test_late_job_failure_cannot_resurrect_observed_ev_absence(self):
        await self.observe()
        await self.h.async_optimisation_push()
        pending = self.rig.records.saved['optimisation_pending_job']
        await self.h.async_execution_observed('ev', self.plan, self.slot, None)
        self.h.client.planning_status.return_value = {'state': 'failed', 'code': 'failed'}
        await self.h._poll_planning_job(pending)
        journal = self.rig.records.saved['execution_replanning']
        self.assertNotIn('$ev', journal['owners'])
        self.assertEqual(journal['pending']['$ev']['status'], 'recovered')

    async def test_verification_and_unscheduled_absence_never_force(self):
        self.rig.options['device_modes']['$ev'] = 'control_verification'
        await self.observe()
        self.assertNotIn('execution_replanning', self.rig.records.saved)
        self.rig.options['device_modes']['$ev'] = 'controlling'
        await self.observe(slot={**self.slot, 'ev_target_current_a': 0})
        self.assertFalse(self.rig.records.saved['execution_replanning']['pending'])
        await self.h.async_execution_observed('ev', self.plan, self.slot, None)
        self.assertFalse(self.rig.records.saved['execution_replanning']['pending'])

    async def test_host_delivery_forces_capture_without_manual_request_or_price_poll(self):
        await self.observe()
        accepted = asyncio.Event()
        async def submit(*args, **kwargs):
            accepted.set()
            return {'job_id': 'automatic', 'state': 'pending', 'retry_after_ms': 1000}
        self.h.client.submit_optimisation.side_effect = submit
        self.h.client.planning_status.return_value = {'state': 'pending', 'retry_after_ms': 1000}
        task = asyncio.create_task(self.h.async_planning_delivery())
        try:
            await asyncio.wait_for(accepted.wait(), timeout=1)
            self.assertEqual(self.h.client.submit_optimisation.await_count, 1)
            self.assertIsNotNone(self.h.client.submit_optimisation.call_args.args[0].get('execution_replan'))
            self.assertIsNone(self.h.client.submit_optimisation.call_args.args[0].get('replan_request_id'))
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_failed_capture_does_not_spin_on_its_own_exchange_wake(self):
        await self.observe()
        self.h.async_optimisation_push = AsyncMock()
        task = asyncio.create_task(self.h.async_planning_delivery())
        try:
            await asyncio.sleep(.03)
            self.assertEqual(self.h.async_optimisation_push.await_count, 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_api_serializes_automatic_evidence_only_with_snapshot(self):
        bodies = []
        class Session:
            @asynccontextmanager
            async def request(self, method, url, **options):
                bodies.append(json.loads(json.dumps(options['json'])))
                envelope = {'api_version': API_VERSION, 'ok': True, 'request_id': 'receipt', 'data': {'job_id': 'automatic', 'snapshot_id': 'snapshot', 'state': 'pending', 'pending': True, 'retry_after_ms': 1000}}
                yield SimpleNamespace(status=200, headers={}, read=AsyncMock(return_value=json.dumps(envelope).encode()),
                    json=AsyncMock(return_value=envelope))
        client = ShsApiClient(Session(), 'https://example.invalid', 'token')
        request = {'request_id': 'id', 'accepted_plan_id': 'plan', 'observed_at': self.rig.now.isoformat(), 'changes': []}
        await client.push_optimisation([], {'snapshot_id': 'snapshot'}, execution_replan=request)
        body = bodies[0]
        self.assertEqual(body['execution_replan'], request)
        self.assertNotIn('replan_request_id', body)
        with self.assertRaisesRegex(ValueError, 'requires a captured snapshot'):
            await client.push_optimisation([], execution_replan=request)


class EvidenceTests(unittest.TestCase):
    def test_battery_work_that_cannot_run_replans_only_from_100_wh(self):
        # User requirement, 26 October 2026: unmet battery energy below 100 Wh
        # in the blocked quarter does not warrant a replan.
        fault = {'status': 'faulted', 'cause': 'battery_command_failed'}
        slot = {'start': '2026-10-23T10:00:00Z', 'battery_charge_w': 399, 'battery_discharge_w': 0}
        self.assertFalse(evidence.requested('$battery', slot))
        journal = {}
        self.assertFalse(evidence.observe(journal, '$battery', slot, fault))
        self.assertEqual(journal['owners']['$battery']['blocked_intervals'], [])
        self.assertFalse(journal['pending'])
        due = {**slot, 'start': '2026-10-23T10:15:00Z', 'battery_charge_w': 0, 'battery_discharge_w': 400}
        self.assertTrue(evidence.requested('$battery', due))
        self.assertTrue(evidence.observe(journal, '$battery', due, fault))
        self.assertEqual(journal['pending']['$battery']['blocked_intervals'][0]['start'], '2026-10-23T10:15:00+00:00')

    def test_raw_device_identity_and_unrelated_pending_are_preserved(self):
        journal = {}
        slot = {'start': '2026-10-23T10:00:00Z', 'device_loads_w': {'heater': 1000}}
        self.assertEqual(evidence.owner_key('device:heater'), 'heater')
        self.assertTrue(evidence.observe(journal, 'heater', slot, {'status': 'faulted', 'cause': 'transport'}))
        evidence.admitted(journal, [{**journal['pending']['heater'], 'cause': 'older'}])
        self.assertIn('heater', journal['pending'])

    def test_refusal_keeps_its_reason_and_rewording_is_not_new_evidence(self):
        self.assertEqual(evidence.fault_cause(GatewayRejected('Device intention belongs to an older configuration or policy.')),
                         'GatewayRejected: Device intention belongs to an older configuration or policy')
        self.assertEqual(evidence.fault_cause(GatewayRejected('')), 'GatewayRejected:')
        self.assertEqual(evidence.fault_cause(ControlObservationError('switch.x is\nunavailable', 'switch.x', '')),
                         'ControlObservationError:switch.x switch.x is unavailable')
        journal = {}
        slot = {'start': '2026-10-23T10:00:00Z', 'device_loads_w': {'heater': 1000},
                'device_commands': {'heater': {'type': 'setpoint', 'target_c': 20, 'minimum_c': 5, 'maximum_c': 90}}}
        self.assertTrue(evidence.observe(journal, 'heater', slot, {'status': 'faulted', 'cause': 'GatewayRejected: first'}))
        evidence.admitted(journal, [journal['pending']['heater']])
        self.assertFalse(evidence.observe(journal, 'heater', slot, {'status': 'faulted', 'cause': 'GatewayRejected: second'}))
        self.assertEqual(journal['owners']['heater']['cause'], 'GatewayRejected: first')

    def test_unavailable_command_is_not_resumable_so_recovery_requests_a_plan(self):
        journal = {}
        slot = {'start': '2026-10-23T10:00:00Z', 'device_loads_w': {'heater': 1000},
                'device_commands': {'heater': {'type': 'unavailable', 'reason': 'Fault'}}}
        self.assertFalse(evidence.requested('heater', slot))
        evidence.observe(journal, 'heater', {**slot, 'device_commands': {}}, {'status': 'faulted', 'cause': 'GatewayRejected:'})
        evidence.admitted(journal, list(journal['pending'].values()))
        self.assertTrue(evidence.observe(journal, 'heater', slot, None))
        self.assertEqual(journal['pending']['heater']['status'], 'recovered')


class ControllerEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        controller_tests.ControllerTests.setUp(self)
        self.coordinator.async_execution_observed = AsyncMock()

    async def test_disconnected_requested_ev_emits_structured_evidence_and_reconnection_recovers(self):
        self.options['device_modes']['$ev'] = 'controlling'
        self.states['binary_sensor.connected'].state = 'off'
        await self.controller.async_start()
        args = self.coordinator.async_execution_observed.call_args.args
        self.assertEqual(args[0], 'ev')
        self.assertEqual(args[3], {'status': 'temporarily_absent', 'cause': 'vehicle_disconnected'})
        self.assertEqual(self.states['switch.charge'].state, 'off')
        self.states['binary_sensor.connected'].state = 'on'
        await self.controller.async_tick()
        self.assertIsNone(self.coordinator.async_execution_observed.call_args.args[3])
        self.assertEqual(self.states['switch.charge'].state, 'on')

    async def test_settings_admission_waits_without_fault_and_a_real_refusal_keeps_its_reason(self):
        self.options['device_modes']['$ev'] = 'controlling'
        empty = {'records': {}, 'overrides': {}, 'retired_pool_temperature_settings': {}}
        self.controller.devices = SimpleNamespace(ownership=AsyncMock(return_value=empty),
            synchronize=AsyncMock(return_value=empty),
            perform=AsyncMock(side_effect=AdmissionPending('Device intention belongs to an older configuration or policy')))
        await self.controller.async_start()
        self.coordinator.async_execution_observed.assert_not_awaited()
        self.assertEqual(self.controller.status['ev']['state'], 'pending')
        self.assertNotIn('ev', self.controller.failed)
        self.controller.devices.perform = AsyncMock(return_value=dict(ownership=empty, deadlines=[], reads=[],
            diagnostics={'commands': [], 'observations': {}}, result=None, reservation_required=False, reports={},
            error={'kind': 'admission', 'message': 'Reconcile and activate the current app session first', 'details': {}}))
        await self.controller.async_tick()
        self.coordinator.async_execution_observed.assert_not_awaited()
        self.assertIn('Reconcile and activate', self.controller.status['ev']['reason'])
        self.controller.devices.perform = AsyncMock(side_effect=GatewayRejected('Invalid EV intention'))
        await self.controller.async_tick()
        self.assertEqual(self.coordinator.async_execution_observed.call_args.args[3],
                         {'status': 'faulted', 'cause': 'GatewayRejected: Invalid EV intention'})
        self.assertEqual(self.controller.status['ev']['state'], 'fault')

    async def test_verification_never_observes_execution_failure(self):
        self.options['device_modes']['$ev'] = 'control_verification'
        self.states['binary_sensor.connected'].state = 'off'
        await self.controller.async_start()
        self.coordinator.async_execution_observed.assert_not_awaited()

    async def test_unavailable_pool_blocks_only_pool_and_preserves_last_setting(self):
        self.options['device_modes'].update({'$pool': 'controlling', '$ev': 'controlling'})
        await self.controller.async_start()
        self.assertEqual(self.states['switch.pool'].state, 'on')
        self.states['switch.pool'].state = 'unavailable'
        self.coordinator.async_execution_observed.reset_mock()
        await self.controller.async_tick()
        conditions = [call.args for call in self.coordinator.async_execution_observed.call_args_list]
        self.assertTrue(any(device == 'pool' and condition['status'] == 'faulted'
            for device, plan, slot, condition in conditions if condition))
        self.assertEqual(self.states['switch.charge'].state, 'on')
        self.assertEqual(self.states['switch.pool'].state, 'unavailable')

    async def test_unavailable_generic_command_only_probes_sources_without_writes(self):
        self.options['device_control_mappings']['heater'] = {
            'control_type': 'switch_schedule', 'actuator_entity_ids': ['switch.pool'], 'power': 'sensor.house_power'}
        slot = {**self.slot, 'device_commands': {'heater': {'type': 'unavailable', 'reason': 'Observed fault'}}}
        result = await self.controller.execute_device('device:heater', self.options, slot)
        self.assertEqual(result['state'], 'unsupported')
        self.assertEqual(set(result['execution_available_entities']), {'switch.pool', 'sensor.house_power'})
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.saved)


class BatteryEvidenceQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_battery_fault_seen_off_reaches_journal_when_accepted_work_becomes_due(self):
        from dataclasses import replace
        rig = battery_tests.Rig()
        await rig.start()
        self.addAsyncCleanup(rig.runtime.close)
        runtime = rig.runtime
        runtime._notify_planning = Mock()
        runtime._execution_observations.clear()
        runtime._execution_condition = None
        runtime._execution_had_work = False
        accepted = runtime.host.state.execution.account
        runtime._bootstrap = accepted
        runtime.host = None
        row = accepted.contract.intervals[0]
        off = replace(row, charge_ac_mwh=0, import_mwh=row.load_mwh,
                      stored_end_mwh=row.stored_start_mwh, rounding_mwh=0)
        runtime._bootstrap = replace(accepted, admissions=(*accepted.admissions[:-1],
            replace(accepted.admissions[-1], contract=replace(accepted.contract, intervals=(off,)))))
        condition = {'status': 'faulted', 'cause': 'battery_command_failed'}
        runtime._queue_execution(condition)
        self.assertEqual(len(runtime._execution_observations), 1)
        runtime._bootstrap = accepted
        runtime._queue_execution(condition)
        self.assertEqual(len(runtime._execution_observations), 2)
        runtime._queue_execution(condition)
        rig.plan['plan_id'] = 'replacement'
        runtime._queue_execution(condition)
        self.assertEqual(len(runtime._execution_observations), 2)


class ForcedReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_execution_request_rejects_telemetry_only_acceptance(self):
        client = ShsApiClient(None, 'https://example.invalid', 'token')
        client._request = AsyncMock(return_value={})
        with self.assertRaisesRegex(ShsApiError, 'invalid job receipt'):
            await client.push_optimisation([], {'snapshot_id': 'snapshot'}, execution_replan={
                'request_id': 'request', 'accepted_plan_id': 'accepted', 'observed_at': '2026-10-23T10:00:00Z', 'changes': []})
