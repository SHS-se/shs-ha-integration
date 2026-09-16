"""Executable session, local native bindings and durable ownership protocol tests.

All native/transport evidence and grants here are synthetic, not commissioned.
"""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from battery_execution_policy import read_execution_policy, ExecutionConditions
from energy_ledger import (MeterSpec, CounterSample, EnergyBounds, create_ledger, mark_actuals,
                           reconciled_actuals)
from home_runtime import (
    OperationBinding, NativeCatalog, ScopeParticipant, ExecutionScope, ExecutionAuthority,
    AuthorityInstalled, ConditionsObserved, PolicyOffered, ExternalDemand, WriterIdentity,
    WriterGrant, GrantConfirmed, GrantRevoked, ReleaseApproved, AuthorityChanged, Requested,
    GroupSpec, Envelope, Limits, Request, Guard, Observation, Observed, Frame, FrameObserved,
    Step, Proposed, TransitionFailed, JournalDurable, TransportResult, Tick, MeterObserved,
    LedgerPruned, Persist, Send, NeedPolicy, NeedTransition, create_home, reduce_home,
    reservation, authorize_send,
)
from home_runtime_checkpoint import encode_checkpoint, decode_checkpoint, restore_checkpoint, event_json

FIXTURES = Path(__file__).parent / 'fixtures'
BACKEND_POLICY = FIXTURES / 'battery-execution-policy.json'


def load(wire=None):
    return read_execution_policy(json.dumps(wire).encode() if wire else (FIXTURES / 'battery-execution-runtime-synthetic.json').read_bytes())


def native(mode='Standby', charge=0, discharge=0):
    return (('mode', mode), ('charge', charge), ('discharge', discharge))


def catalog_for(compiled):
    modes = {'self_consumption':'Maximum Self Consumption', 'solar_charge':'Maximum Self Consumption',
             'supply_house':'Maximum Self Consumption', 'grid_charge':'Command Charging (PV First)',
             'export':'Command Discharging (PV First)', 'hold':'Standby'}
    s = compiled.summary
    bindings = tuple(OperationBinding(op, native(modes[op.operation],
        s.plant.charge_max_w if op.operation == 'self_consumption' else op.charge_limit_w,
        s.plant.discharge_max_w if op.operation == 'self_consumption' else op.discharge_limit_w),
        'pv-first-v1', 'synthetic-native-response', (Guard('ready', 1, 1),)) for op in s.operations)
    return NativeCatalog(s.identity.context.catalog_revision, 'synthetic-pv-first-adapter', 'native-surface-1',
                         'mode', 'charge', 'discharge', tuple(sorted(set(modes.values()))),
                         1, s.plant.charge_max_w, s.plant.discharge_max_w, bindings)


