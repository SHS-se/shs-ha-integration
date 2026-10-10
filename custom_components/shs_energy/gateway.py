"""HA source, socket and native-service composition for the app gateway."""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from time import perf_counter, thread_time

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.energy import async_get_manager
from homeassistant.const import EVENT_STATE_CHANGED, EVENT_STATE_REPORTED, EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.json import json_bytes
from homeassistant.util.json import json_loads

from .configuration import (area_name_by_id, entity_area_id_by_id,
                            entity_display_name_by_id, resolved_options)
from .recorder_source import RecorderSource
from .gateway_wire import ProjectionAssembly, filter_sources
from .shs_wire.protocol import admit, offer
from .shs_core.controller_inputs import configured_entity_ids
from .shs_core.api_contract import INTEGRATION_VERSION
from .shs_core.battery_gateway import BatteryGateway
from .shs_core.native_readings import power
from .shs_core.command_transport import CommandTransport
from .shs_core.controller_inputs import ControllerInputs
from .shs_core.device_gateway import DeviceGateway
from .shs_core.gateway_journal import GatewayJournal, GatewayConflict
from .shs_core.gateway_service import GatewayService, AppConnection
from .shs_core.gateway_stream import GatewayCommands, GatewayOperations, GatewayRecord, GatewayStream
from .shs_core.execution_configuration import ExecutionConfiguration
from .shs_core.source_admission import (validate_bindings, ordered, FACT_ATTRIBUTES, native_pool_idle, owned_device_sources,
    validate_pool_pause, temperature_available, temperature_metadata)
from .shs_core.native_commands import NativeExecutor

_LOGGER = logging.getLogger(__name__)
GATEWAYS = 'shs_energy_gateways'
SOCKETS = 'shs_energy_gateway_sockets'


def wire(value):
    return json_loads(json_bytes(value))


