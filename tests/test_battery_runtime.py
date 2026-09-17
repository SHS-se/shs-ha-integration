"""Production composition, including real adapter and durable host, without HA."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
sys.path.insert(0,str(Path(__file__).parents[1]/'custom_components'/'shs_energy'))
from battery_runtime import BatteryRuntime, exact_start, iso, stamp
from battery_writer import BatteryWriterFence
from battery_execution_policy import read_execution_policy

class Store:
    def __init__(self):self.saved=None;self.writes=[]
    async def async_load(self):return deepcopy(self.saved)
    async def async_save(self,value):self.saved=deepcopy(value);self.writes.append(deepcopy(value))

class Rig:
    def __init__(self,mode='controlling'):
        self.now=60000;self.calls=[];self.options={'device_modes':{'$battery':mode},'battery_enabled':True,
          'battery_capacity_kwh':10,'battery_charge_max_w':4000,'battery_discharge_max_w':4000,'battery_min_soc':0,
          'battery_charge_efficiency':.95,'battery_discharge_efficiency':.95,'battery_export_enabled':False,
          'battery_mode_entity':'select.mode','battery_charge_limit_entity':'number.charge','battery_discharge_limit_entity':'number.discharge',
          'battery_power_measurement_entity':'sensor.battery','house_consumption_power_entity':'sensor.house','solar_production_power_entity':'sensor.pv',
          'battery_soc_entity':'sensor.soc','grid_power_entity':'sensor.grid','device_control_mappings':{}}
        self.rows={}
        def row(key,state,unit,**attrs):self.rows[key]={'state':str(state),'attributes':{'unit_of_measurement':unit,**attrs},'last_reported':iso(self.now)}
        for key,value in [('battery',0),('house',1000),('pv',0),('grid',1000)]:row('sensor.'+key,value,'W',state_class='measurement')
        row('sensor.soc',50,'%',state_class='measurement')
        row('select.mode','Standby',None,options=['Standby','Maximum Self Consumption','Command Charging (PV First)','Command Discharging (PV First)'])
        for key in ('charge','discharge'):row('number.'+key,0,'kW',min=0,max=4,step=.001)
        for key in ('grid_import','grid_export','battery_charge','battery_discharge'):
            entity='sensor.'+key;self.options['entities_'+key]=[entity];row(entity,100,'kWh',state_class='total_increasing')
        self.plan={'plan_id':'p','snapshot_id':'s','valid_until':iso(900000),'binding_until':iso(900000),'battery_supply_scope':{'kind':'whole_house'},
                   'plans':{'priority':{'slots':[{'start':iso(0),'duration_hours':(900000-10000)/3600000}]}}}
        self.store=Store();self.fence_store=Store()
        async def service(domain,name,data,blocking):
            self.calls.append((domain,name,data.copy()))
            assert self.store.saved is not None,'command preceded durable checkpoint'
            assert self.fence_store.saved['owner']=='runtime'
            self.rows[data['entity_id']]['state']=str(data.get('value',data.get('option')))
            self.rows[data['entity_id']]['last_reported']=iso(self.now)
        async def restore(device):self.controller.records.pop(device,None)
        self.controller=SimpleNamespace(options=lambda:deepcopy(self.options),lock=asyncio.Lock(),records={},
            restore=restore,hass=SimpleNamespace(services=SimpleNamespace(async_call=service)))
        self.readbacks=0
        async def readback(entities):
            self.readbacks+=1
            for e in entities:self.rows[e]['last_reported']=iso(self.now)
        async def devices():return []
        async def statistics(options):return {k:[] for k in ('battery','house','pv','grid')}
        async def history(entities,start,end,with_attributes):
            return {e:[(datetime.fromtimestamp(5,timezone.utc),'100',deepcopy(self.rows[e]['attributes']))] for e in entities}
        self.coordinator=SimpleNamespace(_battery_entity_report=lambda e:deepcopy(self.rows.get(e)),async_battery_planned_devices=devices,
            async_battery_native_readback=readback,async_battery_loss_statistics=statistics,_state_history=history,async_update_listeners=lambda:None,
            binding_plan_for=lambda device,options:(self.plan,next((s for s in self.plan['plans']['priority']['slots']
                if stamp(s['start'])<=self.now<min(stamp(s['start'])+900000,stamp(self.plan['valid_until']),stamp(self.plan['binding_until']))),None)),_battery_native_context=None)
        async def refresh():
            c=self.coordinator._battery_native_context
            if (self.exchange.policy and c==self.exchange.context
                    and self.exchange.policy.summary.identity.context.intent_revision==self.plan['snapshot_id']):return
            wire=json.loads((Path(__file__).parent/'fixtures'/'battery-execution-runtime-synthetic.json').read_text())
            wire['identity'].update(response_model_revision='pv-first-dc-v2',catalog_revision=c['catalog_revision'],scope_revision=c['catalog_revision'],revision=self.now,
                                    intent_revision=self.plan['snapshot_id'])
            wire['plant'].update(cutoff_kwh=0,conversion=c['conversion'])
            wire['domain']=c['domain'];wire['operations']=c['operations'];wire['reference_id']='hold';wire['supply_scope']=c['supply_scope']
            wire['actuals_origin_ms']=c['source_cut_ms']
            wire['validity']={'from_ms':c['source_cut_ms'],'refresh_after_ms':max(c['source_cut_ms'],c['valid_until_ms']-60000),
                              'until_ms':c['valid_until_ms'],'boundary_ms':(c['source_cut_ms']//900000+1)*900000}
            wire['permissions']['battery_export_allowed']=False
            self.exchange.policy=read_execution_policy(json.dumps(wire));self.exchange.context=deepcopy(c)
        self.exchange=SimpleNamespace(refresh=refresh,policy=None,context=None,energy_origin_kwh=0,snapshot=lambda:{'reasons':[]},_next_ms=0)
        self.coordinator.battery_policy_exchange=self.exchange
        self.runtime=BatteryRuntime(self.coordinator,self.controller,self.store,lambda:self.now)
        self.fence=BatteryWriterFence(self.fence_store,self.controller.lock,self.controller.options,lambda:self.now,self.runtime.identity)
        self.coordinator.battery_writer=self.fence
    async def start(self):
        await self.fence.open();await self.runtime.open();await self.runtime.refresh()
        if self.runtime.host:await asyncio.wait_for(self.runtime.host.idle(),2)
    async def advance(self,millis=20000):
        self.now+=millis
        for row in self.rows.values():row['last_reported']=iso(self.now)
        await self.runtime.refresh()
        if self.runtime.host:await asyncio.wait_for(self.runtime.host.idle(),2)

    def extend_plan(self):
        self.plan['valid_until']=self.plan['binding_until']=iso(2700000)
        self.plan['plans']['priority']['slots'].extend(
            {'start':iso(at),'duration_hours':.25} for at in (900000,1800000))

    def add_pool(self):
        self.options['device_control_mappings']['pool']={'power':'sensor.pool'}
        async def devices():return [{'key':'pool','planning_role':'controllable'}]
        self.coordinator.async_battery_planned_devices=devices
        self.rows['sensor.pool']={'state':'900','attributes':{'unit_of_measurement':'W','state_class':'measurement'},
                                 'last_reported':iso(0)}

class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_readback_does_not_make_house_reports_out_of_order(self):
        r=Rig();original=r.controller.hass.services.async_call
        async def delayed(domain,name,data,blocking):
            r.now+=200
            await original(domain,name,data,blocking)
        r.controller.hass.services.async_call=delayed
        try:
            await r.start()
            initial_calls=deepcopy(r.calls)
            self.assertTrue(initial_calls)
            for _ in range(3):
                # Native readback follows the write; household reports are still fresh.
                r.now+=1000
                await r.runtime.refresh();await r.runtime.host.idle()
                self.assertEqual(r.runtime.snapshot()['state'],'controlling',r.runtime.snapshot())
                self.assertEqual(r.rows['select.mode']['state'],'Command Charging (PV First)')
                self.assertEqual(r.calls,initial_calls)
                await r.advance(5000)
            self.assertEqual(r.calls,initial_calls)
            self.assertEqual(r.runtime.snapshot()['fault_history'],[])
        finally:await r.runtime.close()

    async def test_later_capture_accepts_updated_values_with_earlier_report_time(self):
        r=Rig('control_verification')
        try:
            await r.start();await r.advance(5000)
            previous=r.runtime.host.state.conditions
            r.rows['sensor.house'].update(state='1100',last_reported=iso(r.now-1000))
            await r.runtime.refresh();await r.runtime.host.idle()
            current=r.runtime.host.state.conditions
            self.assertEqual(r.runtime.snapshot()['state'],'verified',r.runtime.snapshot())
            self.assertGreater(current.revision,previous.revision)
            self.assertLess(current.at_ms,previous.at_ms)
            self.assertEqual(current.residual_load_w,1100)
            self.assertEqual(r.runtime.snapshot()['fault_history'],[])
        finally:await r.runtime.close()

    async def test_changing_house_load_is_one_capture_without_policy_withdrawal(self):
        r=Rig();await r.start();reasons=[]
        async def renew(state,reason):reasons.append(reason);return ()
        r.runtime.host.ports=replace(r.runtime.host.ports,renew=renew)
        try:
            for watts in (1010,980,1100,800,1250):
                r.rows['sensor.house']['state']=str(watts)
                await r.advance()
                state=r.runtime.host.state
                self.assertEqual(state.policy.status,'active')
                self.assertFalse(state.groups[0].release_pending)
                self.assertEqual(state.conditions.residual_load_w,watts)
                self.assertEqual(state.frame.external_demands[0].observed.import_w,watts)
            self.assertNotIn('external_demand_missing',reasons)
        finally:await r.runtime.close()

    async def test_invalid_capture_rejects_all_measurements_atomically(self):
        import home_runtime as rt
        r=Rig();await r.start()
        try:
            r.runtime._last_capture=None
            events=await r.runtime._observe_batch(r.runtime.host.state.groups[0].spec.id)
            capture=events[0]
            self.assertIsInstance(capture,rt.MeasurementsObserved)
            invalid=replace(capture,conditions=replace(capture.conditions,at_ms=capture.conditions.at_ms+1))
            before=r.runtime.host.state
            with self.assertRaisesRegex(ValueError,'capture timestamps'):
                await r.runtime.host.accept(invalid)
            self.assertEqual(r.runtime.host.state,before)
        finally:await r.runtime.close()

    async def test_confirmed_registers_finish_transition_without_timeout_waits(self):
        r=Rig();started=r.now;await r.start()
        try:
            for _ in range(3):await r.advance(5000)
            group=r.runtime.host.state.groups[0]
            self.assertEqual(group.status,'adopted')
            self.assertFalse(group.attempts)
            self.assertEqual(dict(group.observation.controls),dict(group.desired.target))
            self.assertLess(r.now-started,75000)
            self.assertGreater(r.readbacks,1)
        finally:await r.runtime.close()

    async def test_failed_physical_readback_keeps_the_write_uncertain(self):
        r=Rig();original=r.coordinator.async_battery_native_readback
        async def readback(entities):
            if r.calls:raise RuntimeError('Modbus read failed: inverter unavailable')
            await original(entities)
        r.coordinator.async_battery_native_readback=readback
        await r.start()
        try:
            await r.advance(5000)
            attempts=r.runtime.host.state.groups[0].attempts
            self.assertTrue(attempts)
            self.assertEqual(attempts[0].stage,'ambiguous')
            self.assertGreater(attempts[0].latest_effect_ms,r.now)
            status=r.runtime.snapshot()
            self.assertEqual(status['state'],'fault')
            self.assertIn('Modbus read failed: inverter unavailable',status['reason'])
            self.assertEqual(status['fix'],{'kind':'diagnostics'})
            self.assertTrue(any('Modbus read failed' in f['reason'] for f in status['fault_history']))
            r.coordinator.async_battery_native_readback=original
            for _ in range(6):await r.advance()
            status=r.runtime.snapshot()
            self.assertEqual(status['state'],'controlling',status)
            self.assertIsNone(status['runtime_reason'])
            self.assertTrue(any('Modbus read failed' in f['reason'] for f in status['fault_history']))
        finally:await r.runtime.close()

    async def test_settling_terminal_power_pauses_writes_without_withdrawing_policy(self):
        r=Rig();r.rows['sensor.battery']['state']='150'
        await r.start()
        try:
            self.assertEqual(r.runtime.host.state.policy.status,'active')
            self.assertEqual(r.runtime.snapshot()['state'],'pending')
            self.assertIn('settle',r.runtime.snapshot()['reason'])
            self.assertEqual(r.calls,[])
            r.rows['sensor.battery']['state']='0'
            for _ in range(3):await r.advance(5000)
            self.assertEqual(r.runtime.snapshot()['state'],'controlling')
            self.assertFalse(any(f['reason']=='physical_scope_uncovered' for f in r.runtime.snapshot()['fault_history']))
        finally:await r.runtime.close()

    async def test_one_plan_rolls_across_quarters_and_reconciles_meter_origins(self):
        for mode in ('controlling','control_verification'):
            with self.subTest(mode=mode):
                r=Rig(mode);r.extend_plan();await r.start()
                try:
                    for cut in (900000,1800000):
                        previous=r.runtime.host.state.policy.watermark
                        await r.advance(cut-r.now)
                        self.assertNotEqual(r.runtime.snapshot()['state'],'fault',r.runtime.snapshot())
                        session=r.runtime.host.state.policy
                        self.assertEqual(session.compiled.summary.from_ms,cut)
                        self.assertEqual(session.watermark.at_ms,cut)
                        self.assertEqual(session.reconciled_from,previous)
                        self.assertEqual(r.coordinator._battery_native_context['future_permissions'][0]['start'],iso(cut))
                        self.assertEqual(session.compiled.summary.identity.context.intent_revision,'s')
                        for _ in range(4):await r.advance()
                    self.assertEqual(r.runtime.snapshot()['state'],'controlling' if mode=='controlling' else 'verified',r.runtime.snapshot())
                    if mode=='control_verification':self.assertEqual(r.calls,[])
                    else:self.assertTrue(r.calls)
                finally:await r.runtime.close()

    async def test_late_policy_reply_is_diagnostic_and_next_quarter_clears_the_fault(self):
        r=Rig();r.extend_plan();original=r.exchange.refresh
        async def late():
            await original()
            r.now=960000
            for row in r.rows.values():row['last_reported']=iso(r.now)
        r.exchange.refresh=late
        await r.start()
        try:
            status=r.runtime.snapshot()
            self.assertEqual(status['state'],'fault')
            self.assertEqual(status['fix'],{'kind':'diagnostics'})
            self.assertIn('current quarter',status['reason'])
            self.assertEqual(r.calls,[])
            self.assertIsNone(r.runtime.host)
            r.exchange.refresh=original
            await r.advance()
            for _ in range(4):await r.advance()
            self.assertEqual(r.runtime.snapshot()['state'],'controlling',r.runtime.snapshot())
            self.assertNotIn('fix',r.runtime.snapshot())
            self.assertEqual(r.runtime.host.state.policy.watermark.at_ms,900000)
            self.assertTrue(r.calls)
        finally:await r.runtime.close()

    async def test_new_plan_during_exchange_cannot_admit_the_previous_policy(self):
        r=Rig();original=r.exchange.refresh
        async def replaced():
            await original()
            r.plan={**r.plan,'plan_id':'new-plan','snapshot_id':'new-snapshot'}
        r.exchange.refresh=replaced
        await r.start()
        try:
            self.assertEqual(r.calls,[])
            self.assertEqual(r.runtime.snapshot()['fix'],{'kind':'diagnostics'})
            r.exchange.refresh=original
            await r.advance()
            self.assertNotEqual(r.runtime.snapshot()['state'],'fault',r.runtime.snapshot())
            self.assertEqual(r.runtime.host.state.policy.compiled.summary.identity.context.intent_revision,'new-snapshot')
        finally:await r.runtime.close()

    async def test_replacement_plan_moves_actuals_to_its_exact_partial_quarter_cut(self):
        r=Rig('control_verification');r.extend_plan();await r.start()
        try:
            await r.advance(900000-r.now)
            await r.advance()
            previous=r.runtime.host.state.policy.watermark
            cut=r.now
            r.plan={**r.plan,'plan_id':'replacement','snapshot_id':'replacement-snapshot',
                    'plans':{'priority':{'slots':[
                        {'start':iso(900000),'duration_hours':(1800000-cut)/3600000},
                        {'start':iso(1800000),'duration_hours':.25}]}}}
            await r.advance()
            self.assertNotEqual(r.runtime.snapshot()['state'],'fault',r.runtime.snapshot())
            session=r.runtime.host.state.policy
            self.assertEqual(session.watermark.at_ms,cut)
            self.assertEqual(session.reconciled_from,previous)
            self.assertEqual(session.compiled.summary.identity.context.intent_revision,'replacement-snapshot')
        finally:await r.runtime.close()

    async def test_controlling_reaches_actual_service_boundary(self):
        r=Rig();await r.start()
        try:
            self.assertNotEqual(r.runtime.snapshot()['state'],'fault',r.runtime.snapshot())
            self.assertTrue(r.calls,r.runtime.snapshot())
            for _ in range(4):await r.advance()
            self.assertNotEqual(r.runtime.snapshot()['state'],'fault',r.runtime.snapshot())
            self.assertEqual(r.rows['select.mode']['state'],'Command Charging (PV First)')
            self.assertGreater(float(r.rows['number.charge']['state']),0)
            self.assertTrue(all(c[2].get('option')!='Command Charging (Grid First)' for c in r.calls))
        finally:await r.runtime.close()
    async def test_verification_evaluates_but_never_sends_or_takes_writer(self):
        r=Rig('control_verification');await r.start()
        try:
            self.assertNotEqual(r.runtime.snapshot()['state'],'fault',r.runtime.snapshot())
            self.assertIsNotNone(r.runtime.snapshot()['selected_operation'])
            self.assertEqual(r.calls,[])
            self.assertEqual(r.fence.snapshot()['owner'],'legacy')
        finally:await r.runtime.close()
    async def test_source_cut_and_future_permissions_ignore_old_dispatch_choice(self):
        r=Rig();r.plan['plans']['priority']['slots'][0]['battery_command']={'allow_grid_charge':False}
        await r.start()
        try:
            c=r.coordinator._battery_native_context
            self.assertEqual(c['source_cut_ms'],10000)
            self.assertTrue(c['future_permissions'][0]['grid_charge_allowed'])
            self.assertEqual(c['future_permissions'][0]['start'],iso(10000))
        finally:await r.runtime.close()
    async def test_required_stale_sources_block_then_recover_with_sensor_diagnostics(self):
        for entity in ('sensor.house','sensor.pv','sensor.grid','sensor.battery'):
            with self.subTest(entity=entity):
                r=Rig();r.rows[entity]['last_reported']=iso(0)
                await r.start()
                try:
                    self.assertEqual(r.calls,[])
                    status=r.runtime.snapshot()
                    self.assertEqual(status['state'],'fault')
                    self.assertEqual(status['fix'],{'kind':'diagnostics'})
                    self.assertIn(entity,status['reason'])
                    self.assertTrue(status['retry_automatically'])
                    for _ in range(5):await r.advance()
                    self.assertEqual(r.runtime.snapshot()['state'],'controlling',r.runtime.snapshot())
                    self.assertNotIn('fix',r.runtime.snapshot())
                    self.assertTrue(any(entity in f['reason'] for f in r.runtime.snapshot()['fault_history']))
                    self.assertTrue(r.calls)
                finally:await r.runtime.close()

    async def test_whole_house_controls_with_unusable_individual_pool_readings(self):
        for invalid in ('stale','missing','unavailable'):
            with self.subTest(invalid=invalid):
                r=Rig();r.add_pool()
                if invalid=='missing':r.rows.pop('sensor.pool')
                elif invalid=='unavailable':r.rows['sensor.pool']['state']='unavailable'
                r.rows['sensor.house']['state']='2000'
                await r.start()
                try:
                    for _ in range(5):
                        r.now+=20000
                        for entity,row in r.rows.items():
                            if entity!='sensor.pool':row['last_reported']=iso(r.now)
                        await r.runtime.refresh();await r.runtime.host.idle()
                    self.assertEqual(r.runtime.snapshot()['state'],'controlling',r.runtime.snapshot())
                    external=[p for p in r.runtime.host.state.authority.scope.participants if p.owner=='external']
                    self.assertEqual([p.group_id for p in external],['household-load'])
                    frame=r.runtime.host.state.frame
                    self.assertEqual(frame.external.import_w,2000)
                    self.assertEqual(sum(d.observed.import_w for d in frame.external_demands),2000)
                    self.assertTrue(r.calls)
                    self.assertTrue(all(c[2]['entity_id'] in ('select.mode','number.charge','number.discharge') for c in r.calls))
                    if invalid=='stale':self.assertEqual(r.rows['sensor.pool']['last_reported'],iso(0))
                finally:await r.runtime.close()

    async def test_selected_scope_still_requires_fresh_individual_measurements(self):
        r=Rig();r.add_pool()
        r.plan['battery_supply_scope']={'kind':'selected','include_base':True,'planned_device_keys':['pool']}
        await r.start()
        try:
            self.assertEqual(r.calls,[])
            self.assertEqual(r.runtime.snapshot()['fix'],{'kind':'diagnostics'})
            self.assertIn('sensor.pool',r.runtime.snapshot()['reason'])
            for _ in range(5):await r.advance()
            self.assertEqual(r.runtime.snapshot()['state'],'controlling',r.runtime.snapshot())
            self.assertNotIn('fix',r.runtime.snapshot())
            self.assertTrue(r.calls)
        finally:await r.runtime.close()
    async def test_missing_measurements_are_actionable_before_policy_or_writes(self):
        r=Rig()
        for key in ('house_consumption_power_entity','solar_production_power_entity','grid_power_entity'):
            r.options.pop(key)
        try:
            await r.runtime.refresh()
            status=r.runtime.snapshot()
            self.assertEqual(status['state'],'fault')
            self.assertNotIn('ValueError:',status['reason'])
            self.assertEqual(status['fix']['kind'],'fields')
            self.assertEqual({f['key'] for f in status['fix']['fields']},
                {'house_consumption_power_entity','solar_production_power_entity','grid_power_entity'})
            self.assertTrue(status['retry_automatically'])
            self.assertEqual(r.calls,[])
            self.assertIsNone(r.runtime.host)
        finally:await r.runtime.close()

    async def test_policy_service_failure_routes_to_diagnostics_without_commands(self):
        r=Rig()
        async def unavailable(): pass
        r.exchange.refresh=unavailable
        r.exchange.snapshot=lambda: {'state':'unreachable','reasons':['bad envelope'],
            'error':{'code':'invalid_response_envelope','request_id':'request-123'}}
        try:
            await r.runtime.refresh()
            status=r.runtime.snapshot()
            self.assertEqual(status['fix'], {'kind':'diagnostics'})
            self.assertIn('invalid response',status['reason'])
            self.assertIn('request-123',status['reason'])
            self.assertNotIn('ValueError:',status['reason'])
            self.assertTrue(status['retry_automatically'])
            self.assertEqual(r.calls,[])
        finally:await r.runtime.close()

    async def test_failed_journal_prevents_service_calls(self):
        r=Rig()
        async def fail(value):raise OSError('disk full')
        r.store.async_save=fail
        await r.start()
        try:self.assertEqual(r.calls,[])
        finally:await r.runtime.close()

    async def test_unchanged_native_registers_are_explicitly_refreshed(self):
        r=Rig()
        for key in ('select.mode','number.charge','number.discharge'):r.rows[key]['last_reported']=iso(0)
        await r.start()
        try:
            self.assertGreater(r.readbacks,0)
            self.assertTrue(r.calls,r.runtime.snapshot())
        finally:await r.runtime.close()

    async def test_failed_native_refresh_never_sends(self):
        r=Rig()
        async def fail(entities):raise ValueError('incomplete register data')
        r.coordinator.async_battery_native_readback=fail
        await r.start()
        try:
            self.assertEqual(r.calls,[])
            self.assertIn('incomplete register',r.runtime.snapshot()['reason'])
        finally:await r.runtime.close()

    async def test_settings_confirmation_does_not_claim_battery_is_charging(self):
        r=Rig();await r.start()
        try:
            for _ in range(4):await r.advance()
            await r.advance()
            m=r.runtime.snapshot()['measurements']
            self.assertEqual(m['requested_direction'],'charging')
            self.assertEqual(m['physical_response'],'idle')
            self.assertFalse(m['response_matches_direction'])
        finally:await r.runtime.close()

    async def test_pending_other_load_rejects_queued_battery_write(self):
        from home_host import DispatchRejected
        r=Rig();captured=[]
        original=r.runtime._dispatch
        async def queued(effect):captured.append(effect);raise DispatchRejected('test transport queue')
        r.runtime._dispatch=queued
        await r.start()
        try:
            self.assertTrue(captured)
            r.plan['snapshot_id']='superseding-snapshot'
            self.assertFalse(r.runtime._can_send(captured[0]))
            r.plan['snapshot_id']='s'
            r.runtime._closing=True
            self.assertFalse(r.runtime._can_send(captured[0]))
            r.runtime._closing=False
            r.runtime.before_external_command('heater')
            with self.assertRaises(DispatchRejected):await original(captured[0])
            self.assertEqual(r.calls,[])
        finally:await r.runtime.close()

    async def test_external_device_waits_for_zero_charge_ceiling_not_zero_battery_power(self):
        r=Rig();await r.start()
        try:
            for _ in range(4):await r.advance()
            self.assertEqual(r.rows['sensor.battery']['state'],'0')
            self.assertFalse(r.runtime.before_external_command('heater'))
            allowed=False
            for _ in range(20):
                await r.advance(10000)
                allowed=r.runtime.before_external_command('heater')
                if allowed:break
            self.assertTrue(allowed,r.runtime.snapshot())
        finally:await r.runtime.close()

    async def test_expired_policy_releases_even_when_exchange_has_no_policy(self):
        from home_runtime import Tick
        r=Rig();await r.start()
        try:
            for _ in range(4):await r.advance()
            r.exchange.policy=None
            r.now=900001
            for row in r.rows.values():row['last_reported']=iso(r.now)
            for event in await r.runtime._observe(r.runtime.host.state.groups[0].spec.id):await r.runtime.host.accept(event)
            await r.runtime.host.accept(Tick());await r.runtime.host.idle()
            for _ in range(35):
                r.now+=10000
                for row in r.rows.values():row['last_reported']=iso(r.now)
                for event in await r.runtime._observe(r.runtime.host.state.groups[0].spec.id):await r.runtime.host.accept(event)
                await r.runtime.host.accept(Tick());await r.runtime.host.idle()
            self.assertEqual(r.rows['select.mode']['state'],'Maximum Self Consumption',r.runtime.snapshot())
            self.assertFalse(r.runtime.host.state.groups[0].owned)
        finally:await r.runtime.close()

    async def test_release_and_readmission_have_distinct_scope_identity(self):
        r=Rig();await r.start()
        try:
            for _ in range(4):await r.advance()
            previous=r.runtime.host.state.authority.scope.revision
            await r.runtime._release('Temporary override');await r.runtime.host.idle()
            for _ in range(4):await r.advance()
            self.assertNotEqual(r.runtime.snapshot()['state'],'fault',r.runtime.snapshot())
            self.assertNotEqual(r.runtime.host.state.authority.scope.revision,previous)
        finally:await r.runtime.close()

    async def test_restart_recovers_native_commands_without_cloud_or_pool_readings(self):
        from home_runtime import Tick, ScopeParticipant
        from home_runtime_checkpoint import decode_checkpoint, encode_checkpoint
        for old_participants in (False,True):
            with self.subTest(old_participants=old_participants):
                r=Rig();r.add_pool();await r.start()
                for _ in range(5):await r.advance()
                await r.runtime.close()
                restarted=Rig();restarted.now=r.now+25*3600000;restarted.rows=deepcopy(r.rows)
                for row in restarted.rows.values():row['last_reported']=iso(restarted.now)
                restarted.store.saved=deepcopy(r.store.saved)
                if old_participants:
                    state=decode_checkpoint(restarted.store.saved['checkpoint'].encode())
                    scope=state.authority.scope
                    scope=replace(scope,participants=tuple(p for p in scope.participants if p.owner=='new_runtime') +
                                  (ScopeParticipant('load:pool','monitoring',1,'external',('sensor.pool',)),))
                    state=replace(state,authority=replace(state.authority,scope=scope))
                    restarted.store.saved['checkpoint']=encode_checkpoint(state).decode()
                restarted.rows.pop('sensor.pool')
                restarted.fence_store.saved=deepcopy(r.fence_store.saved)
                await restarted.fence.open();await restarted.runtime.open()
                try:
                    self.assertIsNone(restarted.exchange.policy)
                    for _ in range(36):
                        restarted.now+=10000
                        for row in restarted.rows.values():row['last_reported']=iso(restarted.now)
                        await restarted.runtime._release('Recovering without cloud')
                        await restarted.runtime.host.accept(Tick());await restarted.runtime.host.idle()
                    self.assertTrue(restarted.calls)
                    self.assertEqual(restarted.rows['select.mode']['state'],'Maximum Self Consumption')
                    self.assertFalse(restarted.runtime.host.state.groups[0].owned,restarted.runtime.snapshot())
                finally:await restarted.runtime.close()

    async def test_recovery_waits_for_sigen_entities_then_retries(self):
        from battery_runtime import NativeReadbackPending
        r=Rig();await r.start();await r.runtime.close()
        restarted=Rig();restarted.store.saved=deepcopy(r.store.saved)
        restarted.fence_store.saved=deepcopy(r.fence_store.saved)
        await restarted.fence.open()
        missing=restarted.rows.pop('select.mode')
        with self.assertRaises(NativeReadbackPending):await restarted.runtime.open()
        self.assertEqual(restarted.calls,[])
        self.assertIsNone(restarted.runtime.host)
        restarted.rows['select.mode']=missing
        await restarted.runtime.open()
        try:self.assertIsNotNone(restarted.runtime.host)
        finally:await restarted.runtime.close()

    async def test_shutdown_latch_prevents_refresh_readmission(self):
        r=Rig('control_verification');await r.start()
        entered=asyncio.Event();finish=asyncio.Event();original=r.runtime._release
        async def release(reason):
            entered.set();await finish.wait();await original(reason)
        r.runtime._release=release
        closing=asyncio.create_task(r.runtime.close(release=True))
        await asyncio.wait_for(entered.wait(),1)
        r.options['device_modes']['$battery']='controlling'
        await r.runtime.refresh()
        self.assertEqual(r.calls,[])
        self.assertEqual(r.runtime.host.state.groups[0].mode,'control_verification')
        finish.set();await asyncio.wait_for(closing,1)
