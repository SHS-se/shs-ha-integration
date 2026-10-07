"""App-owned household composition and explicit dormant-to-active migration."""
import asyncio
from contextlib import suppress
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from uuid import uuid4

from shs_core.api import ShsApiClient, ShsApiError
from .runtime import AppBatteryRuntime
from .profiling import AppProfiler, profiled
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
from .projection import DisplayPlan
from .sources import ObservationMirror, RemoteHistory
from .upgrades import open_runtime_schema
from .configuration import Configuration
from .backends import Backends, BackendChanged, BackendSetupError, URLS
from shs_core.operating_modes import transfer_admissions
from shs_core.api_contract import validate_server_contract
from shs_core.optimisation import validate_plan_contract
from .indexed_storage import IndexedStorage
from .configuration_editor import ConfigurationEditor
from .entities import project_entities
from shs_core.native_configuration import native_options
from shs_core.source_admission import source_bindings, pool_paused
from shs_core.configuration_schema import resolve_configuration
from shs_core.controller_inputs import configured_entity_ids


LOGGER = logging.getLogger(__name__)
PERIODIC_PROFILES = {'battery_inputs':'battery_inputs',
                     'async_request_refresh':'cloud_refresh','async_replan_poll':'plan_exchange'}


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
        self.profiler = AppProfiler()
        self.processed_receipts = 0
        self._processed_ack = None
        self._source_admission = None
        self.source_counts = {}
        self.live_revision = None
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
        self.display_plan = DisplayPlan()
        self.started = False
        self.consume_lock = asyncio.Lock()
        self.configuration = None
        self.editor = ConfigurationEditor(self)
        self.record_stores = []
        self.backends = None
        self.households = {}
        self.restart_requested = asyncio.Event()

    def spawn(self, work, name='shs_app_work'):
        task = asyncio.create_task(work, name=name)
        self.tasks.add(task)
        def completed(task):
            self.tasks.discard(task)
            if not task.cancelled() and (error := task.exception()) is not None:
                LOGGER.error('App task %s failed: %s: %s',task.get_name(),type(error).__name__,error,exc_info=(type(error),error,error.__traceback__))
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

    @profiled('runtime_load')
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
        self.gateway = GatewayClient(self.http,self.url,self.token,self.identity,self.inbox,paired_release=self.paired_release,profiler=self.profiler)
        await self.gateway.connect()
        snapshot = await self.gateway.snapshot()
        self.mirror.install_snapshot(snapshot)
        history = RemoteHistory(self.gateway)
        self.configuration = Configuration(self.root,self.identity,
            lambda body:history.source('configure',body),project=self.native_configuration)
        await self.configuration.load(dict(snapshot['configuration']['configuration_authority'],
            options=snapshot['configuration']['options']),lambda:history.source('credentials',{}))
        self.backends = Backends(self.root, self.identity)
        await self.backends.load(self.configuration.credentials())
        self.environment = self.backends.controlling
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
        async def refuse_admission(*_args):
            raise GatewayConflict('An observing backend cannot change physical admission')
        for environment, cloud_credentials in self.backends.data['credentials'].items():
            authority = environment == self.environment
            cloud_ports = ports if authority else replace(ports, admit=refuse_admission, repair=lambda *_args:None)
            client = ShsApiClient(self.http,cloud_credentials[CONF_BASE_URL],cloud_credentials[CONF_DEVICE_TOKEN])
            household = Household(cloud_ports,client,
                store=DurableRecord(record(f'cloud.{environment}.'),encode,json.loads),
                battery_inputs_store=record('battery_live_inputs.' if authority else f'cloud.{environment}.battery_inputs.'),
                control_authority=authority)
            self.households[environment] = household
            await household.async_restore_plan()
        h = self.household = self.households[self.environment]
        if self.backends.data['admitted_for'] != self.environment:
            choices = await h.async_cached_planning_configuration()
            options = self.configuration.options()
            transferred = transfer_admissions(options, choices['devices'], choices['home'])
            if transferred != options:
                await self.configuration.commit(self.configuration.revision,transferred,
                    'backend-admission-'+str(self.backends.data['revision']))
            # An observing plan has no physical battery generation. Keep the
            # command journal, hold the last setting and request a fresh plan.
            h._plan_configuration_changed = True
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
        self.battery.profiler = self.profiler
        self.writer = h.battery_writer = RemoteBattery(self.gateway,self.battery,self.battery.now)
        self.battery.physical = self.writer
        self.battery.receipt_driven = True
        self.devices.reserve = self.battery.before_external_command
        await self.battery.load()
        self.battery.reconcile()
        self.checkpoint_digest = await asyncio.to_thread(self.battery.store.checkpoint_digest)
        self.scheduler = ControllerScheduler(controller,self.mirror.subscribe,self.at,self.spawn)
        h.async_add_control_listener(self.scheduler.coordinator_updated)
        h.async_add_battery_listener(controller.publish_battery_status)
        controller.add_listener(self.wake_projection.set)

    async def receive_through(self, through, *, establish_delivery=False):
        # Even an empty suffix establishes delivery in this new socket epoch.
        # The persisted inbox cursor alone cannot authorize HA receipt retirement.
        while establish_delivery or await asyncio.to_thread(self.inbox.through) < through:
            await self.gateway.receive()
            establish_delivery = False

    @profiled('runtime_activate')
    async def activate(self):
        """Persist matching activation identities before starting any runtime job."""
        result = await self.gateway.call('reconcile',{'checkpoint_sha256':self.checkpoint_digest})
        reconciliation = result['reconciliation']
        self.mirror.install_snapshot(result['snapshot'])
        self.battery.reconcile()
        self.controller.adopt_ownership(result['ownership'])
        await self.receive_through(reconciliation['proof']['through'], establish_delivery=True)
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
        self.mirror.durable_rows=self.mirror.rows
        self.mirror.row_receipts={entity:cursor for entity in self.mirror.rows}
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
        await self.admit_sources()

    async def admit_sources(self):
        h = self.household
        slot = h.current_plan_slot
        paused = pool_paused(self.controller.status.get('pool',{}),slot,h.optimisation_plan,
            self.mirror.read,self.controller.ownership)
        value = dict(revision=self.configuration.revision,
            bindings=source_bindings(h.resolved_options(),(h.optimisation_plan or {}).get('device_models', [])),
            pool_pause=(dict(entity=self.controller.status['pool']['control_entity'],
                until=(datetime.fromisoformat(slot['start'].replace('Z','+00:00'))+timedelta(minutes=15)).isoformat()) if paused else None))
        if value != self._source_admission:
            await self.gateway.call('source',dict(operation='admission',body=value))
            self._source_admission = value
            self.live_revision = None

    async def consume(self):
        async with self.consume_lock:
            return await self._consume()

    @profiled('receipt_consume')
    async def _consume(self):
        previous = self.battery.received_checkpoint() if self.started else self.battery._processing
        cursor = previous['receipt']-int(not previous['complete']) if previous else 0
        target = await asyncio.to_thread(self.inbox.through)
        while cursor < target:
            rows = await asyncio.to_thread(self.inbox.after,cursor)
            if not rows:
                raise GatewayConflict('Receipt inbox lost its committed prefix')
            for row in rows:
                if row['ordinal'] > target:
                    break
                with self.profiler.measure('receipt_apply'):
                    self.mirror.apply(row,notify=self.started)
                if self.started:
                    await self.battery.ingest_receipt(self.identity,row)
                else:
                    await self.battery.consume_receipt(self.identity,row)
                cursor = row['ordinal']
                self.processed_receipts += 1
                if self.started and row['kind']=='configuration':
                    self._source_admission = None
                    self.scheduler.request('configuration_update')
                    self.wake_projection.set()
                    if self.household.options_update_requires_reload():
                        await self.replan_all()
        if self.started:
            await self.battery.commit_evidence()
        saved=self.battery.store.source_checkpoint
        if saved is not None:
            completed=saved['receipt']
            if completed != self._processed_ack:
                await asyncio.to_thread(self.inbox.retire,completed)
                await self.gateway.call('ack_processed',{'through':completed})
                self._processed_ack = completed
        return cursor

    async def receipts(self):
        after = {'receipts':0, 'requests':0}
        while True:
            changed = await self.gateway.call('updates', {'after':after})
            if changed['receipts'] != after['receipts']:
                page = await self.gateway.receive()
                while page['through'] < page['high']:
                    page = await self.gateway.receive()
            await self.consume()
            if changed['requests'] != after['requests']:
                for request in await self.gateway.call('requests',{}):
                    self.spawn(self.answer(request),'shs_app_request_'+request['operation'])
            after = changed

    async def battery_inputs(self):
        await self.admit_sources()
        live = await self.gateway.call('source',dict(operation='live',body={'after':self.live_revision}))
        await self.receive_through(live['through'])
        await self.consume()
        if (live['configuration_revision'] == self.configuration.revision
                == self.mirror.context['configuration_authority']['revision']):
            self.mirror.apply_live(live['rows'], through=live['through'])
            self.live_revision = live['revision']
        else:
            self.live_revision = None
        self.source_counts = live['counts']
        await self.battery.commit_evidence()
        await self.household.async_battery_inputs_refresh()
        await self.consume()

    def native_configuration(self,options):
        home=self.mirror.context['home']
        resolved=resolve_configuration(options,home['latitude'],home['longitude'])
        return {**native_options(resolved),'_observed_entities':sorted(configured_entity_ids(resolved))}

    @profiled('projection')
    async def project(self):
        h = self.household
        self.cached = dict(devices=await h.async_cached_device_configuration(),home=await h.async_cached_home_configuration(),
            planning=await h.async_cached_planning_configuration(),exchange=await h.async_cached_exchange_status())
        if h.optimisation_plan is not self.display_plan.source:
            with self.profiler.measure('display_plan_build'):
                self.display_plan.get(h.optimisation_plan)
        with self.profiler.measure('projection_build'):
            value = runtime_projection(h,self.cached,self.repairs,plan=self.display_plan.value)
        value['app_url'] = self.app_url
        value['configuration'] = self.configuration.status()
        value['execution_devices'] = [{key:deepcopy(device.get(key)) for key in
            ('key','name','planned','system_member','permission','mode')}
            for device in await self.editor.devices(self.cached['planning'],include_suggestions=False)]
        with self.profiler.measure('native_projection_build'):
            native = project_entities(self,value['execution_devices'])
        await self.gateway.project(dict(schema=2,entities=native,execution_devices=value["execution_devices"],
            configuration=value["configuration"],repairs=value["repairs"],app_url=self.app_url))
        self.publish_ui(value)

    async def projections(self):
        while True:
            await self.wake_projection.wait()
            self.wake_projection.clear()
            await self.project()
            await asyncio.sleep(1)

    async def periodic(self, operation, seconds):
        profile = PERIODIC_PROFILES.get(operation.__name__)
        while True:
            try:
                if profile:
                    with self.profiler.measure(profile):
                        await operation()
                else:
                    await operation()
            except (ValueError, OSError) as error:
                if isinstance(error, GatewayConflict): raise
                LOGGER.warning('Background operation %s failed: %s: %s',operation.__name__,type(error).__name__,error,exc_info=LOGGER.isEnabledFor(logging.DEBUG))
            await asyncio.sleep(seconds)

    async def planning(self, household):
        await asyncio.sleep(OPTIMISATION_STARTUP_DELAY_SECONDS)
        await self.complete_backend_selection(household)
        while household._plan_configuration_changed:
            await self.request_plan(household)
            if household._plan_configuration_changed:
                await asyncio.sleep(5)
        await self.periodic(household.async_replan_poll,PLAN_EXCHANGE_INTERVAL_MINUTES*60)

    async def complete_backend_selection(self, household):
        if household is not self.household or self.backends.data['admitted_for'] == self.environment:
            return
        # Selecting a source is an explicit request for its executable plan.
        # A background force_plan exchange only recommends a manual replan.
        # Keep the durable selection unsettled until the fresh plan is accepted,
        # so a restart resumes pending work rather than losing the handover.
        while household._plan_configuration_changed:
            exchange = await household.async_cached_exchange_status()
            if not (exchange['planning_job'] or exchange['planning_submission']):
                await self.request_plan(household)
            if household._plan_configuration_changed:
                await asyncio.sleep(5)
        await self.backends.settled()

    async def request_plan(self, household):
        if household is not self.household:
            await household.async_optimisation_push(force_plan=True)
            return True
        exchange = await household.async_cached_exchange_status()
        if exchange['planning_job'] or exchange['planning_submission']:
            return True
        request_id = await household.client.request_replan()
        if await household.async_answer_replan(request_id):
            return True
        # The server retains an unacknowledged observer request. Re-answer it
        # with current execution evidence after promotion; never discard its
        # identity or replace a job concurrently admitted by the listener.
        exchange = await household.async_cached_exchange_status()
        if not (exchange['planning_job'] or exchange['planning_submission']):
            await household.async_optimisation_push(force_plan=True,replan_request_id=request_id)
        return True

    async def cloud_job(self, environment, operation):
        """A failed cloud session cannot stop its sibling or the physical owner."""
        while True:
            try:
                self.backends.errors.pop(environment,None)
                await operation()
                return
            except Exception as error:
                self.backends.errors[environment] = str(error)
                self.wake_projection.set()
                await asyncio.sleep(5)

    async def restart(self):
        await self.restart_requested.wait()
        raise BackendChanged()

    def request_restart(self):
        asyncio.get_running_loop().call_later(0.5,self.restart_requested.set)

    async def replan_all(self):
        for environment, household in self.households.items():
            household._plan_configuration_changed = True
            self.spawn(self.cloud_job(environment,lambda h=household:self.request_plan(h)),
                'configuration_replan_'+environment)

    async def select_backend(self, body):
        environment = body['environment']
        if environment not in self.households:
            raise BackendSetupError('Pair this backend before selecting its plan',
                {f'backend_{environment}_pairing_code':'Pairing is required'})
        h = self.households[environment]
        status = await h.client.status()
        validate_server_contract(status)
        if not status.get('subscription_active'):
            raise ValueError('Activate a subscription on the selected backend billing page')
        await h.async_refresh_device_configuration()
        choices = await h.async_cached_planning_configuration()
        transfer_admissions(self.configuration.options(),choices['devices'],choices['home'])
        if not h.optimisation_plan or h._plan_configuration_changed or h.optimisation_plan.get('status') != 'ready':
            raise ValueError('Wait for this backend to finish a fresh plan before selecting it')
        validate_plan_contract(h.optimisation_plan,datetime.now(timezone.utc))
        await self.backends.change(environment,body['backend_revision'],body['request_id'])

    async def pair_backend(self, body):
        environment = body['environment']
        if environment not in URLS:
            raise ValueError('Choose production or test')
        client = ShsApiClient(self.http,URLS[environment],'')
        code = body.get('pairing_code')
        if not isinstance(code,str) or not code.strip():
            raise BackendSetupError('Enter the pairing code from this backend',
                {f'backend_{environment}_pairing_code':'Pairing code is required'})
        try:
            result = await client.pair(code.strip(),'SHS Energy App '+environment)
        except ShsApiError as error:
            raise BackendSetupError(str(error),{f'backend_{environment}_pairing_code':str(error)}) from error
        current_home = self.backends.credentials().get('home_id')
        if not current_home or result.get('home_id') != current_home:
            raise ValueError('Pair the same home on both backends')
        await self.backends.change(environment,body['backend_revision'],body['request_id'],
            credentials={**result,CONF_BASE_URL:URLS[environment]})

    async def resources(self):
        values = await asyncio.to_thread(process_resources)
        received = await asyncio.to_thread(self.inbox.through)
        checkpoint = self.battery.store.source_checkpoint
        completed = checkpoint['receipt'] if checkpoint else 0
        self.profiler.sample(values,{**self.battery.resource_counts(),
            **{'source_'+key:value for key,value in self.source_counts.items()},
            'app_processed_receipts':self.processed_receipts,'app_receipt_backlog':max(0,received-completed)})
        self.profiler.log_sample()
        self.wake_projection.set()

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
                result = await self.request_plan(h)
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
                result = self.profiler.snapshot(self.battery.resource_counts(),include_samples=True)
            elif op == 'configuration_changed':
                h._plan_configuration_changed = True
                result = await h.async_refresh_device_configuration()
            else: raise ValueError('App request is not implemented: '+op)
            await self.project()
        except Exception as exception:
            error = str(exception)
            LOGGER.warning('App request %s failed: %s: %s',op,type(exception).__name__,exception,exc_info=LOGGER.isEnabledFor(logging.DEBUG))
        await self.gateway.call('reply',{'request_id':request['id'],'result':result,'error':error})

    async def calendar(self):
        last_quarter = None
        last_day = None
        while True:
            now = self.household.local_now()
            quarter = (now.date(),now.hour,now.minute//15,now.fold)
            if quarter != last_quarter:
                for h in self.households.values():
                    await h.async_price_refresh()
                last_quarter = quarter
            if last_day != now.date() and (now.hour,now.minute) >= (PUSH_TIME_HOUR,PUSH_TIME_MINUTE):
                with self.profiler.measure('history_upload'):
                    await asyncio.gather(*(self.cloud_job(env,h.async_scheduled_push) for env,h in self.households.items()))
                last_day = now.date()
            await asyncio.sleep(5)

    async def run(self):
        try:
            LOGGER.info('Restoring saved runtime and connecting to the HA gateway')
            await self.load()
            LOGGER.info('Runtime loaded; reconciling ownership and replaying queued observations')
            await self.activate()
            LOGGER.info('Runtime active; battery_mode=%s plan_id=%s processed_receipts=%s',
                self.battery.snapshot().get('mode'),(self.household.optimisation_plan or {}).get('plan_id'),self.processed_receipts)
            # Cloud jobs start only after the durable ownership handover.
            self.wake_projection.set()
            jobs = [self.spawn(self.receipts(),'receipts'),self.spawn(self.projections(),'projections'),
                self.spawn(self.periodic(self.battery_inputs,5),'battery_inputs'),
                self.spawn(self.periodic(self.resources,60),'runtime_resources'),self.spawn(self.calendar(),'calendar'),
                self.spawn(self.restart(),'backend_restart')]
            for environment, h in self.households.items():
                operations = [('planning',lambda h=h:self.planning(h)),
                    ('cloud_refresh',lambda h=h:self.periodic(h.async_request_refresh,60)),
                    ('replan_listener',h.async_replan_listener),('planning_delivery',h.async_planning_delivery)]
                for name, operation in operations:
                    jobs.append(self.spawn(self.cloud_job(environment,operation),name+'_'+environment))
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
        for h in self.households.values(): h.battery_live_inputs.close()
        if self.gateway: await self.gateway.close()
        if self.inbox: await asyncio.to_thread(self.inbox.close)
        if self.lease: await asyncio.to_thread(self.lease.close)