class HomeAssistantSource:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.history = RecorderSource(hass)
        self.service = None
        self.projection = None
        self.entities = set()
        self.closed = False
        self.update_lock = asyncio.Lock()
        self.last_context = None
        self.configuration = None
        self.admission = None
        self.bindings = {}
        self.pool_metadata = {}
        self.pool_unavailable = set()
        self.live_revision = 0
        self.live_changes = {}
        self.fact_versions = {}
        self.intake_counts = {'ordered':0, 'replaceable':0, 'bytes':0}

    def report(self, entity, state=None, kind='state_report', *, compact=False):
        state = self.hass.states.get(entity) if state is None else state
        now = datetime.now(timezone.utc).isoformat()
        attributes = ({key:value for key,value in state.attributes.items() if not compact or key in FACT_ATTRIBUTES}
                      if state else {})
        return wire(dict(entity_id=entity, state=state.state if state else None,
            attributes=attributes,
            last_changed=state.last_changed.isoformat() if state else now,
            last_updated=state.last_updated.isoformat() if state else now,
            last_reported=state.last_reported.isoformat() if state else now,
            event_id=state.context.id if state else None, kind=kind))

    def options(self):
        options=self.configuration.options()
        if self.configuration.value['revision']==0:
            return resolved_options(self.hass,options)  # One-time adoption source.
        return {key:value for key,value in options.items() if key!='_observed_entities'}

    def context(self, entities=None):
        h = self.hass
        registry = er.async_get(h)
        entities = self.entities if entities is None else entities
        return wire(dict(options=self.configuration.options(),
            configuration_authority={key:self.configuration.value[key] for key in ('revision','digest')},
            home=dict(latitude=h.config.latitude, longitude=h.config.longitude, language=h.config.language,
                      timezone=h.config.time_zone, temperature_unit=h.config.units.temperature_unit),
            observed_entities=sorted(entities), entity_ids=[s.entity_id for s in h.states.async_all()], entity_names=entity_display_name_by_id(h),
            area_names=area_name_by_id(h), entity_areas=entity_area_id_by_id(h),
            platforms={entity:item.platform for entity in entities if (item:=registry.async_get(entity))}))

    def sources(self):
        """Configured, owned and filter-derived entities, read without subscribing."""
        entities = (set(self.configuration.options()['_observed_entities']) if self.configuration.value['revision'] else configured_entity_ids(self.options()))
        entities.update(self.bindings)
        for record in self.service.physical.ownership.records.values():
            entities.update(record['originals'])
            entities.update(configured_entity_ids(record['options']))
        registry = er.async_get(self.hass)
        return filter_sources(entities, self.hass.states.get,
            lambda entity: item.platform if (item := registry.async_get(entity)) else None)

    def install_bindings(self, value):
        self.admission = value
        bindings = validate_bindings(value['bindings'])
        registry = er.async_get(self.hass)
        for entity, uses in tuple(bindings.items()):
            derived = filter_sources({entity}, self.hass.states.get,
                lambda key: item.platform if (item := registry.async_get(key)) else None)
            for source in derived:
                bindings[source] = bindings.get(source, frozenset()) | uses
        self.bindings = bindings
        self.entities = self.sources()
        self.pool_metadata = {entity:temperature_metadata(self.hass.states.get(entity))
                              for entity,uses in bindings.items() if 'pool_temperature' in uses}
        self.pool_unavailable = {entity for entity in self.pool_metadata
                                 if not temperature_available(self.hass.states.get(entity))}

    def pool_idle(self):
        context = self.service.context()
        declared = (self.admission['pool_pause'] if self.admission is not None
                    and self.admission['pool_pause'] is not None
                    and self.admission['pool_context'] == {key:context[key]
                        for key in ('configuration_revision','policy_revision')} else None)
        return native_pool_idle(self.service.physical.pool_pause,
            declared, context, self.hass.states.get,self.service.physical.ownership,
            datetime.now(timezone.utc))

    def owned_sources(self):
        entities = set()
        for record in self.service.physical.ownership.records.values():
            entities.update(owned_device_sources(record))
        registry = er.async_get(self.hass)
        return filter_sources(entities, self.hass.states.get,
            lambda key: item.platform if (item := registry.async_get(key)) else None)

    def execution_source(self, entity):
        # Prior durable input schemas drain normally until the explicit sealed
        # admission installation for the current canonical configuration.
        if self.admission is None or self.admission['revision'] != self.configuration.value['revision']:
            return True
        uses = self.bindings.get(entity, frozenset())
        # Ownership outlives configuration edits until explicit handover retires
        # it. Keep its former controls and override sources in the ordered plane.
        if entity in self.owned_sources():
            uses = uses | {'device'}
        thermal = 'pool_temperature' in uses
        valid = (not self.pool_unavailable and all(temperature_available(self.hass.states.get(source))
                     and temperature_metadata(self.hass.states.get(source)) == self.pool_metadata[source]
                     for source in self.pool_metadata) if thermal else True)
        return ordered(uses, pool_idle=valid and self.pool_idle() if thermal else False)

    def metadata_unchanged(self):
        """Whether a registry or core event leaves the settled context as it is.

        Those events fire for every entity in Home Assistant. Only a context
        the app has not received needs its commands refused and a new capture.
        """
        if self.service.configuration_pending:
            return False
        try:
            return self.context(self.sources()) == self.last_context
        except Exception:
            return False  # Fence first; the refresh then reports the failure.

    def physical_controls(self):
        entities = set()
        options = self.options()
        for key in ('battery_mode_entity', 'battery_charge_limit_entity', 'battery_discharge_limit_entity'):
            if options.get(key):
                entities.add(options[key])
        for record in self.service.physical.ownership.records.values():
            entities.update(record['originals'])
        return {entity:{key:value for key,value in self.report(entity).items() if key in ('state','attributes')}
                for entity in sorted(entities)}

    def invalidate(self):
        self.service.invalidate_configuration()

    async def refresh_configuration(self):
        async with self.update_lock:
            if self.closed:
                return
            if self.admission is not None:
                self.install_bindings(self.admission)
            self.entities = self.sources()
            # Queue the canonical configuration and its initial source values in
            # one callback turn before acknowledging the new revision.
            context = self.context()
            if context == self.last_context:
                self.service.configuration_pending = False
                return
            # A settings installation has already revoked the battery writer.
            self.service.invalidate_context()
            self.last_context = context
            future = self.service.stream.capture('configuration', context)
            pending = [self.service.stream.capture('observation', self.report(entity, compact=True))
                       for entity in sorted(self.entities) if self.execution_source(entity)]
            self.service.configuration_revision = await future
            await asyncio.gather(*pending)
            self.service.configuration_pending = False

    async def request(self, operation, body):
        fields = {'credentials':set(),
            'admission':{'revision','bindings','pool_pause'}, 'live':{'after'},
            'catalog':set(),
            'configure':{'expected_revision','revision','options','digest'},
            'statistics':{'start','end','entities','period','units','kinds'},
            'states':{'start','end','entities','with_attributes'}, 'forecast':{'entity'}}
        if operation not in fields or type(body) is not dict or set(body) != fields[operation]:
            raise ValueError('Unsupported source request')
        if operation == 'credentials':
            return dict(self.entry.data)
        if operation == 'admission':
            validate_bindings(body['bindings'])
            validate_pool_pause(body['pool_pause'])
            if body['revision'] != self.configuration.value['revision']:
                raise GatewayConflict('Source bindings belong to another configuration')
            previous = {entity for entity in self.entities if self.execution_source(entity)}
            context = self.service.context()
            sealed = {**body,'pool_context':{key:context[key] for key in ('configuration_revision','policy_revision')}}
            await self.service.stream.call('save_record','source_admission',sealed)
            if body['revision'] != self.configuration.value['revision']:
                raise GatewayConflict('Configuration changed during source admission')
            self.install_bindings(sealed)
            context = self.last_context
            await self.refresh_configuration()
            if self.last_context == context:
                # Role promotion must deliver the current value even when no
                # state report fires after installation.
                pending = [self.service.stream.capture('observation', self.report(entity, compact=True))
                           for entity in sorted(self.entities-previous) if self.execution_source(entity)]
                await asyncio.gather(*pending)
            return {}
        if operation == 'live':
            after = body['after']
            if after is not None and (type(after) is not int or not 0 <= after <= self.live_revision):
                raise ValueError('Invalid live reading revision')
            # Freeze selected values before the queue barrier in this loop turn.
            revision = self.live_revision
            configuration_revision = self.configuration.value['revision']
            rows = {entity:self.report(entity) for entity in self.entities
                    if (after is None or self.live_changes.get(entity,0) > after) and not self.execution_source(entity)}
            versions = {entity:self.fact_versions.get(entity,0) for entity in rows}
            snapshot = await self.service.stream.call('snapshot',self.service.session())
            rows = {entity:row for entity,row in rows.items() if not self.execution_source(entity)
                    and versions[entity] == self.fact_versions.get(entity,0)}
            return dict(through=snapshot['through'],revision=revision,configuration_revision=configuration_revision,rows=rows,
                        counts={**self.intake_counts,**self.service.stream.metrics})
        if operation == 'catalog':
            manager = await async_get_manager(self.hass)
            attributes = {'friendly_name','unit_of_measurement','device_class','state_class',
                'min','max','step','options','hvac_modes','min_temp','max_temp','target_temp_step'}
            states = {}
            for state in self.hass.states.async_all():
                selected = {key:value for key,value in state.attributes.items() if key in attributes}
                # Discovery needs presence of a PV forecast, not its full history.
                if state.attributes.get('watts'):
                    selected['watts'] = {'available':True}
                states[state.entity_id] = dict(entity_id=state.entity_id,state=state.state,
                    attributes=selected,last_updated=state.last_updated.isoformat())
            return wire(dict(context=self.context(),states=states,
                preferences=manager.data or manager.default_preferences()))
        if operation == 'configure':
            result = await self.configuration.install(body)
            marker = {'settings_owner':'app'}
            data = {key:value for key,value in self.entry.data.items() if key != 'device_token'}
            if dict(self.entry.options) != marker or data != dict(self.entry.data):
                self.hass.config_entries.async_update_entry(self.entry, options=marker, data=data)
            return result
        if operation == 'forecast':
            return wire(await self.history.hourly_forecast(body['entity']))
        start, end = (datetime.fromisoformat(body[key]) for key in ('start','end'))
        if operation == 'statistics':
            rows = await self.history.statistics(start,end,set(body['entities']),body['period'],body['units'],set(body['kinds']))
            started, cpu = perf_counter(), thread_time()
            value = wire(rows)
            _LOGGER.debug('Recorder statistics wire conversion: period=%s rows=%s wall_ms=%.1f cpu_ms=%.1f',
                         body['period'], sum(len(values) for values in rows.values()),
                         (perf_counter()-started)*1000, (thread_time()-cpu)*1000)
            return value
        return wire(await self.history.states(start,end,body['entities'],with_attributes=body['with_attributes']))

    async def publish(self, value):
        if self.projection:
            await self.projection.accept(value)

    def external_ready(self, device):
        # The app has already reserved unknown pending demand. HA independently
        # checks the current battery response at the final native boundary.
        battery = self.service.battery
        if battery.installation is None:
            return True
        options = battery.installation[3]
        try:
            watts, _ = power(self.report(options['battery_power_measurement_entity']),
                source=options['battery_power_measurement_entity'], now_ms=int(datetime.now(timezone.utc).timestamp()*1000), signed=True)
            mode = self.report(options['battery_mode_entity'])['state']
            limit = self.report(options['battery_charge_limit_entity'])
            ceiling = float(limit['state'])*(1000 if limit['attributes']['unit_of_measurement'] == 'kW' else 1)
            return mode != 'Command Charging (PV First)' or (ceiling == 0 and watts <= 100)
        except (ValueError, KeyError, TypeError):
            return False

    def attach(self):
        @callback
        def matches(data):
            return data.get('entity_id') in self.entities
        @callback
        def observe(event):
            entity = event.data['entity_id']
            state = event.data.get('new_state')
            if state and state.attributes.get('entity_id') and er.async_get(self.hass).async_get(entity):
                discovered = filter_sources({entity}, self.hass.states.get,
                    lambda key: item.platform if (item := er.async_get(self.hass).async_get(key)) else None)
                if discovered - self.entities:
                    metadata_changed(event)
            if not self.execution_source(entity):
                self.intake_counts['replaceable'] += 1
                self.live_revision += 1
                self.live_changes[entity] = self.live_revision
                return
            kind = 'state_report' if event.event_type == EVENT_STATE_REPORTED else 'state_change'
            physical = self.service.physical
            physical.observation_changed.set()
            try:
                row = self.report(entity, event.data.get('new_state'), kind, compact=True)
                self.intake_counts['ordered'] += 1
                self.intake_counts['bytes'] += len(json.dumps(row))
                self.service.stream.capture('observation', row)
                self.fact_versions[entity] = self.fact_versions.get(entity,0)+1
                if entity in self.pool_metadata:
                    self.pool_metadata[entity] = temperature_metadata(state)
                    if temperature_available(state):
                        self.pool_unavailable.discard(entity)
                    else:
                        self.pool_unavailable.add(entity)
            except Exception:
                self.service.revoke()
                # The stream logs its initiating fault once with its traceback.
                # Later source reports cannot recover it and must not flood HA.
                if self.service.stream.failure is None:
                    _LOGGER.exception('SHS gateway observation persistence unavailable')
        @callback
        def metadata_changed(event):
            if self.metadata_unchanged():
                return
            self.service.invalidate_context()
            self.entry.async_create_background_task(self.hass, self.refresh_configuration(), name='shs_gateway_configuration')
        for event in (EVENT_STATE_CHANGED, EVENT_STATE_REPORTED):
            self.entry.async_on_unload(self.hass.bus.async_listen(event, observe, event_filter=matches))
        for event in (EVENT_CORE_CONFIG_UPDATE, er.EVENT_ENTITY_REGISTRY_UPDATED):
            self.entry.async_on_unload(self.hass.bus.async_listen(event, metadata_changed))


