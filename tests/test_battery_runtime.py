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
sys.path.append(str(Path(__file__).parents[1]/'custom_components'/'shs_energy'))
from battery_runtime import BatteryRuntime, exact_start, iso, stamp
from battery_writer import BatteryWriterFence
from battery_runtime import digest
from plan_execution import *


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
        self.listener_updates=0
        def update_listeners():self.listener_updates+=1
        self.coordinator=SimpleNamespace(_battery_entity_report=lambda e:deepcopy(self.rows.get(e)),async_battery_planned_devices=devices,
            async_battery_native_readback=readback,async_battery_loss_statistics=statistics,_state_history=history,async_update_listeners=update_listeners,
            binding_plan_for=lambda device,options:(self.plan,next((s for s in self.plan['plans']['priority']['slots']
                if stamp(s['start'])<=self.now<min(stamp(s['start'])+900000,stamp(self.plan['valid_until']),stamp(self.plan['binding_until']))),None)),_battery_native_context=None)
        self.replans=[]
        async def replan(**kw):self.replans.append(kw)
        self.coordinator.async_optimisation_push=replan
        self.plan['grid']={'import_limit_w':10000,'export_limit_w':10000}
        self.install_contract()
        self.archive_stores={}
        self.runtime=BatteryRuntime(self.coordinator,self.controller,self.store,lambda:self.now,
            lambda key:self.archive_stores.setdefault(key,Store()))
        self.fence=BatteryWriterFence(self.fence_store,self.controller.lock,self.controller.options,lambda:self.now,self.runtime.identity)
        self.coordinator.battery_writer=self.fence
    def install_contract(self, generation=0, previous=None, start=10000, end=900000):
        row=ReferenceInterval(start,end,5000000,5000000+round(2000*(end-start)/3600),
            'grid_charge','stored_energy',True,False,4000,0,False,
            round(2000*(end-start)/3600),0,0,round(1000*(end-start)/3600),round(2000*(end-start)/3600)+round(1000*(end-start)/3600),0,0,0,0)
        row=replace(row,rounding_mwh=row.import_mwh-row.load_mwh-row.charge_ac_mwh)
        contract=ExecutionContract('c'+str(generation),self.plan['plan_id'],generation,self.options['device_modes']['$battery'],
            digest(self.options),'configured-95','stored_energy_mwh',10000000,0,10000000,0,end,0,previous,
            (row,), (Objective('charge:'+str(end),'stored_energy',start,end,row.stored_end_mwh,'Charge now'),),(),())
        self.plan['battery_execution']=contract_wire(contract)

    async def start(self):
        self.plan['battery_execution']['scope_revision']=digest(self.options)
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
                self.assertEqual(state.execution.status,'active')
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

    async def test_household_lock_wait_uses_current_evidence_instead_of_one_second_window(self):
        r=Rig();original=r.runtime._dispatch
        waits=[]
        async def delayed(effect):
            await r.controller.lock.acquire()
            pending=asyncio.create_task(original(effect))
            await asyncio.sleep(0)
            r.now+=3000
            waits.append(effect.attempt_id)
            r.controller.lock.release()
            return await pending
        r.runtime._dispatch=delayed
        try:
            await r.start()
            for _ in range(4):await r.advance(1000)
            self.assertTrue(waits)
            self.assertTrue(r.calls,r.runtime.snapshot())
            self.assertEqual(r.runtime.snapshot()['state'],'controlling',r.runtime.snapshot())
            self.assertFalse(r.runtime.snapshot()['fault_history'])
        finally:await r.runtime.close()

    async def test_permission_change_while_waiting_for_household_lock_prevents_service_call(self):
        from home_host import DispatchRejected
        r=Rig();original=r.runtime._dispatch;rejected=[]
        async def withdrawn(effect):
            await r.controller.lock.acquire()
            pending=asyncio.create_task(original(effect))
            await asyncio.sleep(0)
            r.now+=3000
            r.options['device_modes']['$battery']='control_verification'
            r.controller.lock.release()
            try:return await pending
            except DispatchRejected:
                rejected.append(effect.attempt_id)
                raise
        r.runtime._dispatch=withdrawn
        try:
            await r.start()
            self.assertTrue(rejected)
            self.assertEqual(r.calls,[])
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
            self.assertEqual(r.readbacks,0)
        finally:await r.runtime.close()

    async def test_service_success_finishes_sequence_without_post_write_readback(self):
        r=Rig();original=r.coordinator.async_battery_native_readback
        async def readback(entities):
            if r.calls:raise AssertionError('post-write readback must not be required')
            await original(entities)
        async def lagged_service(domain,name,data,blocking):
            r.calls.append((domain,name,data.copy()))  # HA completes, publication lags.
        r.coordinator.async_battery_native_readback=readback
        r.controller.hass.services.async_call=lagged_service
        await r.start()
        try:
            group=r.runtime.host.state.groups[0]
            self.assertEqual(group.status,'adopted',r.runtime.snapshot())
            self.assertEqual(len(r.calls),2)  # mode + requested charging ceiling
            self.assertEqual(r.readbacks,0)
            self.assertEqual(r.rows['select.mode']['state'],'Standby')
            self.assertEqual(dict(group.observation.controls)['number.charge'],0)
            self.assertEqual(r.runtime.snapshot()['measurements']['battery_dc_w'],0)
            self.assertFalse(r.runtime.snapshot()['fault_history'])
            calls=deepcopy(r.calls)
            for _ in range(2):await r.advance(1000)
            self.assertEqual(r.calls,calls)
            self.assertEqual(r.runtime.host.state.groups[0].status,'adopted')
            # Normal reports retire all acknowledged intermediate reservations.
            for domain,name,data in r.calls:
                r.rows[data['entity_id']]['state']=str(data.get('value',data.get('option')))
            await r.advance(1000)
            self.assertFalse(r.runtime.host.state.groups[0].attempts)
        finally:await r.runtime.close()

    async def test_release_reverses_completed_commands_even_when_ha_still_shows_baseline(self):
        r=Rig();r.rows['select.mode']['state']='Maximum Self Consumption'
        async def lagged_service(domain,name,data,blocking):
            r.calls.append((domain,name,data.copy()))
        r.controller.hass.services.async_call=lagged_service
        await r.start()
        try:
            self.assertTrue(any(c[2].get('option')=='Command Charging (PV First)' for c in r.calls))
            self.assertEqual(r.rows['select.mode']['state'],'Maximum Self Consumption')
            await r.advance(80000)  # Acknowledged settings do not expire with the old effect window.
            r.calls.clear()
            r.options['device_modes']['$battery']='control_verification'
            await r.runtime.refresh();await r.runtime.host.idle()
            for _ in range(3):await r.advance(1000)
            self.assertTrue(any(c[2].get('option')=='Maximum Self Consumption' for c in r.calls),r.calls)
            self.assertFalse(r.runtime.host.state.groups[0].release_pending,r.runtime.snapshot())
            self.assertFalse(r.runtime.snapshot()['fault_history'])
        finally:await r.runtime.close()

    async def test_optimistic_zero_settings_do_not_erase_measured_power(self):
        r=Rig('control_verification');r.rows['sensor.battery']['state']='2000'
        await r.start()
        try:
            group=r.runtime.host.state.groups[0]
            self.assertEqual(dict(group.observation.controls)['number.charge'],0)
            self.assertGreaterEqual(group.observation.envelope.import_w,2000)
            self.assertEqual(r.runtime.snapshot()['measurements']['battery_dc_w'],2000)
            self.assertEqual(r.calls,[])
        finally:await r.runtime.close()

    async def test_terminal_power_does_not_gate_settings_calls(self):
        r=Rig();r.rows['sensor.battery']['state']='150'
        await r.start()
        try:
            self.assertTrue(r.calls)
            self.assertEqual(r.runtime.host.state.execution.status,'active')
            self.assertEqual(r.runtime.snapshot()['state'],'controlling')
            self.assertEqual(r.runtime.snapshot()['measurements']['battery_dc_w'],150)
            self.assertFalse(r.runtime.snapshot()['fault_history'])
        finally:await r.runtime.close()

    async def test_refresh_never_rerenders_the_household_or_wakes_other_controllers(self):
        # The coordinator republishes battery status after each refresh on its own
        # channel. A household-wide update here re-rendered every entity every 5 s.
        r=Rig();await r.start()
        try:
            for _ in range(4):await r.advance()
            self.assertNotEqual(r.runtime.snapshot()['state'],'fault',r.runtime.snapshot())
            self.assertEqual(r.listener_updates,0)
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
            self.assertIsNotNone(r.runtime.snapshot()['assessment'])
            self.assertEqual(r.calls,[])
            self.assertEqual(r.fence.snapshot()['owner'],'legacy')
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

    async def test_failed_journal_prevents_service_calls(self):
        r=Rig()
        async def fail(value):raise OSError('disk full')
        r.store.async_save=fail
        await r.start()
        try:self.assertEqual(r.calls,[])
        finally:await r.runtime.close()

    async def test_unchanged_control_settings_do_not_expire(self):
        r=Rig()
        for key in ('select.mode','number.charge','number.discharge'):r.rows[key]['last_reported']=iso(0)
        await r.start()
        try:
            self.assertEqual(r.readbacks,0)
            self.assertTrue(r.calls,r.runtime.snapshot())
        finally:await r.runtime.close()

    async def test_unavailable_ha_control_entity_prevents_commands(self):
        r=Rig();r.rows['select.mode']['state']='unavailable'
        await r.start()
        try:
            self.assertEqual(r.calls,[])
            self.assertEqual(r.runtime.snapshot()['state'],'fault')
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
            r.plan['battery_execution']['id']='superseding-plan'
            self.assertFalse(r.runtime._can_send(captured[0]))
            r.plan['battery_execution']['id']='c0'
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
                restarted.archive_stores.update(r.archive_stores)
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
        restarted.archive_stores.update(r.archive_stores)
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

class ExecutionCutoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_dump_replays_account_without_commands(self):
        from runtime_json import decode_value
        r=Rig('control_verification');await r.start()
        try:
            await r.advance()
            dump=r.runtime.snapshot(include_evidence=True)
            account=decode_value(dump['accounting_journal'],Account)
            self.assertEqual(feedback(account,r.now),dump['accounting'])
            self.assertEqual(r.calls,[])
            self.assertIn('not being changed',dump['explanation']['status'])
            self.assertNotIn('accounting_journal',r.runtime.snapshot())
            self.assertIsNone(dump['native_target'])
        finally:await r.runtime.close()

    async def test_capture_after_host_creation_persists_exact_prefix_and_payload(self):
        r=Rig('control_verification');await r.start()
        try:
            captured=await r.runtime.capture_feedback(5000000,'snapshot')
            saved=await r.runtime.archive.load_session(r.store.saved['execution_root'])
            self.assertEqual(json.loads(saved.captured_feedback),captured)
            await r.advance()
            self.assertEqual(json.loads(r.runtime.host.state.execution.captured_feedback),captured)
            self.assertGreater(r.runtime.host.state.execution.account.receipt,captured['source_receipt'])
        finally:await r.runtime.close()

    async def test_counter_unavailable_keeps_live_execution_and_unknown_delivery(self):
        r=Rig('control_verification');await r.start()
        try:
            r.rows['sensor.battery_charge']['state']='unavailable'
            await r.advance()
            self.assertEqual(r.runtime.snapshot()['state'],'verified')
            self.assertIsNone(r.runtime.snapshot()['accounting']['balance']['charge']['high'])
        finally:await r.runtime.close()

    async def test_receipt_order_reset_and_late_correction_do_not_invent_epochs(self):
        r=Rig('control_verification');await r.start()
        try:
            entity='sensor.battery_charge';attrs=r.rows[entity]['attributes']
            await r.runtime._meter(entity,101,attrs,70000,'before-reset')
            await r.runtime._meter(entity,1,attrs,80000,'reset')
            await r.runtime._meter(entity,100.5,attrs,65000,'late')
            await r.runtime._meter(entity,2,attrs,90000,'after-reset')
            rows=[m for m in r.runtime.host.state.execution.account.meters if m.stream==entity]
            self.assertEqual([m.epoch for m in rows[-4:]],['0','1','0','1'])
            await r.runtime._meter(entity,100.6,attrs,65000,'correction')
            before=r.runtime.host.state.execution.account
            await r.runtime._meter(entity,100.5,attrs,65000,'late')
            self.assertEqual(r.runtime.host.state.execution.account,before)
        finally:await r.runtime.close()

    async def test_multiple_configured_counters_are_explicit_additive_sources(self):
        r=Rig('control_verification')
        r.options['entities_battery_charge'].append('sensor.second_charge')
        r.rows['sensor.second_charge']=deepcopy(r.rows['sensor.battery_charge'])
        await r.start()
        try:
            self.assertEqual(r.runtime.snapshot()['state'],'verified',r.runtime.snapshot())
            sources={m.physical_id for m in r.runtime.host.state.execution.account.meters if m.direction=='charge'}
            self.assertEqual(sources,{'sensor.battery_charge','sensor.second_charge'})
        finally:await r.runtime.close()

    async def test_configuration_change_keeps_physical_account_while_waiting_for_new_plan(self):
        r=Rig('control_verification');await r.start()
        try:
            original=r.runtime.host.state.execution.account
            r.options['battery_export_enabled']=True
            await r.runtime.refresh()
            self.assertEqual(r.runtime._bootstrap,original)
            self.assertFalse(r.calls)
            captured=await r.runtime.capture_feedback(5000000,'new_config')
            self.assertEqual(captured['previous_contract_id'],original.contract.id)
        finally:await r.runtime.close()

    async def test_mode_change_without_matching_plan_preserves_account_and_rebinds(self):
        for capture_first in (False, True):
            with self.subTest(capture_first=capture_first):
                r=Rig('control_verification');await r.start()
                try:
                    original=r.runtime.host.state.execution.account
                    binding=r.coordinator.binding_plan_for
                    r.coordinator.binding_plan_for=lambda device,options: binding(device,options) if r.plan['battery_execution']['mode']==options['device_modes']['$battery'] else ({},None)
                    r.options['device_modes']['$battery']='controlling'
                    if not capture_first:
                        await r.runtime.refresh()
                        self.assertIsNone(r.runtime.host)
                        self.assertEqual(r.runtime._bootstrap.contract,original.contract)
                        self.assertEqual(r.runtime.snapshot()['state'],'idle')
                        dump=r.runtime.snapshot(include_evidence=True)
                        self.assertEqual(dump['mode'],'controlling')
                        self.assertEqual(dump['accounting']['previous_contract_id'],original.contract.id)
                        self.assertIn('accounting_journal',dump)
                    feedback=await r.runtime.capture_feedback(5000000,'new_mode')
                    self.assertIsNone(r.runtime.host)
                    self.assertEqual(feedback['previous_contract_id'],original.contract.id)
                    self.assertEqual(feedback['scope_revision'],digest(r.options))
                    saved=await r.runtime.archive.load_session(r.store.saved['execution_root'])
                    self.assertEqual(saved.account,r.runtime._bootstrap)
                    self.assertEqual(r.calls,[])
                    async def changed_history(entities,start,end,with_attributes):
                        return {entity:[(datetime.fromtimestamp(r.now/1000,timezone.utc),
                            '100.1',deepcopy(r.rows[entity]['attributes']))] for entity in entities}
                    r.coordinator._state_history=changed_history
                    r.install_contract(feedback['generation'],previous=original.contract.id)
                    r.plan['battery_execution']['source_receipt']=feedback['source_receipt']
                    r.runtime.validate_plan_response(r.plan)
                    await r.advance()
                    for _ in range(4):await r.advance()
                    self.assertEqual(r.runtime.snapshot()['state'],'controlling',r.runtime.snapshot())
                    self.assertEqual(r.runtime.snapshot()['plan_status'],'accepted')
                    self.assertTrue(r.calls)
                    self.assertFalse(r.runtime.snapshot()['fault_history'])
                finally:await r.runtime.close()

    async def test_restart_from_retained_account_installs_authority_before_new_meter_receipts(self):
        r=Rig('control_verification');await r.start()
        try:
            r.options['device_modes']['$battery']='controlling'
            feedback=await r.runtime.capture_feedback(5000000,'new_mode')
            original=r.runtime._bootstrap.contract
            await r.runtime.close()
            restarted=Rig('controlling')
            restarted.store.saved=deepcopy(r.store.saved)
            restarted.runtime.archive=r.runtime.archive
            restarted.install_contract(feedback['generation'],previous=original.id)
            restarted.plan['battery_execution']['source_receipt']=feedback['source_receipt']
            async def new_history(entities,start,end,with_attributes):
                return {entity:[(datetime.fromtimestamp(20,timezone.utc),'100.1',
                    deepcopy(restarted.rows[entity]['attributes']))] for entity in entities}
            restarted.coordinator._state_history=new_history
            r=restarted
            await r.start()
            for _ in range(4):await r.advance()
            self.assertEqual(r.runtime.snapshot()['state'],'controlling',r.runtime.snapshot())
            self.assertIsNone(r.runtime.host._fault)
            self.assertTrue(any(m.total_mwh==100100000 for m in r.runtime.host.state.execution.account.meters))
            self.assertTrue(r.calls)
        finally:await r.runtime.close()

    async def test_journal_failure_explains_software_fault_and_retains_technical_evidence(self):
        r=Rig('control_verification');await r.start()
        try:
            r.runtime.host._fault=ValueError('execution account needs its physical battery group')
            status=r.runtime.snapshot()
            self.assertNotIn('group',status['reason'])
            self.assertEqual(status['technical_error'],str(r.runtime.host._fault))
            self.assertFalse(status['retry_automatically'])
            self.assertIn('Restart Home Assistant',status['next_step'])
            from presentation import controller_explanation
            attributes=controller_explanation('battery','controlling',{'battery_runtime':status})
            self.assertEqual(attributes['technical_error'],status['technical_error'])
            self.assertNotIn('physical battery group',attributes['explanation'])
        finally:await r.runtime.close()

    async def test_withdrawing_control_releases_before_retiring_old_configuration(self):
        r=Rig();await r.start()
        try:
            for _ in range(4):await r.advance()
            original=r.runtime.host.state.execution.account.contract
            r.options['device_modes']['$battery']='control_verification'
            r.coordinator.binding_plan_for=lambda device,options: ({},None)
            await r.runtime.refresh()
            # Pending native work must remain journalled until release is confirmed.
            self.assertIsNotNone(r.runtime.host)
            for _ in range(40):
                await r.advance(10000)
                if r.runtime.host is None:break
            self.assertIsNone(r.runtime.host,r.runtime.snapshot())
            self.assertEqual(r.rows['select.mode']['state'],'Maximum Self Consumption')
            self.assertEqual(r.runtime._bootstrap.contract,original)
            calls=deepcopy(r.calls)
            await r.advance()
            self.assertEqual(r.calls,calls)
            self.assertEqual(r.runtime.snapshot()['state'],'idle')
        finally:await r.runtime.close()

    async def test_expired_plan_keeps_recording_energy_without_replan_success(self):
        r=Rig('control_verification');await r.start()
        try:
            before=r.runtime.host.state.execution.account
            r.now=1000000
            r.rows['sensor.battery_charge'].update(state='101',last_reported=iso(r.now))
            await r.runtime.refresh()
            account=r.runtime.host.state.execution.account
            self.assertGreater(len(account.meters),len(before.meters))
            self.assertEqual(account.contract.id,before.contract.id)
            self.assertEqual(r.calls,[])
        finally:await r.runtime.close()

    async def test_diagnostic_dump_replays_all_assessments_and_detects_changed_result(self):
        import runpy
        replay=runpy.run_path(str(Path(__file__).parents[1]/'scripts/replay-battery-execution.py'))['replay']
        r=Rig('control_verification');await r.start()
        try:
            for _ in range(4):await r.advance()
            dump=r.runtime.snapshot(include_evidence=True)
            result=replay({'battery_execution':dump})
            self.assertTrue(result['matches_recorded_results'],result['mismatches'])
            self.assertGreater(result['assessments_checked'],0)
            self.assertFalse(r.calls)
            dump['assessment']['charge_dc_w']+=1
            self.assertFalse(replay(dump)['matches_recorded_results'])
        finally:await r.runtime.close()

    async def test_handover_rejection_is_visible_until_corrected_plan_is_admitted(self):
        r=Rig('control_verification');await r.start()
        try:
            original=read_contract(r.plan['battery_execution'])
            captured=await r.runtime.capture_feedback(5000000,'snapshot')
            revised=replace(original,id='replacement',generation=captured['generation'],
                source_receipt=captured['source_receipt'],previous_contract_id=original.id,
                objectives=(replace(original.objectives[0],target_mwh=original.objectives[0].target_mwh+100),))
            r.plan['battery_execution']=contract_wire(revised)
            await r.advance()
            dump=r.runtime.snapshot(include_evidence=True)
            self.assertEqual(dump['state'],'fault')
            self.assertEqual(dump['plan_status'],'rejected')
            self.assertEqual(dump['accepted_reference_id'],original.id)
            self.assertEqual(dump['plan_rejection']['contract_id'],'replacement')
            self.assertIn('explicit retained amendment',dump['plan_rejection']['reason'])
            self.assertIn('previously accepted',dump['display']['plan_warning'])
            self.assertIn('Previously accepted plan',dump['explanation']['plan'])
            self.assertNotIn('Continue with the current plan',dump['explanation']['next'])
            saved=await r.runtime.archive.load_session(r.store.saved['execution_root'])
            self.assertEqual(saved.plan_rejection.contract_id,'replacement')
            self.assertEqual(dump['fix'],{'kind':'diagnostics'})
            await r.runtime.close()
            restarted=Rig('control_verification');restarted.now=r.now+1000
            for row in restarted.rows.values():row['last_reported']=iso(restarted.now)
            restarted.runtime.store=r.store;restarted.runtime.archive=r.runtime.archive
            r=restarted
            await asyncio.wait_for(r.start(),3)
            self.assertEqual(r.runtime.snapshot()['plan_rejection'],dump['plan_rejection'])
            self.assertEqual(r.runtime.snapshot()['state'],'fault')
            revised=replace(revised,dispositions=(Disposition(original.objectives[0].id,'retained',None,'Amend target'),))
            r.plan['battery_execution']=contract_wire(revised)
            await r.advance()
            dump=r.runtime.snapshot()
            self.assertEqual(dump['state'],'verified',dump)
            self.assertEqual(dump['plan_status'],'accepted')
            self.assertIsNone(dump['plan_rejection'])
            self.assertFalse(dump['display']['plan_warning'])
            self.assertEqual(r.calls,[])
        finally:await r.runtime.close()

    async def test_pre_cache_rejection_survives_bootstrap_restart_and_valid_acceptance(self):
        r=Rig('control_verification')
        await r.runtime.reject_plan_response(r.plan,'Battery plan belongs to a superseded request')
        dump=r.runtime.snapshot()
        self.assertEqual(dump['plan_status'],'rejected')
        self.assertIn('no accepted battery plan',dump['display']['plan_warning'])
        restarted=Rig('control_verification')
        restarted.runtime.store=r.store
        restarted.runtime.archive=r.runtime.archive
        try:
            await restarted.runtime.open()
            self.assertEqual(restarted.runtime.snapshot()['plan_status'],'rejected')
            self.assertEqual(restarted.runtime.snapshot()['plan_rejection'],dump['plan_rejection'])
            await restarted.start()
            self.assertIsNone(restarted.runtime.snapshot()['plan_rejection'])
            await restarted.runtime.reject_plan_response({'battery_execution':{'id':'later','generation':1}},'Invalid receipt prefix')
            self.assertEqual(restarted.runtime.snapshot()['plan_rejection']['contract_id'],'later')
            await restarted.advance()
            self.assertEqual(restarted.runtime.snapshot()['plan_status'],'rejected')
        finally:
            await r.runtime.close();await restarted.runtime.close()

    async def test_restart_reserves_distinct_revisions_before_queued_captures_are_applied(self):
        r=Rig('control_verification');await r.start()
        try:
            for _ in range(5):await r.advance(1000)
            before=r.runtime.host.state.conditions_revision
            await r.runtime.close()
            restarted=Rig('control_verification');restarted.now=r.now+1000
            for row in restarted.rows.values():row['last_reported']=iso(restarted.now)
            restarted.store.saved=deepcopy(r.store.saved)
            restarted.archive_stores.update(r.archive_stores)
            r=restarted
            await asyncio.wait_for(r.start(),3)
            self.assertGreater(r.runtime.host.state.conditions_revision,before)
            await r.advance(1000)
            baseline=r.runtime.host.state
            # Both producers return captures before the host applies either.
            # The second source timestamp is older but still fresh; local receipt
            # order, not source timestamps, determines which update is newer.
            r.rows['sensor.house']['state']='1100'
            first=(await r.runtime._observe('battery'))[0]
            r.rows['sensor.house'].update(state='1200',last_reported=iso(r.now-100))
            second=(await r.runtime._observe('battery'))[0]
            self.assertLess(first.conditions.revision,second.conditions.revision)
            self.assertGreater(first.conditions.revision,baseline.conditions_revision)
            for event in (first,second):
                self.assertEqual(event.observed.observation.revision,event.conditions.revision)
                self.assertEqual(event.frame.revision,event.conditions.revision)
                await r.runtime.host.accept(event)
            await asyncio.wait_for(r.runtime.host.idle(),3)
            self.assertEqual(r.runtime.host.state.conditions.residual_load_w,1200)
            self.assertEqual(r.runtime.host.state.conditions_revision,second.conditions.revision)
            self.assertFalse(any('conflicting conditions revision' in row['reason']
                for row in r.runtime.snapshot()['fault_history']))
            self.assertEqual(r.calls,[])
        finally:await r.runtime.close()
