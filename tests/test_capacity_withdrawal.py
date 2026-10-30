"""Synthetic support handover through real owner, transport, FIFO and SQLite."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import unittest
import test_grid_capacity as fixtures
from shs_core import native_records as rt
from shs_core.controller_inputs import ControllerInputs
from shs_core.device_gateway import DeviceGateway
from shs_core.gateway_journal import AdmissionPending
from shs_core.gateway_stream import GatewayRecord, GatewayOperations
from shs_core.grid_capacity import capacity_key


class WithdrawalCompositionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.LiveImportTests.asyncSetUp
    configure_ev = fixtures.LiveImportTests.configure_ev
    charging_car_at_minimum = fixtures.LiveImportTests.charging_car_at_minimum
    discharging = fixtures.LiveImportTests.discharging

    async def configure_owner(self, initial_base=5550):
        self.charging_car_at_minimum()
        self.base_w=5550
        self.discharging(3000,mode='Command Discharging (PV First)')
        self.rows['sensor.house']['state']=str(initial_base+3450)
        options = self.source.options()
        options.update(ev_connected_entity='binary_sensor.ev',ev_soc_entity='sensor.soc',ev_target_soc_entity='sensor.target')
        options['device_control_mappings']['ev'].update(minimum_value=5,maximum_value=16)
        self.source.configuration.options = lambda:deepcopy(options)
        self.rows.update({'binary_sensor.ev':{'state':'on','attributes':{}},
                          'sensor.soc':{'state':'30','attributes':{'unit_of_measurement':'%'}},
                          'sensor.target':{'state':'90','attributes':{'unit_of_measurement':'%'}}})
        def state(entity):
            row=self.rows.get(entity)
            if row is None:return None
            now=datetime.now(timezone.utc)
            return SimpleNamespace(state=row['state'],attributes=row['attributes'],last_updated=now,last_reported=now,last_changed=now,entity_id=entity)
        def authorize(intent):
            if intent.policy_revision != self.policy:
                raise AdmissionPending('Current policy required')
        self.service.authorize_device=authorize
        self.owner=DeviceGateway(ControllerInputs(state,lambda:'°C',lambda _:None),self.source.options,
            GatewayRecord(self.stream,'ownership'),self.physical.native_executor,authorize=authorize,
            before_external=self.source.external_ready,operations=GatewayOperations(self.stream,lambda:self.session),capacity=self.source)
        self.owner.lock=self.physical.lock
        self.service.physical=self.owner
        self.gateway.physical=self.owner
        await self.owner.load()
        async def send(domain,service,data):
            self.calls.append((domain,service,deepcopy(data)))
            entity=data['entity_id']
            value=data.get('value',service.removeprefix('turn_'))
            self.rows[entity]['state']=str(value)
            await self.record(entity)
            if entity in ('number.ev','switch.ev'):
                watts=float(self.rows['number.ev']['state'])*690 if self.rows['switch.ev']['state']=='on' else 0
                self.rows['sensor.ev']['state']=str(watts)
                self.rows['sensor.house']['state']=str(self.base_w+watts)
                await self.record('sensor.ev')
                await self.record('sensor.house')
        self.owner.native_executor.send=send
        self.value=dict(request_id='synthetic-admission',device='ev',operation='apply',configuration_revision=1,
            policy_revision=self.policy,headroom_reserved=True,parameters=dict(
                start=(datetime.now(timezone.utc)-timedelta(seconds=10)).isoformat(),current_a=16,minimum_a=0,maximum_a=16,models=[dict(key='ev',category='ev_charging',control_type='variable_power')]))
        for entity in ('switch.ev','number.ev','sensor.ev','sensor.house'):
            await self.record(entity)
        result=await self.owner.perform(self.value)
        self.assertIsNone(result['error'],result['error'])
        self.assertEqual(float(self.rows['number.ev']['state']),16)
        before=tuple((k, v if k=='select.mode' else float(self.rows[k]['state'])*1000)
            for k,v in (('select.mode',self.rows['select.mode']['state']),('number.charge',0),('number.discharge',0)))
        target=tuple((k,0 if k=='number.discharge' else v) for k,v in before)
        self.stop=replace(self.effect,request=replace(self.effect.request,target=target),command_controls=before)

    async def record(self, entity):
        row=deepcopy(self.rows[entity]);row['entity_id']=entity
        row.setdefault('last_reported','2026-10-01T00:00:00Z')
        await self.stream.capture('observation',row)

    async def test_dependent_ev_reduces_before_zero_and_cap_prevents_bounce(self):
        await self.configure_owner()
        baseline=deepcopy(self.owner.ownership.records['ev']['originals'])
        await self.source.coordinate_battery(self.stop,lambda:None)
        # 5,550 W base leaves 9,450 W for EV: 13 A is the largest native step.
        self.assertEqual(float(self.rows['number.ev']['state']),13)
        self.assertLessEqual(self.source.import_required(),15000)
        self.assertEqual(self.owner.ownership.records['ev']['originals'],baseline)
        replay=await self.owner.perform({**self.value,'request_id':'ordinary-scheduler'})
        self.assertIsNone(replay['error'])
        self.assertTrue(replay['result']['capacity_curtailed'])
        self.assertEqual(float(self.rows['number.ev']['state']),13)
        self.assertEqual(self.rows['number.discharge']['state'],'4')
        await self.stop_battery()
        self.assertEqual([(call[2]['entity_id'],call[2].get('value')) for call in self.calls],
                         [('number.ev',16),('number.ev',13),('number.discharge',0)])

    async def stop_battery(self):
        from shs_core.runtime_json import encode_value,decode_value
        self.gateway.coordinate=self.source.coordinate_battery
        route=await self.gateway.propose(encode_value(self.stop),encode_value(self.grant))
        proposal=decode_value(route['proposal'],rt.Proposed)
        step=proposal.steps[0]
        send=rt.Send(0, proposal.group_id, 'synthetic-battery-stop',step.key,step.value,self.rig.now+10000,
            self.grant,proposal.generation,proposal.request_id,proposal.request_revision)
        result=await self.gateway.step(route['route_id'],0,encode_value(send))
        self.assertEqual(result['status'],'service_returned')
        self.assertEqual(float(self.rows['number.discharge']['state']),0)

    async def test_new_physical_dependency_is_reconciled_even_if_original_admission_used_no_credit(self):
        await self.configure_owner(initial_base=1000)
        row=next(iter(self.source.capacity['devices'].values()))
        self.assertEqual(row['settled']['support_w'],0)
        await self.source.coordinate_battery(self.stop,lambda:None)
        self.assertEqual(float(self.rows['number.ev']['state']),13)

    async def test_new_feasible_intent_clears_scoped_curtailment_without_original_16a(self):
        await self.configure_owner()
        await self.source.coordinate_battery(self.stop,lambda:None)
        capped=await self.owner.perform({**self.value,'request_id':'curtailed'})
        self.assertEqual(capped['result']['execution_condition'],
            {'status':'temporarily_absent','cause':'house_import_capacity'})
        import test_execution_replan as delivery_fixtures
        delivery=delivery_fixtures.ExecutionReplanTests()
        delivery.setUp()
        slot={**delivery.slot,'start':self.value['parameters']['start'],'ev_target_current_a':16}
        await delivery.h.async_execution_observed('ev',delivery.plan,slot,capped['result']['execution_condition'])
        await delivery.h.async_optimisation_push()
        body=delivery.h.client.submit_optimisation.call_args.args[0]
        availability=body['snapshot']['execution_availability']
        self.assertEqual(len(availability),1)
        self.assertEqual(availability[0]['status'],'temporarily_absent')
        self.assertEqual(availability[0]['blocked_intervals'][0]['start'],slot['start'])
        feasible={**self.value,'request_id':'feasible','parameters':{**self.value['parameters'],'current_a':13}}
        healthy=await self.owner.perform(feasible)
        self.assertIsNone(healthy['error'])
        self.assertIsNone(healthy['result'].get('execution_condition'))
        self.assertEqual(self.source.capacity['coordination']['caps'],{})
        self.assertEqual(float(self.rows['number.ev']['state']),13)
        await delivery.h.async_execution_observed('ev',delivery.plan,{**slot,'ev_target_current_a':13},
            healthy['result'].get('execution_condition'))
        journal=delivery.rig.records.saved['execution_replanning']
        self.assertNotIn('$ev',journal['owners'])
        self.assertEqual(journal['pending']['$ev']['status'],'recovered')

    async def test_newest_protected_water_does_not_block_older_reducible_ev(self):
        await self.configure_owner()
        self.service.import_limit_w=18000
        options=self.source.options()
        options['device_control_mappings']['water']={'control_type':'permit_inhibit',
            'actuator_entity_ids':['switch.water'],'power':'sensor.water'}
        options['device_modes']['water']='controlling'
        self.source.configuration.options=lambda:deepcopy(options)
        self.source.power_peaks['sensor.water']=3000
        self.rows['switch.water']={'state':'off','attributes':{}}
        self.rows['sensor.water']={'state':'0','attributes':{'unit_of_measurement':'W','state_class':'measurement'},
            'last_reported':'2026-10-01T00:00:00Z'}
        await self.record('switch.water');await self.record('sensor.water')
        original=self.owner.native_executor.send
        async def send(domain,service,data):
            await original(domain,service,data)
            if data['entity_id']=='switch.water' and service=='turn_on':
                self.base_w=8550
                self.rows['sensor.water']['state']='3000'
                self.rows['sensor.house']['state']=str(self.base_w+float(self.rows['sensor.ev']['state']))
                await self.record('sensor.water');await self.record('sensor.house')
        self.owner.native_executor.send=send
        water={**self.value,'request_id':'newest-water','device':'device:water','parameters':{
            'start':self.value['parameters']['start'],'commands':{'water':{'type':'permit_inhibit','permitted':True}},
            'models':[{'key':'water','category':'hot_water','control_type':'permit_inhibit'}]}}
        result=await self.owner.perform(water)
        self.assertIsNone(result['error'],result['error'])
        await self.source.coordinate_battery(self.stop,lambda:None)
        self.assertEqual(self.rows['switch.water']['state'],'on')
        self.assertEqual(self.rows['sensor.water']['state'],'3000')
        self.assertEqual(float(self.rows['number.ev']['state']),13)
        self.assertLessEqual(self.source.import_required(),18000)
        self.assertFalse(any(call[1]=='turn_off' and call[2]['entity_id']=='switch.water' for call in self.calls))
        await self.stop_battery()
        self.assertEqual(self.rows['switch.water']['state'],'on')

    async def test_capacity_return_is_observable_without_an_executable_owner_intent(self):
        await self.configure_owner()
        await self.source.coordinate_battery(self.stop,lambda:None)
        self.assertEqual(await self.source.capacity_recoveries(),[])
        # The replacement plan may now suppress EV entirely; no perform call
        # is required to detect independent house capacity returning.
        count=len(self.calls)
        self.rows['sensor.house']['state']=str(1000+13*690)
        await self.record('sensor.house')
        snapshot=await self.owner.synchronize([])
        self.assertEqual(snapshot['capacity_recoveries'],['$ev'])
        self.assertEqual(len(self.calls),count)
        self.assertTrue(self.source.capacity['coordination']['caps'])

    async def test_new_policy_waits_for_current_owner_and_keeps_support_debt(self):
        await self.configure_owner()
        self.policy+=1
        count=len(self.calls)
        with self.assertRaises(AdmissionPending):
            await self.source.coordinate_battery(self.stop,lambda:None)
        self.assertEqual(len(self.calls),count)
        current={**self.value,'request_id':'new-policy','policy_revision':self.policy}
        result=await self.owner.perform(current)
        self.assertIsNone(result['error'])
        self.assertEqual(float(self.rows['number.ev']['state']),13)
        await self.source.coordinate_battery(self.stop,lambda:None)

    async def test_adopted_current_target_replaces_old_withdrawal_without_a_write(self):
        from shs_core.runtime_json import encode_value
        from shs_core.gateway_journal import GatewayConflict
        await self.configure_owner()
        await self.source.coordinate_battery(self.stop,lambda:None)
        retained=deepcopy(self.source.capacity['coordination']['caps'])
        self.assertEqual(self.source.discharge_credit(),0)
        request=replace(self.stop.request,id='battery:adopted',revision=self.stop.request.revision+1,
            target=self.stop.command_controls)
        context=self.gateway.context()
        calls=len(self.calls)
        from test_native_recovery_composition import RemoteBattery
        gateway=self.gateway
        class Port:
            connected=True
            async def call(self,operation,body):
                assert operation=='battery_target'
                return await gateway.publish_target(body['request'],body['grant'],body['context'])
        remote=RemoteBattery(Port(),self.rig.runtime,lambda:self.rig.now,context=lambda:context)
        remote.grant=self.grant
        remote.is_current=lambda *_:True
        await remote.publish_target(request)
        await self.source.refresh_capacity()
        self.assertGreater(self.source.discharge_credit(),0)
        self.assertEqual(self.source.capacity['coordination']['caps'],retained)
        self.assertEqual(len(self.calls),calls)
        revision=self.source.capacity['revision']
        await remote.publish_target(request)
        await self.source.refresh_capacity()
        self.assertEqual(self.source.capacity['revision'],revision)
        newer=replace(request,id='battery:newer',revision=request.revision+1)
        await remote.publish_target(newer)
        with self.assertRaisesRegex(GatewayConflict,'superseded'):
            await remote.publish_target(request)
        self.policy+=1
        with self.assertRaisesRegex(GatewayConflict,'context'):
            await self.gateway.publish_target(encode_value(request),encode_value(self.grant),context)
        self.assertEqual(len(self.calls),calls)

if __name__ == '__main__':unittest.main()
