"""Activation and encoded device operations through the final session service."""
import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import tempfile
from types import SimpleNamespace
import unittest

import test_controller as fixtures
from command_fixture import controller_inputs
from gateway_fixture import IDENTITY, seed
from shs_core.command_transport import CommandTransport
from shs_core.device_gateway import DeviceGateway
from shs_core.gateway_journal import GatewayConflict, digest
from shs_core.gateway_service import GatewayService, AppConnection
from shs_core.gateway_stream import GatewayStream, GatewayRecord, GatewayCommands, GatewayOperations
from shs_core.native_commands import NativeExecutor


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixtures.ControllerTests.setUp(self)
        self.options['device_modes']['$pool'] = 'controlling'
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        _,self.journal = seed(self.temp.name)
        self.journal.open()
        self.addCleanup(self.journal.close)
        self.stream = GatewayStream(self.journal)
        self.stream.start()
        self.addAsyncCleanup(self.stream.close)
        self.source = SimpleNamespace(options=self.controller.options,
            physical_controls=lambda:{'switch.pool':self.states['switch.pool'].state},publish=__import__("unittest.mock",fromlist=["AsyncMock"]).AsyncMock())
        self.service = GatewayService(self.stream,IDENTITY,self.source)
        self.service.battery = SimpleNamespace(revoke=lambda:None)
        commands = GatewayCommands(self.stream)
        original = self.controller.native_executor
        async def send(domain, action, data):
            self.assertTrue(self.journal.load_record('ownership')['records'])
            with closing(self.journal.connect(readonly=True)) as db:
                self.assertTrue(db.execute("SELECT 1 FROM commands WHERE status='prepared'").fetchone())
            entity = data['entity_id']
            self.states[entity].state = 'on' if action=='turn_on' else 'off'
            self.calls.append((entity,self.states[entity].state))
        native = NativeExecutor(CommandTransport(commands,commands.execute),original.read_state,original.temperature_unit,send)
        self.service.physical = DeviceGateway(controller_inputs(self.hass),self.controller.options,
            GatewayRecord(self.stream,'ownership'),native,authorize=self.service.authorize_device,
            before_external=lambda device:True,operations=GatewayOperations(self.stream,self.service.session))
        await self.service.physical.load()
        await self.service.configuration_changed({'options':self.controller.options()})
        self.peer = AppConnection(self.service)
        self.counter = 0
        await self.call('connect',{'identity':IDENTITY,'instance':'app'})

    async def call(self,op,body=None,peer=None):
        self.counter += 1
        return (await (peer or self.peer).request({'id':self.counter,'operation':op,'body':body or {}}))['result']

    async def activate(self):
        result = await self.call('reconcile',{'checkpoint_sha256':'a'*64})
        page = await self.call('receipts',{'after':0,'limit':4096})
        await self.call('ack_delivery',{'through':page['through']})
        await self.call('activate',{'activation_id':'activation','proof':result['reconciliation']['proof']})
        await self.call('policy',{'value':{'plan':'plan','options':digest(__import__('shs_core.native_configuration',fromlist=['native_options']).native_options(self.controller.options()))}})
        await self.call('synchronize',{'models':self.coordinator.optimisation_plan['device_models']})

    def intention(self):
        return dict(request_id='pool-operation',device='pool',operation='apply',headroom_reserved=True,
            configuration_revision=self.service.configuration_revision,policy_revision=self.service.policy_revision,
            parameters=dict(start=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),heating_w=2000,
                stop_temperature_c=30,models=self.coordinator.optimisation_plan['device_models']))

    async def test_dormant_socket_cannot_dispatch_then_active_semantic_operation_is_durable(self):
        with self.assertRaises(GatewayConflict):await self.call('device',{'intent':self.intention()})
        self.assertFalse(self.calls)
        await self.activate()
        intent = self.intention()
        response = await self.call('device',{'intent':intent})
        self.assertIsNone(response['error'])
        calls = list(self.calls)
        self.assertTrue(calls)
        repeated = await self.call('device',{'intent':intent})
        self.assertEqual(response,repeated)
        self.assertEqual(calls,self.calls)

    async def test_reconciliation_rejects_changed_physical_state_before_activation(self):
        result = await self.call('reconcile',{'checkpoint_sha256':'a'*64})
        page = await self.call('receipts',{'after':0,'limit':4096})
        await self.call('ack_delivery',{'through':page['through']})
        self.states['switch.pool'].state = 'on'
        with self.assertRaisesRegex(GatewayConflict,'reconciliation changed'):
            await self.call('activate',{'activation_id':'activation','proof':result['reconciliation']['proof']})
        self.assertFalse(self.service.active)

    async def test_disconnect_revokes_before_any_disk_await(self):
        await self.activate()
        self.peer.disconnected()
        self.assertFalse(self.service.active)
        with self.assertRaises(GatewayConflict):await self.call('device',{'intent':self.intention()})
        self.assertFalse(self.calls)

    async def test_replacement_requires_new_reconciliation_and_old_close_cannot_revoke_it(self):
        await self.activate()
        old = self.peer
        new = self.peer = AppConnection(self.service)
        await self.call('connect',{'identity':IDENTITY,'instance':'new'})
        self.assertFalse(self.service.active)
        result = await self.call('reconcile',{'checkpoint_sha256':'b'*64})
        page = await self.call('receipts',{'after':0,'limit':4096})
        await self.call('ack_delivery',{'through':page['through']})
        await self.call('resume',{'reconciliation_id':result['reconciliation']['id']})
        await old.close()
        self.assertTrue(self.service.active)
        await self.call('snapshot')

    async def test_slow_source_read_does_not_block_receipt_delivery(self):
        started,finish = asyncio.Event(),asyncio.Event()
        async def source(operation,body):
            started.set()
            await finish.wait()
            return {'rows':[]}
        self.source.request = source
        task = asyncio.create_task(self.call('source',{'operation':'statistics','body':{}}))
        await started.wait()
        result = await asyncio.wait_for(self.call('snapshot'),1)
        self.assertIn('through',result)
        finish.set()
        self.assertEqual(await task,{'rows':[]})


class ConfiguredObservationTests(unittest.TestCase):
    def test_top_level_controls_meters_quantities_forecasts_and_mappings(self):
        from shs_core.controller_inputs import configured_entity_ids
        options = {'battery_mode_entity':'select.mode','battery_charge_limit_entity':'number.charge',
            'battery_discharge_limit_entity':'number.discharge','battery_capacity_kwh':'sensor.capacity',
            'entities_grid_import':['sensor.import'],'pv_forecast_entities':['sensor.forecast'],
            'device_control_mappings':{'sensor.appliance':{'power_entity_id':'sensor.power','actuator_entity_ids':['switch.appliance']}},
            'device_modes':{'$battery':'controlling'},'numeric':3,'label':'A readable name'}
        self.assertEqual(configured_entity_ids(options),{'select.mode','number.charge','number.discharge',
            'sensor.capacity','sensor.import','sensor.forecast','sensor.appliance','sensor.power','switch.appliance'})
