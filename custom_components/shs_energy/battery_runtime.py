"""Production battery owner under the household coordinator.

The cloud supplies the energy plan. This owner supplies physical observations,
durable command execution and the exclusive local writer. No schedule fallback.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import floor, isfinite

if __package__:
    from . import home_runtime as rt
    from .home_host import HomeHost, HostPorts, DispatchRejected
    from .battery_native_adapter import SigenAdapter
    from .battery_conversion import conversion_model, windows_from_statistics
    from .battery_physical import ExecutionConditions, BatteryOperation, ContextIdentity, Permissions, BatteryPlant
    from . import plan_execution as execution
    from .runtime_json import Records
    from .resource_profiling import ResourceProfiler
    from .battery_live import native_surface, source_revision, planned_power_bindings
    from .battery_supply import SupplyScope, observe_supply
    from .configuration_schema import METADATA_KEYS
    from .configuration_values import resolve_battery_quantities
    from .energy_ledger import MeterSpec, CounterSample, create_ledger, mark_retained_actuals
    from .device_controls import battery_measurement_errors, BatteryMeasurementConfigurationError
    from .operating_modes import device_mode
    from .presentation import battery_status_text
else:
    import home_runtime as rt
    from home_host import HomeHost, HostPorts, DispatchRejected
    from battery_native_adapter import SigenAdapter
    from battery_conversion import conversion_model, windows_from_statistics
    from battery_physical import ExecutionConditions, BatteryOperation, ContextIdentity, Permissions, BatteryPlant
    import plan_execution as execution
    from runtime_json import Records
    from resource_profiling import ResourceProfiler
    from battery_live import native_surface, source_revision, planned_power_bindings
    from battery_supply import SupplyScope, observe_supply
    from configuration_schema import METADATA_KEYS
    from configuration_values import resolve_battery_quantities
    from energy_ledger import MeterSpec, CounterSample, create_ledger, mark_retained_actuals
    from device_controls import battery_measurement_errors, BatteryMeasurementConfigurationError
    from operating_modes import device_mode
    from presentation import battery_status_text

AGE_MS=30000
ALIGNMENT_MS=15000
# Entities, the mode select and controller status all read one refresh's account.
ACCOUNTING_REUSE_MS=1000
# Journal/lock waits share the existing measurement lifetime. Freshness and
# authority are still checked after the lock, immediately before each write.
RUNTIME_LIMITS=rt.Limits(AGE_MS,1000,30000)
ADAPTER_REVISION='sigen-ess-dc-v2'
OWNER='shs-household-battery'

class NativeReadbackPending(Exception):
    """The provider has not published its configured controls during startup."""

# Only `idle` is inert: it neither spends stored energy nor absorbs surplus.
# `hold` also declines to spend, but stays in the automatic mode so real surplus
# is still absorbed rather than exported.
MODES={'idle':'Standby','grid_charge':'Command Charging (PV First)',
       'export':'Command Discharging (PV First)','supply_house':'Maximum Self Consumption',
       'solar_charge':'Maximum Self Consumption','self_consumption':'Maximum Self Consumption',
       'hold':'Maximum Self Consumption'}

def stamp(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp needs timezone')
    return round(parsed.timestamp()*1000)

def digest(value):
    return sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()

def plan_scope(options):
    """The local setup a battery plan is captured for, without writer authority.

    Verification and Controlling change only who may write, never the plan, so a
    plan or reference captured under one mode stays valid under the other. The
    review timestamp and other metadata describe no equipment either.
    """
    return digest({k:v for k,v in options.items() if k!='device_modes' and k not in METADATA_KEYS})

def exact_start(slot):
    return stamp(slot['start'])+900000-round(slot['duration_hours']*3600000)

def iso(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')


class BatteryPowerReadingError(ValueError):
    """A failed live source is distinct from an actuator configuration error."""
    fix = {'kind': 'diagnostics'}
    next_step = 'Check that the named power sensor is reporting current measurements. Control retries automatically when fresh readings arrive.'


def power(report, *, source, now_ms, signed=False):
    if not isinstance(report,dict):
        raise BatteryPowerReadingError(f'{source}: configured power source is unavailable')
    attrs=report['attributes'];unit=attrs.get('unit_of_measurement')
    if attrs.get('state_class')!='measurement' or unit not in ('W','kW'):
        raise BatteryPowerReadingError(f'{source}: instantaneous W or kW power required')
    try:
        at=stamp(report['last_reported']);value=float(report['state'])*(1000 if unit=='kW' else 1)
    except (KeyError, TypeError, ValueError) as error:
        raise BatteryPowerReadingError(f'{source}: invalid physical power reading') from error
    if not isfinite(value) or (value<0 and not signed) or not at<=now_ms<at+AGE_MS:
        raise BatteryPowerReadingError(f'{source}: power source is invalid or stale (last report: {report["last_reported"]})')
    return value,at


class BatteryRuntime:
    """All commands use HomeHost and one durable writer fence.

    Construction receives the coordinator/controller and transactional execution storage.
    History/calibration reads are ports on the coordinator; tests use the same
    composition with an in-memory state table and real fake service boundary.
    """
    def __init__(self,coordinator,controller,store,now_ms):
        self.coordinator,self.controller,self.store,self.now=coordinator,controller,store,now_ms
        self.profiler=ResourceProfiler()
        self.host=None;self.adapter=None;self._grant=None;self._identity=None
        self._lock=asyncio.Lock();self._observe_lock=asyncio.Lock();self._closed=False;self._closing=False
        self._options=None;self._devices=[];self._model=None;self._fits={};self._model_sources=None
        self._model_at=0;self._surface=None;self._source_cut=None;self._seeded=False
        self._mode_revision=0;self._mode=None;self._capture_revision=0;self._last_capture=None
        self._status={'state':'pending','reason':'Waiting for the battery plan'}
        self._last_error=None;self._external_pending={};self._frame_pending={};self._releasing=False
        self._release_revision=0
        self._bootstrap=execution.Account();self._replan_task=None;self._replan_pending=False
        self._bootstrap_rejection=None;self._bootstrap_captured=None
        self._fault_history=[]
        self._observation_error=None
        self._live_accounting=None
        controller.battery_runtime=self

    async def open(self):
        restored=await self.store.load()
        if restored is not None:
            value, session = restored
            if value.get('schema') != 'battery-runtime-v4':
                raise ValueError('invalid battery runtime journal')
            if __package__:
                from .home_runtime_checkpoint import decode_checkpoint
            else:
                from home_runtime_checkpoint import decode_checkpoint
            if value['checkpoint'] is None:
                self._bootstrap=session.account
                self._bootstrap_rejection=session.plan_rejection
                self._bootstrap_captured=session.captured_feedback
                return
            checkpoint=value['checkpoint'].encode()
            state=replace(decode_checkpoint(checkpoint),execution=session)
            self._options,self._devices=value['options'],value['devices']
            self._model_sources,self._ratings=value['model_sources'],value['ratings']
            group=state.groups[0];authority=state.authority
            if authority is None:
                if group.owned or group.attempts:
                    raise ValueError('owned journal has no native authority')
                self._options=None;self._model_sources=None
                return
            self._mode_revision,self._mode=group.mode_revision,group.mode
            self._model=authority.plant.conversion
            self._scope=authority.supply_scope
            reports={e:self.coordinator._battery_entity_report(e) for e in self._control_entities()}
            if any(not row or row.get('state') in ('unknown','unavailable') for row in reports.values()):
                raise NativeReadbackPending('Waiting for configured Sigen controls to become available')
            self._surface=native_surface(self._options,reports)
            if self._surface['revision']!=authority.catalog.control_surface_revision:
                raise ValueError('journalled Sigen control surface changed')
            self.adapter=SigenAdapter(authority.catalog,self._model)
            self._identity=rt.WriterIdentity(OWNER,authority.config_revision,self._surface['revision'])
            self._releasing=state.execution.account.contract is None;self._seeded=True
            await self._open_host(state,checkpoint)
            if self._scope.kind=='whole_house':
                participants=tuple(p for p in authority.scope.participants if p.owner=='new_runtime') + tuple(
                    rt.ScopeParticipant(key,mode,1,'external',(entity,)) for key,entity,mode in self._demand_sources())
                if participants!=authority.scope.participants:
                    revision=digest({'previous_scope':authority.scope.revision,'participants':[asdict(p) for p in participants]})
                    recovered=replace(authority,identity=replace(authority.identity,scope_revision=revision),
                                      scope=replace(authority.scope,revision=revision,participants=participants))
                    await self.host.accept(rt.AuthorityInstalled(recovered,self.host.state.authority_revision+1))
            # Recover local ownership without waiting for a cloud plan. The old
            # configured sources remain available for this exact baseline release.
            try:
                if self._releasing:
                    await self._release('Recovering previous battery commands')
            except Exception as error:
                self._status={'state':'fault','reason':f'Battery recovery pending: {error}'}
                self._record_fault('recovery',self._status['reason'])

    async def _open_host(self,state,checkpoint=None):
        ports=HostPorts(self._persist,self._dispatch,self._observe,self._confirm,self._transition,self._renew,self._report,self.now,self._persist_state)
        # Apply current scheduling limits on restart without extending any
        # previously prepared or issued attempt's persisted effect window.
        self.host=HomeHost(replace(state,limits=RUNTIME_LIMITS),ports,self.profiler)
        await self.host.start(resume=checkpoint is not None)

    def identity(self):
        if self._closed or self._identity is None or (not self._releasing and self.controller.options()!=self._options):
            return None
        try:
            reports={e:self.coordinator._battery_entity_report(e) for e in self._control_entities()}
            if native_surface(self._options,reports)['revision']!=self._surface['revision']:
                return None
        except (ValueError,TypeError,KeyError):
            return None
        return self._identity

    def snapshot(self, *, include_evidence=False):
        """Current status; with evidence, the journals as immutable Records to encode later."""
        value={**self._status,'loss_model':self._model.wire() if self._model else None,'loss_evidence':self._fits,
            'runtime_reason':getattr(self,'_runtime_reason',None),'measurements':getattr(self,'_measurements',None),
            'writer_current':self.coordinator.battery_writer.is_current(self._grant,self.identity()),
            'fault_history':[dict(row) for row in self._fault_history],
            'fault_history_scope':'Last 64 distinct faults since integration load'}
        if not self.host:
            accounting_at,accounting=self._accounting(self._bootstrap,include_evidence)
            value.update(mode=device_mode(self.controller.options(),'battery'), accounting_at_ms=accounting_at,
                accounting=accounting)
            if include_evidence:
                value.update(accounting_journal=Records(self._bootstrap),
                    captured_replan=json.loads(self._bootstrap_captured) if self._bootstrap_captured else None)
            return self._plan_status(value, self._bootstrap_rejection, self._bootstrap.contract)
        state=self.host.state;group=state.groups[0];session=state.execution
        accounting_at,accounting=self._accounting(session.account,include_evidence)
        value.update(command_state=group.status,mode=group.mode,execution_state=session.status,
            accounting_at_ms=accounting_at,
            pending_writes=sum(a.stage!="accepted" for a in group.attempts),native_target=dict(group.desired.target) if group.desired else None,
            accounting=accounting,
            assessment=asdict(session.assessment) if session.assessment else None,
            )
        if include_evidence:
            value.update(accounting_journal=Records(session.account), command_journal=Records(group),
                captured_replan=json.loads(session.captured_feedback) if session.captured_feedback else None,
                execution_traces=Records(session.traces),
                execution_trace_retention={'retained': len(session.traces), 'max_traces': rt.MAX_EXECUTION_TRACES,
                    'removed_together': rt.TRACE_TRIM,
                    'scope': 'Latest execution traces only; the accounting journal keeps the complete history.'},
                execution_input=asdict(session.live) if session.live else None,
                conversion_basis=state.authority.plant.conversion.wire() if state.authority else None)
        value['pending_commands']=[{'entity_id':a.step.key,'value':a.step.value,'stage':a.stage,
            'started_at':iso(a.prepared_at_ms),'confirmation_deadline':iso(a.confirmation_deadline_ms),
            'uncertain_until':iso(a.latest_effect_ms)} for a in group.attempts]
        if group.desired:
            target=dict(group.desired.target)
            value['requested_settings']={'mode':target[self._options['battery_mode_entity']],
                'charge_limit_w':target[self._options['battery_charge_limit_entity']],
                'discharge_limit_w':target[self._options['battery_discharge_limit_entity']]}
            if session.assessment:
                value['decision']={'kind':'battery','operation':session.assessment.operation,
                    'charge_limit_w':value['requested_settings']['charge_limit_w'],
                    'discharge_limit_w':value['requested_settings']['discharge_limit_w']}
        if state.conditions and session.assessment and state.conditions.valid_until_ms>self.now():
            value['explanation']=execution.explain_execution(session.account,rt.execution_live(state,self.now()),
                session.assessment,mode=group.mode,measured_battery_dc_w=getattr(self,'_measurements',{}).get('battery_dc_w'))
            if self._status['state']!='fault':
                value['reason']=value['explanation']['status']
        if self.host._fault:
            value.update(state='fault',reason='Battery control has stopped because its saved execution state could not be updated.',
                technical_error=str(self.host._fault),fix={'kind':'diagnostics'},retry_automatically=False,
                next_step='Download controller diagnostics and report this software error. Restart Home Assistant after installing the fix.')
        elif self._observation_error:
            value.update(state='fault',reason=self._observation_error)
        elif any(a.stage=='ambiguous' for a in group.attempts):
            reason=next((f['reason'] for f in reversed(self._fault_history) if 'send failed' in f['reason'] or 'Modbus' in f['reason']), 'The battery has not confirmed its settings; waiting for fresh readings')
            value.update(state='fault',reason=reason)
        elif isinstance(group.transition_work,rt.TransitionFailure):
            value.update(state='fault',reason=group.transition_work.reason)
        elif group.mode=='controlling' and session.status=='active':
            if group.status=='adopted':
                value.update(state='controlling',runtime_reason=None)
            else:
                value.update(state='pending',reason='Applying the planned battery settings')
        if value['state']=='fault' and 'fix' not in value:
            value.update(fix={'kind':'diagnostics'},next_step='Check the reported source or command failure. Download diagnostics if it persists.',retry_automatically=True)
        return self._plan_status(value, session.plan_rejection, session.account.contract)

    def _accounting(self,account,include_evidence):
        """Full evidence on request; otherwise one shared live view per account and refresh.

        Accounts are immutable, so the same object means the same records. The
        shared view is read-only for its callers.
        """
        now=self.now()
        if include_evidence:
            with self.profiler.measure('accounting_view'):
                return now,execution.feedback(account,now)
        cached=self._live_accounting
        if cached is not None and cached[0] is account and 0<=now-cached[1]<ACCOUNTING_REUSE_MS:
            return cached[1],cached[2]
        with self.profiler.measure('accounting_view'):
            self._live_accounting=(account,now,execution.live_feedback(account,now))
        return now,self._live_accounting[2]

    def resource_counts(self):
        """Cheap gauges: never traverse or serialize the retained evidence."""
        session=self.host.state.execution if self.host else None
        account=session.account if session else self._bootstrap
        return {**{name:len(getattr(account,name)) for name in
                   ('meters','observations','admissions','requests','reconciliations')},
                'receipt':account.receipt, 'traces':len(session.traces) if session else 0,
                **self.store.resource_counts(),
                **(self.host.resource_counts() if self.host else {'queued_events':0,'active_effects':0})}

    def _plan_status(self, value, rejection, accepted):
        value.update(plan_status='rejected' if rejection else 'accepted' if accepted else 'awaiting_plan',
            accepted_plan_id=accepted.plan_id if accepted else None,
            accepted_reference_id=accepted.id if accepted else None,
            plan_rejection=asdict(rejection) if rejection else None)
        if rejection:
            # The accepted reference remains authoritative within its existing
            # permissions and lifetime. Rejection never authorises new actions.
            if accepted is None and value['state'] != 'fault':
                value.update(state='fault',reason='A new battery plan could not be accepted.',
                    fix={'kind':'diagnostics'},retry_automatically=True,
                    next_step='Waiting for a corrected plan. Download controller diagnostics if this persists.')
            value.setdefault('fix', {'kind':'diagnostics'})
            value.setdefault('next_step', 'Using the previously accepted plan. Download diagnostics if planning failures persist.')
            if value.get('explanation'):
                value['explanation']={**value['explanation'],
                    'plan':'Previously accepted plan: ' + value['explanation']['plan'],
                    'next':'Waiting for a corrected plan; the rejected update is not being used.'}
        value['display'] = battery_status_text(value)
        return value

    async def reject_plan_response(self, plan, reason):
        """Publish pre-cache identity/contract rejection through the same journal."""
        wire=plan.get('battery_execution')
        wire=wire if isinstance(wire,dict) else {}
        contract_id=wire.get('id') if isinstance(wire.get('id'),str) else None
        generation=wire.get('generation') if type(wire.get('generation')) is int else None
        rejection=rt.PlanRejection(self.now(),contract_id,generation,reason)
        async with self._lock:
            if self.host:
                await self.host.accept(rt.ExecutionPlanRejected(rejection))
            else:
                session=rt.record_plan_rejection(rt.ExecutionSession(account=self._bootstrap,
                    plan_rejection=self._bootstrap_rejection),rejection)
                self._bootstrap_rejection=session.plan_rejection
                await self._persist_bootstrap()
            self.coordinator.async_update_listeners()

    async def _persist_bootstrap(self):
        session=rt.ExecutionSession(account=self._bootstrap,
            captured_feedback=self._bootstrap_captured,plan_rejection=self._bootstrap_rejection)
        with self.profiler.measure('checkpoint_save'):
            await self.store.save({'schema':'battery-runtime-v4','checkpoint':None,
                'options':None,'devices':[],'model_sources':None,'ratings':None},session)

    def validate_plan_response(self,plan):
        """Reject stale response identity before replacing the coordinator's cache."""
        wire=plan.get('battery_execution')
        if wire is None:
            if plan.get('battery') and device_mode(self.controller.options(),'battery') in ('controlling','control_verification'):
                raise ValueError('The planner did not provide battery execution instructions')
            return
        contract=execution.read_contract(wire)
        options=self.controller.options()
        account=self.host.state.execution.account if self.host else self._bootstrap
        if account.contract and contract.id==account.contract.id:
            if contract!=account.contract:raise ValueError('Accepted battery reference was changed')
            return
        if contract.scope_revision!=plan_scope(options):
            raise ValueError('Battery plan belongs to a different local setup')
        anchor=next((r for r in account.requests if r.generation==contract.generation),None)
        if contract.generation!=account.requested_generation or anchor is None or anchor.source_receipt!=contract.source_receipt:
            raise ValueError('Battery plan belongs to a superseded request or different actuals prefix')
        if contract.previous_contract_id!=(account.contract.id if account.contract else None):
            raise ValueError('Battery plan does not acknowledge the accepted reference')
        # Run the real handover checks before the new plan can replace the
        # coordinator cache. This is a pure transition: no journal or commands.
        now=self.now()
        ratings=resolve_battery_quantities(options,self.coordinator._battery_entity_report)
        soc=self.coordinator._battery_entity_report(options['battery_soc_entity'])
        stored=round(float(soc['state']) / 100 * ratings['battery_capacity_kwh'] * 1e6)
        execution.admit_plan(account,contract,now,execution.StateObservation(now,stored,'live_soc_at_validation',False))

    async def capture_feedback(self, stored_mwh, source, at_ms=None):
        async with self._lock:
            await self._retire_changed_configuration(self.controller.options())
            return await self._capture_feedback(stored_mwh, source, at_ms)

    async def _capture_feedback(self, stored_mwh, source, at_ms=None):
        """Persist the request generation and freeze its evidence before network I/O."""
        if self.host:
            state=await self.host.accept(rt.ReplanRequested(
                execution.StateObservation(at_ms if at_ms is not None else self.now(),stored_mwh,source),plan_scope(self.controller.options())))
            return json.loads(state.execution.captured_feedback)
        else:
            self._bootstrap=execution.observe_state(self._bootstrap,
                execution.StateObservation(at_ms if at_ms is not None else self.now(),stored_mwh,source))
            self._bootstrap=execution.request_replan(self._bootstrap)
            captured=execution.planner_feedback(self._bootstrap,self.now())
            captured.update(scope_revision=plan_scope(self.controller.options()),reason=getattr(self,'_runtime_reason',None),pending_effects=[])
            self._bootstrap_captured=json.dumps(captured)
            await self._persist_bootstrap()
            return captured

    def _control_entities(self):
        return tuple(self._options[k] for k in ('battery_mode_entity','battery_charge_limit_entity','battery_discharge_limit_entity'))

    async def _persist(self,data):
        raise RuntimeError('battery execution requires an atomic archived checkpoint')

    async def _persist_state(self,state):
        if __package__:
            from .home_runtime_checkpoint import encode_checkpoint, _check_state
        else:
            from home_runtime_checkpoint import encode_checkpoint, _check_state
        with self.profiler.measure('checkpoint_encode'):
            _check_state(state)
            shell=replace(state,execution=rt.ExecutionSession())
            checkpoint=encode_checkpoint(shell).decode()
        with self.profiler.measure('checkpoint_save'):
            await self.store.save({'schema':'battery-runtime-v4','checkpoint':checkpoint,
                'options':self._options,'devices':self._devices,
                'model_sources':self._model_sources,'ratings':self._ratings},state.execution)

    def _report(self,group,reason):
        if reason not in ('meter_recorded','duplicate_meter_sample','stale_meter_sample'):
            self._runtime_reason=reason
            self._record_fault('runtime',reason)

    def _record_fault(self,source,reason):
        now=iso(self.now())
        if self._fault_history and (self._fault_history[-1]['source'],self._fault_history[-1]['reason'])==(source,reason):
            self._fault_history[-1].update(last_at=now,count=self._fault_history[-1]['count']+1)
        else:
            self._fault_history.append({'at':now,'last_at':now,'count':1,'source':source,'reason':reason,
                'source_cut':iso(self._source_cut) if self._source_cut is not None else None})
            del self._fault_history[:-64]

    async def refresh(self):
        """Advance the battery owner; the coordinator publishes its status afterwards."""
        if self._closed or self._closing or self._lock.locked():
            return
        async with self._lock:
            try:
                with self.profiler.measure('refresh'):
                    await self._refresh()
                self._last_error=None
            except Exception as error:
                self._last_error=f'{type(error).__name__}: {error}'
                self._record_fault('refresh',self._last_error)
                self._status={'state':'fault','reason':self._last_error}
                if self.host and (self.host.state.groups[0].owned or self.host.state.groups[0].attempts):
                    try:
                        await self._release('The current battery plan is unavailable')
                    except Exception:
                        pass  # The existing journal/fence retains unresolved work.
                    self._status={'state':'fault','reason':self._last_error}
                if isinstance(error, (BatteryMeasurementConfigurationError, BatteryPowerReadingError)):
                    self._status.update(reason=str(error), fix=error.fix, next_step=error.next_step, retry_automatically=True)

    async def _record_counters(self, options):
        if self.host:
            # Actual energy is recorded even while plans are expired or the
            # planner is unreachable. Excluded source identities are respected.
            excluded=set(options.get('excluded_device_readings',[]))
            for stream in self.host.state.ledger.streams:
                if stream.spec.stream_id in excluded or '$battery' in excluded:
                    continue
                row=self.coordinator._battery_entity_report(stream.spec.stream_id)
                if row and row.get('state') not in ('unknown','unavailable',None):
                    await self._meter(stream.spec.stream_id,row['state'],row['attributes'],
                        stamp(row['last_reported']),row.get('event_id'))

    async def _retire_changed_configuration(self, options):
        """Retain the account, but finish old command ownership before rebinding."""
        if not self.host or self._options is None or options==self._options:
            return True
        self._releasing=True
        await self._record_counters(options)
        await self._release('Rebinding battery control to the current settings')
        old=self.host.state.groups[0]
        if old.owned or old.attempts or old.release_pending:
            return False
        await self.host.close()
        session=self.host.state.execution
        self._bootstrap=session.account
        self._bootstrap_rejection=session.plan_rejection
        self._bootstrap_captured=session.captured_feedback
        self.host=None;self._seeded=False;self._mode=None;self._model=None
        self._identity=None;self._last_capture=None;self._observation_error=None
        await self._persist_bootstrap()
        return True

    async def _refresh(self):
        options=self.controller.options()
        if not await self._retire_changed_configuration(options):
            return
        await self._record_counters(options)
        mode=device_mode(options,'battery')
        plan,slot=self.coordinator.binding_plan_for('battery',options)
        override=any(options.get(k) and (self.coordinator._battery_entity_report(options[k]) or {}).get('state')!='off'
                     for k in ('control_override_entity','battery_control_override_entity'))
        if mode not in ('controlling','control_verification') or not slot or override or not options.get('battery_enabled',True) or '$battery' in options.get('excluded_device_readings',[]):
            self.coordinator._battery_native_context=None
            await self._release('No current battery schedule is available' if not slot and mode in ('controlling','control_verification') and not override else 'Battery is inactive or overridden')
            return
        self._releasing=False
        self._options=options
        if mode=='control_verification' and 'battery' in self.controller.records:
            async with self.controller.lock:
                await self.controller.restore('battery')
        if battery_measurement_errors(options):
            raise BatteryMeasurementConfigurationError(options)
        self._devices=await self.coordinator.async_battery_planned_devices()
        source=source_revision(options,self._devices)
        read=self.coordinator._battery_entity_report
        reports={e:read(e) for e in self._control_entities()}
        self._surface=native_surface(options,reports)
        surface=self._surface
        if any(surface['limits'][k]['quantum_w']!=1 or surface['limits'][k]['minimum_w']!=0 for k in ('charge','discharge')):
            raise ValueError('Sigen ESS runtime requires zero-based one-watt control resolution')
        if not set(MODES.values())<=set(surface['mode_options']):
            raise ValueError('Sigen PV First modes are not configured on this control entity')
        ratings=resolve_battery_quantities(options,read)
        self._ratings=ratings
        if self._model is None or source!=self._model_sources or self.now()-self._model_at>=3600000:
            series=await self.coordinator.async_battery_loss_statistics(options)
            windows=windows_from_statistics(series,source_revision=source,unit_scale=1,end_ms=self.now())
            self._model,self._fits=conversion_model(windows,charge_efficiency=options['battery_charge_efficiency'],
                discharge_efficiency=options['battery_discharge_efficiency'],source_revision=source)
            self._model_sources=source;self._model_at=self.now()
        # Statistics and membership reads may cross a quarter or plan replacement.
        # Select the current captured slot after those awaits, preserving its timestamp.
        plan,slot=self.coordinator.binding_plan_for('battery',options)
        if not slot or self.controller.options()!=options:
            self.coordinator._battery_native_context=None
            raise ValueError('Battery plan changed while readings were collected')
        contract_wire=plan.get('battery_execution')
        if contract_wire is None:
            left_out=next((issue for issue in plan.get('measurement_issues') or []
                           if isinstance(issue,dict) and issue.get('device')=='battery'),None)
            if left_out:
                # The plan left the battery out for its readings. A replan cannot
                # include it until they are real; the server recommends one then.
                await self._release(f"The current plan leaves the battery out: {left_out.get('reason')}")
                return
            await self._release('Waiting for the planner to provide battery execution instructions')
            self._request_replan('execution_contract_required')
            return
        contract=execution.read_contract(contract_wire)
        scope=SupplyScope.read(plan['battery_supply_scope'])
        capacity=ratings['battery_capacity_kwh']
        cc=min(ratings['battery_charge_max_w'],surface['limits']['charge']['maximum_w'])
        dc=min(ratings['battery_discharge_max_w'],surface['limits']['discharge']['maximum_w'])
        if (cc,dc)!=(ratings['battery_charge_max_w'],ratings['battery_discharge_max_w']):
            raise ValueError('native register range is below configured battery rating')
        cc,dc=floor(cc),floor(dc)
        operations=[BatteryOperation('idle','idle',0,0),BatteryOperation('hold','hold',cc,0),
            BatteryOperation('solar','solar_charge',cc,0),BatteryOperation('supply','supply_house',cc,dc),
            BatteryOperation('charge','grid_charge',cc,0),BatteryOperation('export','export',0,dc)]
        config=digest(options)
        if self._mode!=mode:
            self._mode_revision+=1
            self._mode=mode
        catalog_revision=digest({'mode_revision':self._mode_revision,'surface':surface['revision'],
            'conversion':self._model.revision,'operations':[asdict(o) for o in operations]})
        identity=ContextIdentity('battery',contract.plan_id,str(capacity),config,config,
            contract.model_revision,'pv-first-dc-v2',catalog_revision)
        permissions=Permissions(True,True,True,
            contract.export_reserve_mwh/1e6,True,0,contract.model_revision)
        plant=BatteryPlant(ratings['battery_min_soc']*capacity,capacity,cc,dc,
            options['battery_charge_efficiency'],options['battery_discharge_efficiency'],
            plan['grid']['import_limit_w'],plan['grid']['export_limit_w'],
            'discharged_storage',0,self._model)
        cut=contract.intervals[0].start_ms
        keys=self._control_entities()
        bindings=tuple(rt.OperationBinding(o,tuple(zip(keys,(MODES[o.operation],o.charge_limit_w,o.discharge_limit_w))),
                          'pv-first-dc-v2','sigen-modbus-v2.9-ess-pv-first',()) for o in operations)
        catalog=rt.NativeCatalog(catalog_revision,ADAPTER_REVISION,surface['revision'],*keys,tuple(surface['mode_options']),1,cc,dc,bindings)
        self.adapter=SigenAdapter(catalog,self._model)
        self._scope=scope
        self._source_cut=cut
        specs=self._meter_specs(options)
        if self.host is None:
            maximum=rt.Envelope(1000000,1000000)
            state=rt.create_home((rt.GroupSpec('battery',ADAPTER_REVISION,keys,maximum),),RUNTIME_LIMITS,
                    ledger=create_ledger('household-battery-actuals',digest([asdict(s) for s in specs]),specs,max_intervals=256))
            state=replace(state,execution=rt.ExecutionSession(account=self._bootstrap,
                plan_rejection=self._bootstrap_rejection,captured_feedback=self._bootstrap_captured))
            await self._open_host(state)
            if self.host.state.groups[0].spec.control_keys!=keys or self.host.state.ledger.mapping_revision!=state.ledger.mapping_revision:
                raise ValueError('saved battery journal belongs to different control or meter bindings')
        group=self.host.state.groups[0]
        participants=[rt.ScopeParticipant(group.spec.id,mode,self._mode_revision,'new_runtime',keys)]
        for key,entity,participant_mode in self._demand_sources():
            if not entity:
                raise ValueError(f'{key}: Planned power sensor is not configured')
            participants.append(rt.ScopeParticipant(key,participant_mode,1,'external',(entity,)))
        scope_revision=digest({'configuration':config,'participants':[asdict(p) for p in participants]})
        identity=replace(identity,scope_revision=scope_revision)
        authority=rt.ExecutionAuthority(config,identity,permissions,plant,
            rt.ExecutionScope(scope_revision,group.spec.id,tuple(participants)),catalog,OWNER,scope)
        if authority!=self.host.state.authority:
            await self.host.accept(rt.AuthorityInstalled(authority,self.host.state.authority_revision+1))
        # Meter receipts persist the restored account. Install its physical
        # authority first so every checkpoint already names the battery owner.
        if not self._seeded:
            await self._seed_meters(cut,specs)
            self._seeded=True
        release=self._release_request(group,tuple(zip(keys,('Maximum Self Consumption',cc,dc))))
        await self.host.accept(rt.AuthorityChanged(group.spec.id,mode,self._mode_revision,release))
        self._identity=rt.WriterIdentity(OWNER,config,surface['revision'])
        # Admission/takeover is outside the controller lock. The fence acquires
        # it, drains old work and retains the fence through crashes.
        if mode=='controlling' and (not self.coordinator.battery_writer.is_current(self._grant,self.identity()) or self._grant.expires_at_ms-self.now()<60000):
            async def release_old():
                await self.controller.restore('battery')
                if 'battery' in self.controller.records:
                    raise ValueError('previous battery owner has not released its commands')
            self._grant=await self.coordinator.battery_writer.take_over(self._identity,self.now()+900000,release_old)
        if self._grant and self.coordinator.battery_writer.is_current(self._grant,self.identity()):
            await self.host.accept(rt.GrantConfirmed(group.spec.id,self._grant))
        for event in await self._observe(group.spec.id):
            await self.host.accept(event)
        current=self.host.state.execution.account.contract
        if current is None or current.id!=contract.id:
            await self.host.accept(rt.ExecutionPlanOffered(contract))
        await self.host.accept(rt.Tick())
        session=self.host.state.execution
        self._status=({'state':'controlling' if mode=='controlling' else 'verified',
            'reason':'Following the battery plan' if mode=='controlling' else 'Testing the plan; battery settings are not being changed'}
            if session.status in ('active','diagnostic_only') else
            {'state':'limited','reason':'Waiting for a current battery plan and measurements'})
        if self.host._fault:
            raise ValueError('battery command journal is unavailable')

    def _demand_sources(self):
        # Whole-house permission and grid headroom use the gross meter, which
        # already includes every appliance (Planned and Monitored alike).
        if self._scope.kind=='whole_house':
            return (('household-load',self._options['house_consumption_power_entity'],'monitoring'),)
        return tuple(('load:'+key,entity,device_mode(self._options,'device:'+key))
                     for key,entity in planned_power_bindings(self._options,self._devices).items())

    def _meter_specs(self,options):
        specs=[]
        for category,direction,boundary in (('grid_import','import','grid_ac'),('grid_export','export','grid_ac'),
                ('battery_charge','charge','battery_dc'),('battery_discharge','discharge','battery_dc')):
            entities=options.get('entities_'+category,[])
            if not entities:
                raise ValueError(f'{category}: cumulative energy sensor required')
            for entity in entities:
                specs.append(MeterSpec(entity,entity,boundary,direction,None))
        return tuple(specs)

    async def _seed_meters(self,cut,specs):
        start=datetime.fromtimestamp((cut-300000)/1000,timezone.utc)
        history=await self.coordinator._state_history([s.stream_id for s in specs],start,datetime.fromtimestamp(self.now()/1000,timezone.utc),with_attributes=True)
        for spec in specs:
            for at,value,attributes in history.get(spec.stream_id,[]):
                await self._meter(spec.stream_id,value,attributes,round(at.timestamp()*1000))
        # Missing anchors remain uncertain in the execution account.

    async def _meter(self,entity,value,attrs,at,event_id=None):
        stream=next(s for s in self.host.state.ledger.streams if s.spec.stream_id==entity)
        unit=(attrs or {}).get('unit_of_measurement')
        if unit not in ('Wh','kWh','MWh') or (attrs or {}).get('state_class') not in ('total','total_increasing'):
            raise ValueError(f'{entity}: cumulative energy reading required')
        value=float(value)*{'Wh':1000,'kWh':1000000,'MWh':1000000000}[unit]
        if not isfinite(value) or value<0:
            raise ValueError(f'{entity}: invalid cumulative energy reading')
        total=round(value)
        # Same-time changes are corrections; older source time is valid evidence.
        # A forward-time counter decrease marks a physical reset, not negative use.
        same,last=self.host.state.execution.account.meter_index.neighbours(entity,at)
        epoch=(int(last.epoch)+int(at>last.source_at_ms and total<last.total_mwh)) if last else 0
        if same and same.total_mwh==total:
            return
        event_id=digest({'entity':entity,'at':at,'total':total,'source_event':event_id})
        sample=execution.MeterReceipt(event_id,entity,stream.spec.direction,stream.spec.boundary_id,
            same.epoch if same else str(epoch),at,total,0,entity)
        await self.host.accept(rt.CounterReceived(sample))

    async def _observe(self,group_id):
        async with self._observe_lock:
            try:
                events=await self._observe_batch(group_id)
            except Exception as error:
                self._observation_error=f'{type(error).__name__}: {error}'
                self._record_fault('observation',self._observation_error)
                raise
            self._observation_error=None
            return events

    async def _observe_batch(self,group_id):
        if self._closed or self.host is None or self.host.state.authority is None:
            return ()
        options=self.controller.options()
        if options!=self._options and not self._releasing:
            raise ValueError('battery configuration changed')
        options=self._options
        read=self.coordinator._battery_entity_report
        now=self.now()
        entities=set(self._control_entities())|{entity for _,entity,_ in self._demand_sources()}|{options.get(k) for k in
            ('house_consumption_power_entity','solar_production_power_entity','battery_power_measurement_entity','battery_soc_entity','grid_power_entity')}
        if None in entities:
            raise ValueError('house, solar, signed grid/battery, SOC and Planned power bindings are required')
        reports={entity:read(entity) for entity in entities}
        capture=digest({'reports':reports,'authority':asdict(self.host.state.authority.identity),'pending':self._external_pending})
        if capture==self._last_capture:
            return ()
        surface=native_surface(options,reports)
        if surface['revision']!=self._surface['revision']:
            raise ValueError('native control metadata changed')
        source=source_revision(options,self._devices)
        try:
            accounting,evidence=observe_supply(self._scope,options,self._devices,reports.get,at_ms=now,max_age_ms=AGE_MS,max_alignment_ms=ALIGNMENT_MS,
                boundary='configured_household_reported_power',membership_revision=source,expected_membership_revision=self._model_sources)
        except ValueError as error:
            raise BatteryPowerReadingError(str(error)) from error
        battery,bat_at=power(reports[options['battery_power_measurement_entity']],source=options['battery_power_measurement_entity'],now_ms=now,signed=True)
        grid,grid_at=power(reports[options['grid_power_entity']],source=options['grid_power_entity'],now_ms=now,signed=True)
        soc=reports[options['battery_soc_entity']];soc_at=stamp(soc['last_reported'])
        fraction=float(soc['state'])/100
        if soc['attributes'].get('unit_of_measurement')!='%' or not isfinite(fraction) or not 0<=fraction<=1 or not soc_at<=now<soc_at+AGE_MS:
            raise ValueError('fresh percentage battery SOC required')
        readback=surface['readback']
        # Setting values remain current until HA reports a change or makes the
        # entity unavailable. Only physical measurements have a freshness age.
        external=[];times=[bat_at,grid_at,soc_at,*(r.at_ms for r in evidence)]
        for key,entity,_ in self._demand_sources():
            value,at=power(reports[entity],source=entity,now_ms=now);times.append(at)
            external.append(rt.ExternalDemand(key,rt.Envelope(value,0),rt.Envelope(0,0)))
        if max(times)-min(times)>ALIGNMENT_MS:
            raise ValueError('battery source reports are not aligned')
        if sum(e.observed.import_w for e in external)>accounting.house_w+1e-6:
            raise ValueError('Planned device meters exceed gross house consumption')
        controls=tuple(zip(self._control_entities(),(readback['mode'],readback['charge_limit_w'],readback['discharge_limit_w'])))
        native=self.adapter.envelope(controls)
        # HA settings can be optimistic. Preserve actual measured battery flow
        # in the physical reservation until its response catches up.
        envelope=rt.Envelope(max(native.import_w,self._model.grid_charge.input(max(0,battery))),
                            max(native.export_w,self._model.discharge.output(max(0,-battery))))
        valid=min(times)+AGE_MS
        for stream in self.host.state.ledger.streams:
            row=read(stream.spec.stream_id)
            if row and row.get('state') not in ('unknown','unavailable',None):
                await self._meter(stream.spec.stream_id,row['state'],row['attributes'],stamp(row['last_reported']),row.get('event_id'))
        state=self.host.state
        # Reserve a unique local revision while holding _observe_lock. Captures
        # may queue before either reaches the reducer, and persisted watermarks
        # can exceed this adapter's counter after a restart.
        self._capture_revision=max(self._capture_revision, state.conditions_revision,
            state.groups[0].observation_revision, state.frame.revision if state.frame else 0)+1
        revision=self._capture_revision
        authority=state.authority
        observed=rt.Observed(group_id,rt.Observation(revision,min(times),valid,controls,(),envelope))
        # Gross nonbattery load is a conservative import frame. PV is included
        # separately as possible export, and never erased by a planned device.
        self._external_pending={key:until for key,until in self._external_pending.items() if min(times)<until}
        frame=rt.FrameObserved(rt.Frame(revision,min(times),valid,
            rt.Envelope(max(accounting.house_w,authority.plant.import_limit_w if self._external_pending else 0), max(0,accounting.pv_w-accounting.house_w)),
            rt.Envelope(authority.plant.import_limit_w,authority.plant.export_limit_w),tuple(external)))
        self._frame_pending={frame.frame.revision:digest(self._external_pending)}
        energy=fraction*self._ratings['battery_capacity_kwh']
        conditions=rt.ConditionsObserved(ExecutionConditions(revision,min(times),valid,energy,
            accounting.pv_w,accounting.house_w,max(0,grid),authority.identity,authority.permissions,accounting.eligible_gross_w,soc_at))
        response=('charging' if battery>100 else 'discharging' if battery < -100 else 'idle')
        requested=('charging' if readback['mode']=='Command Charging (PV First)' and readback['charge_limit_w']>100
                   else 'discharging' if readback['mode']=='Command Discharging (PV First)' and readback['discharge_limit_w']>100 else None)
        self._measurements={'physical_response':response,'requested_direction':requested,
            'response_matches_direction':response==requested if requested else None,**asdict(accounting),'battery_dc_w':battery,'grid_w':grid,'at_ms':min(times),'valid_until_ms':valid}
        self._last_capture=capture
        return (rt.MeasurementsObserved(observed,frame.frame,conditions.conditions),)

    def _release_request(self,group,target=None):
        if self.host:
            group=next(g for g in self.host.state.groups if g.spec.id==group.spec.id)
        previous=group.release
        target=target if target is not None else previous.target
        if previous and previous.target==target and not previous.native_guards and previous.valid_until_ms>self.now()+600000:
            return previous
        self._release_revision=max(self._release_revision,previous.revision if previous else 0)+1
        return rt.Request('battery-release',self._release_revision,self.now()+86400000,
            target,())

    async def _confirm(self,group_id):
        events=[]
        group=self.host.state.groups[0]
        if group.release and group.release_pending:
            release=self._release_request(group)
            if release!=group.release:
                events.append(rt.ReleaseApproved(group_id,release))
        if group.release_pending and self.identity() and not self.coordinator.battery_writer.is_current(self._grant,self.identity()):
            async def released():
                if 'battery' in self.controller.records:
                    raise ValueError('legacy owner still present during runtime release')
            self._grant=await self.coordinator.battery_writer.take_over(self.identity(),self.now()+900000,released)
        if self._grant and self.coordinator.battery_writer.is_current(self._grant,self.identity()):
            events.append(rt.GrantConfirmed(group_id,self._grant))
        return tuple(events)

    async def _transition(self,effect):
        return self.adapter.propose(effect)

    def _request_replan(self,reason):
        self._runtime_reason=reason
        self._replan_pending=True
        if self._replan_task is not None and not self._replan_task.done():
            return
        async def run():
            while self._replan_pending and not self._closed and not self._closing:
                self._replan_pending=False
                try:
                    await self.coordinator.async_optimisation_push(force_plan=True, replan_reason=f"Battery controller: {self._runtime_reason}. Consider a manual replan.")
                except Exception as error:
                    self._record_fault('replan',str(error))
                    return  # The existing coordinator retry/poll owns transport retry.
        self._replan_task=asyncio.create_task(run())

    async def _renew(self,state,reason):
        self._request_replan(reason)
        return ()

    def _can_send(self,effect):
        if self._closed or not self.coordinator.battery_writer.is_current(effect.grant,self.identity()):
            return False
        group=self.host.state.groups[0]
        releasing=group.release_pending and group.release and effect.request_id==group.release.id
        if releasing:
            return True
        if self._closing:
            return False
        frame=self.host.state.frame
        if frame is None or self._frame_pending.get(frame.revision)!=digest(self._external_pending):
            attempt=next((a for a in group.attempts if a.id==effect.attempt_id),None)
            if attempt is None or not attempt.step.relief_id:
                return False
        options=self.controller.options()
        if device_mode(options,'battery')!='controlling' or not options.get('battery_enabled',True) or '$battery' in options.get('excluded_device_readings',[]):
            return False
        for key in ('control_override_entity','battery_control_override_entity'):
            entity=options.get(key)
            if entity and (self.coordinator._battery_entity_report(entity) or {}).get('state')!='off':
                return False
        plan,slot=self.coordinator.binding_plan_for('battery',options)
        accepted=self.host.state.execution.account.contract
        wire=plan.get('battery_execution')
        return bool(slot and accepted and wire and wire['id']==accepted.id
                    and options==self._options and self.now()<accepted.valid_until_ms)


    async def _dispatch(self,effect):
        # Share the household command lock. Recheck all authority after waiting;
        # then start the HA call without another intervening await.
        async with self.controller.lock:
            if self.host._fault or not self._can_send(effect) or not rt.authorize_send(self.host.state,effect,self.now()):
                raise DispatchRejected('battery writer or request changed')
            entity,value=effect.key,effect.value
            if entity==self._options['battery_mode_entity']:
                call=self.controller.hass.services.async_call('select','select_option',{'entity_id':entity,'option':value},blocking=True)
            else:
                field='charge' if entity==self._options['battery_charge_limit_entity'] else 'discharge'
                limit=self._surface['limits'][field]
                call=self.controller.hass.services.async_call('number','set_value',{'entity_id':entity,'value':value/(1000 if limit['unit']=='kW' else 1)},blocking=True)
            await asyncio.wait_for(call,75)

    def before_external_command(self,device):
        """Reserve unknown pending demand before another adapter changes a load.

        The reservation is intentionally conservative until a house report after
        the response window. It never substitutes scheduled watts for actual load.
        Caller holds the shared household lock and retries after battery relief.
        """
        self._external_pending[device]=self.now()+30000
        if self.host is None:
            return True
        group=self.host.state.groups[0]
        if any(a.stage!='prepared' and a.step.possible.import_w>0 for a in group.attempts):
            return False
        report=self.coordinator._battery_entity_report(self._options['battery_power_measurement_entity'])
        try:
            watts,_=power(report,source=self._options['battery_power_measurement_entity'],now_ms=self.now(),signed=True)
            mode=self.coordinator._battery_entity_report(self._options['battery_mode_entity'])['state']
            limit=self.coordinator._battery_entity_report(self._options['battery_charge_limit_entity'])
            ceiling=float(limit['state'])*(1000 if limit['attributes']['unit_of_measurement']=='kW' else 1)
            return mode!='Command Charging (PV First)' or (ceiling==0 and watts<=100)
        except (ValueError,KeyError,TypeError):
            return False

    async def _release(self,reason):
        self._releasing=True
        self._status={'state':'idle','reason':reason}
        if self.host is None:
            if 'battery' in self.controller.records:
                async with self.controller.lock:
                    await self.controller.restore('battery')
            return
        group=self.host.state.groups[0]
        if group.owned or group.attempts or group.release_pending:
            if group.release:
                await self.host.accept(rt.ReleaseApproved(group.spec.id,self._release_request(group)))
        if group.mode!='monitoring':
            self._mode_revision=group.mode_revision+1
            self._mode='monitoring'
            await self.host.accept(rt.AuthorityChanged(group.spec.id,'monitoring',self._mode_revision,None))
        if group.owned or group.attempts or group.release_pending:
            if self.identity() and not self.coordinator.battery_writer.is_current(self._grant,self.identity()):
                async def already_released():
                    if 'battery' in self.controller.records:
                        raise ValueError('legacy owner still present during runtime release')
                self._grant=await self.coordinator.battery_writer.take_over(self.identity(),self.now()+900000,already_released)
            if self._grant:
                await self.host.accept(rt.GrantConfirmed(group.spec.id,self._grant))
            for event in await self._observe(group.spec.id):
                await self.host.accept(event)
        await self.host.accept(rt.Tick())

    async def close(self, *, release=False):
        if self._closed or self._closing:
            return
        self._closing=True
        await self.profiler.close()
        async with self._lock:
            await self._close(release=release)

    async def _close(self, *, release):
        if release and self.host:
            try:
                await self._release('Releasing battery control')
                async def finish():
                    while self.host.state.groups[0].owned or self.host.state.groups[0].attempts:
                        await asyncio.sleep(1)
                        await self._release('Releasing battery control')
                await asyncio.wait_for(finish(),360)
            except (Exception,asyncio.CancelledError) as error:
                self._status={'state':'fault','reason':f'Battery release remains journalled: {type(error).__name__}'}
        self._closed=True;self._identity=None
        if self._replan_task and self._replan_task is not asyncio.current_task():
            self._replan_task.cancel()
        if self.host:
            await self.host.close()