async def open_gateway(hass, entry):
    path = Path(hass.config.path('.storage', f'shs_energy.gateway.{entry.entry_id}.sqlite'))
    journal = GatewayJournal(path)
    def read_identity():
        with closing(journal.connect(readonly=True)) as db:
            row = db.execute('SELECT seed,activation FROM authority').fetchone()
            return json.loads(row[0])['identity'], row[1] is not None
    identity, activated = await hass.async_add_executor_job(read_identity)
    def core_digest():
        from hashlib import sha256
        root = Path(__file__).parent/'shs_core'
        files = {str(p.relative_to(root)):sha256(p.read_bytes()).hexdigest()
                 for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
        return sha256(json.dumps(files,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    if not activated and identity['pair']['core_sha256'] != await hass.async_add_executor_job(core_digest):
        raise GatewayConflict('The installed companion core differs from the paired app')
    if identity['entry_id'] != entry.entry_id or (not activated and identity['pair']['integration_version'] != INTEGRATION_VERSION):
        raise GatewayConflict('The installed companion differs from the seeded migration release')
    await hass.async_add_executor_job(journal.open)
    stream = GatewayStream(journal, hass.async_add_executor_job)
    stream.start()
    source = HomeAssistantSource(hass, entry)
    service = GatewayService(stream, identity, source)
    source.service = service
    source.configuration = ExecutionConfiguration(GatewayRecord(stream,'execution_configuration'),
        source.invalidate, source.refresh_configuration)

    async def send(domain, action, data):
        await hass.services.async_call(domain, action, data, blocking=True)
    commands = GatewayCommands(stream)
    native = NativeExecutor(CommandTransport(commands, commands.execute), hass.states.get,
                            lambda:hass.config.units.temperature_unit, send)
    registry = er.async_get(hass)
    inputs = ControllerInputs(hass.states.get, lambda:hass.config.units.temperature_unit,
        lambda entity:item.platform if (item:=registry.async_get(entity)) else None)
    service.physical = DeviceGateway(inputs, source.options, GatewayRecord(stream,'ownership'), native,
        authorize=service.authorize_device, before_external=source.external_ready,
        operations=GatewayOperations(stream, service.session))
    now = lambda:int(datetime.now(timezone.utc).timestamp()*1000)
    service.battery = BatteryGateway(stream,service.physical,source.options,source.report,now,
        authorize=service.authorize,session=service.session,context=service.context)
    try:
        await source.configuration.load(dict(entry.options))
        admission = await stream.call('load_record','source_admission')
        if admission is not None:
            source.install_bindings(admission)
        await service.physical.load()
        await service.battery.open()
        source.attach()
        await source.refresh_configuration()
    except BaseException:
        await stream.close()
        await hass.async_add_executor_job(journal.close)
        raise
    service.obligation_task = entry.async_create_background_task(hass,service.maintain(),name='shs_physical_obligations')
    hass.data.setdefault(GATEWAYS,{})[entry.entry_id] = service
    return service


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required('type'):'shs_energy/gateway',vol.Required('operation'):str,vol.Required('body'):dict})
@websocket_api.async_response
async def websocket_gateway(hass, connection, msg):
    sockets = hass.data.setdefault(SOCKETS,{})
    peer = sockets.get(connection)
    try:
        if msg['operation'] == 'connect':
            if peer is not None:
                raise GatewayConflict('Socket already connected')
            entry = msg['body']['identity']['entry_id']
            service = hass.data.get(GATEWAYS,{}).get(entry)
            if service is None:
                raise GatewayConflict('The SHS gateway is not loaded for this entry')
            admit(msg['body'].get('contract'), offer(INTEGRATION_VERSION))
            peer = sockets[connection] = AppConnection(service)
            peer.projection_assembly = ProjectionAssembly()
            @callback
            def disconnected():
                sockets.pop(connection,None)
                current = service.connection is peer
                peer.disconnected()
                if current and service.source.projection:
                    service.source.projection.disconnected()
                hass.async_create_task(peer.close())
            connection.subscriptions['shs_energy_gateway'] = disconnected
        if peer is None:
            raise GatewayConflict('Connect the paired SHS app first')
        request = {key:msg[key] for key in ('id','operation','body')}
        if request['operation'] == 'connect':
            request['body'] = {key:value for key,value in request['body'].items() if key != 'contract'}
        elif request['operation'] == 'projection_chunk':
            if peer.closed or peer.service.connection is not peer or not peer.service.active:
                raise GatewayConflict('Projection belongs to an inactive socket')
            value = peer.projection_assembly.receive(request['body'])
            if value is None:
                connection.send_result(msg['id'], {'received':request['body']['index']})
                return
            request.update(operation='projection',body={'value':value})
        result = await peer.request(request)
        if request['operation'] == 'connect':
            result['result']['contract'] = offer(INTEGRATION_VERSION)
        connection.send_result(msg['id'],result['result'])
    except (ValueError, KeyError, TypeError, RuntimeError) as error:
        stale = peer is None or peer.closed or peer.service.connection is not peer
        connection.send_error(msg['id'],'shs_gateway_stale' if stale else 'shs_gateway_rejected',str(error))


def register_gateway(hass):
    websocket_api.async_register_command(hass,websocket_gateway)
