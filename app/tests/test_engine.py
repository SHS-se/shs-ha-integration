"""Real cold import, app composition and gateway activation without a cloud call."""
import asyncio
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.append(str(Path(__file__).parents[2]/'tests'))
import test_migration_export as export_fixtures
from shs_app.migration_import import import_export
from shs_app.gateway_seed import seed_gateway
from shs_app.engine import AppEngine
from shs_core.const import CONF_BASE_URL, CONF_DEVICE_TOKEN
from shs_core.gateway_journal import GatewayJournal
from shs_core.gateway_service import GatewayService, AppConnection
from shs_core.gateway_stream import GatewayStream, GatewayRecord, GatewayOperations, GatewayCommands
from shs_core.device_gateway import DeviceGateway
from shs_core.battery_gateway import BatteryGateway
from shs_core.controller_inputs import ControllerInputs
from shs_core.command_transport import CommandTransport
from shs_core.native_commands import NativeExecutor
from shs_core.configuration_schema import resolve_configuration
from shs_core.command_journal import CommandJournal
from shs_core.household_ports import HomeFacts
from shs_core.execution_configuration import ExecutionConfiguration


class InProcessClient:
    service = None
    def __init__(self,session,url,token,identity,inbox,paired_release=None):
        self.identity,self.inbox = identity,inbox
        self.peer = AppConnection(self.service)
        self.connected = None
        self.number = 0
    async def call(self,operation,body):
        self.number += 1
        return (await self.peer.request({'id':self.number,'operation':operation,'body':body}))['result']
    async def connect(self):
        self.connected = await self.call('connect',{'identity':self.identity,'instance':'app'})
    async def project(self,value):return await self.call('projection',{'value':value})
    async def snapshot(self):return await self.call('snapshot',{})
    async def receive(self):
        page = await self.call('receipts',{'after':self.inbox.through(),'limit':256})
        through = self.inbox.receive(page)
        await self.call('ack_delivery',{'through':through})
        return page
    async def close(self):
        self.connected = None
        await self.peer.close()


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await export_fixtures.ExportTests.asyncSetUp(self)
        self.entry['data'] = {CONF_BASE_URL:'https://example.invalid',CONF_DEVICE_TOKEN:'fixture-secret'}
        self.entry['options'] = {'planning_mode':'off'}
        (self.storage/'core.config_entries').write_text(json.dumps({'data':{'entries':[self.entry]}}))
        writer = self.storage/'shs_energy.battery_writer.entry'
        writer.write_text(json.dumps({'version':1,'key':writer.name,'data':dict(schema='battery-writer-v1',owner='legacy',epoch=0,surfaces=[])}))
        exported = export_fixtures.ExportTests.capture(self)
        self.pair = dict(protocol=2,app_version='app',integration_version='companion',core_sha256='a'*64)
        self.imported = import_export(exported,self.root/'runtime',source_release='preparation',pair=self.pair)
        gateway_path = seed_gateway(self.imported,self.storage,require_core_stopped=lambda:None)
        self.gateway_journal = GatewayJournal(gateway_path).open()
        self.addCleanup(self.gateway_journal.close)
        self.stream = GatewayStream(self.gateway_journal)
        self.stream.start()
        self.addAsyncCleanup(self.stream.close)
        proof = json.loads((self.imported/'import.json').read_bytes())
        identity = {key:proof[key] for key in ('entry_id','migration_id','export_sha256','pair')}
        self.native_calls = []
        async def send(*args):self.native_calls.append(args)
        async def request(operation,body):
            if operation=='credentials':return self.entry['data']
            if operation=='catalog':return dict(context=self.context,states={},preferences={})
            if operation in ('states','statistics'):return {}
            if operation == 'configure': return await self.configuration.install(body)
            raise AssertionError('unexpected source request '+operation)
        source = SimpleNamespace(options=lambda:resolve_configuration(self.entry['options'],59,18),
            physical_controls=lambda:{},request=request,publish=lambda value:None)
        self.service = GatewayService(self.stream,identity,source)
        self.context = dict(home=dict(latitude=59,longitude=18,language='en',timezone='Europe/Stockholm',temperature_unit='°C'),
            entity_ids=[],entity_names={},area_names={},entity_areas={},platforms={})
        async def capture():
            await self.service.configuration_changed(dict(self.context,options=self.configuration.options(),
                configuration_authority={k:self.configuration.value[k] for k in ('revision','digest')}))
        self.configuration = ExecutionConfiguration(GatewayRecord(self.stream,'execution_configuration'),
            self.service.invalidate_configuration,capture)
        await self.configuration.load(self.entry['options'])
        source.options = lambda:resolve_configuration(self.configuration.options(),59,18)
        native_journal = GatewayCommands(self.stream)
        native = NativeExecutor(CommandTransport(native_journal,native_journal.execute),lambda e:None,lambda:'°C',send)
        self.service.physical = DeviceGateway(ControllerInputs(lambda e:None,lambda:'°C',lambda e:None),source.options,
            GatewayRecord(self.stream,'ownership'),native,authorize=self.service.authorize_device,before_external=lambda device:True,
            operations=GatewayOperations(self.stream,self.service.session))
        self.service.battery = BatteryGateway(self.stream,self.service.physical,source.options,lambda e:None,
            lambda:int(datetime.now(timezone.utc).timestamp()*1000),authorize=self.service.authorize,
            session=self.service.session,context=self.service.context)
        await self.service.physical.load()
        await self.service.battery.open()
        await capture()
        InProcessClient.service = self.service
        self.patcher = patch('shs_app.engine.GatewayClient',InProcessClient)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.published = []

    def engine(self):
        engine = AppEngine(self.imported,object(),'fixture','fixture',paired_release=self.pair,publish=self.published.append)
        self.addAsyncCleanup(engine.close)
        return engine

    async def test_dormant_load_activates_once_and_restart_resumes_app_stores(self):
        engine = self.engine()
        await engine.load()
        self.assertFalse(self.service.active)
        self.assertFalse(engine.started)
        self.assertFalse(self.native_calls)
        self.assertIsNone(engine.battery.host)
        await engine.activate()
        self.assertTrue(self.service.active)
        self.assertTrue(engine.started)
        await engine.project()
        self.assertEqual(self.published[-1]['schema'],1)
        first = engine.activation['activation_id']
        consumed = engine.battery._processing['receipt']
        await engine.close()
        self.assertEqual(CommandJournal(self.journal_path).state()['owner'],'fenced')
        replacement = self.engine()
        replacement.paired_release = dict(self.pair,app_version='app-fix',integration_version='transport-fix',core_sha256='b'*64)
        await replacement.load()
        await replacement.activate()
        self.assertEqual(replacement.activation['activation_id'],first)
        self.assertGreater(replacement.battery._processing['receipt'],consumed)
        self.assertFalse(self.native_calls)

    async def test_controlling_runtime_uses_remote_admission_and_durable_native_gateway(self):
        from test_battery_runtime import Rig
        from shs_app.physical_ports import RemoteBattery
        rig = Rig()
        # Use the same finite native rows on opposite sides of the wire codec.
        self.entry['options'] = rig.options
        self.service.source.options = lambda:deepcopy(rig.options)
        self.service.battery.options = self.service.source.options
        self.service.battery.reports = lambda entity:rig.rows.get(entity)
        self.service.battery.now = lambda:rig.now
        self.service.battery.fence._now_ms = lambda:rig.now
        native = self.service.physical.native_executor
        native.read_state = lambda entity:SimpleNamespace(state=rig.rows[entity]['state'],attributes=rig.rows[entity]['attributes'])
        async def send(domain, action, data):
            self.native_calls.append((domain,action,data))
            rig.rows[data['entity_id']]['state'] = str(data.get('value',data.get('option')))
        native.send = send
        peer = AppConnection(self.service)
        number = 0
        async def call(operation,body):
            nonlocal number
            number += 1
            return (await peer.request(dict(id=number,operation=operation,body=body)))['result']
        await call('connect',dict(identity=self.service.identity,instance='battery-runtime'))
        proof = (await call('reconcile',dict(checkpoint_sha256='a'*64)))['reconciliation']['proof']
        page = await call('receipts',dict(after=0,limit=256))
        await call('ack_delivery',dict(through=page['through']))
        await call('activate',dict(activation_id='battery-test',proof=proof))
        bridge = SimpleNamespace(call=call,connected=True)
        writer = RemoteBattery(bridge,rig.runtime,lambda:rig.now)
        writer.enabled = True
        rig.coordinator.battery_writer = writer
        rig.runtime.physical = writer
        self.addAsyncCleanup(rig.runtime.close,release=False)
        await rig.runtime.open()
        await rig.runtime.refresh()
        await rig.runtime.host.idle()
        self.assertTrue(self.native_calls, str(rig.runtime.snapshot()))
        self.assertTrue(self.service.battery.fence.snapshot()['grant_current'])
        self.assertTrue(rig.store.writes)
        with closing(self.gateway_journal.connect(readonly=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM commands WHERE status='service_returned'").fetchone()[0],len(self.native_calls))
        peer.disconnected()
        self.assertFalse(self.service.battery.fence.snapshot()['grant_current'])
        await peer.close()

    async def test_wrong_app_pair_cannot_open_or_activate(self):
        engine = self.engine()
        engine.paired_release = dict(self.pair,app_version='wrong')
        with self.assertRaisesRegex(ValueError,'binary differs'):
            await engine.load()
        self.assertFalse(self.service.active)
        self.assertFalse(self.native_calls)
