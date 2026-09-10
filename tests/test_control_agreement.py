"""Settings agreement under interruption, disconnection and changing clocks."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'custom_components/shs_energy'))
from control_agreement import ControlAgreement, local_advertisement
from control_setup import ControlSetup
from contract_v1.validate import revision, validate
from test_control_setup import battery, pool

EXAMPLES = json.loads((ROOT / 'contracts/control/v1/examples.json').read_text())
HOME = EXAMPLES['battery-and-customer-pool']['home_id']
NOW = datetime(2026, 9, 10, 12, 0, 1, tzinfo=timezone.utc)


class Store:
    def __init__(self): self.payload = None; self.fail = None; self.writes = 0
    async def async_load(self): return deepcopy(self.payload)
    async def async_save(self, payload):
        self.writes += 1
        if self.fail == 'before': raise OSError('disk full')
        self.payload = deepcopy(payload)
        if self.fail == 'after': raise asyncio.CancelledError()


class AgreementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        inv, b = battery(); pinv, p = pool(); inv.update(pinv)
        self.definition = deepcopy(EXAMPLES['battery-and-customer-pool'])
        b['control_id'], p['control_id'] = [c['control_id'] for c in self.definition['controls']]
        self.setup = ControlSetup(Store(), HOME, lambda: inv, lambda _: {})
        await self.setup.async_save(b, 0); await self.setup.async_save(p, 0)
        self.store = Store(); self.wall = NOW; self.mono = 1000.; self.enabled = True
        self.requests = []; self.next_response = None
        async def exchange(request):
            self.requests.append(deepcopy(request))
            if isinstance(self.next_response, Exception): raise self.next_response
            return deepcopy(self.next_response)
        self.agreement = ControlAgreement(self.store, self.setup, exchange, wall=lambda: self.wall,
                                          monotonic=lambda: self.mono, permission=lambda _: {'revision': 1, 'enabled': self.enabled})
        for c, ad in zip(self.definition['controls'], self.agreement.advertisements()): c['local'] = ad['local']
        validate(self.definition)
        self.accepted = {c['control_id']: revision(c) for c in self.definition['controls']}
        self.plan = deepcopy(EXAMPLES['explicit-intents-forecast-is-not-ceiling'])
        for c in self.plan['commands']: c['accepted'] = self.accepted[c['control_id']]
        validate(self.plan, self.definition)

    def response(self, *, plan=True, lease=True, epoch=1):
        now = self.wall.isoformat().replace('+00:00', 'Z')
        expires = min(self.wall + timedelta(seconds=120), datetime.fromisoformat(self.plan['valid_until_utc'].replace('Z', '+00:00')))
        return dict(schema_version=1, home_id=HOME, epoch=epoch, edit_revision=1,
                    server_time_utc=now, definition=deepcopy(self.definition), acknowledged=deepcopy(self.accepted),
                    plan=deepcopy(self.plan) if plan else None,
                    lease=dict(plan_id=self.plan['plan_id'], epoch=epoch, issued_at_utc=now,
                               expires_at_utc=expires.isoformat().replace('+00:00','Z')) if lease else None)

    async def grant(self):
        self.next_response = self.response()
        await self.agreement.async_poll()
        self.assertIsNone(self.agreement.error)
        return self.guard()

    def guard(self):
        c = self.plan['commands'][0]
        return self.agreement.guard(c['control_id'], c['accepted'], self.plan['plan_id'])

    def advance(self, seconds): self.wall += timedelta(seconds=seconds); self.mono += seconds

    async def test_save_before_ack_and_idempotent_repeated_poll(self):
        await self.grant()
        self.assertEqual(self.requests[0]['acknowledged'], {})
        self.assertEqual(self.store.payload['accepted'], self.accepted)
        await self.agreement.async_poll()
        self.assertEqual(self.requests[1]['acknowledged'], self.accepted)
        self.assertEqual(self.store.writes, 1)

    async def test_failed_save_cannot_publish_ack_or_authority(self):
        self.store.fail = 'before'; self.next_response = self.response()
        await self.agreement.async_poll()
        self.assertIn('disk full', self.agreement.error)
        self.assertEqual(self.agreement.saved['accepted'], {})
        self.assertIsNone(self.agreement.lease)
        await self.agreement.async_poll()
        self.assertEqual(self.requests[-1]['acknowledged'], {})

    async def test_interruption_after_durable_save_retries_same_ack_after_restart(self):
        self.store.fail = 'after'; self.next_response = self.response()
        with self.assertRaises(asyncio.CancelledError): await self.agreement.async_poll()
        self.assertEqual(self.agreement.saved['accepted'], {})
        self.store.fail = None
        await self.agreement.async_load()
        self.assertEqual(self.agreement.saved['accepted'], self.accepted)
        self.assertIsNone(self.agreement.lease)
        await self.agreement.async_poll()
        self.assertEqual(self.requests[-1]['acknowledged'], self.accepted)

    async def test_restart_keeps_revisions_but_no_plan_or_lease(self):
        await self.grant()
        restarted = ControlAgreement(self.store, self.setup, self.agreement.exchange)
        await restarted.async_load()
        self.assertEqual(restarted.saved['accepted'], self.accepted)
        self.assertIsNone(restarted.plan)
        self.assertIsNone(restarted.lease)
        self.assertFalse(restarted.permission(next(iter(self.accepted)))['enabled'])
        self.assertNotIn('lease', self.store.payload)

    async def test_network_failure_does_not_renew_or_create_revisions(self):
        await self.grant(); saved = deepcopy(self.agreement.saved)
        self.next_response = OSError('offline')
        self.advance(119); await self.agreement.async_poll(); self.guard()
        self.advance(1)
        with self.assertRaisesRegex(ValueError, 'expired'): self.guard()
        self.assertEqual(self.agreement.saved, saved)
        self.assertEqual(self.store.writes, 1)

    async def test_local_permission_off_is_immediate(self):
        await self.grant(); self.enabled = False
        with self.assertRaisesRegex(ValueError, 'permission off'): self.guard()
        self.assertIsNone(self.agreement.active)

    async def test_new_binding_and_changed_capabilities_block_immediately(self):
        await self.grant()
        record = next(iter(self.setup.records.values()))
        record['binding_revision'] += 1
        with self.assertRaisesRegex(ValueError, 'binding changed'): self.guard()
        self.assertIsNone(self.agreement.lease)

    async def test_remote_change_revokes_even_if_local_save_fails(self):
        await self.grant(); self.store.fail = 'before'
        self.definition['controls'][1]['desired']['included'] = False
        self.definition['controls'][1]['desired']['revision'] += 1
        self.next_response = self.response(plan=False, lease=False, epoch=2)
        await self.agreement.async_poll()
        self.assertIsNone(self.agreement.lease)
        self.assertEqual(self.agreement.saved['epoch'], 1)

    async def test_delayed_response_cannot_extend_elapsed_lease(self):
        response = self.response()
        async def delayed(_request): self.advance(119); return response
        self.agreement.exchange = delayed
        await self.agreement.async_poll(); self.guard()
        self.advance(1)
        with self.assertRaisesRegex(ValueError, 'expired'): self.guard()

    async def test_expired_response_never_activates(self):
        response = self.response()
        async def delayed(_request): self.advance(121); return response
        self.agreement.exchange = delayed
        await self.agreement.async_poll()
        self.assertIsNone(self.agreement.lease)

    async def test_backward_and_forward_clock_changes_revoke(self):
        await self.grant(); self.wall -= timedelta(seconds=60)
        with self.assertRaisesRegex(ValueError, 'clock discontinuity'): self.guard()
        self.wall = NOW; self.agreement.last_clock = None
        await self.grant(); self.wall += timedelta(seconds=60)
        with self.assertRaisesRegex(ValueError, 'clock discontinuity'): self.guard()

    async def test_lease_cannot_outlive_plan_or_be_overlong(self):
        for change in ('overlong', 'plan_expiry', 'future', 'foreign', 'stale'):
            with self.subTest(change=change):
                await self.grant(); response = self.response()
                if change == 'overlong': response['lease']['expires_at_utc'] = (self.wall + timedelta(seconds=121)).isoformat().replace('+00:00','Z')
                if change == 'plan_expiry': response['plan']['valid_until_utc'] = (self.wall + timedelta(seconds=30)).isoformat().replace('+00:00','Z')
                if change == 'future': response['server_time_utc'] = '2026-09-10T13:00:00Z'
                if change == 'foreign': response['home_id'] = '00000000-0000-4000-8000-000000000099'
                if change == 'stale': response['epoch'] = 0
                self.next_response = response; await self.agreement.async_poll()
                self.assertIsNone(self.agreement.lease)

    async def test_partial_household_plan_or_changed_instruction_tuple_rejected(self):
        self.next_response = self.response(); self.next_response['plan']['commands'].pop()
        await self.agreement.async_poll()
        self.assertIn('incomplete household', self.agreement.error)
        self.next_response = self.response(); self.next_response['plan']['commands'][0]['accepted']['binding'] += 1
        await self.agreement.async_poll()
        self.assertIn('revision_mismatch', self.agreement.error)

    async def test_unacknowledged_server_plan_cannot_grant(self):
        self.next_response = self.response(); self.next_response['acknowledged'] = {}
        await self.agreement.async_poll()
        self.assertIn('persisted acknowledgement', self.agreement.error)
        self.assertIsNone(self.agreement.lease)

    async def test_poll_overlap_and_unload_discard_inflight_response(self):
        entered, resume = asyncio.Event(), asyncio.Event()
        async def delayed(request): entered.set(); await resume.wait(); return self.response()
        self.agreement.exchange = delayed
        task = asyncio.create_task(self.agreement.async_poll()); await entered.wait()
        await self.agreement.async_poll()
        self.agreement.close(); resume.set(); await task
        self.assertIsNone(self.agreement.lease)
        self.assertEqual(self.store.writes, 0)

    async def test_no_actuator_identity_in_server_advertisement(self):
        wire = json.dumps(self.agreement.advertisements())
        for forbidden in ('entity_id', 'unique_id', 'number.', 'script.'):
            self.assertNotIn(forbidden, wire)

    async def test_deployed_validator_is_the_shared_contract(self):
        for name in ('validate.py', 'schema.json'):
            self.assertEqual((ROOT / 'contracts/control/v1' / name).read_bytes(),
                             (ROOT / 'custom_components/shs_energy/contract_v1' / name).read_bytes())


    async def test_permission_revision_change_requires_new_authority(self):
        await self.grant()
        self.agreement.permission = lambda _: {'revision': 2, 'enabled': True}
        with self.assertRaisesRegex(ValueError, 'revision changed'): self.guard()
        self.assertIsNone(self.agreement.lease)

    async def test_reused_desired_revision_is_rejected_even_with_a_new_epoch(self):
        await self.grant()
        self.definition['controls'][1]['desired']['included'] = False
        self.next_response = self.response(plan=False, lease=False, epoch=2)
        await self.agreement.async_poll()
        self.assertIn('revision reused', self.agreement.error)
        self.assertIsNone(self.agreement.lease)

    async def test_unload_during_durable_save_cannot_install_a_lease(self):
        entered, resume = asyncio.Event(), asyncio.Event()
        original = self.store.async_save
        async def delayed(payload):
            entered.set(); await resume.wait(); await original(payload)
        self.store.async_save = delayed
        self.next_response = self.response()
        task = asyncio.create_task(self.agreement.async_poll()); await entered.wait()
        self.agreement.close(); resume.set(); await task
        self.assertIsNone(self.agreement.lease)
        self.assertIsNone(self.agreement.plan)

    async def test_live_python_typescript_exchange_when_peer_available(self):
        peer = ROOT.parent / 'smart-home-solutions-t-by'
        if not shutil.which('deno') or not peer.exists(): self.skipTest('Deno and peer checkout required')
        driver = ROOT / 'tests/control-agreement-peer.ts'
        state = None
        def run(action, request):
            nonlocal state
            result = subprocess.run(['deno', 'run', '--config', str(peer / 'deno.json'), '--allow-read', str(driver)], input=json.dumps(
                dict(action=action, state=state, request=request, home_id=HOME, now=self.wall.timestamp()*1000)),
                text=True, capture_output=True, cwd=peer)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = json.loads(result.stdout); state = result['state']; return result.get('response')
        desired = deepcopy(self.definition)
        for c in desired['controls']: c.pop('local')
        run('edit', {'expected_revision': 0, 'definition': desired})
        async def wire(request): return run('sync', request)
        self.agreement.exchange = wire
        await self.agreement.async_poll()  # HA saves desired + reviewed local acceptance.
        self.assertIsNone(self.agreement.error)
        await self.agreement.async_poll()  # Server commits same acknowledgement.
        self.assertIsNone(self.agreement.error)
        run('publish', self.plan)
        await self.agreement.async_poll()  # Receives plan, no lease until plan id is returned.
        self.assertIsNone(self.agreement.error)
        self.assertIsNone(self.agreement.lease)
        await self.agreement.async_poll(); self.guard()
        # Response lost after server commit: replay does not change settings revisions.
        old_epoch = state['epoch']
        async def lost(request): run('sync', request); raise OSError('response lost')
        self.agreement.exchange = lost; await self.agreement.async_poll()
        self.assertEqual(state['epoch'], old_epoch)
        self.agreement.exchange = wire
        desired['controls'][1]['desired']['included'] = False
        run('edit', {'expected_revision': state['edit_revision'], 'definition': desired})
        await self.agreement.async_poll()
        self.assertIsNone(self.agreement.lease)
        self.assertIsNone(self.agreement.error)
        await self.agreement.async_poll()
        self.assertEqual(state['received_epoch'], state['epoch'])
