"""Production battery owner under the household coordinator.

The cloud supplies economic policy. This owner supplies physical observations,
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
    from .battery_execution_policy import ExecutionConditions, BatteryOperation
    from .battery_live import native_surface, source_revision, planned_power_bindings
    from .battery_supply import SupplyScope, observe_supply
    from .configuration_values import resolve_battery_quantities
    from .energy_ledger import MeterSpec, CounterSample, create_ledger, mark_retained_actuals
    from .device_controls import battery_measurement_errors, BatteryMeasurementConfigurationError
    from .battery_policy_exchange import BatteryPolicyUnavailableError
    from .battery_execution_outlook import describe_outlook
    from .operating_modes import device_mode
else:
    import home_runtime as rt
    from home_host import HomeHost, HostPorts, DispatchRejected
    from battery_native_adapter import SigenAdapter
    from battery_conversion import conversion_model, windows_from_statistics
    from battery_execution_policy import ExecutionConditions, BatteryOperation
    from battery_live import native_surface, source_revision, planned_power_bindings
    from battery_supply import SupplyScope, observe_supply
    from configuration_values import resolve_battery_quantities
    from energy_ledger import MeterSpec, CounterSample, create_ledger, mark_retained_actuals
    from device_controls import battery_measurement_errors, BatteryMeasurementConfigurationError
    from battery_policy_exchange import BatteryPolicyUnavailableError
    from battery_execution_outlook import describe_outlook
    from operating_modes import device_mode

AGE_MS=30000
ALIGNMENT_MS=15000
ADAPTER_REVISION='sigen-ess-dc-v2'
OWNER='shs-household-battery'

class NativeReadbackPending(Exception):
    """The provider has not published its configured controls during startup."""

MODES={'hold':'Standby','grid_charge':'Command Charging (PV First)',
       'export':'Command Discharging (PV First)','supply_house':'Maximum Self Consumption',
       'solar_charge':'Maximum Self Consumption','self_consumption':'Maximum Self Consumption'}

def stamp(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp needs timezone')
    return round(parsed.timestamp()*1000)

def digest(value):
    return sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()

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

    Construction receives the existing coordinator/controller, and two stores.
    History/calibration reads are ports on the coordinator; tests use the same
    composition with an in-memory state table and real fake service boundary.
    """
    def __init__(self,coordinator,controller,store,now_ms):
        self.coordinator,self.controller,self.store,self.now=coordinator,controller,store,now_ms
        self.host=None;self.adapter=None;self._grant=None;self._identity=None
        self._lock=asyncio.Lock();self._observe_lock=asyncio.Lock();self._closed=False;self._closing=False
        self._options=None;self._devices=[];self._model=None;self._fits={};self._model_sources=None
        self._model_at=0;self._surface=None;self._source_cut=None;self._seeded=False
        self._mode_revision=0;self._mode=None;self._capture_revision=0;self._last_capture=None
        self._status={'state':'pending','reason':'Waiting for current battery policy'}
        self._last_error=None;self._external_pending={};self._frame_pending={};self._releasing=False
        self._native_checked=False
        self._fault_history=[]
        self._observation_error=None
        controller.battery_runtime=self

    async def open(self):
        value=await self.store.async_load()
        if value is not None:
            if set(value)!={'schema','checkpoint','options','devices','model_sources','ratings'} or value['schema']!='battery-runtime-v2':
                raise ValueError('invalid battery runtime journal')
            if __package__:
                from .home_runtime_checkpoint import decode_checkpoint
            else:
                from home_runtime_checkpoint import decode_checkpoint
            checkpoint=value['checkpoint'].encode()
            state=decode_checkpoint(checkpoint)
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
            self._releasing=True;self._seeded=True
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
                await self._release('Recovering previous battery commands')
            except Exception as error:
                self._status={'state':'fault','reason':f'Battery recovery pending: {error}'}
                self._record_fault('recovery',self._status['reason'])

    async def _open_host(self,state,checkpoint=None):
        ports=HostPorts(self._persist,self._dispatch,self._can_send,self._observe,self._confirm,self._transition,self._renew,self._report,self.now)
        self.host=HomeHost(state,ports)
        await self.host.start(checkpoint)

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

    def snapshot(self):
        value={**self._status,'loss_model':self._model.wire() if self._model else None,'loss_evidence':self._fits,
               'runtime_reason':getattr(self,'_runtime_reason',None),'measurements':getattr(self,'_measurements',None),'writer_current':self.coordinator.battery_writer.is_current(self._grant,self.identity()),
               'fault_history':[dict(row) for row in self._fault_history], 'fault_history_scope':'Last 64 distinct faults since integration load'}
        value['outlook'] = {'state': 'unavailable'}
        if self.host:
            group=self.host.state.groups[0];session=self.host.state.policy
            value.update(command_state=group.status,mode=group.mode,
                selected_operation=session.selected_id if session else None,
                policy_state=session.status if session else None,
                pending_writes=len(group.attempts),native_target=dict(group.desired.target) if group.desired else None)
            value['pending_commands']=[{'entity_id':a.step.key,'value':a.step.value,'stage':a.stage,
                'started_at':iso(a.prepared_at_ms),'confirmation_deadline':iso(a.confirmation_deadline_ms),
                'uncertain_until':iso(a.latest_effect_ms)} for a in group.attempts]
            if group.desired:
                target=dict(group.desired.target)
                value['requested_settings']={'mode':target[self._options['battery_mode_entity']],
                    'charge_limit_w':target[self._options['battery_charge_limit_entity']],
                    'discharge_limit_w':target[self._options['battery_discharge_limit_entity']]}
            if self._status['state']!='fault' and group.mode=='controlling':
                uncertain=next((a for a in group.attempts if a.stage=='ambiguous'),None)
                if self.host._fault:
                    value.update(state='fault',reason=f'Battery command journal failed: {self.host._fault}')
                elif self._observation_error:
                    value.update(state='fault',reason=self._observation_error)
                elif uncertain:
                    reason=next((f['reason'] for f in reversed(self._fault_history) if uncertain.step.key in f['reason']),None)
                    value.update(state='fault',reason=reason or f'{uncertain.step.key}: write of {uncertain.step.value} is unconfirmed; waiting for fresh physical register readings')
                elif isinstance(group.transition_work,rt.TransitionFailure):
                    value.update(state='fault',reason=group.transition_work.reason)
                elif session and session.status=='active' and group.status!='adopted':
                    reason={
                        'reconciling':'Waiting for physical confirmation of battery settings',
                        'native_guard_blocked':'Waiting for measured battery power to settle within the current limits',
                        'awaiting_observation':'Waiting for fresh battery and household measurements',
                        'awaiting_grant':'Waiting for exclusive battery write permission',
                        'physical_scope_blocked':'Battery command is blocked by the measured household power limits',
                    }.get(group.status,'Applying battery settings; physical confirmation is pending')
                    value.update(state='pending',reason=reason)
                elif session and session.status=='active' and group.status=='adopted':
                    value.update(state='controlling',reason='Battery settings confirmed; live policy active',runtime_reason=None)
                elif session and session.status not in ('active','diagnostic_only'):
                    value.update(state='limited',reason='Battery policy is waiting for an executable choice: '+str(getattr(self,'_runtime_reason',None) or session.status))
            if value['state']=='fault' and 'fix' not in value:
                value.update(fix={'kind':'diagnostics'},next_step='Check the reported source or command failure. Download diagnostics if it persists.',retry_automatically=True)
            if session and session.decision:
                value['alternatives']=[{'operation':r.operation.id,'delta_sek':r.total_delta_sek,'energy_end_kwh':r.energy_end_kwh} for r in session.decision.ranked]
            if value['state'] not in ('fault', 'limited'):
                value['outlook'] = describe_outlook(self.coordinator.battery_policy_exchange.outlook, self.host.state, self.now())
        return value

    def _control_entities(self):
        return tuple(self._options[k] for k in ('battery_mode_entity','battery_charge_limit_entity','battery_discharge_limit_entity'))

    async def _persist(self,data):
        await self.store.async_save({'schema':'battery-runtime-v2','checkpoint':data.decode(),
            'options':self._options,'devices':self._devices,'model_sources':self._model_sources,'ratings':self._ratings})

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
        if self._closed or self._closing or self._lock.locked():
            return
        async with self._lock:
            try:
                await self._refresh()
                self._last_error=None
            except Exception as error:
                self._last_error=f'{type(error).__name__}: {error}'
                self._record_fault('refresh',self._last_error)
                self._status={'state':'fault','reason':self._last_error}
                if self.host and (self.host.state.groups[0].owned or self.host.state.groups[0].attempts):
                    try:
                        await self._release('Current battery policy is unavailable')
                    except Exception:
                        pass  # The existing journal/fence retains unresolved work.
                    self._status={'state':'fault','reason':self._last_error}
                if isinstance(error, (BatteryMeasurementConfigurationError, BatteryPolicyUnavailableError, BatteryPowerReadingError)):
                    self._status.update(reason=str(error), fix=error.fix, next_step=error.next_step, retry_automatically=True)
            self.coordinator.async_update_listeners()

    async def _refresh(self):
        options=self.controller.options()
        mode=device_mode(options,'battery')
        plan,slot=self.coordinator.binding_plan_for('battery',options)
        override=any(options.get(k) and (self.coordinator._battery_entity_report(options[k]) or {}).get('state')!='off'
                     for k in ('control_override_entity','battery_control_override_entity'))
        if mode not in ('controlling','control_verification') or not slot or override or not options.get('battery_enabled',True) or '$battery' in options.get('excluded_device_readings',[]):
            self.coordinator._battery_native_context=None
            await self._release('Battery is inactive, overridden or has no current binding plan')
            return
        if self._options is not None and options!=self._options and self.host:
            old=self.host.state.groups[0]
            if old.owned or old.attempts or old.release_pending:
                raise ValueError('releasing previous battery configuration before admitting new bindings')
            await self.host.close()
            self.host=None;self._seeded=False;self._mode=None;self._model=None;self._native_checked=False
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
            raise BatteryPolicyUnavailableError({'reasons':['local_context_changed']})
        all_slots=plan['plans']['priority']['slots']
        slots=all_slots[all_slots.index(slot):]
        cut=exact_start(slot);until=min(stamp(slot['start'])+900000,stamp(plan['valid_until']),stamp(plan['binding_until']))
        if not cut<=self.now()<until:
            self.coordinator._battery_native_context=None
            raise BatteryPolicyUnavailableError({'reasons':['plan_window_unavailable']})
        scope=SupplyScope.read(plan['battery_supply_scope'])
        capacity=(1-ratings['battery_min_soc'])*ratings['battery_capacity_kwh']
        cc=min(ratings['battery_charge_max_w'],surface['limits']['charge']['maximum_w'])
        dc=min(ratings['battery_discharge_max_w'],surface['limits']['discharge']['maximum_w'])
        if (cc,dc)!=(ratings['battery_charge_max_w'],ratings['battery_discharge_max_w']):
            raise ValueError('native register range is below configured battery rating')
        cc,dc=floor(cc),floor(dc)
        operations=[BatteryOperation('hold','hold',0,0),BatteryOperation('solar','solar_charge',cc,0),
            BatteryOperation('supply','supply_house',0,dc),BatteryOperation('charge','grid_charge',cc,0)]
        if options.get('battery_export_enabled'):
            operations.append(BatteryOperation('export','export',0,dc))
        config=digest(options)
        if self._mode!=mode:
            self._mode_revision+=1
            self._mode=mode
        catalog_revision=digest({'mode_revision':self._mode_revision,'surface':surface['revision'],'conversion':self._model.revision,'operations':[asdict(o) for o in operations]})
        scope_wire={'kind':scope.kind,**({'include_base':scope.include_base,'planned_device_keys':list(scope.planned_device_keys)} if scope.kind=='selected' else {})}
        context={'config_revision':config,'catalog_revision':catalog_revision,'evidence_id':'sigen-modbus-v2.9-ess-pv-first',
            'response_model_revision':'pv-first-dc-v2','conversion':self._model.wire(),
            'energy_basis':'usable_kwh_above_min_soc','source_cut_ms':cut,'valid_until_ms':until,'supply_scope':scope_wire,
            'operations':[asdict(o) for o in operations],'reference_id':'hold',
            'domain':{'energy_kwh':[0,capacity],'pv_w':[0,1000000],'residual_load_w':[0,1000000]},
            'future_permissions':[{'start':iso(exact_start(s)),'available':True,'grid_charge_allowed':True,
                                  'battery_export_allowed':bool(options.get('battery_export_enabled'))} for s in slots]}
        self.coordinator._battery_native_context=context
        exchange=self.coordinator.battery_policy_exchange
        await exchange.refresh()
        policy=exchange.policy
        if policy is None:
            raise BatteryPolicyUnavailableError(exchange.snapshot())
        summary=policy.summary
        current_plan,current_slot=self.coordinator.binding_plan_for('battery',options)
        if (self.controller.options()!=options or not current_slot
                or any(current_plan[key]!=plan[key] for key in ('plan_id','snapshot_id'))
                or exact_start(current_slot)!=cut or not cut<=self.now()<until
                or summary.identity.context.intent_revision!=plan['snapshot_id']
                or summary.actuals_origin_ms!=cut or summary.from_ms!=cut
                or not self.now()<summary.until_ms<=until):
            self.coordinator._battery_native_context=None
            raise BatteryPolicyUnavailableError({'reasons':['local_context_changed']})
        if summary.plant.conversion!=self._model or summary.supply_scope!=scope or summary.identity.context.catalog_revision!=catalog_revision:
            raise ValueError('delivered policy does not match current native model')
        if abs(exchange.energy_origin_kwh-ratings['battery_min_soc']*ratings['battery_capacity_kwh'])>1e-6:
            raise ValueError('policy uses a different battery energy origin')
        if (summary.plant.charge_max_w,summary.plant.discharge_max_w)!=(cc,dc):
            raise ValueError('policy uses different battery ratings')
        keys=self._control_entities()
        bindings=tuple(rt.OperationBinding(o,tuple(zip(keys,(MODES[o.operation],o.charge_limit_w,o.discharge_limit_w))),
                          'pv-first-dc-v2','sigen-modbus-v2.9-ess-pv-first',(rt.Guard('ready',1,1),)) for o in summary.operations)
        catalog=rt.NativeCatalog(catalog_revision,ADAPTER_REVISION,surface['revision'],*keys,tuple(surface['mode_options']),1,cc,dc,bindings)
        self.adapter=SigenAdapter(catalog,self._model)
        self._scope=scope
        self._source_cut=cut
        specs=self._meter_specs(options)
        if self.host is None:
            maximum=rt.Envelope(1000000,1000000)
            state=rt.create_home((rt.GroupSpec(summary.identity.context.battery_id,ADAPTER_REVISION,keys,maximum),),rt.Limits(1000,1000,30000),
                    ledger=create_ledger('household-battery-actuals',digest([asdict(s) for s in specs]),specs,max_intervals=256))
            await self._open_host(state)
            if self.host.state.groups[0].spec.control_keys!=keys or self.host.state.ledger.mapping_revision!=state.ledger.mapping_revision:
                raise ValueError('saved battery journal belongs to different control or meter bindings')
        if not self._seeded:
            await self._seed_meters(cut,specs)
            self._seeded=True
        group=self.host.state.groups[0]
        participants=[rt.ScopeParticipant(group.spec.id,mode,self._mode_revision,'new_runtime',keys)]
        for key,entity,participant_mode in self._demand_sources():
            if not entity:
                raise ValueError(f'{key}: Planned power sensor is not configured')
            participants.append(rt.ScopeParticipant(key,participant_mode,1,'external',(entity,)))
        authority=rt.ExecutionAuthority(config,summary.identity.context,summary.permissions,summary.plant,
            rt.ExecutionScope(summary.identity.context.scope_revision,group.spec.id,tuple(participants)),catalog,OWNER,scope)
        if authority!=self.host.state.authority:
            await self.host.accept(rt.AuthorityInstalled(authority,self.host.state.authority_revision+1))
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
        current=self.host.state.policy
        if current is None or current.compiled.summary.identity!=summary.identity:
            watermark=mark_retained_actuals(self.host.state.ledger,cut,self.now())
            await self.host.accept(rt.PolicyOffered(policy,watermark))
        await self.host.accept(rt.Tick())
        session=self.host.state.policy
        self._status=({'state':'controlling' if mode=='controlling' else 'verified','reason':'Live battery policy connected'}
            if session and session.status in ('active','diagnostic_only') else
            {'state':'limited','reason':'Battery policy is waiting for an executable choice: '+getattr(self,'_runtime_reason','current evidence')})
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
        # A missing anchor is an explicit fault; do not manufacture one from SOC.
        mark_retained_actuals(self.host.state.ledger,cut,self.now())

    async def _meter(self,entity,value,attrs,at):
        stream=next(s for s in self.host.state.ledger.streams if s.spec.stream_id==entity)
        if stream.samples and at<=stream.samples[-1].at_ms:
            return
        unit=(attrs or {}).get('unit_of_measurement')
        if unit not in ('Wh','kWh','MWh') or (attrs or {}).get('state_class') not in ('total','total_increasing'):
            raise ValueError(f'{entity}: cumulative energy reading required')
        value=float(value)*{'Wh':1000,'kWh':1000000,'MWh':1000000000}[unit]
        if not isfinite(value) or value<0:
            raise ValueError(f'{entity}: invalid cumulative energy reading')
        total=round(value);last=stream.samples[-1] if stream.samples else None
        reset=last is not None and total<last.total_mwh
        sample=CounterSample(entity,entity,(last.epoch+int(reset)) if last else 0,(last.revision+1) if last else 0,at,total,
                             'counter_reset' if reset else 'configured_counter' if last is None else None)
        await self.host.accept(rt.MeterObserved(sample))

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
        controls=[read(e) for e in self._control_entities()]
        if not self._native_checked or any(not r or self.now()-stamp(r['last_reported'])>=10000 for r in controls):
            await self.coordinator.async_battery_native_readback(self._control_entities())
            self._native_checked=True
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
        readback=surface['readback'];native_at=min(readback[k] for k in ('mode_reported_at_ms','charge_reported_at_ms','discharge_reported_at_ms'))
        if not native_at<=now<native_at+AGE_MS:
            raise ValueError('native register report is stale')
        external=[];times=[bat_at,grid_at,soc_at,native_at,*(r.at_ms for r in evidence)]
        for key,entity,_ in self._demand_sources():
            value,at=power(reports[entity],source=entity,now_ms=now);times.append(at)
            external.append(rt.ExternalDemand(key,rt.Envelope(value,0),rt.Envelope(0,0)))
        if max(times)-min(times)>ALIGNMENT_MS:
            raise ValueError('battery source reports are not aligned')
        if sum(e.observed.import_w for e in external)>accounting.house_w+1e-6:
            raise ValueError('Planned device meters exceed gross house consumption')
        self._capture_revision+=1
        revision=self._capture_revision
        controls=tuple(zip(self._control_entities(),(readback['mode'],readback['charge_limit_w'],readback['discharge_limit_w'])))
        envelope=self.adapter.envelope(controls)
        # The native ceiling must bound actual battery power. Settings readback
        # confirms register assignment; physical response is reported separately.
        ready=int(-readback['discharge_limit_w']-100<=battery<=readback['charge_limit_w']+100)
        valid=min(times)+AGE_MS
        for stream in self.host.state.ledger.streams:
            row=read(stream.spec.stream_id)
            if row:
                await self._meter(stream.spec.stream_id,row['state'],row['attributes'],stamp(row['last_reported']))
        authority=self.host.state.authority
        observed=rt.Observed(group_id,rt.Observation(max(revision,self.host.state.groups[0].observation_revision+1),min(times),valid,controls,(('ready',ready),),envelope))
        # Gross nonbattery load is a conservative import frame. PV is included
        # separately as possible export, and never erased by a planned device.
        self._external_pending={key:until for key,until in self._external_pending.items() if min(times)<until}
        frame=rt.FrameObserved(rt.Frame(max(revision,(self.host.state.frame.revision+1) if self.host.state.frame else 1),min(times),valid,
            rt.Envelope(max(accounting.house_w,authority.plant.import_limit_w if self._external_pending else 0), max(0,accounting.pv_w-accounting.house_w)),
            rt.Envelope(authority.plant.import_limit_w,authority.plant.export_limit_w),tuple(external)))
        self._frame_pending={frame.frame.revision:digest(self._external_pending)}
        energy=(fraction-self._ratings['battery_min_soc'])*self._ratings['battery_capacity_kwh']
        energy=max(0,energy)
        conditions=rt.ConditionsObserved(ExecutionConditions(max(revision,self.host.state.conditions_revision+1),min(times),valid,energy,
            accounting.pv_w,accounting.house_w,max(0,grid),authority.identity,authority.permissions,accounting.eligible_gross_w))
        response=('charging' if battery>100 else 'discharging' if battery < -100 else 'idle')
        requested=('charging' if readback['mode']=='Command Charging (PV First)' and readback['charge_limit_w']>100
                   else 'discharging' if readback['mode']=='Command Discharging (PV First)' and readback['discharge_limit_w']>100 else None)
        self._measurements={'physical_response':response,'requested_direction':requested,
            'response_matches_direction':response==requested if requested else None,**asdict(accounting),'battery_dc_w':battery,'grid_w':grid,'at_ms':min(times),'valid_until_ms':valid}
        self._last_capture=capture
        return (rt.MeasurementsObserved(observed,frame.frame,conditions.conditions),)

    def _release_request(self,group,target=None):
        previous=group.release
        target=target if target is not None else previous.target
        if previous and previous.target==target and previous.valid_until_ms>self.now()+600000:
            return previous
        return rt.Request('battery-release',previous.revision+1 if previous else 1,self.now()+86400000,
            target,(rt.Guard('ready',1,1),))

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

    async def _renew(self,state,reason):
        # One periodic owner performs network exchanges and installs the result.
        self._runtime_reason=reason
        if reason!='refresh_due':
            self._record_fault('policy',reason)
        self.coordinator.battery_policy_exchange._next_ms=0
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
        return bool(slot and self.coordinator.battery_policy_exchange.policy and self.host.state.policy
                    and self.host.state.policy.compiled.summary.identity.context.intent_revision==plan['snapshot_id']
                    and self.host.state.policy.compiled.summary.from_ms==exact_start(slot)
                    and self.coordinator.battery_policy_exchange.policy.summary.identity==self.host.state.policy.compiled.summary.identity)

    async def _dispatch(self,effect):
        # Share the household command lock. Recheck all authority after waiting;
        # then start the HA call without another intervening await.
        async with self.controller.lock:
            if not self._can_send(effect) or not rt.authorize_send(self.host.state,effect,self.now()):
                raise DispatchRejected('battery writer or request changed')
            entity,value=effect.key,effect.value
            if entity==self._options['battery_mode_entity']:
                call=self.controller.hass.services.async_call('select','select_option',{'entity_id':entity,'option':value},blocking=True)
            else:
                field='charge' if entity==self._options['battery_charge_limit_entity'] else 'discharge'
                limit=self._surface['limits'][field]
                call=self.controller.hass.services.async_call('number','set_value',{'entity_id':entity,'value':value/(1000 if limit['unit']=='kW' else 1)},blocking=True)
            await asyncio.wait_for(call,75)
            # HA service completion alone is not physical confirmation. Explicitly
            # refresh the Sigen registers after the write has finished; optimistic
            # number/select state must never shorten the uncertainty window.
            after_write=self.now()
            await self.coordinator.async_battery_native_readback(self._control_entities())
            surface=native_surface(self._options,{e:self.coordinator._battery_entity_report(e) for e in self._control_entities()})
            readback=surface['readback']
            at=min(readback[k] for k in ('mode_reported_at_ms','charge_reported_at_ms','discharge_reported_at_ms'))
            controls=tuple(zip(self._control_entities(),(readback['mode'],readback['charge_limit_w'],readback['discharge_limit_w'])))
            group=self.host.state.groups[0]
            attempt=next((a for a in group.attempts if a.id==effect.attempt_id),None)
            if (attempt is None or surface['revision']!=self._surface['revision']
                    or not after_write<=at<=self.now()<at+AGE_MS or dict(controls)!=dict(attempt.step.after)):
                raise ValueError(f'{entity}: physical register readback did not confirm the completed write')
            # Confirm settings with physical register readback; separately check
            # the measured terminal response before permitting the next write.
            battery,bat_at=power(self.coordinator._battery_entity_report(self._options['battery_power_measurement_entity']),
                source=self._options['battery_power_measurement_entity'],now_ms=self.now(),signed=True)
            ready=int(-readback['discharge_limit_w']-100<=battery<=readback['charge_limit_w']+100)
            return rt.Observation(group.observation_revision+1,at,min(at,bat_at)+AGE_MS,controls,(('ready',ready),),self.adapter.envelope(controls))

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
        if self.host:
            await self.host.close()
