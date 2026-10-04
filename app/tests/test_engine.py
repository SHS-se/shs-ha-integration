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
from shs_wire.protocol import offer


class InProcessClient:
    service = None
    def __init__(self,session,url,token,identity,inbox,paired_release=None,profiler=None):
        self.profiler = profiler
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


class LoopbackSocket:
    """HA's WebSocket envelope around the real gateway service, with a busy journal."""
    def __init__(self,service):
        self.peer = AppConnection(service)
        self.replies = asyncio.Queue()
        self.replies.put_nowait({'type':'auth_required'})
        self.busy = {}
        self.work = set()
    def hold(self,operation):
        self.busy[operation] = asyncio.Event()
        return self.busy[operation]
    async def receive_json(self, **kwargs):
        reply = await self.replies.get()
        if reply is None:raise TypeError('Received message 8:1000 is not str')
        return reply
    async def send_json(self,value, **kwargs):
        if value['type'] == 'auth':
            self.replies.put_nowait({'type':'auth_ok'})
            return
        task = asyncio.create_task(self.answer(value))
        self.work.add(task)
        task.add_done_callback(self.work.discard)
    async def answer(self,value):
        request = {key:value[key] for key in ('id','operation','body')}
        connecting = request['operation'] == 'connect'
        if connecting:
            request['body'] = {key:item for key,item in request['body'].items() if key != 'contract'}
        try:
            if request['operation'] in self.busy:
                await self.busy[request['operation']].wait()
            result = (await self.peer.request(request))['result']
            if connecting:result['contract'] = offer('companion')
            reply = dict(success=True,result=result)
        except (ValueError,KeyError,TypeError,RuntimeError) as error:
            stale = self.peer.closed or self.peer.service.connection is not self.peer
            reply = dict(success=False,error=dict(code='shs_gateway_stale' if stale else 'shs_gateway_rejected',message=str(error)))
        self.replies.put_nowait(dict(id=value['id'],type='result',**reply))
    async def close(self):
        self.peer.disconnected()
        await self.peer.close()
        self.replies.put_nowait(None)


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
            if operation == 'admission':
                from shs_core.source_admission import validate_bindings
                validate_bindings(body['bindings'])
                self.assertEqual(body['revision'],self.configuration.value['revision'])
                await self.stream.call('save_record','source_admission',body)
                return {}
            if operation == 'live':
                snapshot = await self.stream.call('snapshot',self.service.session())
                return dict(through=snapshot['through'],revision=0,
                            configuration_revision=self.configuration.value['revision'],rows={},counts={})
            raise AssertionError('unexpected source request '+operation)
        source = SimpleNamespace(options=lambda:resolve_configuration(self.entry['options'],59,18),
            physical_controls=lambda:{},request=request,publish=__import__("unittest.mock",fromlist=["AsyncMock"]).AsyncMock())
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

    async def test_sensor_projection_shares_calculations_only_within_one_pass(self):
        from contextlib import ExitStack
        from unittest.mock import PropertyMock
        from shs_app.entities import EntityContext, project_entities
        engine = self.engine()
        await engine.load()
        household = engine.household
        household.latest_calculation = {'total_amount_sek':12}
        household.tariff_components = {
            key:{'label':key,'category':'fixed'} for key in ('one','two')}
        derived = {
            'grid_prices':{'import_price_sek_per_kwh':1,'export_price_sek_per_kwh':0.2},
            'total_price_forecast':[{'start':'2026-10-04T10:00:00Z',
                                    'import_price_sek_per_kwh':2,'export_price_sek_per_kwh':0.5}],
            'latest_display_components':[],
        }
        with ExitStack() as stack:
            calculations = {name:stack.enter_context(patch.object(type(household),name,
                new_callable=PropertyMock,return_value=value)) for name,value in derived.items()}
            with patch.object(EntityContext,'_shared_display_values',frozenset()):
                expected = project_entities(engine,[])
            self.assertEqual(calculations['total_price_forecast'].call_count,2)
            self.assertGreater(calculations['grid_prices'].call_count,1)
            self.assertGreater(calculations['latest_display_components'].call_count,1)
            for calculation in calculations.values():calculation.reset_mock()
            self.assertEqual(project_entities(engine,[]),expected)
            for calculation in calculations.values():calculation.assert_called_once_with()
            # No values, including empty results, survive into the next pass.
            calculations['grid_prices'].return_value = None
            calculations['total_price_forecast'].return_value = []
            refreshed = project_entities(engine,[])
            self.assertIsNone(refreshed['values']['entry_grid_import_price']['value'])
            self.assertEqual(refreshed['values']['entry_total_import_price']['attributes']['forecast'],[])
            for calculation in calculations.values():self.assertEqual(calculation.call_count,2)

    async def test_restart_acknowledges_delivery_even_without_new_receipts(self):
        from unittest.mock import AsyncMock
        engine=self.engine()
        engine.inbox=SimpleNamespace(through=lambda:42)
        engine.gateway=SimpleNamespace(receive=AsyncMock())
        await engine.receive_through(42, establish_delivery=True)
        engine.gateway.receive.assert_awaited_once()
        engine.inbox=None;engine.gateway=None

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
        self.assertEqual(engine.inbox.progress()['floor'],consumed)
        self.assertEqual(engine.battery.store.source_checkpoint['receipt'],consumed)
        await engine.close()
        self.assertEqual(CommandJournal(self.journal_path).state()['owner'],'fenced')
        replacement = self.engine()
        replacement.paired_release = dict(self.pair,app_version='app-fix',integration_version='transport-fix',core_sha256='b'*64)
        await replacement.load()
        await replacement.activate()
        self.assertEqual(replacement.activation['activation_id'],first)
        self.assertGreater(replacement.battery._processing['receipt'],consumed)
        self.assertFalse(self.native_calls)

    def battery_rig(self):
        from test_battery_runtime import Rig
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
        return rig

    async def remote_battery(self, rig):
        """The real multiplexed client and remote writer over HA's result envelope."""
        from shs_app.gateway_client import GatewayClient
        from shs_app.physical_ports import RemoteBattery
        socket = LoopbackSocket(self.service)
        async def ws_connect(*args,**kwargs):return socket
        client = GatewayClient(SimpleNamespace(ws_connect=ws_connect),'loopback','token',self.service.identity,None,
            paired_release={'app_version':'app'})
        self.addAsyncCleanup(client.close)
        await client.connect()
        proof = (await client.call('reconcile',dict(checkpoint_sha256='a'*64)))['reconciliation']['proof']
        page = await client.call('receipts',dict(after=0,limit=256))
        await client.call('ack_delivery',dict(through=page['through']))
        await client.call('activate',dict(activation_id='battery-test',proof=proof))
        writer = RemoteBattery(client,rig.runtime,lambda:rig.now)
        writer.enabled = True
        rig.coordinator.battery_writer = writer
        rig.runtime.physical = writer
        self.addAsyncCleanup(rig.runtime.close,release=False)
        return socket,client,writer

    async def test_slow_route_admission_keeps_the_session_and_the_battery_grant(self):
        # 3 October 2026: HA's journal answered a battery route after the host's
        # preparation budget. Abandoning that one call closed the whole socket,
        # so HA revoked the grant and handed the battery back every quarter hour.
        from dataclasses import replace
        from shs_core import battery_runtime
        rig = self.battery_rig()
        socket,client,writer = await self.remote_battery(rig)
        journal = socket.hold('battery_route')
        with patch.object(battery_runtime,'RUNTIME_LIMITS',replace(battery_runtime.RUNTIME_LIMITS,transition_timeout_ms=20)):
            await rig.runtime.open()
            await rig.runtime.refresh()
            await asyncio.wait_for(rig.runtime.host.idle(),2)
        failure = rig.runtime.host.state.groups[0].transition_work
        self.assertIn('timed out',failure.reason)
        self.assertFalse(self.native_calls)
        self.assertIsNotNone(client.socket)
        self.assertTrue(self.service.active)
        self.assertTrue(writer.is_current(writer.grant,rig.runtime.identity()))
        self.assertTrue(self.service.battery.fence.snapshot()['grant_current'])
        # HA answers after the app stopped waiting; the next preparation is admitted
        # on the same session and reaches the native controls.
        # The host copied the artificial 20 ms limit when it opened. Restore its
        # normal budget: an ordinary durable route need not finish within 20 ms.
        rig.runtime.host.state = replace(rig.runtime.host.state,limits=battery_runtime.RUNTIME_LIMITS)
        propose = self.service.battery.propose
        async def delayed_route(*args):
            await asyncio.sleep(.04)
            return await propose(*args)
        with patch.object(self.service.battery,'propose',delayed_route):
            journal.set()
            await rig.advance(failure.retry_at_ms-rig.now)
        self.assertTrue(self.native_calls,str(rig.runtime.snapshot()))
        self.assertIsNotNone(client.socket)
        self.assertEqual(client.pending,{})
        self.assertTrue(self.service.battery.fence.snapshot()['grant_current'])

    async def test_route_admission_may_outlast_a_second_while_its_observation_is_usable(self):
        rig = self.battery_rig()
        socket,client,writer = await self.remote_battery(rig)
        journal = socket.hold('battery_route')
        await rig.runtime.open()
        await rig.runtime.refresh()
        asyncio.get_running_loop().call_later(1.2,journal.set)
        await asyncio.wait_for(rig.runtime.host.idle(),10)
        self.assertTrue(self.native_calls,str(rig.runtime.snapshot()))
        self.assertFalse([row for row in rig.runtime._fault_history if 'timed out' in row['reason']])

    async def test_grant_revoked_by_ha_is_requested_again_without_waiting_for_its_expiry(self):
        # 3 October 2026: HA revoked the grant with the session still active, and
        # the app kept proposing with it until its own renewal almost fourteen
        # minutes later.
        rig = self.battery_rig()
        socket,client,writer = await self.remote_battery(rig)
        await rig.runtime.open()
        await rig.runtime.refresh()
        await asyncio.wait_for(rig.runtime.host.idle(),2)
        def settings():
            return (rig.rows['select.mode']['state'],float(rig.rows['number.charge']['state']),float(rig.rows['number.discharge']['state']))
        commanded = settings()
        self.assertEqual(commanded[0],'Command Charging (PV First)',str(rig.runtime.snapshot()))
        await rig.advance(5000)  # The commanded settings are observed before HA changes them.
        self.service.invalidate_configuration()
        self.service.configuration_pending = False  # The settings revision has been captured.
        await self.service.battery.maintain_obligation()
        self.assertEqual(settings(),commanded)  # A revoked writer is not a release.
        rig.rows['number.charge']['state'] = '1.0'  # A correction is due under the revoked grant.
        granted = writer.grant
        for _ in range(4):
            await rig.advance(5000)
        self.assertNotEqual(writer.grant,granted)
        self.assertTrue(self.service.battery.fence.snapshot()['grant_current'])
        self.assertEqual(settings(),commanded,str(rig.runtime.snapshot()))
        self.assertIsNotNone(client.socket)

    async def test_route_refused_while_ha_captures_metadata_keeps_the_grant_and_the_plan(self):
        # A renamed or new entity makes HA refuse commands until the app has the
        # new context. Nothing is handed back, and the standing grant is used again.
        rig = self.battery_rig()
        socket,client,writer = await self.remote_battery(rig)
        await rig.runtime.open()
        await rig.runtime.refresh()
        await asyncio.wait_for(rig.runtime.host.idle(),2)
        await rig.advance(5000)
        granted,epoch = writer.grant,self.service.battery.fence.snapshot()['epoch']
        self.service.invalidate_context()
        rig.rows['number.charge']['state'] = '1.0'  # A correction is due while HA is unsettled.
        calls = len(self.native_calls)
        await rig.advance(5000)
        await self.service.battery.maintain_obligation()
        self.assertEqual(len(self.native_calls),calls)
        self.assertIs(writer.grant,granted)
        self.assertFalse(rig.runtime._releasing)
        self.assertTrue(rig.runtime.snapshot()['writer_current'])
        self.service.configuration_pending = False
        await rig.advance(5000)
        self.assertEqual(float(rig.rows['number.charge']['state']),1.899,str(rig.runtime.snapshot()))
        self.assertEqual(rig.rows['select.mode']['state'],'Command Charging (PV First)')
        self.assertIs(writer.grant,granted)
        self.assertEqual(self.service.battery.fence.snapshot()['epoch'],epoch)

    async def test_lost_app_session_keeps_the_battery_settings(self):
        # An app restart, crash or dropped socket is not a release. HA fences the
        # writer and leaves the battery in the mode the controller last set.
        rig = self.battery_rig()
        socket,client,writer = await self.remote_battery(rig)
        await rig.runtime.open()
        await rig.runtime.refresh()
        await asyncio.wait_for(rig.runtime.host.idle(),2)
        await rig.advance(5000)
        commanded = {key:rig.rows[key]['state'] for key in ('select.mode','number.charge','number.discharge')}
        self.assertEqual(commanded['select.mode'],'Command Charging (PV First)')
        calls = len(self.native_calls)
        await client.close()
        self.assertFalse(self.service.active)
        self.assertFalse(self.service.battery.fence.snapshot()['grant_current'])
        for _ in range(3):
            rig.now += 600000
            await self.service.battery.maintain_obligation()
        self.assertEqual(len(self.native_calls),calls)
        self.assertEqual({key:rig.rows[key]['state'] for key in commanded},commanded)
        # Taking the battery out of Controlling is what hands it back, app or no app.
        rig.options['device_modes']['$battery'] = 'monitoring'
        await self.service.battery.maintain_obligation()
        self.assertEqual(rig.rows['select.mode']['state'],'Maximum Self Consumption')

    async def test_verification_hands_the_battery_back_through_the_gateway(self):
        # Leaving Controlling on the select is the release (docs/control-continuity.md).
        # Home Assistant must admit that handover although Verification writes no schedule.
        rig = self.battery_rig()
        socket,client,writer = await self.remote_battery(rig)
        await rig.runtime.open()
        await rig.runtime.refresh()
        await asyncio.wait_for(rig.runtime.host.idle(),2)
        await rig.advance(5000)
        self.assertEqual(rig.rows['select.mode']['state'],'Command Charging (PV First)')
        rig.options['device_modes']['$battery'] = 'control_verification'
        self.service.invalidate_configuration()
        self.service.configuration_pending = False  # The settings revision has been captured.
        for _ in range(12):
            await rig.advance(5000)
            if rig.runtime.host is None:
                break
        self.assertEqual((rig.rows['select.mode']['state'],float(rig.rows['number.charge']['state']),
            float(rig.rows['number.discharge']['state'])),('Maximum Self Consumption',4.0,4.0),str(rig.runtime.snapshot()))
        calls = len(self.native_calls)
        for _ in range(3):
            await rig.advance(5000)
        self.assertEqual(len(self.native_calls),calls)  # Verification itself writes nothing.
        self.assertIsNotNone(client.socket)

    async def test_controlling_runtime_uses_remote_admission_and_durable_native_gateway(self):
        from shs_app.physical_ports import RemoteBattery
        rig = self.battery_rig()
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
