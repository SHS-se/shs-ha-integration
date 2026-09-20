"""Executable session, local native bindings and durable ownership protocol tests.

All native/transport evidence and grants here are synthetic, not commissioned.
"""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from battery_physical import ExecutionConditions, ContextIdentity, Permissions, BatteryPlant, BatteryOperation
from battery_supply import SupplyScope
from types import SimpleNamespace
from battery_conversion import Conversion, Curve
from plan_execution import *
from home_runtime import ReplanRequested, CounterReceived
from energy_ledger import (MeterSpec, CounterSample, EnergyBounds, create_ledger, mark_actuals,
                           reconciled_actuals)
from home_runtime import (
    OperationBinding, NativeCatalog, ScopeParticipant, ExecutionScope, ExecutionAuthority,
    AuthorityInstalled, ConditionsObserved, ExecutionPlanOffered, ExternalDemand, WriterIdentity,
    WriterGrant, GrantConfirmed, GrantRevoked, ReleaseApproved, AuthorityChanged, Requested,
    GroupSpec, Envelope, Limits, Request, Guard, Observation, Observed, Frame, FrameObserved,
    Step, Proposed, TransitionFailed, JournalDurable, TransportResult, Tick, MeterObserved,
    LedgerPruned, Persist, Send, NeedPlan, NeedTransition, create_home, reduce_home,
    reservation, authorize_send,
)
from home_runtime_checkpoint import encode_checkpoint, decode_checkpoint, restore_checkpoint, event_json

FIXTURES = Path(__file__).parent / 'fixtures'


def load():
    plant=BatteryPlant(1,10,4000,4000,.95,.95,17000,13000,'discharged_storage',0)
    identity=ContextIdentity('battery','intent-1','plant-1','scope-1','external-1','tariff-1','pv-first-v1','catalog-1')
    operations=(BatteryOperation('idle','idle',0,0),BatteryOperation('hold','hold',4000,0),
        BatteryOperation('grid_charge','grid_charge',4000,0),
        BatteryOperation('solar_charge','solar_charge',4000,0),BatteryOperation('supply_house','supply_house',4000,4000),
        BatteryOperation('export','export',0,4000))
    return SimpleNamespace(summary=SimpleNamespace(plant=plant,identity=SimpleNamespace(context=identity),
        permissions=Permissions(True,True,True,3,True,0,'tariff-1'),operations=operations,
        supply_scope=SupplyScope.read({'kind':'whole_house'}),from_ms=0,until_ms=900000,actuals_origin_ms=0))


def native(mode='Standby', charge=0, discharge=0):
    return (('mode', mode), ('charge', charge), ('discharge', discharge))