class Harness:
    def __init__(self, *, compiled=None, mode='controlling', mixed=False, initial=None):
        self.compiled = compiled or load()
        s = self.compiled.summary
        self.now = s.from_ms
        self.state = create_home((GroupSpec(s.identity.context.battery_id, 'synthetic-pv-first-adapter',
            ('mode','charge','discharge'), Envelope(s.plant.charge_max_w, s.plant.discharge_max_w)),),
            Limits(100, 300, 1200), ledger=create_ledger('house', 'mapping-1',
                (MeterSpec('charge','battery','AC','charge',None),)))
        self.effects = ()
        self.records = []
        self.initial = json.loads(encode_checkpoint(self.state))
        self.event(MeterObserved(CounterSample('charge','physical-register',0,0,s.actuals_origin_ms,1000000,'initial binding')))
        participants = (ScopeParticipant(s.identity.context.battery_id, mode,1,'new_runtime',('mode','charge','discharge')),)
        if mixed:
            participants += (ScopeParticipant('ev','planning',1,'legacy',('ev.enable',)),
                             ScopeParticipant('pool','control_verification',1,'external',('pool.enable',)))
        self.authority = ExecutionAuthority('config-1',s.identity.context,s.permissions,s.plant,
            ExecutionScope(s.identity.context.scope_revision,s.identity.context.battery_id,participants),
            catalog_for(self.compiled), 'shs-runtime', s.supply_scope)
        self.event(AuthorityInstalled(self.authority,1))
        release = Request('approved-release',1,s.until_ms + 900000,native('Maximum Self Consumption',s.plant.charge_max_w,s.plant.discharge_max_w),())
        self.event(AuthorityChanged(self.group.spec.id,mode,1,release))
        self.grant()
        self.frame(mixed=mixed)
        self.observe(initial or native())
        self.conditions(load_w=8000 if mixed else 1000)
        self.watermark = mark_actuals(self.state.ledger,s.actuals_origin_ms)

    @property
    def group(self):
        return self.state.groups[0]

    def event(self, event, now=None):
        if now is not None:
            self.now = now
        self.records.append({'at_ms':self.now,'event':event_json(event)})
        self.state, self.effects = reduce_home(self.state,event,self.now)
        self.state = decode_checkpoint(encode_checkpoint(self.state))
        return self.effects

    def grant(self, epoch=None, **changes):
        grant = self.group.grant if epoch is None and self.group.grant else WriterGrant('shs-runtime',epoch or 1,
            self.authority.config_revision,self.authority.catalog.control_surface_revision,self.compiled.summary.until_ms+900000)
        return self.event(GrantConfirmed(self.group.spec.id,replace(grant,**changes)))

    def frame(self, *, mixed=False, limit=None):
        s=self.compiled.summary
        evidence=(ExternalDemand('ev',Envelope(7000,0),Envelope(7000,0)), ExternalDemand('pool',Envelope(1000,0),Envelope(1000,0))) if mixed else ()
        return self.event(FrameObserved(Frame((self.state.frame.revision+1) if self.state.frame else 1,
            self.now,self.now+1800000,Envelope(8000 if mixed else 1000,0),
            Envelope(limit or s.plant.import_limit_w,s.plant.export_limit_w),evidence)))

    def observe(self, target=None, *, ready=1, envelope=None):
        return self.event(Observed(self.group.spec.id,Observation(self.group.observation_revision+1,
            self.now,self.now+1800000,target or self.group.observation.controls,(('ready',ready),),envelope or Envelope(0,0))))

    def conditions(self, *, energy=5, pv=0, load_w=1000, now=None, **changes):
        if now is not None: self.now=now
        s=self.compiled.summary
        c=ExecutionConditions(self.state.conditions_revision+1,self.now,self.now+1800000,energy,pv,load_w,load_w,s.identity.context,s.permissions)
        return self.event(ConditionsObserved(replace(c,**changes)))

    def offer(self, compiled=None, watermark=None):
        if compiled is not None: self.compiled=compiled
        return self.event(PolicyOffered(self.compiled,watermark or self.watermark))

    def prepare(self):
        group=self.group
        request=group.release if group.release_pending or group.mode!='controlling' or not group.desired else group.desired
        current=group.observation.controls
        steps=[]
        # Explicit synthetic transition evidence; no hardware sequencing claim.
        for key,value in request.target:
            if dict(current)[key] != value:
                step=Step(key,value,current,(Guard('ready',1,1),),group.spec.maximum,100,200,True,'synthetic-transient')
                steps.append(step);current=step.after
        self.event(Proposed(group.spec.id,group.generation,request.id,request.revision,group.observation_revision,
            group.spec.adapter_revision,tuple(steps),group.transition_work.token))
        return self.group.attempts[-1].prepared_revision if self.group.attempts else None

    def durable(self):
        a=next(a for a in self.group.attempts if a.stage=='prepared')
        self.event(JournalDurable(a.prepared_revision))
        send=next((e for e in self.effects if isinstance(e,Send)),None)
        if send: assert authorize_send(self.state,send,self.now)
        return send

    def replacement(self, *, cut=None, revision=None, **economic_changes):
        wire=json.loads(self.compiled.source_json)
        wire['identity']['revision']=revision or self.compiled.summary.identity.revision+1
        wire['identity']['policy_id']=f"replacement-{wire['identity']['revision']}"
        if cut is not None:
            wire['actuals_origin_ms']=cut
            wire['validity']['from_ms']=cut
        wire['economics'].update(economic_changes)
        return load(wire)

    def refresh_scope(self):
        scope_revision = f'scope-mode-{self.group.mode_revision}'
        scope = replace(self.authority.scope, revision=scope_revision,
            participants=tuple(replace(p, mode=self.group.mode, mode_revision=self.group.mode_revision)
                               if p.group_id == self.group.spec.id else p for p in self.authority.scope.participants))
        self.authority = replace(self.authority, scope=scope,
                                 identity=replace(self.authority.identity, scope_revision=scope_revision))
        self.event(AuthorityInstalled(self.authority, self.state.authority_revision + 1))
        wire = json.loads(self.replacement().source_json)
        wire['identity']['scope_revision'] = scope_revision
        self.compiled = load(wire)
        self.grant(); self.conditions(); self.offer()

    def add_generic_group(self, power=100):
        spec = GroupSpec('fan', 'fake', ('fan.enable',), Envelope(power, 0),
                         writer=WriterIdentity('fan-runtime', 'fan-config', 'fan-surface'))
        extra = create_home((spec,), self.state.limits).groups[0]
        self.state = replace(self.state, groups=(*self.state.groups, extra))
        scope = replace(self.authority.scope, revision='scope-with-fan', participants=(*self.authority.scope.participants,
            ScopeParticipant('fan', 'controlling', 1, 'new_runtime', ('fan.enable',))))
        self.authority = replace(self.authority, scope=scope,
                                 identity=replace(self.authority.identity, scope_revision=scope.revision))
        self.event(AuthorityInstalled(self.authority, self.state.authority_revision + 1)); self.grant(); self.conditions()
        self.event(AuthorityChanged('fan', 'controlling', 1, None))
        self.event(GrantConfirmed('fan', WriterGrant('fan-runtime', 1, 'fan-config', 'fan-surface', 1800000)))
        self.event(Observed('fan', Observation(1, self.now, 1800000, (('fan.enable', 0),), (), Envelope(0, 0))))
        request = Request('fan-request', 1, 900000, (('fan.enable', 1),), ())
        self.event(Requested('fan', 1, request))
        return request


