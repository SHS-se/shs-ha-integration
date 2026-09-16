"""The real server/compiler wire can be delivered without physical authority."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from battery_policy_exchange import BatteryPolicyExchange, BatteryPolicyUnavailableError, read_policy_delivery

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/battery-policy-delivery.json').read_text())


class DeliveryTests(unittest.TestCase):
    def test_real_compiler_wire_has_the_declared_basis_and_no_control_authority(self):
        policy = read_policy_delivery(FIXTURE['delivery'], FIXTURE['request'], FIXTURE['now'])
        self.assertEqual(policy.summary.actuals_origin_ms, FIXTURE['now'])
        self.assertEqual(policy.summary.identity.context.intent_revision, FIXTURE['request']['snapshot_id'])
        self.assertEqual(FIXTURE['delivery']['energy_origin_kwh'], .05 * 18.08)
        self.assertTrue(FIXTURE['synthetic'])

    def test_delivery_rejects_mismatched_or_expired_sources_and_never_accepts_a_grant(self):
        mutations = [
            lambda d: d.update(control_authority=True),
            lambda d: d.update(plan_id='another-plan'),
            lambda d: d.update(request_id='another-request'),
            lambda d: d.update(unknown=1),
            lambda d: d.update(energy_basis='absolute_kwh'),
            lambda d: d['native_context'].update(catalog_revision='changed'),
            lambda d: d['policy']['identity'].update(intent_revision='changed'),
        ]
        for mutate in mutations:
            value = deepcopy(FIXTURE['delivery']); mutate(value)
            with self.assertRaises(ValueError):
                read_policy_delivery(value, FIXTURE['request'], FIXTURE['now'])
        with self.assertRaises(ValueError):
            read_policy_delivery(FIXTURE['delivery'], FIXTURE['request'], FIXTURE['delivery']['policy']['validity']['until_ms'])


class ExchangeTests(unittest.IsolatedAsyncioTestCase):
    async def make(self, native=True):
        self.context = {'plan_id': FIXTURE['request']['plan_id'], 'snapshot_id': FIXTURE['request']['snapshot_id'],
            'config_revision': 'test-config', 'native_context': deepcopy(FIXTURE['request']['native_context']) if native else None}
        self.now = FIXTURE['now']; self.calls = []; self.saved = []
        async def request(body):
            self.calls.append(deepcopy(body))
            if body['native_context'] is None:
                return {'schema':'battery-policy-delivery-v1','request_id':body['request_id'],'plan_id':body['plan_id'],
                    'snapshot_id':body['snapshot_id'],'purpose':'verification','control_authority':False,
                    'status':'blocked','reasons':['native_context_required']}
            value = deepcopy(FIXTURE['delivery']); value['request_id'] = body['request_id']; return value
        async def persist(value): self.saved.append(value)
        return BatteryPolicyExchange(request, lambda:self.context, persist, lambda:self.now)

    async def test_delivered_policy_is_persisted_but_not_admitted_or_given_a_writer(self):
        exchange = await self.make()
        await exchange.refresh()
        self.assertIsNotNone(exchange.policy)
        self.assertEqual(exchange.snapshot()['state'], 'delivered_not_admitted')
        self.assertFalse(exchange.snapshot()['control_authority'])
        self.assertEqual(self.saved[-1]['state'], 'delivered_not_admitted')
        await exchange.refresh(); self.assertEqual(len(self.calls), 1)
        self.now = FIXTURE['delivery']['policy']['validity']['until_ms']
        self.assertEqual(exchange.snapshot()['reasons'], ['policy_expired'])

    async def test_missing_native_evidence_is_explicit_and_no_ratings_are_invented(self):
        exchange = await self.make(native=False)
        await exchange.refresh()
        self.assertIsNone(self.calls[0]['native_context'])
        self.assertEqual(exchange.snapshot()['reasons'], ['native_context_required'])
        self.assertIsNone(exchange.policy)
        await exchange.refresh(); self.assertEqual(len(self.calls), 1)

    async def test_old_native_context_cannot_be_reused_for_a_new_configuration(self):
        exchange = await self.make()
        self.context['config_revision'] = 'new-configuration'
        await exchange.refresh()
        self.assertEqual(self.calls, [])
        self.assertEqual(exchange.snapshot()['reasons'], ['native_context_configuration_mismatch'])
        self.assertIsNone(exchange.policy)

    async def test_in_flight_response_cannot_survive_configuration_change_or_unload(self):
        for close in (False, True):
            exchange = await self.make()
            started, release = asyncio.Event(), asyncio.Event()
            original = exchange._request
            async def slow(body):
                started.set(); await release.wait(); return await original(body)
            exchange._request = slow
            pending = asyncio.create_task(exchange.refresh()); await started.wait()
            if close: exchange.close()
            else: self.context = {**self.context, 'config_revision':'changed'}
            release.set(); await pending
            self.assertIsNone(exchange.policy); self.assertEqual(self.saved, [])
            self.assertEqual(exchange.snapshot()['state'], 'stopped' if close else 'blocked')

    async def test_response_failure_preserves_request_id_and_diagnostic_action(self):
        exchange = await self.make()
        class ResponseError(Exception):
            code = 'invalid_response_envelope'
            request_id = 'request-123'
        async def invalid(_): raise ResponseError('invalid envelope')
        exchange._request = invalid
        await exchange.refresh()
        status = exchange.snapshot()
        self.assertEqual(status['error'], {'code': 'invalid_response_envelope', 'request_id': 'request-123'})
        error = BatteryPolicyUnavailableError(status)
        self.assertIn('service returned an invalid response', str(error))
        self.assertIn('request-123', str(error))
        self.assertEqual(error.fix, {'kind': 'diagnostics'})
        self.assertIsNone(exchange.policy)

    async def test_network_and_durable_storage_failures_do_not_deliver_policy_and_retry_is_bounded(self):
        exchange = await self.make()
        async def broken(_): raise OSError('offline')
        exchange._request = broken
        await exchange.refresh(); self.assertEqual(exchange.snapshot()['state'], 'unreachable')
        self.assertIsNone(exchange.policy)
        exchange = await self.make(); exchange._persist = broken
        await exchange.refresh(); self.assertIsNone(exchange.policy)
        self.assertEqual(exchange.snapshot()['state'], 'unreachable')
        await exchange.refresh(); self.assertEqual(len(self.calls), 1)

    async def test_late_failures_cannot_overwrite_shutdown_or_changed_context(self):
        for port in ('_request', '_persist'):
            for close in (False, True):
                exchange = await self.make()
                started, release = asyncio.Event(), asyncio.Event()
                async def fail_late(_):
                    started.set()
                    await release.wait()
                    raise OSError('late failure')
                setattr(exchange, port, fail_late)
                pending = asyncio.create_task(exchange.refresh())
                await started.wait()
                if close:
                    exchange.close()
                else:
                    self.context = {**self.context, 'config_revision': 'changed'}
                release.set()
                await pending
                self.assertIsNone(exchange.policy)
                self.assertEqual(exchange.snapshot()['state'], 'stopped' if close else 'blocked')

    async def test_reconfiguration_during_persistence_cannot_install_old_delivery(self):
        exchange = await self.make()
        async def persist(_): self.context = None
        exchange._persist = persist
        await exchange.refresh()
        self.assertIsNone(exchange.policy)
        self.assertEqual(exchange.snapshot()['state'], 'blocked')
