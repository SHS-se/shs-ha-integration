"""Synthetic reducer/remote port/gateway recovery with real durable native I/O."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / 'app'))
import test_battery_gateway as gateway_fixtures
from test_home_runtime import Harness, controls
from shs_app.physical_ports import RemoteBattery
from shs_core import home_runtime as rt
from shs_core.home_host import DispatchRejected
from shs_core.runtime_json import decode_value


class NativeRecoveryCompositionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = gateway_fixtures.BatteryGatewayTests.asyncSetUp
    send = gateway_fixtures.BatteryGatewayTests.send
    step = gateway_fixtures.BatteryGatewayTests.step

    async def prepare_port(self):
        gateway = self.gateway
        class Port:
            connected = True
            async def call(self, operation, body):
                if operation == 'battery_route':
                    return await gateway.propose(body['effect'], body['grant'])
                return await gateway.step(body['route_id'], body['index'], body['send'])
        self.remote = RemoteBattery(Port(), self.rig.runtime, lambda:self.rig.now,context=self.gateway.context)
        self.remote.grant = self.grant
        self.remote.enabled = True
        self.remote.is_current = lambda *_: True
        await self.remote.propose(self.effect)

    async def test_definite_unsent_attempt_can_retry_same_logical_step(self):
        await self.prepare_port()
        original = self.physical.native_executor.read_state
        self.physical.native_executor.read_state = lambda _entity:None
        first = decode_value(self.send(), rt.Send)
        with self.assertRaises(DispatchRejected):
            await self.remote.dispatch(first, lambda:None)
        self.physical.native_executor.read_state = original
        await self.remote.dispatch(replace(first, attempt_id='synthetic-retry'), lambda:None)
        self.assertEqual(len(self.calls), 1)
        # Redelivery of this very attempt remains idempotent.
        await self.remote.dispatch(replace(first, attempt_id='synthetic-retry'), lambda:None)
        self.assertEqual(len(self.calls), 1)

    async def test_physical_settlement_advances_route_after_lost_service_result(self):
        await self.prepare_port()
        # The real gateway completed the first assignment; its RPC result never
        # reached RemoteBattery. The reducer has independently reconciled it.
        self.assertEqual((await self.step())['status'], 'service_returned')
        next_send = decode_value(self.send(1), rt.Send)
        await self.remote.dispatch(next_send, lambda:None)
        self.assertEqual(len(self.calls), 2)

    async def test_out_of_order_admission_does_not_fault_native_transport(self):
        self.assertEqual((await self.step(1))['status'],'not_sent')
        self.assertEqual((await self.step(0))['status'],'service_returned')
        self.assertEqual(len(self.calls),1)

    async def test_real_native_boundary_preserves_sigen_truncation_safe_wire_scalar(self):
        from shs_core.command_journal import Command
        self.rows['number.charge']['attributes']['max'] = 10
        step = replace(self.proposal.steps[0], key='number.charge', value=8184)
        action = self.gateway.action(step,self.options)
        await self.physical.native_executor.execute(Command('synthetic-8184','battery','battery','plan',action),
            authorize=lambda:None,timeout=1)
        wire = self.calls[-1][2]['value']
        self.assertEqual(int(wire*1000),8184)
        self.assertNotEqual(wire,8184/1000)


class ReliefObservationTests(unittest.TestCase):
    def test_power_only_change_rebinds_a_supported_sigen_relief(self):
        from shs_core.battery_native_adapter import SigenAdapter
        from test_home_runtime_execution import Harness as PolicyHarness, native
        policy = PolicyHarness()
        adapter = SigenAdapter(policy.authority.catalog, policy.authority.plant.conversion)
        h = Harness(limit=1000)
        h.state = replace(h.state, groups=(replace(h.group(), spec=replace(h.group().spec,
            adapter_revision=adapter.catalog.adapter_revision)),))
        before = native('Command Charging (PV First)', 2000)
        h.observe(target=before, envelope=rt.Envelope(2001, 0))
        h.request(target=native('Command Charging (PV First)', 0))
        effect = next(e for e in h.effects if isinstance(e, rt.NeedTransition))
        proposal = adapter.propose(effect)
        h.now += 1
        h.observe(envelope=rt.Envelope(2002, 0))
        h.event(proposal)
        self.assertEqual(h.group().status, 'awaiting_durability')
        h.now += 1
        h.observe(envelope=rt.Envelope(2003, 0))
        self.assertIsNotNone(h.durable())


if __name__ == '__main__':
    unittest.main()


class ObservedRouteCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import test_gateway_metadata as fixtures
        await fixtures.MetadataTests.asyncSetUp(self)

    async def test_old_capacity_effect_is_migrated_from_admitted_native_evidence(self):
        from copy import deepcopy
        from types import SimpleNamespace
        from shs_core.gateway_journal import GatewayJournal
        self.source.report=self.rows.get
        executor=self.physical.native_executor
        executor.capacity=self.source
        executor.transport.journal.capacity=self.source
        executor.read_state=lambda entity:SimpleNamespace(**self.rows[entity])
        from unittest.mock import patch
        finished_at=self.rig.now+1000
        with patch('shs_core.gateway_journal.now_ms',return_value=finished_at):
            result=await self.gateway.step(self.route['route_id'],0,gateway_fixtures.BatteryGatewayTests.send(self))
        self.assertEqual(result['status'],'service_returned')
        old=deepcopy(self.source.capacity);old['schema']=1;old.pop('receipts')
        for row in old['devices'].values():
            for effect in row['effects'].values():
                for key in ('native_model','latest_effect_ms','route_id','step_index'):effect.pop(key,None)
        await self.stream.call('save_record','grid_capacity',old)
        await self.stream.close();self.journal.close()
        reopened=GatewayJournal(self.journal.path).open()
        try:
            migrated=reopened.load_record('grid_capacity')
            effect=next(iter(next(iter(migrated['devices'].values()))['effects'].values()))
            self.assertEqual(migrated['schema'],2)
            self.assertEqual(effect['latest_effect_ms'],finished_at+self.proposal.steps[0].latest_effect_delay_ms)
            self.assertEqual(effect['native_model']['charge'],self.catalog.charge_key)
            self.assertEqual(reopened.command_outcome(result['command_id'])['status'],'service_returned')
        finally:reopened.close()

    async def test_reducer_and_gateway_advance_together_after_lost_outcome(self):
        from copy import deepcopy
        from types import SimpleNamespace
        from unittest.mock import patch
        from shs_core.battery_native_adapter import SigenAdapter
        from shs_core.command_transport import CommandUncertain
        from shs_core.runtime_json import encode_value
        self.source.report = self.rows.get
        executor = self.physical.native_executor
        executor.capacity = self.source
        executor.transport.journal.capacity = self.source
        executor.read_state = lambda entity:SimpleNamespace(**self.rows[entity])
        keys=(self.catalog.mode_key,self.catalog.charge_key,self.catalog.discharge_key)
        surface=self.gateway.surface(self.options)['readback']
        before=tuple(zip(keys,(surface['mode'],surface['charge_limit_w'],surface['discharge_limit_w'])))
        adapter=SigenAdapter(self.catalog,self.conversion)
        target=tuple(zip(keys,('Command Discharging (PV First)',0,1000)))
        self.state=rt.create_home((rt.GroupSpec('battery',self.catalog.adapter_revision,keys,
            rt.Envelope(100000,100000),writer=self.gateway.identity()),),rt.Limits(10000,30,120))
        def event(value):
            self.state,effects=rt.reduce_home(self.state,value,self.rig.now)
            return effects
        event(rt.FrameObserved(rt.Frame(1,self.rig.now,self.rig.now+500000,rt.Envelope(0,0),rt.Envelope(100000,100000))))
        event(rt.Observed('battery',rt.Observation(1,self.rig.now,self.rig.now+500000,before,(),adapter.envelope(before))))
        event(rt.AuthorityChanged('battery','controlling',1,rt.Request('release',1,self.rig.now+500000,before,())))
        event(rt.GrantConfirmed('battery',self.grant))
        effects=event(rt.Requested('battery',1,rt.Request('synthetic-route',1,self.rig.now+500000,target,())))
        effect=next(value for value in effects if isinstance(value,rt.NeedTransition))
        gateway=self.gateway
        class Port:
            connected=True
            async def call(self,operation,body):
                if operation=='battery_route':return await gateway.propose(body['effect'],body['grant'])
                return await gateway.step(body['route_id'],body['index'],body['send'])
        remote=RemoteBattery(Port(),self.rig.runtime,lambda:self.rig.now,context=self.gateway.context)
        remote.grant=self.grant;remote.enabled=True;remote.is_current=lambda *_:True
        proposal=await remote.propose(effect)
        event(proposal)
        first=next(value for value in event(rt.JournalDurable(self.state.revision)) if isinstance(value,rt.Send))
        async def record(entity):
            row=deepcopy(self.rows[entity]);row['entity_id']=entity
            await self.stream.capture('observation',row)
        for entity in (*keys,self.options['battery_power_measurement_entity']):await record(entity)
        failed=True
        async def send(domain,service,data):
            nonlocal failed
            self.calls.append((domain,service,data))
            entity=data['entity_id'];self.rows[entity]['state']=str(data.get('value',data.get('option')))
            await record(entity)
            if failed:
                failed=False
                raise ConnectionError('synthetic lost service outcome')
        executor.send=send
        with self.assertRaises(CommandUncertain):await remote.dispatch(first,lambda:None)
        event(rt.TransportResult('battery',first.attempt_id,'ambiguous','synthetic lost reply'))
        route_id=next(iter(remote.routes.values()))[0]
        row=await self.stream.call('read_route',self.session,route_id)
        self.assertEqual((row['next_step'],row['status']),(0,'uncertain'))
        self.rig.now=first.send_by_ms+proposal.steps[0].latest_effect_delay_ms+1
        # Persistence latency cannot turn a pre-bound callback into new
        # post-bound physical evidence.
        with patch('shs_core.gateway_stream.now_ms',return_value=self.rig.now-2), \
                patch('shs_core.gateway_journal.now_ms',return_value=self.rig.now):
            await record(first.key)
            await record(self.options['battery_power_measurement_entity'])
        row=await self.stream.call('read_route',self.session,route_id)
        self.assertEqual(row['next_step'],0)
        with patch('shs_core.gateway_stream.now_ms',return_value=self.rig.now):
            await record(first.key)
            await record(self.options['battery_power_measurement_entity'])
        row=await self.stream.call('read_route',self.session,route_id)
        self.assertEqual((row['next_step'],row['status']),(1,'active'))
        after=proposal.steps[0].after
        event(rt.Observed('battery',rt.Observation(2,self.rig.now,self.rig.now+500000,after,(),adapter.envelope(after))))
        effects=event(rt.JournalDurable(self.state.revision))
        self.assertTrue(any(isinstance(value,rt.Send) for value in effects),repr((self.state.groups[0],effects)))
        second=next(value for value in effects if isinstance(value,rt.Send))
        self.assertEqual(second.step_index,1)
        await remote.dispatch(second,lambda:None)
        self.assertEqual(len(self.calls),2)
        outcome=await self.stream.call('command_outcome','route:'+route_id+':0:'+first.attempt_id)
        self.assertEqual(outcome['status'],'uncertain')