def catalog_for(compiled):
    modes = {'self_consumption':'Maximum Self Consumption', 'solar_charge':'Maximum Self Consumption',
             'supply_house':'Maximum Self Consumption', 'hold':'Maximum Self Consumption',
             'grid_charge':'Command Charging (PV First)',
             'export':'Command Discharging (PV First)', 'idle':'Standby'}
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
        self.authority = ExecutionAuthority('config-1',s.identity.context,s.permissions,replace(s.plant,conversion=Conversion('lossless',Curve(1,0),Curve(1,0),Curve(1,0),0)),
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
        start,end=s.from_ms,s.until_ms
        charge=round(2000*(end-start)/3600);load_energy=round(1000*(end-start)/3600)
        row=ReferenceInterval(start,end,5000000,5000000+charge,'grid_charge','stored_energy',True,False,
            4000,0,False,charge,0,0,load_energy,load_energy+charge,0,0,0,0)
        self.contract=ExecutionContract('initial','plan',0,mode,'config-1','model','stored_energy_mwh',
            round(s.plant.capacity_kwh*1e6),round(s.plant.cutoff_kwh*1e6),round(s.plant.capacity_kwh*1e6),
            round(s.plant.cutoff_kwh*1e6),end,0,None,(row,),
            (Objective('charge','stored_energy',start,end,row.stored_end_mwh,'charge now'),),(),())


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

    def offer(self, contract=None):
        return self.event(ExecutionPlanOffered(contract or self.contract))

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


class ExecutableRuntimeTests(unittest.TestCase):
    def test_only_the_latest_traces_are_retained_in_whole_archive_pages(self):
        from home_runtime import MAX_EXECUTION_TRACES, TRACE_TRIM, ExecutionTrace, retain_traces
        # The archive stores traces 128 to a page; trimming whole pages keeps the rest reusable.
        self.assertEqual((MAX_EXECUTION_TRACES % 128, TRACE_TRIM % 128), (0, 0))
        full = tuple(range(MAX_EXECUTION_TRACES))
        self.assertIs(retain_traces(full), full)
        self.assertEqual(retain_traces((*full, 'new')), (*full[TRACE_TRIM:], 'new'))
        history = tuple(range(97_609))  # a session saved before traces were limited
        kept = retain_traces(history)
        self.assertEqual(kept[-1], history[-1])
        self.assertTrue(MAX_EXECUTION_TRACES - TRACE_TRIM < len(kept) <= MAX_EXECUTION_TRACES)
        self.assertEqual((len(history) - len(kept)) % TRACE_TRIM, 0)
        # The reducer applies the same limit as each evaluation adds its trace.
        h=Harness(mode='control_verification');h.offer()
        old=ExecutionTrace(0,0,0,0,None,'{}','[]',None,None,None)
        state=replace(h.state,execution=replace(h.state.execution,traces=(old,)*MAX_EXECUTION_TRACES))
        state,_=reduce_home(state,Tick(),h.now+1)
        traces=state.execution.traces
        self.assertEqual(len(traces),MAX_EXECUTION_TRACES+1-TRACE_TRIM)
        self.assertEqual(traces[-1].at_ms,h.now+1)
        self.assertEqual(traces[-1].input_json,json.dumps({'type':'Tick'}))

    def test_durable_ack_with_advancing_clock_does_not_persist_forever(self):
        h=Harness(mode='control_verification');h.offer()
        h.event(Tick(),1)
        revision=h.state.revision
        for now in range(2,6):
            h.event(JournalDurable(revision),now)
            self.assertFalse(any(isinstance(e,Persist) for e in h.effects))
            self.assertEqual(h.state.revision,revision)


    def test_nominal_plan_selects_charge_without_economic_ranking_or_timer(self):
        h=Harness();h.offer()
        self.assertEqual(h.state.execution.assessment.operation,'grid_charge')
        self.assertEqual(dict(h.group.desired.target)['charge'],2000)
        self.assertFalse(hasattr(h.state,'policy'))

    def test_retaining_stored_energy_keeps_the_rated_charge_permission(self):
        """`hold` declines to spend, so the plant may still absorb real surplus."""
        h = Harness()
        start, end = h.contract.intervals[0].start_ms, h.contract.intervals[0].end_ms
        load = round(1000 * (end - start) / 3600)
        row = ReferenceInterval(start, end, 5000000, 5000000, 'hold', 'permission', False, False,
            4000, 0, False, 0, 0, 0, load, load, 0, 0, 0, 0)
        h.offer(replace(h.contract, intervals=(row,), objectives=(
            Objective('role', 'permission', start, end, 5000000, 'keep the role'),)))
        self.assertEqual(h.state.execution.assessment.operation, 'hold')
        self.assertEqual(h.state.execution.assessment.charge_dc_w, 0)
        self.assertEqual(dict(h.group.desired.target),
                         {'mode': 'Maximum Self Consumption', 'charge': 4000, 'discharge': 0})

    def test_only_idle_closes_the_charge_permission(self):
        h = Harness()
        start, end = h.contract.intervals[0].start_ms, h.contract.intervals[0].end_ms
        load = round(1000 * (end - start) / 3600)
        row = ReferenceInterval(start, end, 5000000, 5000000, 'idle', 'permission', False, False,
            0, 0, False, 0, 0, 0, load, load, 0, 0, 0, 0)
        h.offer(replace(h.contract, intervals=(row,), objectives=(
            Objective('role', 'permission', start, end, 5000000, 'let surplus export'),)))
        self.assertEqual(h.state.execution.assessment.operation, 'idle')
        self.assertEqual(dict(h.group.desired.target),
                         {'mode': 'Standby', 'charge': 0, 'discharge': 0})

    def test_sized_purchase_still_writes_its_assessed_ceiling(self):
        h = Harness(); h.offer()
        self.assertEqual(h.state.execution.assessment.operation, 'grid_charge')
        self.assertEqual(dict(h.group.desired.target)['charge'],
                         h.state.execution.assessment.charge_dc_w)

    def test_verification_assesses_without_request_or_writer_effect(self):
        h=Harness(mode='control_verification');h.offer()
        self.assertEqual(h.state.execution.status,'diagnostic_only')
        self.assertIsNone(h.group.desired)
        self.assertFalse(any(isinstance(e,Send) for e in h.effects))

    def test_stale_measurements_withdraw_owned_request(self):
        h=Harness();h.offer();h.prepare();h.durable()
        h.event(Tick(),h.state.conditions.valid_until_ms)
        self.assertIsNone(h.group.desired)
        self.assertTrue(h.group.release_pending)
        self.assertTrue(h.group.attempts)

    def test_pending_native_effect_survives_plan_expiry(self):
        h=Harness();h.offer();h.prepare();h.durable()
        before=h.group.attempts
        h.event(Tick(),h.contract.valid_until_ms)
        self.assertEqual(h.group.attempts[0].id,before[0].id)
        self.assertEqual(h.group.attempts[0].stage,'ambiguous')
        self.assertIsNone(h.group.desired)
        self.assertTrue(h.group.release_pending)

    def test_unchanged_target_preserves_native_request_identity(self):
        h=Harness();h.offer();request=h.group.desired
        h.conditions(load_w=1500)
        self.assertEqual(h.group.desired.id,request.id)
        self.assertEqual(h.group.desired.revision,request.revision)

    def test_large_load_error_only_reduces_charge_at_physical_limit(self):
        h=Harness();h.offer();h.conditions(load_w=2500)
        self.assertEqual(h.state.execution.assessment.charge_dc_w,2000)
        h.conditions(load_w=h.authority.plant.import_limit_w-100)
        self.assertEqual(h.state.execution.assessment.charge_dc_w,100)

    def test_rejected_stale_observation_cannot_create_deadline_evidence(self):
        h=Harness();h.offer();before=h.state.execution.account.observations
        h.event(ConditionsObserved(h.state.conditions),h.now+10)
        self.assertEqual(h.state.execution.account.observations,before)

    def test_capture_is_atomic_and_old_response_cannot_replace_new_generation(self):
        h=Harness();h.offer()
        h.event(ReplanRequested(StateObservation(h.now,5000000,'soc'),'config-1'))
        first=json.loads(h.state.execution.captured_feedback)
        h.event(CounterReceived(MeterReceipt('m','charge','charge','battery_dc','0',h.now,1000,0)))
        self.assertEqual(json.loads(h.state.execution.captured_feedback),first)
        h.event(ReplanRequested(StateObservation(h.now,5000000,'soc'),'config-1'))
        h.offer(replace(h.contract,id='late',generation=1,source_receipt=first['source_receipt'],previous_contract_id='initial'))
        self.assertEqual(h.state.execution.account.contract.id,'initial')

    def test_replan_capture_and_issued_effects_survive_restart(self):
        h=Harness();h.offer();h.prepare();h.durable()
        h.event(ReplanRequested(StateObservation(h.now,5000000,'soc'),'config-1'))
        restored,effects=restore_checkpoint(encode_checkpoint(h.state),h.now+1)
        self.assertEqual(restored.execution.account,h.state.execution.account)
        self.assertEqual(restored.execution.captured_feedback,h.state.execution.captured_feedback)
        self.assertEqual(restored.groups[0].attempts[0].stage,'ambiguous')
        self.assertFalse(restored.groups[0].grant_confirmed)
        self.assertFalse(any(isinstance(e,Send) for e in effects))

    def test_final_send_requires_live_grant_and_current_plan(self):
        h=Harness();h.offer();h.prepare();send=h.durable()
        self.assertTrue(authorize_send(h.state,send,h.now))
        state=replace(h.state,execution=replace(h.state.execution,status='unavailable'))
        self.assertFalse(authorize_send(state,send,h.now))
        h.event(GrantRevoked(h.group.spec.id,h.group.grant_epoch+1))
        self.assertFalse(authorize_send(h.state,send,h.now))

    def test_direct_request_cannot_replace_battery_owner(self):
        h=Harness();h.offer()
        with self.assertRaisesRegex(ValueError,'execution owner'):
            h.event(Requested(h.group.spec.id,h.group.mode_revision,h.group.desired))

    def test_mode_change_keeps_verifying_the_accepted_plan_without_writes(self):
        h=Harness();h.offer();h.event(AuthorityChanged('battery','control_verification',2,None))
        self.assertIsNone(h.group.desired)
        self.assertEqual(h.state.execution.status,'diagnostic_only')
        self.assertEqual(h.state.execution.account.contract,h.contract)

    def test_legacy_journal_retires_policy_preserving_issued_effect_and_release(self):
        h=Harness();h.offer();h.prepare();h.durable()
        wire=json.loads(encode_checkpoint(h.state));wire['schema_version']=7
        wire['state'].pop('execution');wire['state']['policy']={'retired':True}
        wire['state']['conditions'].pop('stored_at_ms')
        restored=decode_checkpoint(json.dumps(wire))
        self.assertIsNone(restored.groups[0].desired)
        self.assertTrue(restored.groups[0].release_pending)
        self.assertEqual(restored.groups[0].attempts,h.group.attempts)
        self.assertEqual(restored.groups[0].release,h.group.release)
        self.assertIsNone(restored.execution.account.contract)

    def test_rejected_handover_survives_updates_restart_and_clears_on_acceptance(self):
        h=Harness(mode='control_verification');h.offer()
        h.event(ReplanRequested(StateObservation(h.now,5000000,'soc'),'config-1'))
        updated=replace(h.contract,id='replacement',generation=1,previous_contract_id='initial',
            source_receipt=json.loads(h.state.execution.captured_feedback)['source_receipt'],
            objectives=(replace(h.contract.objectives[0],target_mwh=h.contract.objectives[0].target_mwh+100),))
        h.offer(updated)
        rejection=h.state.execution.plan_rejection
        self.assertEqual(rejection.contract_id,'replacement')
        self.assertIn('explicit retained amendment',rejection.reason)
        self.assertEqual(h.state.execution.account.contract.id,'initial')
        self.assertTrue(any(isinstance(e,NeedPlan) for e in h.effects))
        h.conditions(load_w=1200);h.event(Tick());h.offer(h.contract)
        self.assertEqual(h.state.execution.plan_rejection,rejection)
        restored,_=restore_checkpoint(encode_checkpoint(h.state),h.now)
        self.assertEqual(restored.execution.plan_rejection,rejection)
        updated=replace(updated,dispositions=(Disposition('charge','retained',None,'Amend target'),))
        h.offer(updated)
        self.assertEqual(h.state.execution.account.contract.id,'replacement')
        self.assertIsNone(h.state.execution.plan_rejection)
        # An older response arriving late is recorded in the trace but must not
        # replace the successful handover's current status.
        h.offer(replace(h.contract,id='obsolete'))
        self.assertIsNone(h.state.execution.plan_rejection)

    def test_v9_journal_gains_idle_without_losing_its_writer_fence(self):
        """A journal written before the split keeps its authority, fence and effects."""
        h=Harness();h.offer();h.prepare();h.durable()
        old=json.loads(encode_checkpoint(h.state));old['schema_version']=9
        catalog=old['state']['authority']['catalog']
        # Restore the schema-9 vocabulary: one `hold` closing both ceilings under
        # the inert mode, and `supply_house` without a charge permission.
        rebuilt=[]
        for binding in catalog['bindings']:
            name=binding['operation']['operation']
            if name=='idle':
                continue
            if name=='hold':
                binding={**binding,'operation':{**binding['operation'],'id':'hold','charge_limit_w':0},
                         'target':native('Standby',0,0)}
                binding={**binding,'target':[list(pair) for pair in binding['target']]}
            elif name=='supply_house':
                binding={**binding,'operation':{**binding['operation'],'charge_limit_w':0},
                         'target':[[k,0 if k=='charge' else v] for k,v in binding['target']]}
            rebuilt.append(binding)
        catalog['bindings']=rebuilt
        upgraded=decode_checkpoint(json.dumps(old))
        operations={b.operation.operation:b for b in upgraded.authority.catalog.bindings}
        self.assertEqual(dict(operations['idle'].target),{'mode':'Standby','charge':0,'discharge':0})
        self.assertEqual(dict(operations['hold'].target),
                         {'mode':'Maximum Self Consumption','charge':4000,'discharge':0})
        self.assertEqual(dict(operations['supply_house'].target)['charge'],4000)
        # The live fence and issued native effect survive the vocabulary change.
        self.assertEqual(upgraded.groups[0].attempts,h.state.groups[0].attempts)
        self.assertEqual(upgraded.groups[0].grant,h.state.groups[0].grant)
        self.assertTrue(upgraded.groups[0].owned)
        self.assertEqual(json.loads(encode_checkpoint(upgraded))['schema_version'],10)

    def test_v8_checkpoint_upgrade_preserves_account_and_issued_effects(self):
        h=Harness();h.offer();h.prepare();h.durable()
        old=json.loads(encode_checkpoint(h.state));old['schema_version']=8
        old['state']['execution'].pop('plan_rejection')
        upgraded=decode_checkpoint(json.dumps(old))
        self.assertEqual(upgraded,h.state)
        self.assertEqual(json.loads(encode_checkpoint(upgraded))['schema_version'],10)
