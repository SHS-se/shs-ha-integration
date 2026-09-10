"""Exercise the real agreement and adapter with an ordered customer receiver."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import unittest

import test_control_agreement as agreement_tests
from pool_controller import PoolController
from pool_policy import feedback, request
from control_setup import describe_inventory
from contract_v1.validate import revision, validate, utc


class PoolTests(unittest.IsolatedAsyncioTestCase):
    response = agreement_tests.AgreementTests.response
    grant = agreement_tests.AgreementTests.grant
    guard = agreement_tests.AgreementTests.guard
    advance = agreement_tests.AgreementTests.advance

    async def asyncSetUp(self):
        await agreement_tests.AgreementTests.asyncSetUp(self)
        self.key = self.definition['controls'][1]['control_id']
        self.inv = self.setup.inventory()
        self.states = {key: SimpleNamespace(state='25' if key=='sensor.water' else 'idle' if key=='sensor.feedback' else 'off',
                       attributes=deepcopy(e['attributes']), last_reported=self.wall) for key,e in self.inv.items()}
        def inventory():
            raw = deepcopy(self.inv)
            for key,state in self.states.items(): raw[key].update(state=state.state, attributes=state.attributes)
            return describe_inventory(raw)
        self.setup.inventory = inventory
        self.store = agreement_tests.Store(); self.calls = []; self.hook = None
        self.receiver = {}; self.delivery = 'scheduled'; self.acknowledge = True
        async def service(domain, service, data, blocking):
            self.assertEqual(domain, 'script'); self.assertEqual(service, 'request')
            self.assertEqual(set(data), {'request'})
            sent = deepcopy(data['request']); self.calls.append(sent)
            self.assertEqual(self.store.payload['records'][self.key]['request'], sent, 'persist before invoking customer')
            validate(sent, self.store.payload['records'][self.key]['definition'])
            if self.hook and await self.hook(sent): return
            self.receive(sent)
        self.hass = SimpleNamespace(states=SimpleNamespace(get=self.states.get), services=SimpleNamespace(async_call=service))
        self.controller = self.new_controller()
        await self.controller.async_start(); await self.grant()

    def new_controller(self):
        return PoolController(self.hass, self.setup, self.agreement, self.store, wall=lambda:self.wall,
                              monotonic=lambda:self.mono, service_timeout=.05)

    def receive(self, sent):
        previous = self.receiver.get(sent['control_id'])
        if self.wall >= utc(sent['expires_at_utc']): return False
        if previous and sent['request_sequence'] < previous['request_sequence']: return False
        if previous and sent['request_sequence'] == previous['request_sequence']:
            self.assertEqual(sent, previous, 'same sequence must be exactly idempotent')
        self.receiver[sent['control_id']] = deepcopy(sent)
        if self.acknowledge: self.emit(sent)
        return True

    def emit(self, sent, **updates):
        self.advance(.001)
        value = {k:deepcopy(sent[k]) for k in ('home_id','control_id','request_id','request_sequence','accepted','plan_id')}
        value.update(kind='pool_feedback',schema_version=1, reported_at_utc=self.wall.isoformat().replace('+00:00','Z'),
                     acknowledgement='released' if sent['action']=='release' else 'accepted',
                     operation='released' if sent['action']=='release' else self.delivery, reason='Customer observation')
        value.update(updates)
        self.states['sensor.feedback'].attributes['feedback'] = value
        self.states['sensor.feedback'].last_reported = self.wall
        return value

    async def run_request(self, action='heat'):
        self.plan['commands'][1]['instruction']['request'] = action
        await self.grant(); await self.controller.async_tick()

    async def test_heat_defer_and_same_request_are_complete_and_idempotent(self):
        await self.run_request(); self.assertEqual(len(self.calls),1)
        sent = self.calls[0]
        self.assertEqual(sent['objective'],self.definition['controls'][1]['desired']['objective'])
        self.assertEqual(sent['plan_id'],self.plan['plan_id'])
        self.assertLessEqual(utc(sent['expires_at_utc']),utc(self.agreement.lease['expires_at_utc']))
        self.assertEqual(self.controller.status['state'],'scheduled',self.controller.status)
        self.assertTrue(self.controller.status['request_acknowledged'])
        self.advance(5); await self.controller.async_tick(); self.assertEqual(len(self.calls),1)
        await self.run_request('defer')
        self.assertEqual(self.calls[-1]['request_sequence'],2)
        self.assertEqual(self.calls[-1]['action'],'defer')
        self.assertNotEqual(self.calls[-1]['request_id'],sent['request_id'])

    async def test_renewal_extends_only_with_fresh_authority_and_new_sequence(self):
        await self.run_request(); first=deepcopy(self.calls[-1])
        self.advance(95); self.states['sensor.water'].last_reported=self.wall
        await self.grant(); await self.controller.async_tick()
        second=self.calls[-1]
        self.assertGreater(second['request_sequence'],first['request_sequence'])
        self.assertGreater(second['expires_at_utc'],first['expires_at_utc'])
        self.assertEqual(second['objective'],first['objective'])
        self.assertFalse(self.receive(first))

    async def test_target_edit_uses_new_accepted_objective_not_captured_hardware_band(self):
        await self.run_request()
        control=self.definition['controls'][1]; control['desired']['revision']+=1
        control['desired']['objective'].update(start_c=28,stop_c=28.5)
        self.plan['commands'][1]['instruction'].update(start_c=28,stop_c=28.5)
        self.accepted[self.key]=revision(control); self.plan['commands'][1]['accepted']=revision(control)
        self.next_response=self.response(epoch=2); await self.agreement.async_poll(); await self.controller.async_tick()
        self.assertEqual(self.calls[-1]['objective']['start_c'],28)
        self.assertEqual(self.calls[-1]['accepted']['desired'],2)
        self.assertEqual(self.controller.status['state'],'scheduled',self.controller.status)

    async def test_each_customer_operation_is_reported_without_inference_from_forecast(self):
        for operation in ('scheduled','observed_heating','limited_deferral','unavailable_heat','unknown'):
            self.delivery=operation
            await self.run_request('defer')
            self.emit(self.calls[-1]); await self.controller.async_tick()
            self.assertEqual(self.controller.status['state'],operation,self.controller.status)
            self.assertEqual(self.agreement.active['controls'][self.key]['operation'],operation)
            self.assertNotIn('pump',str(self.calls[-1]))

    async def test_missing_feedback_is_unknown_then_release_pending(self):
        self.acknowledge=False
        await self.run_request()
        self.assertEqual(self.controller.status['state'],'awaiting_acknowledgement')
        self.assertIsNone(self.agreement.active)
        first=deepcopy(self.calls[-1]); self.advance(5); await self.controller.async_tick()
        self.assertEqual(self.calls[-1],first)
        self.advance(11); await self.controller.async_tick()
        self.assertEqual(self.calls[-1]['action'],'release')
        self.assertEqual(self.controller.status['state'],'release_pending')
        self.assertTrue(self.controller.records)
        self.emit(self.calls[-1]); await self.controller.async_tick()
        self.assertFalse(self.controller.records)

    async def test_delayed_correlated_ack_does_not_require_resending(self):
        self.acknowledge=False; await self.run_request(); sent=deepcopy(self.calls[-1])
        self.advance(7); self.emit(sent, operation='observed_heating')
        await self.controller.async_tick()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.controller.status['state'],'observed_heating')

    async def test_old_duplicate_wrong_revision_and_future_feedback_cannot_confirm(self):
        await self.run_request(); sent=self.calls[-1]
        for field, value in [('request_id','00000000-0000-4000-8000-000000000099'),('request_sequence',99),
                             ('plan_id',None),('accepted',{**sent['accepted'],'desired':99}),
                             ('reported_at_utc','2026-09-10T12:05:00Z')]:
            wrong=self.emit(sent); wrong[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                feedback(wrong,sent,self.definition,self.wall)

    async def test_disable_expiry_clock_jump_and_stop_release_to_customer_policy(self):
        for reason in ('disable','lease','clock','stop'):
            await self.asyncSetUp(); await self.run_request()
            if reason=='disable': self.enabled=False
            elif reason=='lease': self.advance(121)
            elif reason=='clock': self.wall-=timedelta(seconds=30)
            if reason=='stop': await self.controller.async_stop()
            else: await self.controller.async_tick()
            self.assertEqual(self.calls[-1]['action'],'release')
            self.assertIsNone(self.calls[-1]['objective']); self.assertIsNone(self.calls[-1]['plan_id'])
            self.assertFalse(self.controller.records,self.controller.status)
            self.assertIsNone(self.agreement.active)

    async def test_restart_releases_before_new_dispatch_and_sequence_survives_release(self):
        await self.run_request(); first=self.calls[-1]
        self.controller=self.new_controller(); await self.controller.async_start()
        self.assertEqual(self.calls[-1]['action'],'release')
        self.assertFalse(self.controller.records)
        self.assertIsNone(self.agreement.lease)
        await self.grant(); await self.controller.async_tick()
        self.assertGreater(self.calls[-1]['request_sequence'],first['request_sequence']+1)

    async def test_late_old_heat_cannot_supersede_release(self):
        await self.run_request(); heat=deepcopy(self.calls[-1])
        self.enabled=False; await self.controller.async_tick()
        self.assertFalse(self.receive(heat))
        self.assertEqual(self.receiver[self.key]['action'],'release')

    async def test_customer_rejection_latches_until_released_and_explicitly_reviewed(self):
        async def reject(sent):
            if sent['action']!='release':
                self.emit(sent,acknowledgement='rejected',operation='unknown',reason='Manual customer override')
                return True
            return False
        self.hook=reject; await self.run_request()
        self.assertIn(self.key,self.controller.overrides)
        self.assertEqual(self.controller.records[self.key]['phase'],'released')
        self.hook=None; self.calls.clear(); await self.controller.async_tick()
        self.assertEqual(self.calls,[])
        await self.controller.async_review_release(self.key,1,True)
        self.assertFalse(self.controller.records); self.assertFalse(self.controller.overrides)
        self.assertIsNone(self.agreement.lease)

    async def test_release_failure_is_retained_and_retried_with_identical_request(self):
        await self.run_request()
        async def fail(sent):
            self.emit(sent,acknowledgement='release_failed',operation='unknown',reason='Customer handover unavailable'); return True
        self.hook=fail; self.enabled=False; await self.controller.async_tick()
        release=deepcopy(self.calls[-1]); self.assertTrue(self.controller.records)
        self.hook=None; self.advance(5); await self.controller.async_tick()
        self.assertEqual(self.calls[-1],release); self.assertFalse(self.controller.records)

    async def test_binding_edit_releases_old_script_and_never_retargets_replacement(self):
        await self.run_request()
        self.inv['script.other']=deepcopy(self.inv['script.request'])
        self.inv['script.other']['registry']['unique_id']='other'
        self.states['script.other']=deepcopy(self.states['script.request'])
        proposal=deepcopy(self.setup.records[self.key]['spec']); proposal['roles']['request']=self.inv['script.other']['registry']
        await self.setup.async_save(proposal,1); await self.controller.async_tick()
        self.assertEqual(self.calls[-1]['action'],'release'); self.assertFalse(self.controller.records)
        await self.asyncSetUp(); await self.run_request(); count=len(self.calls)
        self.inv['script.request']['registry']['unique_id']='replacement'; self.enabled=False
        await self.controller.async_tick(); self.assertEqual(len(self.calls),count)
        self.assertTrue(self.controller.records)

    async def test_permission_revoked_during_write_ahead_never_submits_heat(self):
        original=self.store.async_save
        async def save(payload):
            await original(payload)
            if payload['records'].get(self.key,{}).get('request',{}).get('action')=='heat': self.enabled=False
        self.store.async_save=save; await self.run_request()
        self.assertEqual([r['action'] for r in self.calls],['release'])
        self.assertFalse(self.controller.records)

    async def test_disk_failure_before_or_after_capture_never_submits_unjournalled_request(self):
        for failure in ('before','after'):
            await self.asyncSetUp(); self.store.fail=failure
            if failure=='after':
                with self.assertRaises(asyncio.CancelledError): await self.run_request()
            else: await self.run_request()
            self.assertEqual(self.calls,[])
            self.assertIsNone(self.agreement.active)
            self.store.fail=None; await self.controller.async_tick()
            self.assertFalse(self.controller.records)
            self.assertTrue(all(c['action']=='release' for c in self.calls))

    async def test_service_failure_and_cancellation_preserve_release_authority(self):
        for failure in (OSError('offline'),asyncio.CancelledError()):
            await self.asyncSetUp()
            async def fail(sent): raise failure
            self.hook=fail
            if isinstance(failure,asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError): await self.run_request()
            else: await self.run_request()
            self.assertTrue(self.controller.records)
            self.hook=None; self.controller=self.new_controller(); await self.controller.async_start()
            self.assertEqual(self.calls[-1]['action'],'release')
            self.assertFalse(self.controller.records)

    async def test_stale_temperature_stops_requests_but_does_not_prevent_release(self):
        await self.run_request()
        self.states['sensor.water'].last_reported-=timedelta(seconds=121)
        await self.controller.async_tick()
        self.assertEqual(self.calls[-1]['action'],'release'); self.assertFalse(self.controller.records)
        await self.asyncSetUp(); self.states['sensor.water'].last_reported-=timedelta(seconds=121)
        await self.controller.async_tick(); self.assertEqual(self.calls,[])

    async def test_corrupt_journal_and_nonmonotonic_sequence_fail_without_writes(self):
        await self.run_request(); self.calls.clear()
        self.store.payload['sequences'][self.key]=0
        other=self.new_controller()
        with self.assertRaises(ValueError): await other.async_start()
        await other.async_tick(); self.assertEqual(self.calls,[])
        self.assertEqual(self.store.payload['sequences'][self.key],0)

    async def test_bounds_steps_wrong_intent_and_forecast_only_are_not_requests(self):
        control=self.definition['controls'][1]; command=self.plan['commands'][1]
        for field,value in [('start_c',19),('stop_c',32.5),('start_c',29.55)]:
            d=deepcopy(self.definition); d['controls'][1]['desired']['objective'][field]=value
            c=deepcopy(command); c['instruction'][field]=value
            with self.assertRaises(ValueError): request(d,self.key,1,self.wall,command=c,plan_id=self.plan['plan_id'],expires=self.wall+timedelta(seconds=120))
        c=deepcopy(command); c['instruction']={'pool_w':5000}
        with self.assertRaises(ValueError): request(self.definition,self.key,1,self.wall,command=c,plan_id=self.plan['plan_id'],expires=self.wall+timedelta(seconds=120))

    async def test_expired_release_gets_a_new_sequence_and_old_feedback_is_not_acknowledgement(self):
        await self.run_request(); self.acknowledge=False; self.enabled=False
        await self.controller.async_tick(); old=deepcopy(self.calls[-1])
        self.advance(121); await self.controller.async_tick(); new=deepcopy(self.calls[-1])
        self.assertEqual(new['action'],'release')
        self.assertGreater(new['request_sequence'],old['request_sequence'])
        self.emit(old); await self.controller.async_tick(); self.assertTrue(self.controller.records)
        self.emit(new); await self.controller.async_tick(); self.assertFalse(self.controller.records)

    async def test_service_timeout_keeps_release_pending_until_customer_acknowledges(self):
        async def hang(sent): await asyncio.sleep(10)
        self.hook=hang; await self.run_request()
        self.assertTrue(self.controller.records)
        self.assertEqual(self.calls[-1]['action'],'release')
        self.hook=None; self.advance(5); await self.controller.async_tick()
        self.assertFalse(self.controller.records)

    async def test_revocation_during_customer_call_sends_release_after_return(self):
        async def revoke(sent):
            if sent['action']=='heat': self.enabled=False
            return False
        self.hook=revoke; await self.run_request()
        self.assertEqual([c['action'] for c in self.calls],['heat','release'])
        self.assertFalse(self.controller.records)

    async def test_script_rename_follows_registry_identity_for_release(self):
        await self.run_request()
        self.inv['script.renamed']=self.inv.pop('script.request')
        self.states['script.renamed']=self.states.pop('script.request')
        async def renamed(domain, service, data, blocking):
            self.assertEqual((domain,service),('script','renamed'))
            self.assertEqual(data['request']['action'],'release')
            self.calls.append(deepcopy(data['request'])); self.receive(data['request'])
        self.hass.services.async_call=renamed; self.enabled=False
        await self.controller.async_tick(); self.assertFalse(self.controller.records)

    async def test_lease_shortened_during_save_prevents_request_with_old_longer_expiry(self):
        original=self.store.async_save
        async def save(payload):
            await original(payload)
            if payload['records'].get(self.key,{}).get('last_sent_at') and self.agreement.lease:
                self.agreement.deadline=self.mono+10
                self.agreement.lease['expires_at_utc']=(self.wall+timedelta(seconds=10)).isoformat().replace('+00:00','Z')
        self.store.async_save=save; await self.run_request()
        self.assertEqual([c['action'] for c in self.calls],['release'])

    async def test_restart_keeps_customer_override_without_reclaiming_scheduling(self):
        await self.run_request()
        self.emit(self.calls[-1],acknowledgement='rejected',operation='unknown',reason='Manual override')
        await self.controller.async_tick()
        self.calls.clear(); self.controller=self.new_controller(); await self.controller.async_start()
        self.assertEqual(self.calls,[])
        self.assertIn(self.key,self.controller.overrides)
        self.assertEqual(self.controller.status['state'],'overridden')

    async def test_rejection_at_renewal_boundary_is_not_overwritten_by_another_heat_request(self):
        await self.run_request(); self.advance(95)
        self.emit(self.calls[-1],acknowledgement='rejected',operation='unknown',reason='Manual override')
        await self.grant(); await self.controller.async_tick()
        self.assertEqual([c['action'] for c in self.calls],['heat','release'])
        self.assertIn(self.key,self.controller.overrides)

    async def test_short_plan_does_not_renew_identical_expiry_or_restart_acknowledgement_timeout(self):
        self.plan['valid_until_utc']=(self.wall+timedelta(seconds=25)).isoformat().replace('+00:00','Z')
        self.acknowledge=False; await self.run_request(); first=deepcopy(self.calls[-1])
        self.advance(5); await self.controller.async_tick()
        self.assertEqual(self.calls[-1],first)
        self.advance(11); await self.controller.async_tick()
        self.assertEqual(self.calls[-1]['action'],'release')
        self.assertEqual(self.calls[-1]['request_sequence'],first['request_sequence']+1)

    async def test_method_change_waits_for_the_previous_adapter_to_hand_over(self):
        prior = deepcopy(self.setup.records[self.definition['controls'][0]['control_id']])
        prior['spec']['control_id'] = self.key
        self.setup.runtime_records = lambda: {'battery:' + self.key: {'binding': prior}}
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertIn('previously owned adapter', self.controller.status['reason'])
