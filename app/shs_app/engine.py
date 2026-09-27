"""App-owned household composition and explicit dormant-to-active migration."""
import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from uuid import uuid4

from shs_core.api import ShsApiClient
from .runtime import AppBatteryRuntime
from shs_core.command_journal import WriterLease
from shs_core.const import CONF_BASE_URL, CONF_DEVICE_TOKEN, PLAN_EXCHANGE_INTERVAL_MINUTES, OPTIMISATION_STARTUP_DELAY_SECONDS, PUSH_TIME_HOUR, PUSH_TIME_MINUTE
from shs_core.controller import ScheduledController
from shs_core.controller_inputs import ControllerInputs
from shs_core.controller_scheduler import ControllerScheduler
from shs_core.durable_record import DurableRecord
from shs_core.execution_storage import ExecutionStorage
from shs_core.gateway_journal import GatewayConflict, validate_identity
from shs_core.household import Household
from shs_core.household_ports import HouseholdPorts
from shs_core.receipt_inbox import ReceiptInbox
from shs_core.resource_profiling import process_resources
from shs_core.runtime_projection import runtime_projection
from .retention import RecentVerification
from shs_core.verification_storage import VerificationStorage

from .gateway_client import GatewayClient
from .migration_export import file_digest
from .migration_import import verify_import, regular_path
from .physical_ports import RemoteDevices, RemoteBattery
from .records import RecordStore
from .projection import display_plan
from .sources import ObservationMirror, RemoteHistory
from .upgrades import open_runtime_schema
from .configuration import Configuration
from .indexed_storage import IndexedStorage
from .configuration_editor import ConfigurationEditor


def encode(value):
    return json.dumps(value, separators=(',', ':'), allow_nan=False).encode()


class NoNativeServices:
    async def execute(self, *args, **kwargs):
        raise RuntimeError('The app cannot dispatch a native service; use its semantic physical port')


class OwnershipView:
    async def async_load(self):
        raise RuntimeError('Load physical ownership through the paired gateway')
    async def async_save(self, value):
        raise RuntimeError('Only HA can persist physical ownership')


