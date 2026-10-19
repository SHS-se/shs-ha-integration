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
from shs_core.battery_runtime import BatteryRuntime, exact_start, iso, stamp
from command_fixture import command_transport
from shs_core.native_commands import NativeExecutor
from shs_core.battery_writer import BatteryWriterFence
from shs_core.battery_runtime import digest, plan_scope
from shs_core.plan_execution import *


def downloaded(value):
    """A snapshot as the controller diagnostics file contains it."""
    from shs_core.controller_diagnostics import report_fragments, report_parts
    dumps=lambda item:json.dumps(item,allow_nan=False).encode()
    return json.loads(b''.join(report_fragments(report_parts(value,dumps),dumps)))

class Store:
    def __init__(self):self.saved=None;self.writes=[]
    async def async_load(self):return deepcopy(self.saved)
    async def async_save(self,value):self.saved=deepcopy(value);self.writes.append(deepcopy(value))

class ExecutionStore(Store):
    def __init__(self):
        super().__init__()
        from shs_core.home_runtime import ExecutionSession
        self.session=ExecutionSession()
    async def load(self):
        return (deepcopy(self.saved),self.session) if self.saved is not None else None
    async def save(self, value, session):
        await self.async_save(value)
        self.session=session
    def resource_counts(self):return {}

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
        self.store=ExecutionStore();self.fence_store=Store()
        async def service(domain,name,data,blocking):
            self.calls.append((domain,name,data.copy()))
            assert (self.store.saved is not None if isinstance(self.store, ExecutionStore)
                    else self.store._revision > 0),'command preceded durable checkpoint'
            assert self.fence_store.saved['owner']=='runtime'
            self.rows[data['entity_id']]['state']=str(data.get('value',data.get('option')))
            self.rows[data['entity_id']]['last_reported']=iso(self.now)
        async def restore(device):self.controller.ownership.records.pop(device,None)
        async def send_native(domain,service,data):
            await self.controller.hass.services.async_call(domain,service,data,blocking=True)
        def read_native(entity):
            row=self.rows.get(entity)
            return SimpleNamespace(state=row['state'],attributes=row['attributes']) if row else None
        native=NativeExecutor(command_transport(),read_native,lambda:'°C',send_native)
        self.controller=SimpleNamespace(native_executor=native,options=lambda:deepcopy(self.options),lock=asyncio.Lock(),ownership=SimpleNamespace(records={}),
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
        self.coordinator=SimpleNamespace(ports=SimpleNamespace(battery_report=lambda e:deepcopy(self.rows.get(e))),async_battery_planned_devices=devices,
            async_battery_native_readback=readback,async_battery_loss_statistics=statistics,_state_history=history,async_update_listeners=update_listeners,
            house_import_limit_w=lambda:self.plan['grid']['import_limit_w'],
            binding_plan_for=lambda device,options:(self.plan,next((s for s in self.plan['plans']['priority']['slots']
                if stamp(s['start'])<=self.now<min(stamp(s['start'])+900000,stamp(self.plan['valid_until']),stamp(self.plan['binding_until']))),None)),_battery_native_context=None)
        self.replans=[]
        self.runtime_reports=[]
        async def report_runtime():
            self.runtime_reports.append(self.runtime.plan_feedback())
            return {}
        self.coordinator.async_report_runtime=report_runtime
        async def replan(**kw):self.replans.append(kw)
        self.coordinator.async_optimisation_push=replan
        self.plan['grid']={'import_limit_w':10000,'export_limit_w':10000}
        self.install_contract()
        self.runtime=BatteryRuntime(self.coordinator,self.controller,self.store,lambda:self.now)
        self.fence=BatteryWriterFence(self.fence_store,self.controller.lock,self.controller.options,lambda:self.now,self.runtime.identity)
        self.coordinator.battery_writer=self.fence
    def install_contract(self, generation=0, previous=None, start=10000, end=900000):
        row=ReferenceInterval(start,end,5000000,5000000+round(2000*(end-start)/3600),
            'grid_charge','stored_energy',True,False,4000,0,'none', None, None, None,
            round(2000*(end-start)/3600),0,0,round(1000*(end-start)/3600),round(2000*(end-start)/3600)+round(1000*(end-start)/3600),0,0,0,0)
        row=replace(row,rounding_mwh=row.import_mwh-row.load_mwh-row.charge_ac_mwh)
        contract=ExecutionContract('c'+str(generation),self.plan['plan_id'],generation,self.options['device_modes']['$battery'],
            plan_scope(self.options),'configured-95','stored_energy_mwh',10000000,0,10000000,0,end,0,previous,
            (row,), (Objective('charge:'+str(end),'stored_energy',start,end,row.stored_end_mwh,'Charge now'),),(),())
        self.plan['battery_execution']=contract_wire(contract)

    async def start(self):
        self.plan['battery_execution']['scope_revision']=plan_scope(self.options)
        await self.fence.open();await self.runtime.open();await self.runtime.refresh()
        # A hang guard only: CI runners vary about 2x and some tests delay admission 1.2 s.
        if self.runtime.host:await asyncio.wait_for(self.runtime.host.idle(),10)
    async def advance(self,millis=20000):
        self.now+=millis
        for row in self.rows.values():row['last_reported']=iso(self.now)
        await self.runtime.refresh()
        if self.runtime.host:await asyncio.wait_for(self.runtime.host.idle(),10)

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
    async def test_a_missed_charge_target_is_measured_for_input_accuracy(self):
        # User requirement, 19 October 2026: a banner only asks for manual action.
        r = Rig()
        self.addAsyncCleanup(r.runtime.close)
        await r.start()
        self.assertEqual(r.runtime.snapshot()['assessment']['replan_reason'], 'observed_state_cannot_meet_target')
        self.assertEqual(r.replans, [])
        self.assertEqual(r.runtime.charge_target_outcomes(86400000), [])
        for _ in range(45):
            await r.advance()
        # The rig's plan ends at 900000; only that expiry needs a manual replan.
        self.assertEqual([kw['replan_reason'] for kw in r.replans],
                         ['Battery controller: plan_expired. Consider a manual replan.'])
        outcomes = r.runtime.charge_target_outcomes(86400000)
        self.assertEqual([deadline for deadline, _ in outcomes], [900000])
        self.assertGreater(outcomes[0][1], 0)

    async def test_replacement_solar_plan_clears_shortfall_recommendation(self):
        r = Rig()
        self.addAsyncCleanup(r.runtime.close)
        await r.start()
        original = read_contract(r.plan['battery_execution'])
        self.assertEqual(r.runtime.snapshot()['assessment']['replan_reason'],
                         'observed_state_cannot_meet_target')
        captured = await r.runtime.capture_feedback(5000000, 'fresh-solar-plan')
        start, end = r.now, original.valid_until_ms
        energy = lambda watts: round(watts * (end - start) / 3600)
        row = ReferenceInterval(start, end, 5000000, 5000000 + energy(95),
            'solar_charge', 'stored_energy', False, False, 4000, 4000, 'none', None, None, None,
            energy(100), 0, energy(1100), energy(1000), 0, 0, 0, 0,
            energy(1100) - energy(1000) - energy(100))
        revised = replace(original, id='fresh-solar-contract', generation=captured['generation'],
            source_receipt=captured['source_receipt'], previous_contract_id=original.id,
            intervals=(row,), objectives=(Objective('solar-window', 'stored_energy', start, end,
                row.stored_end_mwh, 'Store available spare solar'),), recovery=(),
            dispositions=(Disposition(original.objectives[0].id, 'incorporated', 'solar-window',
                                      'Replanned from observed state'),))
        r.plan['battery_execution'] = contract_wire(revised)
        r.rows['sensor.pv']['state'] = '900'
        r.rows['sensor.grid']['state'] = '100'
        for _ in range(3):
            await r.advance(20000)
        status = r.runtime.snapshot()
        self.assertEqual(status['accepted_reference_id'], revised.id)
        self.assertIsNone(status['plan_rejection'])
        self.assertIsNone(status['assessment']['replan_reason'])
        self.assertEqual(status['reason'], 'Following the plan')
        self.assertEqual(status['command_state'], 'adopted')
        self.assertEqual(status['requested_settings'],
            {'mode': 'Maximum Self Consumption', 'charge_limit_w': 4000, 'discharge_limit_w': 0})
        history = objective_history(r.runtime.host.state.execution.account, r.now)
        self.assertEqual(history[0]['responsibility'], 'incorporated')

    async def test_solar_backed_charge_adapts_to_gross_import_capacity(self):
        from unittest.mock import patch
        from shs_core.battery_conversion import Conversion, Curve

        # 17 October capture: the solar-aware decision requested 8,325 DC W,
        # but the native safety envelope was 8,800 AC W plus 6,329 W of house
        # demand. The 15 kW gross budget must size the request before dispatch.
        r = Rig()
        self.addAsyncCleanup(r.runtime.close)
        r.options.update(battery_capacity_kwh=18.08, battery_charge_max_w=8800,
                         battery_discharge_max_w=9600)
        for field, maximum in (('charge', 8.8), ('discharge', 9.6)):
            r.rows['number.' + field]['attributes']['max'] = maximum
        r.rows['sensor.house']['state'] = '6329'
        r.rows['sensor.pv']['state'] = '4020'
        r.rows['sensor.grid']['state'] = '2281'
        r.rows['sensor.soc']['state'] = '18.8'
        r.rows['select.mode']['state'] = 'Command Charging (PV First)'
        r.plan['grid']['import_limit_w'] = 15000
        model = Conversion('captured-loss-model', Curve(.9557645926252383, 85.31059199396077),
                           Curve(.95, 0), Curve(.9716846305625433, 140.6251520512858),
                           122.87613431000018)
        original = read_contract(r.plan['battery_execution'])
        row = original.intervals[0]
        duration = row.end_ms - row.start_ms
        charge = round(8325 * duration / 3600)
        ac = round(8800 * duration / 3600)
        row = replace(row, stored_end_mwh=row.stored_start_mwh + charge,
                      charge_ac_limit_w=8800, charge_ac_mwh=ac,
                      import_mwh=row.load_mwh + ac, rounding_mwh=0)
        r.plan['battery_execution'] = contract_wire(replace(original,
            capacity_mwh=18080000, maximum_mwh=18080000, intervals=(row,),
            objectives=(replace(original.objectives[0], target_mwh=row.stored_end_mwh),)))

        with patch('shs_core.battery_runtime.conversion_model', return_value=(model, {})):
            await r.start()
            for house, expected in ((6329, 8202), (7500, 7082), (5000, 8325)):
                with self.subTest(house_w=house):
                    r.rows['sensor.house']['state'] = str(house)
                    for _ in range(3):
                        await r.advance(5000)
                    state = r.runtime.host.state
                    self.assertEqual(state.execution.assessment.charge_dc_w, expected)
                    self.assertEqual(round(float(r.rows['number.charge']['state']) * 1000), expected)
                    self.assertEqual(state.groups[0].status, 'adopted')
                    self.assertLessEqual(house + model.grid_charge.input(expected), 15000)
                    self.assertFalse(state.groups[0].release_pending)
                    self.assertEqual(r.runtime.snapshot()['fault_history'], [])
        self.assertTrue(r.calls)

    async def test_obsolete_cached_plan_is_not_offered_while_new_request_is_pending(self):
        from unittest.mock import patch
        from shs_core.home_runtime import ExecutionPlanOffered
        r=Rig();await r.start()
        try:
            accepted=r.runtime.host.state.execution.account.contract
            await r.runtime.capture_feedback(5000000,'next-request')
            r.runtime_reports.clear()
            r.plan['battery_execution']=contract_wire(replace(accepted,id='obsolete-cached-candidate'))
            with patch.object(r.runtime.host,'accept',wraps=r.runtime.host.accept) as receive:
                for _ in range(4):await r.advance(5000)
                self.assertFalse(any(isinstance(call.args[0],ExecutionPlanOffered)
                                     for call in receive.call_args_list))
            self.assertEqual(r.runtime.plan_feedback()['accepted']['contract_id'],accepted.id)
            self.assertIsNone(r.runtime.plan_feedback()['rejection'])
            self.assertFalse(r.runtime_reports)
            self.assertEqual(r.rows['select.mode']['state'],'Command Charging (PV First)')
            self.assertTrue(r.runtime.host.state.groups[0].owned)
            self.assertFalse(r.runtime.host.state.groups[0].release_pending)
            await r.runtime.close()
            restarted=Rig();restarted.now=r.now+1000;restarted.rows=deepcopy(r.rows)
            for row in restarted.rows.values():row['last_reported']=iso(restarted.now)
            restarted.plan=deepcopy(r.plan);restarted.runtime.store=r.store
            restarted.fence_store.saved=deepcopy(r.fence_store.saved)
            r=restarted
            await r.start()
            for _ in range(3):await r.advance(5000)
            self.assertEqual(r.runtime.plan_feedback()['accepted']['contract_id'],accepted.id)
            self.assertIsNone(r.runtime.plan_feedback()['rejection'])
            self.assertFalse(r.runtime_reports)
            self.assertTrue(r.runtime.host.state.groups[0].owned)
            self.assertFalse(r.runtime.host.state.groups[0].release_pending)
        finally:await r.runtime.close()

    async def test_startup_waits_for_current_generation_then_accepts_the_replacement(self):
        r=Rig()
        feedback=await r.runtime.capture_feedback(5000000,'new-request-before-start')
        await r.start()
        try:
            for _ in range(3):await r.advance(5000)
            self.assertEqual(r.runtime.plan_feedback(),{'accepted':None,'rejection':None})
            self.assertFalse(r.calls)
            current=read_contract(r.plan['battery_execution'])
            current=replace(current,id='current-candidate',generation=feedback['generation'],source_receipt=feedback['source_receipt'])
            r.plan['battery_execution']=contract_wire(current)
            await r.advance(5000)
            self.assertEqual(r.runtime.plan_feedback()['accepted']['contract_id'],current.id)
            self.assertIsNone(r.runtime.plan_feedback()['rejection'])
            self.assertTrue(r.calls)
            await r.runtime._replan_task
            self.assertEqual(r.runtime_reports[-1]['accepted']['contract_id'],current.id)
            self.assertFalse(r.replans)
        finally:await r.runtime.close()

    async def test_a_plan_that_leaves_the_battery_out_says_why_and_requests_no_replan(self):
        async def run(issues):
            r=Rig();del r.plan['battery_execution']
            if issues:r.plan['measurement_issues']=issues
            await r.fence.open();await r.runtime.open();await r.runtime.refresh()
            if r.runtime._replan_task:await r.runtime._replan_task
            try:return r.runtime.snapshot()['reason'],r.replans
            finally:await r.runtime.close()
        reason='The home battery reported a state of charge of 130%, outside 0–100%.'
        status,replans=await run([{'device':'battery','field':'soc','entity_id':'sensor.soc','value':1.3,
                                   'reason':reason,'detected_by':'planner'}])
        self.assertEqual(status,'The current plan leaves the battery out: '+reason)
        # Replanning cannot include it until the reading is real; the server recommends one then.
        self.assertEqual(replans,[])
        status,replans=await run(None)
        self.assertEqual(status,'Waiting for the planner to provide battery execution instructions')
        self.assertEqual(len(replans),1)

    async def test_old_disabled_export_setting_does_not_block_export_authority(self):
        r=Rig('control_verification')
        self.assertFalse(r.options['battery_export_enabled'])
        try:
            await r.start()
            self.assertTrue(r.runtime.host.state.authority.permissions.battery_export_allowed)
        finally:
            await r.runtime.close()

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
                status=r.runtime.snapshot()
                self.assertEqual(status['state'],'controlling',status)
                self.assertEqual(status['decision']['operation'],status['assessment']['operation'])
                self.assertEqual(status['decision']['charge_limit_w'],status['requested_settings']['charge_limit_w'])
                self.assertEqual(status['decision']['discharge_limit_w'],status['requested_settings']['discharge_limit_w'])
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
            self.assertEqual(current.at_ms,r.now)
            self.assertEqual(r.runtime.snapshot()['measurements']['source_report_ages_ms']['sensor.house'],1000)
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
        from shs_core import home_runtime as rt
        r=Rig();await r.start()
        try:
            r.runtime._last_capture=None
            events=await r.runtime._observe_batch(r.runtime.host.state.groups[0].spec.id)
            capture=events[0]
            self.assertIsInstance(capture,rt.MeasurementsReceived)
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

    async def test_route_admission_wait_uses_the_measurement_lifetime_instead_of_one_second(self):
        # Through the app, admission is a round trip to HA's durable journal. A
        # one-second budget abandoned it at every busy quarter-hour boundary.
        r=Rig();original=r.runtime._transition
        async def journalled(effect):
            await asyncio.sleep(1.2)
            return await original(effect)
        r.runtime._transition=journalled
        try:
            await r.start()
            self.assertTrue(r.calls,r.runtime.snapshot())
            self.assertFalse(r.runtime.snapshot()['fault_history'])
        finally:await r.runtime.close()

    async def test_permission_change_while_waiting_for_household_lock_prevents_service_call(self):
        from shs_core.home_host import DispatchRejected
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

    async def test_permission_change_during_durable_prepare_prevents_service_call(self):
        import sqlite3
        from contextlib import closing
        r=Rig()
        transport=r.controller.native_executor.transport
        async def executor(fn,*args):
            result=await asyncio.to_thread(fn,*args)
            if fn==transport.journal.prepare:
                r.options['device_modes']['$battery']='control_verification'
            return result
        transport.executor=executor
        try:
            await r.start()
            self.assertEqual(r.calls,[])
            with closing(sqlite3.connect(transport.journal.path)) as db:
                outcomes=[row[0] for row in db.execute('SELECT status FROM commands')]
            self.assertTrue(outcomes)
            self.assertEqual(set(outcomes),{'not_sent'})
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

    async def test_snapshot_readers_share_one_live_account_view(self):
        from shs_core import battery_runtime as module
        from unittest.mock import patch
        r=Rig();await r.start()
        try:
            with patch.object(module.execution,'live_feedback',wraps=module.execution.live_feedback) as live, \
                    patch.object(module.execution,'feedback',wraps=module.execution.feedback) as full:
                # Sensor state, sensor details, the mode select and controller status read one refresh.
                views=[r.runtime.snapshot() for _ in range(4)]
                self.assertEqual(live.call_count,1)
                self.assertTrue(all(view['accounting'] is views[0]['accounting'] for view in views))
                self.assertNotIn('sha256',views[0]['accounting']['settled_history'])
                r.now+=2000
                self.assertEqual(r.runtime.snapshot()['accounting_at_ms'],r.now)
                self.assertEqual(live.call_count,2)
                evidence=r.runtime.snapshot(include_evidence=True)
                self.assertEqual(full.call_count,1)
                self.assertTrue(evidence['accounting']['objectives'])
                self.assertIn('versions',evidence['accounting']['objectives'][0])
                await r.advance()
                r.runtime.snapshot()
                self.assertEqual(live.call_count,3)
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
            self.assertEqual(r.fence.snapshot()['owner'],'fenced')
        finally:await r.runtime.close()
    async def test_available_old_and_future_source_timestamps_do_not_block_control(self):
        for entity in ('sensor.house','sensor.pv','sensor.grid','sensor.battery','sensor.soc'):
            for reported in (0, 86400000):
                with self.subTest(entity=entity,reported=reported):
                    r=Rig();r.rows[entity]['last_reported']=iso(reported)
                    await r.start()
                    try:
                        self.assertEqual(r.runtime.snapshot()['state'],'controlling',r.runtime.snapshot())
                        self.assertNotIn('fix',r.runtime.snapshot())
                        self.assertEqual(r.runtime.snapshot()['measurements']['source_report_ages_ms'][entity],r.now-reported)
                        self.assertTrue(r.calls)
                    finally:await r.runtime.close()

    async def test_unavailable_required_power_still_blocks_and_recovers(self):
        r=Rig();r.rows['sensor.house']['state']='unavailable'
        await r.start()
        try:
            self.assertEqual(r.calls,[])
            self.assertEqual(r.runtime.snapshot()['fix'],{'kind':'diagnostics'})
            self.assertIn('sensor.house',r.runtime.snapshot()['reason'])
            r.rows['sensor.house']['state']='1000'
            await r.runtime.refresh();await r.runtime.host.idle()
            self.assertEqual(r.runtime.snapshot()['state'],'controlling',r.runtime.snapshot())
            self.assertNotIn('fix',r.runtime.snapshot())
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

    async def test_hot_water_source_permission_uses_measured_power_even_while_finishing(self):
        for allowed, expected in ((False, 100), (True, 1000)):
            with self.subTest(surplus=allowed):
                r=Rig(); r.add_pool()
                r.plan['capabilities']={'boiler':True}
                r.plan['device_models']=[{'key':'pool','category':'hot_water'}]
                r.plan['battery_supply_scope']={'kind':'selected','include_base':True,'planned_device_keys':['pool']}
                r.plan['plans']['priority']['slots'][0].update(boiler_permitted=False, boiler_battery_allowed=allowed)
                r.rows['sensor.pool']['last_reported']=iso(r.now)
                await r.start()
                try:
                    status=r.runtime.snapshot()
                    self.assertEqual(status['state'],'controlling',status)
                    self.assertEqual(status['measurements']['eligible_gross_w'],expected)
                finally: await r.runtime.close()

    async def test_selected_scope_accepts_old_available_individual_measurements(self):
        r=Rig();r.add_pool()
        r.plan['battery_supply_scope']={'kind':'selected','include_base':False,'planned_device_keys':['pool']}
        await r.start()
        try:
            self.assertEqual(r.runtime.snapshot()['state'],'controlling',r.runtime.snapshot())
            self.assertNotIn('fix',r.runtime.snapshot())
            self.assertEqual(r.rows['sensor.pool']['last_reported'],iso(0))
            self.assertTrue(r.calls)
        finally:await r.runtime.close()

    async def test_switched_power_uses_available_values_without_report_age_windows(self):
        for switch_state, age_minutes, expected in (('off',240,'controlling'),('on',20,'controlling'),('on',240,'controlling')):
            with self.subTest(switch_state=switch_state,age_minutes=age_minutes):
                r=Rig();r.add_pool();r.now=12*3600000
                for value in r.rows.values():value['last_reported']=iso(r.now)
                end=r.now+900000
                r.plan['valid_until']=r.plan['binding_until']=iso(end)
                r.plan['plans']['priority']['slots']=[{'start':iso(r.now),'duration_hours':.25}]
                r.install_contract(start=r.now,end=end)
                r.plan['battery_supply_scope']={'kind':'selected','include_base':False,'planned_device_keys':['pool']}
                r.options['device_control_mappings']['pool']['actuator_entity_ids']=['switch.pool']
                r.rows['switch.pool']={'state':switch_state,'attributes':{},'last_reported':iso(1000)}
                r.rows['sensor.pool']['last_reported']=iso(r.now-age_minutes*60000)
                await r.start()
                try:
                    status=r.runtime.snapshot()
                    self.assertEqual(status['state'],expected,status)
                    self.assertIn('switch.pool',r.runtime.capture_entities())
                    if expected=='controlling':
                        self.assertEqual(status['measurements']['eligible_gross_w'],0 if switch_state=='off' else 900)
                        self.assertTrue(r.calls)
                        evidence=next(row for row in status['measurements']['supply_evidence'] if row['source']=='sensor.pool')
                        self.assertEqual(evidence['basis'],'switch_off' if switch_state=='off' else 'switched_ac')
                        if switch_state=='off':
                            r.rows['switch.pool']['state']='on'
                            await r.runtime.refresh();await r.runtime.host.idle()
                            self.assertEqual(r.runtime.snapshot()['state'],'controlling',r.runtime.snapshot())
                            self.assertTrue(r.runtime.host.state.groups[0].owned)
                            self.assertFalse(r.runtime.host.state.groups[0].release_pending)
                    else:
                        self.assertIn('sensor.pool',status['reason'])
                        self.assertFalse(r.calls)
                    self.assertFalse(any(call[2]['entity_id']=='switch.pool' for call in r.calls))
                finally:await r.runtime.close()

    async def test_house_supply_measures_exactly_the_plans_listed_loads(self):
        """Contract v2: base load is the house minus the plan's listed loads.

        HA's own inventory neither adds nor removes a load: a device enrolled
        after the plan stays in base, and a listed load HA does not plan is
        still subtracted.
        """
        from shs_core.battery_supply import ExecutionDemand
        r=Rig();r.add_pool()
        for key,watts in (('ev',800),('heater',300),('tv',500)):
            r.options['device_control_mappings'][key]={'power':'sensor.'+key}
            r.rows['sensor.'+key]={'state':str(watts),'attributes':{'unit_of_measurement':'W','state_class':'measurement'},
                                   'last_reported':iso(r.now)}
        async def devices():
            return [{'key':key,'planning_role':'controllable'} for key in ('ev','pool','tv')]
        r.coordinator.async_battery_planned_devices=devices
        await r.start()
        try:
            for _ in range(2):await r.advance(5000)
            original=r.runtime.host.state.execution.account.contract
            r.extend_plan()
            r.now=900000
            r.rows['sensor.soc']['state']='100'
            r.rows['sensor.house']['state']='2800'
            r.rows['sensor.grid']['state']='2800'
            r.plan.pop('battery_supply_scope')
            demand=ExecutionDemand.read({'loads':[{'key':key,'name':key,'category':'other'} for key in ('ev','heater','pool')],
                                         'profiles':[{'id':'base+pool','include_base':True,'eligible_keys':['pool']}]})
            row=ReferenceInterval(900000,1800000,10000000,9500000,
                'supply_house','demand_following',False,False,0,4000,'follow_demand',None,None,'base+pool',
                0,500000,0,700000,200000,0,0,0,0)
            for value in r.rows.values():value['last_reported']=iso(r.now)
            feedback=await r.runtime.capture_feedback(10000000,'full-battery')
            contract=replace(original,id='v2-plan',generation=feedback['generation'],source_receipt=feedback['source_receipt'],
                previous_contract_id=original.id,valid_until_ms=1800000,intervals=(row,),demand=demand,
                objectives=(Objective('supply-house','demand_following',900000,1800000,0,'Supply current house demand'),),
                dispositions=(Disposition(original.objectives[0].id,'retired',None,'Charging window finished'),))
            r.plan['battery_execution']=contract_wire(contract)
            for _ in range(5):
                for value in r.rows.values():value['last_reported']=iso(r.now)
                await r.runtime.refresh();await r.runtime.host.idle()
                r.now+=5000
            status=r.runtime.snapshot()
            self.assertEqual(status['accepted_reference_id'],contract.id,status)
            self.assertEqual(status['measurements']['eligible_gross_w'],2800-800-300)
            self.assertIn('sensor.heater',r.runtime.capture_entities())
            self.assertNotIn('sensor.tv',r.runtime.capture_entities())
        finally:await r.runtime.close()

    async def test_charging_to_house_supply_ignores_stale_selected_pool_readings(self):
        r=Rig();r.add_pool()
        r.options['device_control_mappings']['ev']={'power':'sensor.ev'}
        async def devices():
            return [{'key':key,'planning_role':'controllable'} for key in ('ev','pool')]
        r.coordinator.async_battery_planned_devices=devices
        r.rows['sensor.ev']={'state':'0','attributes':{'unit_of_measurement':'W','state_class':'measurement'},
                             'last_reported':iso(r.now)}
        r.rows['sensor.pool']['state']='0'
        await r.start()
        try:
            for _ in range(2):await r.advance(5000)
            self.assertEqual(r.rows['select.mode']['state'],'Command Charging (PV First)')
            self.assertGreater(float(r.rows['number.charge']['state']),0)
            self.assertEqual(float(r.rows['number.discharge']['state']),0)
            original=r.runtime.host.state.execution.account.contract
            r.calls.clear()
            r.extend_plan()
            r.now=900000
            r.rows['sensor.soc']['state']='100'
            r.rows['sensor.house']['state']='2800'
            r.rows['sensor.grid']['state']='2800'
            r.rows['sensor.ev']['state']='800'
            r.rows['sensor.pool']['last_reported']=iso(0)
            r.plan['battery_supply_scope']={'kind':'selected','include_base':True,'planned_device_keys':['pool']}
            row=ReferenceInterval(900000,1800000,10000000,9500000,
                'supply_house','demand_following',False,False,0,4000,'follow_demand', None, None, None,
                0,500000,0,700000,200000,0,0,0,0)
            for entity,value in r.rows.items():
                if entity!='sensor.pool':value['last_reported']=iso(r.now)
            feedback=await r.runtime.capture_feedback(10000000,'full-battery')
            contract=replace(original,id='discharge-plan',generation=feedback['generation'],source_receipt=feedback['source_receipt'],
                previous_contract_id=original.id,valid_until_ms=1800000,intervals=(row,),
                objectives=(Objective('supply-house','demand_following',900000,1800000,0,'Supply current house demand'),),
                dispositions=(Disposition(original.objectives[0].id,'retired',None,'Charging window finished'),))
            r.plan['battery_execution']=contract_wire(contract)
            for _ in range(5):
                for entity,value in r.rows.items():
                    if entity!='sensor.pool':value['last_reported']=iso(r.now)
                await r.runtime.refresh();await r.runtime.host.idle()
                r.now+=5000
            status=r.runtime.snapshot()
            self.assertEqual(status['state'],'controlling',status)
            self.assertEqual(status['accepted_reference_id'],contract.id)
            self.assertEqual(r.rows['select.mode']['state'],'Maximum Self Consumption')
            # Self-consumption permits PV charging while enabling house discharge.
            self.assertEqual(float(r.rows['number.charge']['state']),4)
            self.assertGreater(float(r.rows['number.discharge']['state']),0)
            self.assertLessEqual(float(r.rows['number.discharge']['state'])*1000*.95,2000)
            self.assertEqual(status['measurements']['eligible_gross_w'],2000)
            self.assertEqual(r.runtime.host.state.frame.external.import_w,2800)
            self.assertEqual(sum(d.observed.import_w for d in r.runtime.host.state.frame.external_demands),2800)
            self.assertEqual(r.rows['sensor.pool']['last_reported'],iso(0))
            self.assertNotIn('sensor.pool',r.runtime.capture_entities())
            self.assertIn('sensor.ev',r.runtime.capture_entities())
            self.assertTrue(r.runtime.host.state.groups[0].owned)
            self.assertFalse(r.runtime.host.state.groups[0].release_pending)
            self.assertTrue(r.calls)
            self.assertFalse(any(call[2].get('option')=='Command Charging (PV First)' for call in r.calls))
            self.assertFalse(status['fault_history'])
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
        from shs_core.home_host import DispatchRejected
        r=Rig();captured=[]
        original=r.runtime._dispatch
        async def queued(effect):captured.append(effect);raise DispatchRejected('test transport queue')
        r.runtime._dispatch=queued
        await r.start()
        try:
            self.assertTrue(captured)
            r.plan['battery_execution']['id']='unaccepted-candidate'
            self.assertTrue(r.runtime._can_send(captured[0]))
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

    async def test_pool_reservation_ends_after_response_window_with_unchanged_soc(self):
        r=Rig();r.rows['sensor.soc']['last_reported']=iso(0)
        r.rows['sensor.battery']['state']='2'
        await r.start()
        try:
            self.assertFalse(r.runtime.before_external_command('pool'))
            allowed=False
            for _ in range(10):
                r.now+=5000
                for entity,row in r.rows.items():
                    if entity!='sensor.soc':row['last_reported']=iso(r.now)
                await r.runtime.refresh();await r.runtime.host.idle()
                allowed=r.runtime.before_external_command('pool')
                if allowed:break
            self.assertTrue(allowed,r.runtime.snapshot())
            # The pool now starts. Its observed load replaces the temporary
            # reservation; quiet SOC must not leave all grid capacity reserved.
            r.rows['sensor.house']['state']='4300'
            r.now+=25000
            await r.runtime.refresh();await r.runtime.host.idle()
            self.assertGreater(r.runtime.host.state.execution.live.pending_import_w,0)
            # No source changes between these captures. Expiry itself must
            # invalidate capture reuse, rather than waiting for another report.
            r.now+=5001
            await r.runtime.refresh();await r.runtime.host.idle()
            status=r.runtime.snapshot()
            self.assertEqual(status['assessment']['operation'],'grid_charge',status)
            self.assertEqual(r.runtime.host.state.execution.live.pending_import_w,0)
            self.assertEqual(r.rows['select.mode']['state'],'Command Charging (PV First)')
            self.assertGreater(float(r.rows['number.charge']['state']),0)
            self.assertTrue(r.runtime.host.state.groups[0].owned)
            self.assertFalse(r.runtime.host.state.groups[0].release_pending)
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
        from shs_core.home_runtime import Tick, ScopeParticipant
        from shs_core.home_runtime_checkpoint import decode_checkpoint, encode_checkpoint
        for old_participants in (False,True):
            with self.subTest(old_participants=old_participants):
                r=Rig();r.add_pool();await r.start()
                for _ in range(5):await r.advance()
                await r.runtime.close()
                restarted=Rig();restarted.now=r.now+25*3600000;restarted.rows=deepcopy(r.rows)
                for row in restarted.rows.values():row['last_reported']=iso(restarted.now)
                restarted.store.saved=deepcopy(r.store.saved)
                restarted.store.session=r.store.session
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
        from shs_core.battery_runtime import NativeReadbackPending
        r=Rig();await r.start();await r.runtime.close()
        restarted=Rig();restarted.store.saved=deepcopy(r.store.saved)
        restarted.store.session=r.store.session
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
    async def test_mode_change_executes_retained_reference_despite_failed_replan_and_restart(self):
        r=Rig('control_verification');await r.start()
        try:
            original=r.runtime.host.state.execution.account.contract
            r.options['device_modes']['$battery']='controlling'
            await r.runtime.capture_feedback(5000000,'mode_changed')
            await r.runtime.reject_plan_response({'battery_execution':{'id':'failed','generation':1}},'HTTP 546')
            for _ in range(4):await r.advance(1000)
            self.assertEqual(r.runtime.host.state.execution.account.contract,original)
            self.assertTrue(r.calls)
            self.assertEqual(r.runtime.snapshot()['state'],'controlling',r.runtime.snapshot())
            self.assertNotIn('not being changed',r.runtime.snapshot()['explanation']['status'])
            await r.runtime.close()
            restarted=Rig('controlling');restarted.now=r.now+1000
            restarted.plan=deepcopy(r.plan)
            restarted.rows=deepcopy(r.rows)
            restarted.fence_store.saved=deepcopy(r.fence_store.saved)
            restarted.store=r.store;restarted.runtime.store=r.store
            r=restarted
            for row in r.rows.values():row['last_reported']=iso(r.now)
            await r.fence.open();await r.runtime.open()
            for _ in range(4):await r.advance(1000)
            self.assertEqual(r.runtime.host.state.execution.account.contract,original)
            self.assertEqual(r.runtime.snapshot()['state'],'controlling',r.runtime.snapshot())
            # Withdrawing permission still stops optimization writes immediately.
            r.options['device_modes']['$battery']='control_verification'
            for _ in range(4):await r.advance(1000)
            self.assertEqual(r.runtime.snapshot()['state'],'verified',r.runtime.snapshot())
            count=len(r.calls)
            await r.advance(1000)
            self.assertEqual(len(r.calls),count)
        finally:await r.runtime.close()

    async def test_handover_is_validated_before_replacing_a_controlling_reference(self):
        r=Rig();await r.start()
        try:
            original=r.runtime.host.state.execution.account.contract
            captured=await r.runtime.capture_feedback(5000000,'snapshot')
            candidate=deepcopy(r.plan)
            revised=replace(original,id='bad',generation=captured['generation'],
                source_receipt=captured['source_receipt'],previous_contract_id=original.id,
                objectives=(replace(original.objectives[0],target_mwh=original.objectives[0].target_mwh+100),))
            candidate['battery_execution']=contract_wire(revised)
            with self.assertRaisesRegex(ValueError,'explicit retained amendment'):
                r.runtime.validate_plan_response(candidate)
            await r.runtime.reject_plan_response(candidate,'Rejected objective amendment')
            for _ in range(3):await r.advance(1000)
            self.assertEqual(r.runtime.host.state.execution.account.contract,original)
            self.assertEqual(r.runtime.snapshot()['state'],'controlling',r.runtime.snapshot())
            self.assertEqual(r.plan['battery_execution']['id'],original.id)
        finally:await r.runtime.close()

    async def test_rejected_household_candidate_does_not_block_accepted_battery_commands(self):
        r=Rig();await r.start()
        try:
            original=r.runtime.host.state.execution.account.contract
            captured=await r.runtime.capture_feedback(5000000,'snapshot')
            revised=replace(original,id='rejected-household-candidate',generation=captured['generation'],
                source_receipt=captured['source_receipt'],previous_contract_id=original.id,
                objectives=(replace(original.objectives[0],target_mwh=original.objectives[0].target_mwh+100),))
            r.plan['battery_execution']=contract_wire(revised)
            await r.advance(1000)
            self.assertEqual(r.runtime.snapshot()['plan_status'],'rejected')
            r.calls.clear()
            r.rows['sensor.house']['state']='9900'
            r.rows['sensor.grid']['state']='9900'
            for _ in range(4):await r.advance(1000)
            self.assertEqual(r.runtime.host.state.execution.account.contract,original)
            self.assertTrue(r.calls,r.runtime.snapshot())
            self.assertFalse(any('Write not sent' in fault['reason'] for fault in r.runtime.snapshot()['fault_history']))
            self.assertEqual(r.runtime.snapshot()['state'],'controlling')
        finally:await r.runtime.close()

    async def test_plan_arriving_after_charge_deadline_is_admitted_and_executes_future_supply(self):
        r=Rig();await r.start()
        try:
            original=r.runtime.host.state.execution.account.contract
            captured=await r.runtime.capture_feedback(5000000,'snapshot')
            last=original.intervals[-1]
            supply=ReferenceInterval(last.end_ms,1800000,last.stored_end_mwh,last.stored_end_mwh-500000,
                'supply_house','demand_following',False,False,4000,4000,'follow_demand', None, None, None,
                0,500000,0,500000,0,0,0,0,0)
            proposed=replace(original,id='late-but-usable',generation=captured['generation'],
                source_receipt=captured['source_receipt'],previous_contract_id=original.id,valid_until_ms=1800000,
                intervals=(*original.intervals,supply),
                objectives=(replace(original.objectives[0],target_mwh=original.objectives[0].target_mwh+100),
                            Objective('future-supply','demand_following',last.end_ms,1800000,supply.stored_end_mwh,'Supply the house')),
                dispositions=(Disposition(original.objectives[0].id,'retained',None,'Revised target'),))
            r.extend_plan()
            r.plan['battery_execution']=contract_wire(proposed)
            r.now=last.end_ms+14
            r.calls.clear()
            for _ in range(4):await r.advance(1000)
            account=r.runtime.host.state.execution.account
            self.assertEqual(account.contract,proposed)
            self.assertIsNone(r.runtime.snapshot()['plan_rejection'])
            self.assertEqual(objective_history(account,r.now)[0]['objective']['target_mwh'],original.objectives[0].target_mwh)
            self.assertTrue(r.calls,r.runtime.snapshot())
            self.assertEqual(r.rows['select.mode']['state'],'Maximum Self Consumption')
            self.assertGreater(float(r.rows['number.discharge']['state']),0)
            self.assertFalse(any('Write not sent' in fault['reason'] for fault in r.runtime.snapshot()['fault_history']))
        finally:await r.runtime.close()

    async def test_verified_dump_replays_account_without_commands(self):
        from shs_core.runtime_json import decode_value
        r=Rig('control_verification');await r.start()
        try:
            await r.advance()
            dump=downloaded(r.runtime.snapshot(include_evidence=True))
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
            saved=r.store.session
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

    async def test_historical_counter_outage_does_not_block_current_control(self):
        for mode in ('controlling','control_verification'):
            with self.subTest(mode=mode):
                r=Rig(mode)
                async def history(entities,start,end,with_attributes):
                    return {entity:[
                        (datetime.fromtimestamp(5,timezone.utc),'99',deepcopy(r.rows[entity]['attributes'])),
                        (datetime.fromtimestamp(10,timezone.utc),'unavailable',deepcopy(r.rows[entity]['attributes'])),
                        (datetime.fromtimestamp(15,timezone.utc),'unknown',None),
                        (datetime.fromtimestamp(20,timezone.utc),None,{}),
                        (datetime.fromtimestamp(25,timezone.utc),'100',deepcopy(r.rows[entity]['attributes'])),
                    ] for entity in entities}
                r.coordinator._state_history=history
                await r.start()
                try:
                    for _ in range(5):await r.advance()
                    status=r.runtime.snapshot()
                    self.assertEqual(status['state'],'controlling' if mode=='controlling' else 'verified',status)
                    self.assertTrue(r.runtime._seeded)
                    self.assertFalse(status['fault_history'])
                    self.assertNotIn('fix',status)
                    self.assertEqual(bool(r.calls),mode=='controlling')
                    for entity in ('sensor.battery_charge','sensor.battery_discharge','sensor.grid_import','sensor.grid_export'):
                        receipts=[m for m in r.runtime.host.state.execution.account.meters if m.stream==entity]
                        self.assertEqual([(m.source_at_ms,m.total_mwh,m.epoch) for m in receipts[:2]],
                                         [(5000,99000000,'0'),(25000,100000000,'0')])
                finally:await r.runtime.close()

    async def test_missing_counter_history_does_not_invent_energy_evidence(self):
        r=Rig('control_verification')
        entity='sensor.battery_charge'
        r.rows[entity]['state']='unavailable'
        async def history(entities,start,end,with_attributes):
            return {key:[(datetime.fromtimestamp(5,timezone.utc),'unavailable',{})] for key in entities}
        r.coordinator._state_history=history
        await r.start()
        try:
            await r.advance()
            status=r.runtime.snapshot()
            self.assertEqual(status['state'],'verified',status)
            self.assertIsNone(status['accounting']['balance']['charge']['high'])
            self.assertFalse(any(m.stream==entity for m in r.runtime.host.state.execution.account.meters))
            r.rows[entity]['state']='101'
            await r.advance()
            receipts=[m for m in r.runtime.host.state.execution.account.meters if m.stream==entity]
            self.assertEqual([(m.total_mwh,m.epoch) for m in receipts],[(101000000,'0')])
            self.assertEqual(r.runtime.snapshot()['state'],'verified')
            self.assertFalse(r.runtime.snapshot()['fault_history'])
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

    async def test_a_plan_captured_under_either_mode_is_accepted_under_the_other(self):
        """Verification and Controlling change who may write, never the plan."""
        for captured_in,accepted_in in (('control_verification','controlling'),('controlling','control_verification')):
            with self.subTest(captured=captured_in,accepted=accepted_in):
                r=Rig(captured_in);await r.start()
                try:
                    original=r.runtime.host.state.execution.account.contract
                    feedback=await r.runtime.capture_feedback(5000000,'snapshot')
                    # The mode changes, and so does another device's, while the plan is solved.
                    r.options['device_modes']['$battery']=accepted_in
                    r.options['device_modes']['sensor.pool']='controlling'
                    r.options['configuration_reviewed_at']='2026-09-23T15:00:00+00:00'
                    self.assertEqual(plan_scope(r.options),feedback['scope_revision'])
                    candidate=deepcopy(r.plan)
                    revised=replace(original,id='next',generation=feedback['generation'],mode=captured_in,
                        source_receipt=feedback['source_receipt'],previous_contract_id=original.id)
                    candidate['battery_execution']=contract_wire(revised)
                    r.runtime.validate_plan_response(candidate)
                    # A changed setup still belongs to a different plan.
                    r.options['battery_capacity_kwh']=12
                    with self.assertRaisesRegex(ValueError,'current battery capacity'):
                        r.runtime.validate_plan_response(candidate)
                finally:await r.runtime.close()

    async def test_another_device_entering_verification_is_not_a_battery_fault(self):
        # User requirement, 19 October 2026: Verification of another device has
        # no effect on plan execution. A receipt observed before the next
        # refresh rebinds the changed settings is not a fault, and nothing is written.
        r=Rig();await r.start()
        try:
            calls=len(r.calls)
            r.options['device_modes']['switch.towel_rack']='control_verification'
            self.assertEqual(await r.runtime._observe_batch(r.runtime.host.state.groups[0].spec.id),())
            await r.advance()
            self.assertEqual(r.runtime.snapshot()['fault_history'],[])
            self.assertEqual(r.runtime.host.state.execution.status,'active')
            self.assertFalse(r.runtime.host.state.groups[0].release_pending)
            self.assertEqual(r.calls[calls:],[])
        finally:await r.runtime.close()

    async def test_unrelated_options_do_not_reject_a_captured_battery_reference(self):
        r=Rig('control_verification');await r.start()
        try:
            original=r.runtime.host.state.execution.account.contract
            feedback=await r.runtime.capture_feedback(5000000,'snapshot')
            candidate=deepcopy(r.plan)
            revised=replace(original,id='next',generation=feedback['generation'],
                source_receipt=feedback['source_receipt'],previous_contract_id=original.id)
            candidate['battery_execution']=contract_wire(revised)
            r.options['unrelated_setting']='changed'
            r.runtime.validate_plan_response(candidate)
            candidate['plan_id']='different-plan'
            with self.assertRaisesRegex(ValueError,'another plan'):
                r.runtime.validate_plan_response(candidate)
            r.plan=candidate
            await r.advance()
            self.assertEqual(r.runtime.plan_feedback()['accepted']['contract_id'],original.id)
            self.assertIn('another plan',r.runtime.snapshot()['reason'])
            self.assertEqual(r.calls,[])
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
                    self.assertEqual(feedback['scope_revision'],plan_scope(r.options))
                    saved=r.store.session
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
            restarted.store.session=r.store.session
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
            from shs_core.presentation import controller_explanation
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
            from shs_core.runtime_json import Records
            raw=r.runtime.snapshot(include_evidence=True)
            # The journals are encoded when the file is written, not on the event loop.
            self.assertTrue(all(isinstance(raw[k],Records) for k in ('accounting_journal','command_journal','execution_traces')))
            dump=downloaded(raw)
            self.assertEqual(dump['execution_trace_retention']['retained'],len(dump['execution_traces']))
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
            self.assertEqual(dump['state'],'verified')
            self.assertEqual(dump['plan_status'],'rejected')
            self.assertEqual(dump['accepted_reference_id'],original.id)
            self.assertEqual(dump['plan_rejection']['contract_id'],'replacement')
            self.assertIn('explicit retained amendment',dump['plan_rejection']['reason'])
            self.assertIn('previously accepted',dump['display']['plan_warning'])
            self.assertIn('Previously accepted plan',dump['explanation']['plan'])
            self.assertNotIn('Continue with the current plan',dump['explanation']['next'])
            saved=r.store.session
            self.assertEqual(saved.plan_rejection.contract_id,'replacement')
            self.assertEqual(dump['fix'],{'kind':'diagnostics'})
            await r.runtime._replan_task
            self.assertEqual(r.runtime_reports[-1]['accepted']['contract_id'],original.id)
            self.assertEqual(r.runtime_reports[-1]['rejection']['contract_id'],'replacement')
            self.assertFalse(r.replans)
            await r.runtime.close()
            restarted=Rig('control_verification');restarted.now=r.now+1000
            for row in restarted.rows.values():row['last_reported']=iso(restarted.now)
            restarted.runtime.store=r.store
            r=restarted
            await asyncio.wait_for(r.start(),3)
            self.assertEqual(r.runtime.snapshot()['plan_rejection'],dump['plan_rejection'])
            self.assertEqual(r.runtime.snapshot()['state'],'verified')
            revised=replace(revised,dispositions=(Disposition(original.objectives[0].id,'retained',None,'Amend target'),))
            r.plan['battery_execution']=contract_wire(revised)
            await r.advance()
            dump=r.runtime.snapshot()
            self.assertEqual(dump['state'],'verified',dump)
            self.assertEqual(dump['plan_status'],'accepted')
            self.assertIsNone(dump['plan_rejection'])
            self.assertFalse(dump['display']['plan_warning'])
            self.assertEqual(r.calls,[])
            await r.runtime._replan_task
            self.assertEqual(r.runtime_reports[-1]['accepted']['contract_id'],'replacement')
            self.assertIsNone(r.runtime_reports[-1]['rejection'])
            self.assertFalse(r.replans)
        finally:await r.runtime.close()

    async def test_pre_cache_rejection_survives_bootstrap_restart_and_valid_acceptance(self):
        r=Rig('control_verification')
        await r.runtime.reject_plan_response(r.plan,'Battery plan belongs to a superseded request')
        dump=r.runtime.snapshot()
        self.assertEqual(dump['plan_status'],'rejected')
        self.assertIn('no accepted battery plan',dump['display']['plan_warning'])
        restarted=Rig('control_verification')
        restarted.runtime.store=r.store
        restarted.store.session=r.store.session
        try:
            await restarted.runtime.load()
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
            restarted.store.session=r.store.session
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

class MeasurementWarningTests(unittest.IsolatedAsyncioTestCase):
    async def test_disagreeing_device_meter_warns_without_stopping_and_recovers(self):
        r=Rig();r.add_pool();r.rows['sensor.pool']['state']='3000'
        r.plan['battery_supply_scope']={'kind':'selected','include_base':True,'planned_device_keys':[]}
        await r.start();self.addAsyncCleanup(r.runtime.close)
        for _ in range(2):await r.advance(5000)
        self.assertTrue(r.runtime.snapshot()['planning_warnings'])
        self.assertTrue(r.runtime.host.state.groups[0].owned)
        before=r.runtime.host.state.groups[0].release_pending
        r.rows['sensor.pool']['state']='500'
        await r.advance(5000)
        self.assertFalse(r.runtime.snapshot()['planning_warnings'])
        self.assertEqual(r.runtime.host.state.groups[0].release_pending,before)