class ExecutableRuntimeTests(unittest.TestCase):
    def test_mode_entry_and_revision_change_require_new_execution_scope(self):
        h = Harness(mode='planning'); h.offer()
        self.assertEqual(h.state.policy.status, 'diagnostic_only')
        h.event(AuthorityChanged('battery', 'controlling', 2, None))
        self.assertIsNone(h.group.desired)
        self.assertEqual(h.state.policy.status, 'outside_coverage')
        h.offer(h.replacement())
        self.assertIsNone(h.group.desired, 'economic renewal cannot reauthorise an old scope')
        h.refresh_scope()
        self.assertEqual(h.state.policy.status, 'active')
        h.event(AuthorityChanged('battery', 'controlling', 3, None))
        self.assertIsNone(h.group.desired, 'even same-mode revision changes fence old policy')
        h.refresh_scope()
        self.assertEqual(h.state.policy.status, 'active')

    def test_changed_scope_cannot_reuse_its_identity(self):
        h = Harness()
        scope = replace(h.authority.scope, participants=tuple(replace(p, mode_revision=2) for p in h.authority.scope.participants))
        with self.assertRaisesRegex(ValueError, 'new identity'):
            h.event(AuthorityInstalled(replace(h.authority, scope=scope), 2))

    def test_stale_conditions_withdraw_owned_operation_and_request_release(self):
        h = Harness(); h.conditions(valid_until_ms=100); h.offer(); h.observe(h.group.desired.target)
        h.event(Tick(), 100)
        self.assertIsNone(h.group.desired)
        self.assertTrue(h.group.release_pending)
        self.assertEqual(h.state.policy.status, 'outside_coverage')
        self.assertTrue(any(isinstance(e, NeedTransition) and e.purpose == 'release' for e in h.effects))

    def test_fresh_reentry_supersedes_unissued_release_but_preserves_issued_effects(self):
        for issued in (False, True):
            h = Harness(); h.offer(); target = h.group.desired.target; h.observe(target)
            h.event(AuthorityChanged('battery', 'planning', 2, None))
            revision = h.prepare()
            if issued: h.durable()
            attempts = h.group.attempts
            h.event(AuthorityChanged('battery', 'controlling', 3, None))
            h.refresh_scope()
            self.assertFalse(h.group.release_pending)
            self.assertEqual(h.group.attempts, attempts if issued else ())
            self.assertEqual(h.group.desired.target, target)
            self.assertEqual(h.group.status, 'reconciling' if issued else 'adopted')
            h.event(JournalDurable(revision))
            self.assertFalse(any(isinstance(e, Send) for e in h.effects))

    def test_policy_is_usable_for_minutes_with_live_conditions_and_no_observation_journal_churn(self):
        h=Harness();h.offer()
        self.assertEqual(h.state.policy.selected_id,'grid_charge')
        request=h.group.desired;generation=h.group.generation
        # Establish the target without any fabricated transport confirmation.
        h.observe(request.target)
        for now in (60000,180000,300000):
            h.conditions(now=now,energy=5.1)
            self.assertEqual(h.group.generation,generation)
            self.assertEqual(h.group.desired,request)
            self.assertFalse(any(isinstance(e,Persist) for e in h.effects))
            h.observe(request.target)
            self.assertFalse(any(isinstance(e,Persist) for e in h.effects))
        self.assertEqual(h.state.policy.status,'active')

    def test_same_target_renewal_keeps_identity_generation_and_original_prepared_window(self):
        h=Harness();h.offer();rev=h.prepare();old=h.group.attempts[0];request=h.group.desired
        h.offer(h.replacement())
        self.assertEqual(h.group.desired,request)
        self.assertEqual(h.group.attempts[0],old)
        h.event(JournalDurable(rev),old.send_by_ms)
        self.assertFalse(any(isinstance(e,Send) for e in h.effects))
        self.assertFalse(any(a.id==old.id for a in h.group.attempts))

    def test_fresh_conditions_do_not_extend_an_old_prepared_send_window(self):
        h=Harness();h.conditions(valid_until_ms=50);h.offer();rev=h.prepare()
        self.assertEqual(h.group.attempts[0].send_by_ms,50)
        h.conditions(valid_until_ms=900000)
        self.assertEqual(h.group.attempts[0].send_by_ms,50)
        h.event(JournalDurable(rev),50)
        self.assertFalse(any(isinstance(e,Send) for e in h.effects))

    def test_old_ack_can_send_same_target_only_within_original_proof(self):
        h=Harness();h.offer();rev=h.prepare();old=h.group.attempts[0]
        h.offer(h.replacement());h.event(JournalDurable(rev),old.send_by_ms-1)
        send=next(e for e in h.effects if isinstance(e,Send))
        self.assertEqual(send.send_by_ms,old.send_by_ms)
        self.assertTrue(authorize_send(h.state,send,h.now))
        self.assertFalse(authorize_send(h.state,send,old.send_by_ms))

    def test_discretionary_change_requires_five_seconds_and_two_fresh_observations(self):
        h=Harness();h.offer();request=h.group.desired
        h.offer(h.replacement(import_sek_per_kwh=1,export_sek_per_kwh=.5))
        self.assertEqual(h.group.desired,request)
        h.event(Tick(),6000)
        self.assertEqual(h.group.desired,request,'time alone is not a fresh observation')
        h.conditions(now=6000)
        self.assertNotEqual(h.group.desired.target,request.target)
        self.assertGreater(h.group.generation,request.revision)

    def test_native_guard_pauses_writes_and_retains_policy_and_issued_attempt(self):
        h=Harness();h.offer();h.prepare();h.durable();issued=h.group.attempts
        # A transient response guard affects command dispatch, not economics.
        # It cannot discard already-issued work or start a baseline release.
        h.observe(ready=0)
        self.assertEqual(h.state.policy.status,'active')
        self.assertIsNotNone(h.group.desired)
        self.assertFalse(h.group.release_pending)
        self.assertEqual(h.group.status,'native_guard_blocked')
        self.assertFalse(any(isinstance(e,Send) for e in h.effects))
        self.assertEqual(h.group.attempts,issued)

    def test_refresh_effect_is_deduplicated_and_expiry_has_no_coverage_grace(self):
        h=Harness();h.offer();h.observe(h.group.desired.target)
        h.event(Tick(),840000)
        self.assertEqual(sum(isinstance(e,NeedPolicy) for e in h.effects),1)
        h.event(Tick(),850000)
        self.assertFalse(any(isinstance(e,(NeedPolicy,Persist)) for e in h.effects))
        h.event(Tick(),900000)
        self.assertIsNone(h.group.desired)
        self.assertTrue(h.group.release_pending)
        self.assertEqual(h.state.policy.status,'outside_coverage')
        self.assertTrue(any(isinstance(e,NeedTransition) and e.purpose=='release' for e in h.effects))

    def test_scope_blocks_direct_battery_requests_even_without_or_after_policy(self):
        h=Harness()
        for event in (None,PolicyOffered(h.compiled,h.watermark),Tick()):
            if event: h.event(event,900000 if isinstance(event,Tick) else h.now)
            with self.assertRaises(ValueError): h.event(Requested('battery',1,Request('bypass',99,1800000,native(),())))

    def test_mixed_external_ev_and_pool_are_real_demand_and_cannot_be_planned_away(self):
        h=Harness(mixed=True);h.offer();frame=h.state.frame
        self.assertEqual(frame.external.import_w,8000)
        h.conditions(load_w=0)
        self.assertIsNone(h.group.desired)
        self.assertEqual(h.state.frame,frame)
        h.conditions(load_w=8000);h.frame(mixed=True,limit=10000)
        h.offer(h.replacement())
        self.assertNotEqual(h.state.policy.selected_id,'grid_charge')
        self.assertEqual(h.state.frame.external.import_w,8000)
        bad=replace(h.state.frame,revision=99,external=Envelope(0,0))
        h.event(FrameObserved(bad))
        self.assertFalse(any(isinstance(e,Send) for e in h.effects))

    def test_overlapping_surfaces_conditional_policy_and_native_mapping_mismatches_reject(self):
        h=Harness()
        with self.assertRaises(ValueError):
            replace(h.authority.scope,participants=(*h.authority.scope.participants,
                ScopeParticipant('ev','planning',1,'legacy',('mode',))))
        wire=json.loads(h.compiled.source_json);wire['view']='conditional'
        with self.assertRaises(ValueError):load(wire)
        catalog=h.authority.catalog
        for change in ({'quantum_w':700},{'mode_options':('Standby',)}, {'charge_max_w':3999}):
            with self.assertRaises(ValueError):replace(catalog,**change)
        b=next(b for b in catalog.bindings if b.operation.operation=='grid_charge')
        with self.assertRaises(ValueError):
            replace(catalog,bindings=tuple(replace(b,target=native('Command Charging (Grid First)',3000)) if x==b else x for x in catalog.bindings))

    def test_grants_are_explicit_and_final_send_rechecks_every_identity(self):
        h=Harness();h.event(GrantRevoked('battery',1));h.offer()
        self.assertFalse(any(isinstance(e,NeedTransition) for e in h.effects))
        h.grant(2);h.prepare();send=h.durable()
        self.assertTrue(authorize_send(h.state,send,h.now))
        for modified in (replace(send,generation=send.generation+1),replace(send,request_revision=999),
                         replace(send,attempt_id='battery:999'),replace(send,send_by_ms=send.send_by_ms+1),
                         replace(send,grant=replace(send.grant,epoch=999))):
            self.assertFalse(authorize_send(h.state,modified,h.now))
        h.event(GrantRevoked('battery',2))
        self.assertFalse(authorize_send(h.state,send,h.now))
        self.assertEqual(h.group.attempts[0].stage,'sent')
        h.event(GrantConfirmed('battery',send.grant))
        self.assertIsNone(h.group.grant)

    def test_grant_expiry_fences_prepared_attempt_and_foreign_identity_cannot_send(self):
        h=Harness();h.grant(2,expires_at_ms=50);h.offer();rev=h.prepare()
        self.assertEqual(h.group.attempts[0].send_by_ms,50)
        h.event(JournalDurable(rev),50)
        self.assertFalse(any(isinstance(e,Send) for e in h.effects))
        self.assertEqual(h.group.attempts,())
        for change in ({'owner_id':'legacy-writer'}, {'config_revision':'foreign'},
                       {'control_surface_revision':'foreign'}):
            h=Harness();h.grant(2,**change);h.offer()
            self.assertFalse(any(isinstance(e,NeedTransition) for e in h.effects))
            self.assertEqual(h.group.status,'awaiting_grant')

    def test_runtime_deadband_retains_incumbent_within_two_ore(self):
        wire=json.loads(load().source_json)
        wire['plant']['wear_sek_per_kwh']=0
        wire['operations']=[o for o in wire['operations'] if o['id'] in ('hold','grid_charge')]
        h=Harness(compiled=load(wire));h.offer();request=h.group.desired
        h.offer(h.replacement(import_sek_per_kwh=.01))
        h.conditions(now=6000)
        self.assertEqual(h.group.desired,request)
        self.assertIsNone(h.state.policy.candidate_id)

    def test_expired_restart_uses_only_approved_release_after_fresh_grant_and_frame(self):
        h=Harness();h.offer();h.observe(h.group.desired.target)
        target=h.group.desired.target
        h.state,_=restore_checkpoint(encode_checkpoint(h.state),900001);h.now=900001
        h.event(Tick())
        self.assertIsNone(h.group.desired)
        self.assertFalse(any(isinstance(e,Send) for e in h.effects))
        h.event(AuthorityChanged('battery','controlling',1,None));h.grant();h.frame();h.observe(target)
        self.assertTrue(any(isinstance(e,NeedTransition) and e.purpose=='release' for e in h.effects))
        self.assertTrue(h.group.release_pending)

    def test_new_grant_and_config_fence_unsent_but_preserve_issued(self):
        for issued in (False,True):
            h=Harness();h.offer();rev=h.prepare()
            if issued:h.durable()
            original=h.group.attempts[0]
            h.grant(2)
            self.assertEqual(h.group.attempts,(original,) if issued else ())
            h.event(JournalDurable(rev))
            self.assertFalse(any(isinstance(e,Send) for e in h.effects))
        h=Harness();h.offer();rev=h.prepare()
        h.event(AuthorityInstalled(replace(h.authority,config_revision='config-2'),2))
        h.event(JournalDurable(rev))
        self.assertFalse(any(isinstance(e,Send) for e in h.effects))
        self.assertEqual(h.group.attempts,())

    def test_restart_retains_valid_operation_without_baseline_cycle(self):
        h=Harness();h.offer();h.observe(h.group.desired.target);request=h.group.desired
        h.state,effects=restore_checkpoint(encode_checkpoint(h.state),1000);h.now=1000
        self.assertEqual(h.group.desired,request)
        self.assertEqual(h.state.policy.status,'awaiting_context')
        h.event(Tick())
        self.assertFalse(any(isinstance(e,NeedTransition) for e in h.effects))
        h.grant();h.frame();h.observe(request.target)
        h.conditions()
        self.assertEqual(h.group.status,'adopted')
        self.assertEqual(h.group.desired,request)
        self.assertFalse(any(isinstance(e,(Send,NeedTransition)) for e in h.effects))

    def test_release_failure_and_rapid_reentry_keep_release_contract_independent(self):
        h=Harness();h.offer();h.observe(h.group.desired.target)
        h.event(AuthorityChanged('battery','planning',2,None))
        self.assertTrue(h.group.release_pending)
        h.event(TransitionFailed('battery',h.group.transition_work.token,'unsupported','synthetic release unsupported'))
        h.event(AuthorityChanged('battery','controlling',3,None))
        self.assertTrue(h.group.release_pending)
        self.assertTrue(any(isinstance(e,NeedTransition) and e.purpose=='release' for e in h.effects))
        release=replace(h.group.release,revision=2,target=native())
        h.event(ReleaseApproved('battery',release))
        self.assertEqual(h.group.mode_revision,3)
        self.assertEqual(h.group.release,release)
        h.prepare();h.durable();attempt=h.group.attempts[0]
        h.state,_=restore_checkpoint(encode_checkpoint(h.state),h.now)
        self.assertEqual(h.group.attempts[0].stage,'ambiguous')
        self.assertEqual(h.group.attempts[0].latest_effect_ms,attempt.latest_effect_ms)
        self.assertTrue(h.group.release_pending)

    def test_delayed_receipt_accounts_at_source_cut_replacement_pruning_and_restart(self):
        h=Harness();h.offer();origin=h.state.policy.watermark
        h.event(MeterObserved(CounterSample('charge','physical-register',0,1,10000,1000100)),10000)
        cut=mark_actuals(h.state.ledger,10000)
        replacement=h.replacement(cut=10000)
        h.event(MeterObserved(CounterSample('charge','physical-register',0,2,20000,1000300)),20000)
        h.offer(replacement,cut)
        self.assertEqual(h.state.policy.reconciled_from,origin)
        self.assertEqual(h.state.policy.reconciled_actuals[0].energy,EnergyBounds(100,100))
        actual=reconciled_actuals(h.state.ledger,cut,h.state.policy.settled_actuals,h.now)
        self.assertEqual(actual[0].energy,EnergyBounds(200,200))
        h.event(LedgerPruned(20000))
        h.state,_=restore_checkpoint(encode_checkpoint(h.state),h.now)
        self.assertEqual(reconciled_actuals(h.state.ledger,cut,h.state.policy.settled_actuals,h.now),actual)
        self.assertEqual(h.state.ledger.streams[0].lifetime,EnergyBounds(300,300))

    def test_delayed_initial_policy_and_duplicate_stale_foreign_or_pruned_cuts_are_atomic(self):
        h=Harness();h.event(MeterObserved(CounterSample('charge','physical-register',0,1,10000,1000100)),10000)
        h.offer();self.assertEqual(h.state.policy.watermark.at_ms,0)
        old=h.state.policy;request=h.group.desired
        for compiled,watermark in ((h.compiled,h.watermark),(h.replacement(),replace(h.watermark,ledger_id='foreign'))):
            h.offer(compiled,watermark)
            self.assertEqual(h.state.policy,old);self.assertEqual(h.group.desired,request)
        h.event(LedgerPruned(10000));old=h.state.policy
        h.offer(h.replacement(cut=5000),replace(h.watermark,at_ms=5000))
        self.assertEqual(h.state.policy,old)

    def test_generic_independent_group_keeps_its_request_path_in_policy_scope(self):
        h=Harness();h.offer()
        request=h.add_generic_group()
        self.assertEqual(h.state.groups[1].desired,request)
        self.assertTrue(any(isinstance(e,NeedTransition) and e.group_id=='fan' for e in h.effects))

    def test_external_ownership_handover_cannot_erase_issued_demand(self):
        from home_runtime import _room, _scope_frame_valid
        h = Harness(); h.frame(limit=10000); request = h.add_generic_group(7000)
        group = h.state.groups[1]
        step = Step('fan.enable', 1, group.observation.controls, (), Envelope(7000, 0), 100, 200, True, 'synthetic')
        h.event(Proposed('fan', group.generation, request.id, request.revision, group.observation_revision,
                        group.spec.adapter_revision, (step,), group.transition_work.token))
        h.event(JournalDurable(h.state.groups[1].attempts[0].prepared_revision))
        self.assertFalse(_room(h.state, h.group, Envelope(4000, 0), h.now))
        scope = replace(h.authority.scope, revision='scope-fan-external', participants=tuple(
            replace(p, owner='external') if p.group_id == 'fan' else p for p in h.authority.scope.participants))
        h.authority = replace(h.authority, scope=scope,
                              identity=replace(h.authority.identity, scope_revision=scope.revision))
        h.event(AuthorityInstalled(h.authority, 3)); h.grant()
        for revision, unresolved, external, valid in ((3, 0, 1000, False), (4, 7000, 8000, True)):
            h.event(FrameObserved(Frame(revision, h.now, 1800000, Envelope(external, 0), Envelope(10000, 10000),
                (ExternalDemand('fan', Envelope(0, 0), Envelope(unresolved, 0)),))))
            self.assertEqual(_scope_frame_valid(h.state, h.now), valid)
            self.assertFalse(_room(h.state, h.group, Envelope(4000, 0), h.now))
        self.assertTrue(_room(h.state, h.group, Envelope(1000, 0), h.now), 'external reservation must count once')
        self.assertEqual(h.state.groups[1].attempts[0].stage, 'sent')

    def test_export_reserve_and_actual_response_violation_reselect_immediately(self):
        wire=json.loads(load().source_json)
        wire['economics'].update(import_sek_per_kwh=.1,export_sek_per_kwh=2)
        h=Harness(compiled=load(wire));h.conditions(load_w=0);h.offer()
        self.assertEqual(h.state.policy.selected_id,'export')
        old=h.group.desired
        h.conditions(energy=3.1,load_w=0)
        self.assertNotEqual(h.state.policy.selected_id,'export')
        self.assertNotEqual(h.group.desired,old)
        h=Harness(compiled=load(wire));h.conditions(load_w=0);h.offer()
        h.observe(h.group.desired.target,envelope=Envelope(0,3000))
        self.assertNotEqual(h.state.policy.selected_id,'export')

    def test_generated_schema_seven_policy_trace_keeps_issued_reservation_after_expiry(self):
        root=Path(__file__).parents[1]
        output=subprocess.run([sys.executable,str(root/'scripts/replay-home-runtime.py'),
            str(FIXTURES/'home-runtime-policy-binding.json')],check=True,capture_output=True,text=True)
        trace=json.loads(output.stdout)
        sends=[e for row in trace['rows'] for e in row['effects'] if e['type']=='Send']
        self.assertEqual(len(sends),1)
        self.assertEqual(trace['final_checkpoint']['schema_version'],7)
        self.assertEqual(trace['rows'][-1]['policy']['status'],'outside_coverage')
        self.assertEqual(len(trace['rows'][-1]['groups'][0]['attempts']),1)
        self.assertEqual(trace['rows'][-1]['groups'][0]['reservation_w']['import'],4000)

    def test_schema_seven_rejects_six_and_corrupt_grant_or_actuals_proof(self):
        h=Harness();h.offer();h.prepare();encoded=json.loads(encode_checkpoint(h.state))
        self.assertEqual(encoded['schema_version'],7)
        for mutate in (lambda v:v.update(schema_version=6),
            lambda v:v['state']['groups'][0]['attempts'][0]['grant'].update(epoch=999),
            lambda v:v['state']['policy']['watermark'].update(at_ms=1)):
            value=json.loads(json.dumps(encoded));mutate(value)
            with self.assertRaises(ValueError):decode_checkpoint(json.dumps(value).encode())

    def test_scope_change_cannot_reuse_an_old_context_revision(self):
        h = Harness()
        wire = json.loads(h.compiled.source_json)
        wire['supply_scope'] = {'kind': 'none'}
        h.compiled = load(wire)
        effects = h.offer()
        self.assertIsNone(h.state.policy)
        self.assertTrue(any(getattr(e, 'reason', '').startswith('policy_rejected') for e in effects))

    def test_backend_generated_policy_is_usable_with_local_native_catalog(self):
        compiled=read_execution_policy(BACKEND_POLICY.read_bytes());h=Harness(compiled=compiled)
        h.conditions(energy=(compiled.summary.plant.cutoff_kwh+compiled.summary.plant.capacity_kwh)/2)
        h.offer()
        self.assertEqual(h.state.policy.status,'active')
        self.assertIsNotNone(h.group.desired)
        h.observe(h.group.desired.target)
        for elapsed in (60000,180000,300000):
            h.conditions(now=compiled.summary.from_ms+elapsed,energy=5.5)
            self.assertEqual(h.state.policy.status,'active')
            self.assertEqual(h.state.policy.compiled,compiled)


if __name__=='__main__':unittest.main()