class AppEngine:
    def __init__(self, root, session, url, token, *, paired_release, publish, app_url=None):
        self.root = regular_path(root)
        self.http, self.url, self.token = session, url, token
        self.paired_release, self.publish_ui = paired_release, publish
        self.app_url = app_url
        self.tasks = set()
        self.timers = set()
        self.wake_projection = asyncio.Event()
        self.closed = False
        self.lease = None
        self.inbox = None
        self.gateway = None
        self.household = None
        self.controller = None
        self.battery = None
        self.writer = None
        self.scheduler = None
        self.mirror = ObservationMirror()
        self.repairs = {}
        self.cached = {}
        self.started = False
        self.consume_lock = asyncio.Lock()
        self.configuration = None
        self.editor = ConfigurationEditor(self)
        self.record_stores = []

    def spawn(self, work, name='shs_app_work'):
        task = asyncio.create_task(work, name=name)
        self.tasks.add(task)
        def completed(task):
            self.tasks.discard(task)
            if not task.cancelled() and (error := task.exception()) is not None:
                logging.getLogger(__name__).error('App task %s failed: %s',task.get_name(),error)
        task.add_done_callback(completed)
        return task

    def at(self, when, action):
        loop = asyncio.get_running_loop()
        handle = None
        def run():
            self.timers.discard(handle)
            action()
        handle = loop.call_later(max(0,(when-datetime.now(timezone.utc)).total_seconds()),run)
        self.timers.add(handle)
        return handle.cancel

    def repair(self, key, severity, placeholders):
        self.repairs[key] = {'severity':severity, 'placeholders':deepcopy(placeholders)}
        self.wake_projection.set()

    async def load(self):
        """Open verified app stores and current sources, without control or cloud jobs."""
        self.lease = await asyncio.to_thread(WriterLease, self.root/'runtime.lock')
        self.proof = json.loads((self.root/'import.json').read_bytes())
        self.identity = validate_identity({key:self.proof[key] for key in ('entry_id','migration_id','export_sha256','pair')})
        self.marker = RecordStore(self.root/'activation.json')
        self.activation = await self.marker.async_load()
        self.schema = await open_runtime_schema(self.root, self.identity, self.activation, self.paired_release)
        if self.activation is None:
            await asyncio.to_thread(verify_import,self.root,self.proof)
        elif self.activation['identity'] != self.identity:
            raise GatewayConflict('Runtime activation belongs to another migration')
        self.inbox = await asyncio.to_thread(ReceiptInbox(self.root/'receipts.sqlite', self.identity).open)
        self.gateway = GatewayClient(self.http,self.url,self.token,self.identity,self.inbox,paired_release=self.paired_release)
        await self.gateway.connect()
        snapshot = await self.gateway.snapshot()
        self.mirror.install_snapshot(snapshot)
        history = RemoteHistory(self.gateway)
        self.configuration = Configuration(self.root,self.identity,
            lambda body:history.source('configure',body))
        await self.configuration.load(dict(snapshot['configuration']['configuration_authority'],
            options=snapshot['configuration']['options']),lambda:history.source('credentials',{}))
        credentials = self.configuration.credentials()
        ports = HouseholdPorts(self.mirror.home,self.configuration.options,self.configuration.admit,self.mirror.read,
            lambda:set(self.mirror.context['entity_ids']),lambda:self.mirror.context['entity_names'],
            lambda:self.mirror.context['area_names'],lambda:self.mirror.context['entity_areas'],
            self.editor.inventory,self.mirror.report,history,
            lambda:datetime.now(timezone.utc),lambda:False,self.repair,self.wake_projection.set,self.spawn)
        entry = self.identity['entry_id']
        stores = self.root/'stores'
        def record(prefix):
            store = RecordStore(stores/f'shs_energy.{prefix}{entry}')
            self.record_stores.append(store)
            return store
        client = ShsApiClient(self.http,credentials[CONF_BASE_URL],credentials[CONF_DEVICE_TOKEN])
        h = self.household = Household(ports,client,
            store=DurableRecord(record(''),encode,json.loads),battery_inputs_store=record('battery_live_inputs.'))
        self.devices = RemoteDevices(self.gateway,h)
        verification_store = VerificationStorage(stores/f'shs_energy.verification.{entry}.sqlite',asyncio.to_thread,
            record('verification.'),encode)
        controller = self.controller = ScheduledController(
            ControllerInputs(self.mirror.read,lambda:self.mirror.context['home']['temperature_unit'],
                lambda entity:self.mirror.context['platforms'].get(entity)),h,OwnershipView(),h.resolved_options,
            RecentVerification(verification_store,record('verification_samples.')),
            native_executor=NoNativeServices(),devices=self.devices)
        h.controller = controller
        controller.metrics.performance = {'verification_storage':verification_store.metrics}
        path = stores/f'shs_energy.execution.{entry}.sqlite'
        self.battery = h.battery_runtime = AppBatteryRuntime(h,controller,IndexedStorage(path,asyncio.to_thread,self.mirror),
            lambda:int(datetime.now(timezone.utc).timestamp()*1000))
        self.writer = h.battery_writer = RemoteBattery(self.gateway,self.battery,self.battery.now)
        self.battery.physical = self.writer
        self.battery.receipt_driven = True
        self.devices.reserve = self.battery.before_external_command
        await h.async_restore_plan()
        await self.battery.load()
        self.battery.reconcile()
        self.checkpoint_digest = await asyncio.to_thread(self.battery.store.checkpoint_digest)
        self.scheduler = ControllerScheduler(controller,self.mirror.subscribe,self.at,self.spawn)
        h.async_add_control_listener(self.scheduler.coordinator_updated)
        h.async_add_battery_listener(controller.publish_battery_status)
        controller.add_listener(self.wake_projection.set)

    async def receive_through(self, through):
        while await asyncio.to_thread(self.inbox.through) < through:
            await self.gateway.receive()

    async def activate(self):
        """Persist matching activation identities before starting any runtime job."""
        result = await self.gateway.call('reconcile',{'checkpoint_sha256':self.checkpoint_digest})
        reconciliation = result['reconciliation']
        self.mirror.install_snapshot(result['snapshot'])
        self.battery.reconcile()
        self.controller.adopt_ownership(result['ownership'])
        await self.receive_through(reconciliation['proof']['through'])
        previous = self.activation
        activation_id = previous['activation_id'] if previous else uuid4().hex
        self.activation = dict(identity=self.identity,activation_id=activation_id,state='pending',proof=reconciliation['proof'])
        await self.marker.async_save(self.activation)
        if result['snapshot']['activation'] is None:
            await self.gateway.call('activate',{'activation_id':activation_id,'proof':reconciliation['proof']})
        else:
            if result['snapshot']['activation'] != activation_id:
                raise GatewayConflict('HA activation differs from this app runtime')
            await self.gateway.call('resume',{'reconciliation_id':reconciliation['id']})
        self.activation['state'] = 'active'
        await self.marker.async_save(self.activation)
        # Restore the mirror at the consumed prefix. Facts arriving after that
        # point reach accounting one receipt at a time, even after a partial commit.
        checkpoint = self.battery._processing
        through = checkpoint['receipt']-int(not checkpoint['complete']) if checkpoint else 0
        saved=self.battery.store.source_checkpoint
        if saved is None:
            # Explicit one-time upgrade from the original transport archive.
            self.mirror.rows = {}
            cursor = 0
        else:
            self.mirror.context=deepcopy(saved['context'])
            self.mirror.rows=deepcopy(saved['rows'])
            self.mirror.revision=saved['receipt']
            cursor=saved['receipt']
            if cursor!=through: raise GatewayConflict('Source checkpoint does not match completed execution')
        while cursor < through:
            rows = await asyncio.to_thread(self.inbox.after,cursor)
            if not rows:
                raise GatewayConflict('Execution checkpoint is ahead of the receipt inbox')
            for row in rows:
                if row['ordinal'] > through:
                    break
                self.mirror.apply(row,notify=False)
                cursor = row['ordinal']
        # HomeHost restores old grants as revoked. The remote writer stays
        # disabled until all the already received observations have been consumed.
        await self.battery.start()
        await self.consume()
        self.writer.enabled = True
        await self.devices.synchronize((self.household.optimisation_plan or {}).get('device_models',[]))
        self.started = True
        await self.controller.async_start(reason='app_migration_or_restart')

    async def consume(self):
        async with self.consume_lock:
            return await self._consume()

    async def _consume(self):
        previous = self.battery._processing
        cursor = previous['receipt']-int(not previous['complete']) if previous else 0
        target = await asyncio.to_thread(self.inbox.through)
        while cursor < target:
            rows = await asyncio.to_thread(self.inbox.after,cursor)
            if not rows:
                raise GatewayConflict('Receipt inbox lost its committed prefix')
            for row in rows:
                if row['ordinal'] > target:
                    break
                self.mirror.apply(row,notify=self.started)
                await self.battery.consume_receipt(self.identity,row)
                cursor = row['ordinal']
                if self.started and row['kind']=='configuration':
                    self.scheduler.request('configuration_update')
                    self.wake_projection.set()
                    if self.household.options_update_requires_reload():
                        self.spawn(self.household.async_optimisation_push(force_plan=True),'shs_app_configuration_replan')
        saved=self.battery.store.source_checkpoint
        if saved is not None:
            completed=saved['receipt']
            await asyncio.to_thread(self.inbox.retire,completed)
            await self.gateway.call('ack_processed',{'through':completed})
        return cursor

    async def receipts(self):
        while True:
            await self.gateway.receive()
            await self.consume()
            await asyncio.sleep(.5)

    async def project(self):
        h = self.household
        self.cached = dict(devices=await h.async_cached_device_configuration(),home=await h.async_cached_home_configuration(),
            planning=await h.async_cached_planning_configuration(),exchange=await h.async_cached_exchange_status())
        value = runtime_projection(h,self.cached,self.repairs)
        value['app_url'] = self.app_url
        value['configuration'] = self.configuration.status()
        value['execution_devices'] = [{key:deepcopy(device.get(key)) for key in
            ('key','name','planned','system_member','permission','mode')}
            for device in await self.editor.devices(self.cached['planning'],include_suggestions=False)]
        value['values']['optimisation_plan'] = display_plan(value['values']['optimisation_plan'])
        await self.gateway.project(value)
        self.publish_ui(value)

    async def projections(self):
        while True:
            await self.wake_projection.wait()
            self.wake_projection.clear()
            await self.project()
            await asyncio.sleep(1)

    async def periodic(self, operation, seconds):
        while True:
            try:
                await operation()
            except (ValueError, OSError) as error:
                if isinstance(error, GatewayConflict): raise
                logging.getLogger(__name__).warning('SHS background operation failed: %s',error)
            await asyncio.sleep(seconds)

    async def planning(self):
        await asyncio.sleep(OPTIMISATION_STARTUP_DELAY_SECONDS)
        await self.periodic(self.household.async_replan_poll,PLAN_EXCHANGE_INTERVAL_MINUTES*60)

    async def resources(self):
        values = await asyncio.to_thread(process_resources)
        self.battery.profiler.sample(values,self.battery.resource_counts())
        self.wake_projection.set()

    async def requests(self):
        while True:
            for request in await self.gateway.call('requests',{}):
                self.spawn(self.answer(request),'shs_app_request_'+request['operation'])
            await asyncio.sleep(.5)

    async def answer(self, request):
        h, op, body = self.household, request['operation'], request['body']
        result, error = None, None
        try:
            if op == 'configuration':
                result = await self.editor.action(body['operation'],body['body'])
                if body['operation'] == 'control': result = {'revision':result['revision']}
            elif op == 'refresh': result = await h.async_request_refresh()
            elif op == 'refresh_devices': result = await h.async_refresh_device_configuration()
            elif op == 'cached_devices': result = await h.async_cached_device_configuration()
            elif op == 'cached_home': result = await h.async_cached_home_configuration()
            elif op == 'cached_planning': result = await h.async_cached_planning_configuration()
            elif op == 'cached_exchange': result = await h.async_cached_exchange_status()
            elif op == 'report_mapping': result = await h.async_report_device_mapping(body['device_key'],body['mappings'])
            elif op == 'replan':
                request_id = await h.client.request_replan()
                result = await h.async_answer_replan(request_id)
                if h.last_optimisation_error: raise ValueError(h.last_optimisation_error)
            elif op == 'backfill_prices': result = await h.async_backfill_prices(body['days'])
            elif op == 'tick':
                snapshot = await self.gateway.snapshot()
                await self.receive_through(snapshot['through'])
                await self.consume()
                if body.get('configuration_changed'): h._plan_configuration_changed = True
                await h.async_battery_inputs_refresh()
                result = await self.controller.async_tick()
            elif op == 'optimisation': result = await h.async_optimisation_push(force_plan=body['force_plan'])
            elif op == 'runtime_report': result = await h.async_report_runtime()
            elif op == 'profile':
                seconds = body.get('allocation_seconds',0)
                if seconds: self.battery.profiler.start_allocations(seconds,asyncio.to_thread)
                result = self.battery.profiler.snapshot(self.battery.resource_counts())
            elif op == 'configuration_changed':
                h._plan_configuration_changed = True
                result = await h.async_refresh_device_configuration()
            else: raise ValueError('App request is not implemented: '+op)
            await self.project()
        except Exception as exception:
            error = str(exception)
        await self.gateway.call('reply',{'request_id':request['id'],'result':result,'error':error})

    async def calendar(self):
        last_quarter = None
        last_day = None
        while True:
            now = self.household.local_now()
            quarter = (now.date(),now.hour,now.minute//15,now.fold)
            if quarter != last_quarter:
                await self.household.async_price_refresh()
                last_quarter = quarter
            if last_day != now.date() and (now.hour,now.minute) >= (PUSH_TIME_HOUR,PUSH_TIME_MINUTE):
                await self.household.async_scheduled_push()
                last_day = now.date()
            await asyncio.sleep(5)

    async def run(self):
        try:
            await self.load()
            await self.activate()
            # Cloud jobs start only after the durable ownership handover.
            self.wake_projection.set()
            jobs = [self.spawn(self.receipts()),self.spawn(self.projections()),self.spawn(self.requests()),
                self.spawn(self.periodic(self.household.async_battery_inputs_refresh,5)),
                self.spawn(self.planning()),
                self.spawn(self.periodic(self.household.async_request_refresh,60)),
                self.spawn(self.periodic(self.resources,60)),self.spawn(self.calendar()),
                self.spawn(self.household.async_replan_listener())]
            done,_ = await asyncio.wait(jobs,return_when=asyncio.FIRST_EXCEPTION)
            for task in done: task.result()
        finally:
            await self.close()

    async def close(self):
        if self.closed: return
        self.closed = True
        if self.writer: self.writer.revoke()
        if self.scheduler: self.scheduler.close()
        for handle in self.timers: handle.cancel()
        for task in tuple(self.tasks): task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        if self.battery: await self.battery.close(release=False)
        if self.controller: await self.controller.async_stop()
        for store in self.record_stores: await store.async_close()
        if self.household: self.household.battery_live_inputs.close()
        if self.gateway: await self.gateway.close()
        if self.inbox: await asyncio.to_thread(self.inbox.close)
        if self.lease: await asyncio.to_thread(self.lease.close)
